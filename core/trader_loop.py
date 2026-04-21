"""
core/trader_loop.py — Main Trading Event Loop (v4 Φάση 3α/3γ).

Υποστηρίζει δύο modes:
  --mode paper   Virtual trading με real-time prices (default, ασφαλές)
  --mode live    Real KuCoin margin orders (ΠΡΟΣΟΧΗ — πραγματικά λεφτά)

Για live mode χρειάζεται και:
  --confirm-live         Ρητή επιβεβαίωση από χρήστη
  API keys σε config/secrets.env

State persistence: όλα τα state transitions αποθηκεύονται σε SQLite.
Αν crashθεί το process, --resume-state το επαναφέρει.

Usage (paper):
  python -m core.trader_loop --symbol PEPE/USDT --start-base 1000000000 \\
         --scale 4 --min-profit 10 --promote 1

Usage (live, με safety gate):
  python -m core.trader_loop --symbol PEPE/USDT --start-base 1000000000 \\
         --scale 4 --min-profit 10 --promote 1 \\
         --mode live --confirm-live
"""

import os
import sys
import time
import signal
import logging
import argparse
from datetime import datetime

from core.price_feed         import PriceFeed
from core.paper_executor     import PaperExecutor
from core.state_persistence  import StatePersistence
from strategies.arbitrading_v2 import ArbitradingV2, BotConfig, BotState

logger = logging.getLogger(__name__)


class TraderLoop:
    """Orchestrator: price_feed → strategy → executor (paper ή live)."""

    def __init__(self,
                 symbol:          str,
                 config:          BotConfig,
                 executor:        object,
                 state_db_path:   str   = "bot_state.db",
                 exchange_id:     str   = "kucoin",
                 poll_interval:   float = 1.0,
                 snapshot_every:  int   = 30,
                 persist_every_tick: bool = False,
                 resume_state:    bool  = False):
        self.symbol   = symbol
        self.config   = config
        self.executor = executor

        self.strategy = ArbitradingV2(config=config, executor=self.executor)

        # State persistence
        self.state_persistence = StatePersistence(state_db_path)
        self.persist_every_tick = persist_every_tick

        # Optional resume
        if resume_state:
            self._resume_state()

        self.feed = PriceFeed(
            symbol=symbol,
            on_tick=self._on_tick,
            exchange_id=exchange_id,
            poll_interval=poll_interval,
        )

        self.snapshot_every = snapshot_every
        self._tick_count    = 0
        self._started       = False
        self._last_state    = None  # για detection state changes

    def _resume_state(self) -> None:
        snap = self.state_persistence.restore_latest()
        if not snap:
            logger.info(f"[TraderLoop] No prior state — starting fresh")
            return
        logger.warning(f"[TraderLoop] Resuming from {snap['ts_iso']} | "
                       f"event={snap['event']} | state={snap['state']}")
        self.state_persistence.apply_to_memory(snap['memory'], self.strategy.memory)
        try:
            self.strategy.state = BotState(snap['state'])
        except Exception:
            logger.warning(f"[TraderLoop] Could not restore state {snap['state']} — fallback IDLE")
            self.strategy.state = BotState.IDLE
        self._started = self.strategy.state != BotState.IDLE

    def _on_tick(self, price: float, ts: datetime) -> None:
        self.executor.current_price = price
        self._tick_count += 1
        prev_state = self.strategy.state

        if not self._started:
            logger.info(f"[TraderLoop] First tick @ {price} — strategy.start()")
            self.strategy.start(price, ts)
            self._started = True
            self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                         event="INIT")
        else:
            self.strategy.on_price_update(price, ts)

        # Persist state on state change or important events
        if self.strategy.state != prev_state:
            self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                         event=f"STATE_{prev_state.value}→{self.strategy.state.value}")
        elif self.persist_every_tick:
            self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                         event="TICK")

        # Persist after each cycle completion
        if self.strategy.memory.cycle_count > 0 and self._last_state != self.strategy.state.value:
            self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                         event=f"CYCLE_{self.strategy.memory.cycle_count}")

        self._last_state = self.strategy.state.value

        if self.snapshot_every > 0 and self._tick_count % self.snapshot_every == 0:
            snap = self.executor.snapshot(price)
            stat = self.strategy.get_status(price)
            logger.info(f"[Snap #{self._tick_count}] price={price} | "
                        f"state={stat['state']} | cycles={stat['cycle_count']} | "
                        f"ratio={snap['margin_ratio']} | "
                        f"buy_cnt={stat['buy_trigger_count']} sell_cnt={stat['sell_trigger_count']}")

    def start(self) -> None:
        logger.info(f"[TraderLoop] Starting paper trading for {self.symbol}")
        logger.info(f"[TraderLoop] Config: promote={self.config.promote} | "
                    f"SP_enabled={self.config.second_profit_enabled} | "
                    f"min_profit={self.config.min_profit_percent}%")
        self.feed.start()

    def stop(self) -> None:
        logger.info(f"[TraderLoop] Stop requested")
        self.feed.stop()
        self._print_final_report()
        # Final snapshot
        price = self.feed.get_last_price() or 0.0
        self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                     event="STOP")
        self.state_persistence.close()
        self.executor.close()

    def _print_final_report(self) -> None:
        price = self.feed.get_last_price() or 0.0
        snap  = self.executor.snapshot(price)
        stat  = self.strategy.get_status(price)
        logger.info("")
        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║         TRADING FINAL REPORT             ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Symbol:         {self.symbol:<24} ║")
        logger.info(f"║  Ticks received: {self._tick_count:<24} ║")
        logger.info(f"║  Cycles:         {stat['cycle_count']:<24} ║")
        logger.info(f"║  State:          {stat['state']:<24} ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Last price:     {price:<24} ║")
        logger.info(f"║  base_coin:      {snap['base_coin']:<24} ║")
        logger.info(f"║  USDT:           {snap['usdt']:<24} ║")
        logger.info(f"║  USDT debt:      {snap['usdt_debt']:<24} ║")
        logger.info(f"║  VIP debt:       {snap['vip_debt_usdt']:<24} ║")
        logger.info(f"║  VIP holdings:   {str(snap['vip_holdings']):<24} ║")
        logger.info(f"║  Total assets:   {snap['total_assets']:<24} ║")
        logger.info(f"║  Total debt:     {snap['total_debt']:<24} ║")
        logger.info(f"║  Margin ratio:   {snap['margin_ratio']:<24} ║")
        logger.info("╚══════════════════════════════════════════╝")

    def execute_resset_invest(self) -> None:
        """Public trigger για Promote 3 (manual reset)."""
        if not self._started:
            logger.warning("Cannot resset_invest — not started")
            return
        price = self.feed.get_last_price() or 0.0
        self.strategy.execute_resset_invest(price, datetime.now())
        self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                     event="RESSET_INVEST")


# ── Factory helpers ──────────────────────────────────────────────────────────

def _build_paper_executor(args, cfg):
    return PaperExecutor(
        start_base_coin=cfg.start_base_coin,
        db_path=args.db_path,
        exchange_id=args.exchange,
        slippage_pct=args.slippage_pct,
    )


def _build_live_executor(args, cfg):
    """Δημιουργεί LiveExecutor μετά από safety checks + user confirmation."""
    from api.kucoin_client import KuCoinMarginClient
    from core.live_executor import LiveExecutor

    if not args.confirm_live:
        logger.error("="*70)
        logger.error("LIVE MODE requires --confirm-live flag — refusing to start")
        logger.error("To run live trading with REAL money, you MUST explicitly")
        logger.error("add --confirm-live to the command line")
        logger.error("="*70)
        sys.exit(1)

    # Load API keys from config/secrets.env via config/settings.py
    from config import settings as s
    if not (s.KUCOIN_API_KEY and s.KUCOIN_API_SECRET and s.KUCOIN_API_PASSPHRASE):
        logger.error("="*70)
        logger.error("KuCoin API keys NOT found in config/secrets.env")
        logger.error("Create the file based on secrets.env.example")
        logger.error("="*70)
        sys.exit(1)

    base_ccy = args.symbol.split('/')[0]

    # Big warning + ENTER confirmation
    print()
    print("#"*70)
    print("#" + " "*68 + "#")
    print("#" + "  *** LIVE TRADING MODE - REAL MONEY ***".center(68) + "#")
    print("#" + " "*68 + "#")
    print("#" + f"  Symbol: {args.symbol}".ljust(68) + "#")
    print("#" + f"  Start base: {cfg.start_base_coin} {base_ccy}".ljust(68) + "#")
    print("#" + f"  Scale: {cfg.scale_base_coin}".ljust(68) + "#")
    print("#" + f"  Promote: {cfg.promote} | SP_enabled: {cfg.second_profit_enabled}".ljust(68) + "#")
    print("#" + " "*68 + "#")
    print("#" + "  Real orders will be sent to KuCoin.".ljust(68) + "#")
    print("#" + "  Your money is at risk.".ljust(68) + "#")
    print("#" + " "*68 + "#")
    print("#"*70)
    print()
    try:
        input("Press ENTER to confirm LIVE mode (or Ctrl+C to cancel)... ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        sys.exit(0)

    client = KuCoinMarginClient(
        api_key=s.KUCOIN_API_KEY,
        api_secret=s.KUCOIN_API_SECRET,
        api_passphrase=s.KUCOIN_API_PASSPHRASE,
        sandbox=False,
    )
    return LiveExecutor(
        client=client,
        symbol=args.symbol,
        base_ccy=base_ccy,
        db_path=args.db_path,
    )


# ── CLI entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Arbitrading Trader Loop (paper/live)")
    parser.add_argument("--symbol",        type=str,   default="PEPE/USDT")
    parser.add_argument("--exchange",      type=str,   default="kucoin")
    parser.add_argument("--start-base",    type=float, default=1_000_000_000.0)
    parser.add_argument("--scale",         type=float, default=4.0)
    parser.add_argument("--min-profit",    type=float, default=10.0)
    parser.add_argument("--step-point",    type=float, default=0.5)
    parser.add_argument("--trailing-stop", type=float, default=2.0)
    parser.add_argument("--margin-level",  type=float, default=1.07)
    parser.add_argument("--promote",       type=int,   default=1, choices=[1, 2, 3])
    parser.add_argument("--second-profit", type=str,   default="off", choices=["on", "off"])
    parser.add_argument("--second-profit-pct", type=float, default=4.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--slippage-pct",  type=float, default=0.0)
    parser.add_argument("--snapshot-every", type=int,  default=30)
    parser.add_argument("--db-path",       type=str,   default="paper_trades.db",
                        help="SQLite audit log path (διαφορετικό για paper/live)")
    parser.add_argument("--state-db-path", type=str,   default="bot_state.db",
                        help="SQLite state persistence path")
    parser.add_argument("--duration",      type=int,   default=0,
                        help="Duration in seconds (0 = until Ctrl+C)")
    parser.add_argument("--log-level",     type=str,   default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    # v4 live mode
    parser.add_argument("--mode",          type=str,   default="paper",
                        choices=["paper", "live"],
                        help="paper = virtual | live = real KuCoin orders")
    parser.add_argument("--confirm-live",  action="store_true",
                        help="REQUIRED explicit confirmation for live mode")
    parser.add_argument("--resume-state",  action="store_true",
                        help="Resume state from bot_state.db")
    parser.add_argument("--persist-every-tick", action="store_true",
                        help="Save state on every tick (heavy I/O — default off)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    cfg = BotConfig(
        trading_pair          = args.symbol,
        start_base_coin       = args.start_base,
        scale_base_coin       = args.scale,
        min_profit_percent    = args.min_profit,
        step_point            = args.step_point,
        trailing_stop         = args.trailing_stop,
        limit_order           = 0.5,
        margin_level          = args.margin_level,
        promote               = args.promote,
        second_profit_enabled = (args.second_profit == "on"),
        second_profit_percent = args.second_profit_pct,
    )

    # Build executor based on mode
    if args.mode == "live":
        executor = _build_live_executor(args, cfg)
    else:
        executor = _build_paper_executor(args, cfg)

    loop = TraderLoop(
        symbol             = args.symbol,
        config             = cfg,
        executor           = executor,
        state_db_path      = args.state_db_path,
        exchange_id        = args.exchange,
        poll_interval      = args.poll_interval,
        snapshot_every     = args.snapshot_every,
        persist_every_tick = args.persist_every_tick,
        resume_state       = args.resume_state,
    )

    def _handle_signal(signum, frame):
        logger.info(f"[SIGNAL] {signum} — stopping...")
        loop.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except Exception:
        pass  # SIGTERM not supported on Windows

    loop.start()
    try:
        if args.duration > 0:
            time.sleep(args.duration)
            loop.stop()
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        loop.stop()


if __name__ == "__main__":
    main()
