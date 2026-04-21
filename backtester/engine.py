"""
Backtester — Engine
Τρέχει ιστορικά δεδομένα μέσα από τη στρατηγική ArbitradingV1.

Αποτελείται από:
  BacktestExecutor     — προσομοιώνει εντολές αγοράς/πώλησης/δανεισμού
  BacktestEngine       — OHLCV candle mode (legacy)
  TickBacktestEngine   — Tick-by-tick mode (ακριβές)

Tick mode: κάθε πραγματική συναλλαγή (trade) στέλνεται στη στρατηγική.
Μηδέν ambiguity — η σειρά τιμών είναι η πραγματική χρονολογική σειρά.
"""

import logging
from datetime import datetime
from typing import List, Tuple, Optional

from strategies.arbitrading_v1 import ArbitradingV1, BotConfig
from strategies.arbitrading_v2 import ArbitradingV2, BotConfig as BotConfigV2
from backtester.data_loader import DataLoader, Candle
from backtester.tick_loader import TickLoader, Tick

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# BACKTEST EXECUTOR
# ──────────────────────────────────────────────────────────────────────────────

class BacktestExecutor:
    """
    Προσομοιώνει τις εντολές του exchange με virtual balance.
    Υλοποιεί το ίδιο interface με τον LiveExecutor (KuCoin API).
    Δεν γίνονται ΠΟΤΕ πραγματικές εντολές.
    """

    def __init__(self, start_base_coin: float, vip_mock_prices: Optional[dict] = None):
        self.base_coin  = start_base_coin  # Αρχικό BASE_COIN (π.χ. 10 SOL)
        self.usdt       = 0.0              # Αρχικό USDT cash
        self.usdt_debt  = 0.0              # Δάνειο USDT (αρχικό)
        self.base_debt  = 0.0              # Δάνειο BASE_COIN
        self.current_price = 0.0           # Ενημερώνεται από engine
        # v4: VIP support (for Promote 2 code-path validation στο backtester)
        self.vip_mock_prices = vip_mock_prices or {}  # {coin: price_usdt}
        self.vip_holdings    = {}                     # {coin: quantity}
        self.vip_debt_usdt   = 0.0                    # Δάνειο USDT μέσω VIP collateral

    # ── Δανεισμός ─────────────────────────────────────────────────────────────

    def borrow_usdt(self, amount: float) -> float:
        self.usdt      += amount
        self.usdt_debt += amount
        logger.debug(f"  [EXE] borrow_usdt {amount:.2f} | USDT balance: {self.usdt:.2f}")
        return amount

    def repay_usdt(self, amount: float) -> None:
        actual = min(amount, self.usdt)
        self.usdt      -= actual
        self.usdt_debt  = max(0.0, self.usdt_debt - actual)
        logger.debug(f"  [EXE] repay_usdt {actual:.2f} | USDT debt remaining: {self.usdt_debt:.2f}")

    def borrow_base_coin(self, quantity: float) -> float:
        self.base_coin += quantity
        self.base_debt += quantity
        logger.debug(f"  [EXE] borrow_base {quantity:.4f} | base balance: {self.base_coin:.4f}")
        return quantity

    def repay_base_coin(self, quantity: float) -> None:
        actual = min(quantity, self.base_coin)
        self.base_coin -= actual
        self.base_debt  = max(0.0, self.base_debt - actual)
        logger.debug(f"  [EXE] repay_base {actual:.4f} | base balance: {self.base_coin:.4f}")

    # ── Αγορά / Πώληση ────────────────────────────────────────────────────────

    def buy_base_coin(self, quantity: float, limit_price: float) -> Tuple[float, float]:
        """
        Αγορά BASE_COIN. Γεμίζει στη limit_price (simulation — χωρίς slippage).
        ΑΝ δεν υπάρχει αρκετό USDT, αγοράζει όσο μπορεί.
        """
        cost = quantity * limit_price
        if cost > self.usdt:
            quantity = self.usdt / limit_price if limit_price > 0 else 0
            cost     = self.usdt

        self.usdt      -= cost
        self.base_coin += quantity
        logger.debug(f"  [EXE] buy {quantity:.4f} @ {limit_price:.4f} | cost: {cost:.2f} USDT")
        return (quantity, limit_price)

    def sell_base_coin(self, quantity: float,
                       limit_price: float) -> Tuple[float, float, float]:
        """
        Πώληση BASE_COIN. Γεμίζει στη limit_price.
        ΑΝ δεν υπάρχει αρκετό BASE_COIN, πουλάει όσο μπορεί.
        """
        actual_qty   = min(quantity, self.base_coin)
        usdt_received = actual_qty * limit_price
        self.base_coin -= actual_qty
        self.usdt      += usdt_received
        logger.debug(f"  [EXE] sell {actual_qty:.4f} @ {limit_price:.4f} | received: {usdt_received:.2f} USDT")
        return (actual_qty, limit_price, usdt_received)

    def cancel_all_orders(self) -> None:
        """Στο backtest δεν υπάρχουν pending orders — no-op."""
        logger.debug("  [EXE] cancel_all_orders (no-op στο backtest)")

    # ── v4: VIP support (Promote 2) ───────────────────────────────────────────
    # Στο backtester δεν έχουμε parallel VIP price feeds — χρησιμοποιούμε
    # mock prices. Επαρκεί για code path validation, ΟΧΙ για accurate backtest
    # του Promote 2 (θα γίνει σε paper trading με real prices).

    def get_vip_price(self, coin: str) -> float:
        price = self.vip_mock_prices.get(coin, 0.0)
        if price <= 0:
            logger.warning(f"  [EXE] get_vip_price({coin}) no mock price set, returning 1.0")
            return 1.0
        return price

    def buy_vip(self, coin: str, usdt_amount: float) -> Tuple[float, float]:
        """Αγορά VIP coin με usdt_amount USDT. Επιστρέφει (qty, price)."""
        if usdt_amount > self.usdt:
            logger.warning(f"  [EXE] buy_vip({coin}, {usdt_amount}) insufficient USDT ({self.usdt:.2f})")
            usdt_amount = max(0, self.usdt)
        price = self.get_vip_price(coin)
        qty = usdt_amount / price if price > 0 else 0
        self.usdt -= usdt_amount
        self.vip_holdings[coin] = self.vip_holdings.get(coin, 0.0) + qty
        logger.debug(f"  [EXE] buy_vip {qty:.8f} {coin} @ {price:.4f} = {usdt_amount:.2f} USDT")
        return (qty, price)

    def borrow_usdt_vip(self, amount: float) -> float:
        """Δανεισμός USDT βασισμένος στο VIP collateral (Promote 2 βήμα 5)."""
        self.usdt         += amount
        self.vip_debt_usdt += amount
        logger.debug(f"  [EXE] borrow_usdt_vip {amount:.2f} | vip_debt total: {self.vip_debt_usdt:.2f}")
        return amount

    def repay_usdt_vip(self, amount: float) -> None:
        """Εξόφληση μέρους του VIP-based USDT debt."""
        actual = min(amount, self.usdt, self.vip_debt_usdt)
        self.usdt          -= actual
        self.vip_debt_usdt  = max(0.0, self.vip_debt_usdt - actual)
        logger.debug(f"  [EXE] repay_usdt_vip {actual:.2f} | vip_debt remaining: {self.vip_debt_usdt:.2f}")

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, price: float) -> dict:
        """Τρέχουσα κατάσταση virtual λογαριασμού"""
        total_assets = (self.base_coin * price) + self.usdt
        total_debt   = self.usdt_debt + (self.base_debt * price)
        ratio        = total_assets / total_debt if total_debt > 0 else 0
        return {
            "base_coin":   round(self.base_coin, 4),
            "usdt":        round(self.usdt, 2),
            "usdt_debt":   round(self.usdt_debt, 2),
            "base_debt":   round(self.base_debt, 4),
            "total_assets": round(total_assets, 2),
            "total_debt":   round(total_debt, 2),
            "margin_ratio": round(ratio, 4),
        }


# ──────────────────────────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ──────────────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Τρέχει τη στρατηγική σε ιστορικά OHLCV δεδομένα.

    Για κάθε candle εξομοιώνει την κίνηση τιμής:
      Bullish: low πρώτα → high (τιμή έπεσε πρώτα, μετά ανέβηκε)
      Bearish: high πρώτα → low (τιμή ανέβηκε πρώτα, μετά έπεσε)
    """

    def __init__(self, config, candles: List[Candle], strategy_class=ArbitradingV1):
        """v4: strategy_class επιτρέπει επιλογή μεταξύ ArbitradingV1 / ArbitradingV2.
        Default = ArbitradingV1 (backward compatible)."""
        self.config         = config
        self.candles        = candles
        self.executor       = BacktestExecutor(start_base_coin=config.start_base_coin)
        self.strategy_class = strategy_class
        self.strategy       = strategy_class(config=config, executor=self.executor)

    # ─────────────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Εκτελεί το backtest και επιστρέφει αναφορά αποτελεσμάτων.
        """
        if not self.candles:
            logger.error("Δεν υπάρχουν candles για backtest.")
            return {}

        first_candle = self.candles[0]
        start_price  = first_candle[4]  # close τιμή πρώτου candle
        start_time   = first_candle[0]

        logger.info(f"=== BACKTEST START ===")
        logger.info(f"  Σύμβολο:    {self.config.trading_pair}")
        logger.info(f"  Candles:    {len(self.candles)}")
        logger.info(f"  Περίοδος:   {self.candles[0][0].date()} → {self.candles[-1][0].date()}")
        logger.info(f"  Αρχ. τιμή: {start_price}")
        logger.info(f"  START SOL:  {self.config.start_base_coin}")

        # Εκκίνηση bot
        self.strategy.start(start_price, start_time)

        # Επανάληψη ανά candle
        for i, candle in enumerate(self.candles[1:], start=1):
            timestamp, open_, high, low, close, volume = candle

            # Ενημέρωση current_price στον executor
            self.executor.current_price = close

            # Simulation intra-candle κίνησης τιμής
            if close >= open_:
                # Bullish: πρώτα low → μετά high
                self.strategy.on_price_update(low,   timestamp)
                self.strategy.on_price_update(high,  timestamp)
            else:
                # Bearish: πρώτα high → μετά low
                self.strategy.on_price_update(high,  timestamp)
                self.strategy.on_price_update(low,   timestamp)

            # Τελικό close για ακριβέστερο state
            self.strategy.on_price_update(close, timestamp)

            # Progress log κάθε 1000 candles
            if i % 1000 == 0:
                pct = i / len(self.candles) * 100
                logger.info(f"  [{pct:.0f}%] {timestamp.date()} | τιμή: {close:.2f} | "
                            f"κύκλοι: {self.strategy.memory.cycle_count}")

        logger.info(f"=== BACKTEST ΤΕΛΟΣ ===")
        return self._generate_report(start_price)

    # ─────────────────────────────────────────────────────────────────────────

    def _generate_report(self, start_price: float) -> dict:
        """
        Παράγει αναφορά αποτελεσμάτων backtest.

        ΚΑΝΟΝΑΣ 1: Η απόδοση μετριέται σε USDT ΑΞΙΑ, όχι σε ποσότητα SOL.
        Η στρατηγική εγγυάται αύξηση ΑΞΙΑΣ — ο αριθμός SOL μπορεί να μειωθεί
        αν η τιμή ανέβηκε (λιγότερα SOL × υψηλότερη τιμή = μεγαλύτερη αξία).

        P&L μετριέται ΜΟΝΟ από ολοκληρωμένους κύκλους.
        """
        m       = self.strategy.memory
        trades  = self.strategy.trade_log
        cfg     = self.config
        end_price     = self.candles[-1][4] if self.candles else start_price
        last_cycle_px = m.last_cycle_price if m.last_cycle_price > 0 else start_price

        # ── Συναλλαγές ────────────────────────────────────────────────────
        buy_trades    = [t for t in trades if t.action == "BUY"]
        repay_sells   = [t for t in trades if t.action == "REPAY_SELL"]
        closing_sells = [t for t in trades if t.action == "CLOSING_SELL"]
        sell_trades   = repay_sells + closing_sells  # για συμβατότητα
        locks         = closing_sells
        protects      = [t for t in trades if t.action == "MARGIN_PROTECT"]

        # ── Αρχική επένδυση ───────────────────────────────────────────────
        # = αξία των 50 SOL (total_base_coin) στην έναρξη
        initial_sol   = cfg.start_base_coin * (1 + cfg.scale_base_coin)  # 10 × 5 = 50 SOL
        initial_value = initial_sol * start_price                          # 50 × 101.74 = 5087 USDT

        # ── Τελική αξία (από ολοκληρωμένους κύκλους) ──────────────────────
        # Χρησιμοποιούμε last_cycle_sol: το SOL στη ΛΗΞΗ του τελευταίου
        # ΟΛΟΚΛΗΡΩΜΕΝΟΥ κύκλου (πριν ξεκινήσει ο επόμενος και τα sells μειώσουν το SOL)
        final_sol   = m.last_cycle_sol if m.last_cycle_sol > 0 else initial_sol
        final_value = final_sol * last_cycle_px      # αξία σε USDT

        # ── Σύγκριση ──────────────────────────────────────────────────────
        pnl_usdt     = final_value - initial_value
        pnl_pct      = (pnl_usdt / initial_value * 100) if initial_value > 0 else 0

        # Buy & Hold: αν είχαμε απλώς κρατήσει τα 10 SOL αρχικά
        bh_value     = cfg.start_base_coin * end_price
        bh_pnl       = bh_value - (cfg.start_base_coin * start_price)
        bh_pct       = (bh_pnl / (cfg.start_base_coin * start_price) * 100) if start_price > 0 else 0

        report = {
            # ── Βασικά ──────────────────────────────────────────────────────
            "symbol":           cfg.trading_pair,
            "period":           f"{self.candles[0][0].date()} → {self.candles[-1][0].date()}",
            "total_candles":    len(self.candles),
            "start_price":      round(start_price, 4),
            "end_price":        round(end_price, 4),
            "last_cycle_price": round(last_cycle_px, 4),

            # ── Συναλλαγές ──────────────────────────────────────────────────
            "total_trades":     len(trades),
            "buy_trades":       len(buy_trades),
            "repay_sells":      len(repay_sells),
            "closing_sells":    len(closing_sells),
            "margin_protects":  len(protects),
            "cycles_completed": m.cycle_count,

            # ── Αποτέλεσμα σε USDT ΑΞΙΑ ─────────────────────────────────────
            "initial_sol":          round(initial_sol, 4),
            "initial_value_usdt":   round(initial_value, 2),
            "final_sol":            round(final_sol, 4),
            "final_value_usdt":     round(final_value, 2),
            "pnl_usdt":             round(pnl_usdt, 2),
            "pnl_pct":              round(pnl_pct, 2),
            "buy_hold_pct":         round(bh_pct, 2),

            # ── Επεξήγηση ────────────────────────────────────────────────────
            "note": (
                "Κανόνας 1: Αξία USDT αυξάνεται πάντα. "
                "Ο αριθμός SOL μπορεί να μειωθεί όταν τιμή ανεβαίνει "
                "(λιγότερα SOL × υψηλότερη τιμή = μεγαλύτερη αξία). "
                "P&L από ολοκληρωμένους κύκλους μόνο."
            )
        }

        self._print_report(report)
        return report

    def _print_report(self, r: dict) -> None:
        logger.info("")
        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║          BACKTEST ΑΠΟΤΕΛΕΣΜΑΤΑ           ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Σύμβολο:        {r['symbol']:<24} ║")
        logger.info(f"║  Περίοδος:       {r['period']:<24} ║")
        logger.info(f"║  Candles:        {r['total_candles']:<24} ║")
        logger.info(f"║  Τιμή αρχής:     {r['start_price']:<24} ║")
        logger.info(f"║  Τιμή τελ.κύκλου:{r['last_cycle_price']:<24} ║")
        logger.info(f"║  Τιμή τέλους:    {r['end_price']:<24} ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Κύκλοι:         {r['cycles_completed']:<24} ║")
        logger.info(f"║  BUY orders:     {r['buy_trades']:<24} ║")
        logger.info(f"║  Repay SELLs:    {r['repay_sells']:<24} ║")
        logger.info(f"║  Closing SELLs:  {r['closing_sells']:<24} ║")
        logger.info(f"║  Margin Protect: {r['margin_protects']:<24} ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Αρχικά SOL:     {r['initial_sol']:<24} ║")
        logger.info(f"║  Αρχ. αξία USDT: {r['initial_value_usdt']:<24} ║")
        logger.info(f"║  Τελικά SOL:     {r['final_sol']:<24} ║")
        logger.info(f"║  Τελ. αξία USDT: {r['final_value_usdt']:<24} ║")
        logger.info(f"║  P&L USDT:       {r['pnl_usdt']:<24} ║")
        logger.info(f"║  P&L %:          {r['pnl_pct']:<24} ║")
        logger.info(f"║  Buy & Hold %:   {r['buy_hold_pct']:<24} ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  * Αξία USDT = SOL × τιμή               ║")
        logger.info(f"║  * Μόνο ολοκληρωμένοι κύκλοι            ║")
        logger.info(f"║  * Τελ. ανοιχτός κύκλος εξαιρείται      ║")
        logger.info("╚══════════════════════════════════════════╝")


# ──────────────────────────────────────────────────────────────────────────────
# TICK BACKTEST ENGINE — ακριβές tick-by-tick simulation
# ──────────────────────────────────────────────────────────────────────────────

class TickBacktestEngine:
    """
    Τρέχει τη στρατηγική σε tick-by-tick trade data.

    Κάθε tick = μία πραγματική συναλλαγή (trade) από το exchange.
    Η σειρά τιμών είναι η ακριβής χρονολογική σειρά — μηδέν ambiguity.
    Δεν υπάρχει intra-candle simulation.

    Streaming mode: διαβάζει ticks απευθείας από CSV χωρίς να τα φορτώνει
    όλα στη μνήμη. Κρατάει μόνο first/last tick για το report.
    """

    def __init__(self, config, ticks: List[Tick] = None,
                 tick_csv: str = None, strategy_class=ArbitradingV1):
        """v4: strategy_class επιτρέπει επιλογή μεταξύ ArbitradingV1 / ArbitradingV2.
        Default = ArbitradingV1 (backward compatible)."""
        self.config         = config
        self.ticks          = ticks          # legacy mode (in-memory)
        self.tick_csv       = tick_csv       # streaming mode (file path)
        self.executor       = BacktestExecutor(start_base_coin=config.start_base_coin)
        self.strategy_class = strategy_class
        self.strategy       = strategy_class(config=config, executor=self.executor)
        # Streaming state
        self._first_tick = None
        self._last_tick  = None
        self._total_ticks = 0

    def run(self) -> dict:
        """Εκτελεί tick-by-tick backtest (streaming ή in-memory)."""
        if self.tick_csv:
            return self._run_streaming()
        elif self.ticks:
            return self._run_memory()
        else:
            logger.error("No ticks or tick_csv provided.")
            return {}

    def _run_streaming(self) -> dict:
        """Streaming mode: διαβάζει CSV γραμμή-γραμμή, μηδέν memory overhead."""
        import csv as _csv
        from datetime import datetime as _dt, timezone as _tz

        logger.info(f"=== TICK BACKTEST START (STREAMING) ===")
        logger.info(f"  Symbol:     {self.config.trading_pair}")
        logger.info(f"  File:       {self.tick_csv}")
        logger.info(f"  START COIN: {self.config.start_base_coin}")

        count = 0
        start_price = None

        with open(self.tick_csv, "r") as f:
            reader = _csv.reader(f)
            for row in reader:
                if len(row) < 6:
                    continue
                try:
                    price = float(row[1])
                    ts_ms = int(row[4])
                    ts = _dt.fromtimestamp(ts_ms / 1000, tz=_tz.utc)
                except (ValueError, IndexError):
                    continue

                count += 1

                if count == 1:
                    # First tick — start bot
                    start_price = price
                    self._first_tick = (ts, price)
                    self.strategy.start(price, ts)
                else:
                    # Process tick
                    self.executor.current_price = price
                    self.strategy.on_price_update(price, ts)

                self._last_tick = (ts, price)

                # Progress log every 5M ticks
                if count % 5_000_000 == 0:
                    logger.info(f"  [{count:,} ticks] {ts.date()} | "
                                f"price: {price} | "
                                f"cycles: {self.strategy.memory.cycle_count}")

        self._total_ticks = count
        logger.info(f"=== TICK BACKTEST END === ({count:,} ticks)")
        return self._generate_report(start_price)

    def _run_memory(self) -> dict:
        """Legacy in-memory mode."""
        start_price = self.ticks[0][1]
        start_time  = self.ticks[0][0]
        total_ticks = len(self.ticks)
        self._first_tick = self.ticks[0]
        self._last_tick  = self.ticks[-1]
        self._total_ticks = total_ticks

        logger.info(f"=== TICK BACKTEST START ===")
        logger.info(f"  Symbol:     {self.config.trading_pair}")
        logger.info(f"  Ticks:      {total_ticks:,}")
        logger.info(f"  Period:     {self.ticks[0][0].date()} -> {self.ticks[-1][0].date()}")
        logger.info(f"  Start price: {start_price}")
        logger.info(f"  START COIN:  {self.config.start_base_coin}")

        self.strategy.start(start_price, start_time)

        log_interval = total_ticks // 20 or 1
        for i, (timestamp, price) in enumerate(self.ticks[1:], start=1):
            self.executor.current_price = price
            self.strategy.on_price_update(price, timestamp)

            if i % log_interval == 0:
                pct = i / total_ticks * 100
                logger.info(f"  [{pct:.0f}%] {timestamp.date()} | price: {price} | "
                            f"cycles: {self.strategy.memory.cycle_count}")

        logger.info(f"=== TICK BACKTEST END ===")
        return self._generate_report(start_price)

    def _generate_report(self, start_price: float) -> dict:
        """Παράγει αναφορά αποτελεσμάτων (ίδια λογική με BacktestEngine)."""
        m       = self.strategy.memory
        trades  = self.strategy.trade_log
        cfg     = self.config
        end_price     = self._last_tick[1] if self._last_tick else start_price
        last_cycle_px = m.last_cycle_price if m.last_cycle_price > 0 else start_price

        buy_trades    = [t for t in trades if t.action == "BUY"]
        repay_sells   = [t for t in trades if t.action == "REPAY_SELL"]
        closing_sells = [t for t in trades if t.action == "CLOSING_SELL"]
        protects      = [t for t in trades if t.action == "MARGIN_PROTECT"]

        initial_sol   = cfg.start_base_coin * (1 + cfg.scale_base_coin)
        initial_value = initial_sol * start_price

        final_sol   = m.last_cycle_sol if m.last_cycle_sol > 0 else initial_sol
        final_value = final_sol * last_cycle_px

        pnl_usdt = final_value - initial_value
        pnl_pct  = (pnl_usdt / initial_value * 100) if initial_value > 0 else 0

        bh_value = cfg.start_base_coin * end_price
        bh_pnl   = bh_value - (cfg.start_base_coin * start_price)
        bh_pct   = (bh_pnl / (cfg.start_base_coin * start_price) * 100) if start_price > 0 else 0

        report = {
            "mode":             "TICK",
            "symbol":           cfg.trading_pair,
            "period":           f"{self._first_tick[0].date()} -> {self._last_tick[0].date()}",
            "total_ticks":      self._total_ticks,
            "start_price":      round(start_price, 4),
            "end_price":        round(end_price, 4),
            "last_cycle_price": round(last_cycle_px, 4),
            "total_trades":     len(trades),
            "buy_trades":       len(buy_trades),
            "repay_sells":      len(repay_sells),
            "closing_sells":    len(closing_sells),
            "margin_protects":  len(protects),
            "cycles_completed": m.cycle_count,
            "initial_sol":          round(initial_sol, 4),
            "initial_value_usdt":   round(initial_value, 2),
            "final_sol":            round(final_sol, 4),
            "final_value_usdt":     round(final_value, 2),
            "pnl_usdt":             round(pnl_usdt, 2),
            "pnl_pct":              round(pnl_pct, 2),
            "buy_hold_pct":         round(bh_pct, 2),
        }

        self._print_report(report)
        return report

    def _print_report(self, r: dict) -> None:
        logger.info("")
        logger.info("╔══════════════════════════════════════════╗")
        logger.info("║      TICK BACKTEST ΑΠΟΤΕΛΕΣΜΑΤΑ          ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Mode:           {'TICK DATA':24s} ║")
        logger.info(f"║  Σύμβολο:        {r['symbol']:<24} ║")
        logger.info(f"║  Περίοδος:       {r['period']:<24} ║")
        logger.info(f"║  Ticks:          {r['total_ticks']:<24,} ║")
        logger.info(f"║  Τιμή αρχής:     {r['start_price']:<24} ║")
        logger.info(f"║  Τιμή τελ.κύκλου:{r['last_cycle_price']:<24} ║")
        logger.info(f"║  Τιμή τέλους:    {r['end_price']:<24} ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Κύκλοι:         {r['cycles_completed']:<24} ║")
        logger.info(f"║  BUY orders:     {r['buy_trades']:<24} ║")
        logger.info(f"║  Repay SELLs:    {r['repay_sells']:<24} ║")
        logger.info(f"║  Closing SELLs:  {r['closing_sells']:<24} ║")
        logger.info(f"║  Margin Protect: {r['margin_protects']:<24} ║")
        logger.info("╠══════════════════════════════════════════╣")
        logger.info(f"║  Αρχικά SOL:     {r['initial_sol']:<24} ║")
        logger.info(f"║  Αρχ. αξία USDT: {r['initial_value_usdt']:<24} ║")
        logger.info(f"║  Τελικά SOL:     {r['final_sol']:<24} ║")
        logger.info(f"║  Τελ. αξία USDT: {r['final_value_usdt']:<24} ║")
        logger.info(f"║  P&L USDT:       {r['pnl_usdt']:<24} ║")
        logger.info(f"║  P&L %:          {r['pnl_pct']:<24} ║")
        logger.info(f"║  Buy & Hold %:   {r['buy_hold_pct']:<24} ║")
        logger.info("╚══════════════════════════════════════════╝")


# ──────────────────────────────────────────────────────────────────────────────
# SETUP-ONLY TEST — επαλήθευση δομής SETUP, μία ανά μέρα
# ──────────────────────────────────────────────────────────────────────────────

def run_setup_only_test(symbol: str = "SOL/USDT",
                        start: str  = "2024-01-01",
                        end: str    = "2024-02-01",
                        csv_path: Optional[str] = None,
                        config: Optional[BotConfig] = None) -> None:
    """
    ΒΗΜΑ 1 ΕΛΕΓΧΟΥ: Τρέχει ΜΟΝΟ τη δημιουργία δομής SETUP.
    Κάθε μέρα δημιουργεί νέα δομή με την τιμή ανοίγματος.
    Όλη η υπόλοιπη λογική (monitoring, buy, sell) είναι ΑΠΕΝΕΡΓΟΠΟΙΗΜΕΝΗ.
    Σκοπός: επαλήθευση ορθότητας SETUP βημάτων 1-6.
    """
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout
    )

    loader = DataLoader(exchange_id="kucoin")
    if csv_path and __import__("os").path.exists(csv_path):
        candles = loader.load_csv(csv_path)
    else:
        candles = loader.fetch(symbol, "1m", start, end)
        if csv_path:
            loader.save_csv(candles, csv_path)

    if config is None:
        config = BotConfig(
            trading_pair       = symbol,
            start_base_coin    = 10.0,
            scale_base_coin    = 4.0,
            ratio_scale        = 0.005,
            min_profit_percent = 5.0,
            step_point         = 0.5,
            trailing_stop      = 2.0,
            limit_order        = 0.5,
            margin_level       = 1.07,
        )

    logger.info("=" * 60)
    logger.info("  SETUP-ONLY TEST — ΕΛΕΓΧΟΣ ΔΟΜΗΣ")
    logger.info(f"  Σύμβολο: {symbol} | {start} → {end}")
    logger.info("=" * 60)

    # Ομαδοποίηση candles ανά ημέρα
    from collections import defaultdict
    days = defaultdict(list)
    for c in candles:
        day_key = c[0].date()
        days[day_key].append(c)

    for day, day_candles in sorted(days.items()):
        open_price  = day_candles[0][1]   # open του πρώτου candle της μέρας
        close_price = day_candles[-1][4]  # close του τελευταίου candle

        executor = BacktestExecutor(start_base_coin=config.start_base_coin)
        strategy = ArbitradingV1(config=config, executor=executor)

        logger.info(f"\n{'─'*60}")
        logger.info(f"  ΗΜΕΡΑ: {day} | Open: {open_price:.4f} | Close: {close_price:.4f}")
        logger.info(f"{'─'*60}")

        # Εκτέλεση ΜΟΝΟ SETUP (με τιμή ανοίγματος)
        from datetime import datetime, timezone
        ts = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
        strategy.start(open_price, ts)

        # Επαλήθευση αποτελεσμάτων SETUP
        m = strategy.memory
        expected_buy  = config.scale_base_coin * config.start_base_coin
        expected_borrow_usdt = expected_buy * open_price * (1 + config.ratio_scale)
        expected_total = config.start_base_coin + expected_buy
        expected_sel_price = open_price  # μέσος όρος πώλησης ≈ open_price

        logger.info(f"  ΕΛΕΓΧΟΣ:")
        logger.info(f"  BUY_BASE_COIN:       {expected_buy:.4f} SOL  ✓")
        logger.info(f"  BORROW_USDT:         {expected_borrow_usdt:.2f} USDT  (actual debt: {m.usdt_debt:.2f})")
        logger.info(f"  TOTAL_BASE_COIN:     {expected_total:.4f} SOL  (actual: {m.total_base_coin:.4f})")
        logger.info(f"  BORROW_BASE_COIN:    {m.borrow_base_coin:.4f} SOL")
        logger.info(f"  SEL_PRICE_BASE_COIN: {m.sel_price_base_coin:.4f} USDT")
        logger.info(f"  AVAILABLE_USDT:      {m.available_usdt:.2f} USDT")

        # Έλεγχος consistency
        ok = (
            abs(m.total_base_coin - expected_total) < 0.001 and
            abs(m.borrow_base_coin - expected_total) < 0.001 and
            m.available_usdt > 0
        )
        logger.info(f"  STATUS: {'✅ ΣΩΣΤΟ' if ok else '❌ ΛΑΘΟΣ'}")

    logger.info("\n" + "=" * 60)
    logger.info("  SETUP-ONLY TEST ΤΕΛΟΣ")
    logger.info("=" * 60)


# ──────────────────────────────────────────────────────────────────────────────
# QUICK RUN — για γρήγορη δοκιμή από command line
# ──────────────────────────────────────────────────────────────────────────────

def run_backtest(symbol: str = "SOL/USDT",
                 start: str  = "2024-01-01",
                 end: str    = "2024-03-01",
                 csv_path: Optional[str] = None,
                 config: Optional[BotConfig] = None) -> dict:
    """
    Συντόμευση για εκκίνηση backtest.
    Αν csv_path υπάρχει → φορτώνει από CSV (γρήγορο).
    Αλλιώς → κατεβάζει από KuCoin (απαιτεί internet).
    """
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout
    )

    loader = DataLoader(exchange_id="kucoin")

    if csv_path and __import__("os").path.exists(csv_path):
        candles = loader.load_csv(csv_path)
    else:
        candles = loader.fetch(symbol, "1m", start, end)
        if csv_path:
            loader.save_csv(candles, csv_path)

    if config is None:
        config = BotConfig(
            trading_pair       = symbol,
            start_base_coin    = 10.0,
            scale_base_coin    = 4.0,
            min_profit_percent = 5.0,
            step_point         = 0.5,
            trailing_stop      = 2.0,
            limit_order        = 0.5,
            margin_level       = 1.07,
        )

    engine = BacktestEngine(config=config, candles=candles)
    return engine.run()


def run_tick_backtest(symbol: str = "SOL/USDT",
                      tick_csv: str = "data/SOLUSDT-trades-2024-01.csv",
                      config = None,
                      streaming: bool = True,
                      strategy_class=ArbitradingV1) -> dict:
    """Εκκίνηση tick-by-tick backtest (streaming by default).
    v4: strategy_class default ArbitradingV1 — backward compatible."""
    if config is None:
        config = BotConfig(
            trading_pair       = symbol,
            start_base_coin    = 10.0,
            scale_base_coin    = 4.0,
            min_profit_percent = 10.0,
            step_point         = 0.5,
            trailing_stop      = 2.0,
            limit_order        = 0.5,
            margin_level       = 1.07,
        )

    if streaming:
        engine = TickBacktestEngine(config=config, tick_csv=tick_csv,
                                     strategy_class=strategy_class)
    else:
        loader = TickLoader()
        ticks = loader.load_csv(tick_csv)
        engine = TickBacktestEngine(config=config, ticks=ticks,
                                     strategy_class=strategy_class)
    return engine.run()


if __name__ == "__main__":
    import sys
    import argparse
    import glob as _glob
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout
    )

    parser = argparse.ArgumentParser(description="Arbitrading Backtester")
    parser.add_argument("--tick", type=str, default="data/SOLUSDT-trades-2024-01.csv",
                        help="Path to tick CSV file (or glob pattern)")
    parser.add_argument("--symbol", type=str, default="SOL/USDT",
                        help="Trading pair symbol (e.g. PEPE/USDT)")
    parser.add_argument("--start-base", type=float, default=10.0,
                        help="Start base coin amount")
    parser.add_argument("--scale", type=float, default=4.0,
                        help="Scale base coin factor")
    parser.add_argument("--candle", action="store_true",
                        help="Use candle mode instead of tick mode")
    parser.add_argument("--csv", type=str, default="data/SOLUSDT_1m.csv",
                        help="Path to candle CSV (candle mode only)")
    # v4 params
    parser.add_argument("--strategy", type=str, default="v1", choices=["v1", "v2"],
                        help="Strategy version (default v1 για baseline, v2 για new v4 logic)")
    parser.add_argument("--min-profit", type=float, default=10.0,
                        help="MIN_PROFIT_PERCENT (default 10)")
    parser.add_argument("--step-point", type=float, default=0.5,
                        help="STEP_POINT (default 0.5)")
    parser.add_argument("--trailing-stop", type=float, default=2.0,
                        help="TRAILING_STOP (default 2)")
    parser.add_argument("--promote", type=int, default=1, choices=[1, 2, 3],
                        help="[v2 only] Promote route 1/2/3 (default 1)")
    parser.add_argument("--second-profit", type=str, default="off",
                        choices=["on", "off"],
                        help="[v2 only] SECOND_PROFIT_ENABLED (default off)")
    parser.add_argument("--second-profit-pct", type=float, default=4.0,
                        help="[v2 only] SECOND_PROFIT_PERCENT (default 4)")
    args = parser.parse_args()

    if args.candle:
        run_backtest(
            symbol   = args.symbol,
            start    = "2024-01-01",
            end      = "2024-02-01",
            csv_path = args.csv
        )
    else:
        # Find files (support glob pattern)
        files = sorted(_glob.glob(args.tick))
        if not files:
            files = [args.tick]

        # v4: Επιλογή strategy + config class
        if args.strategy == "v2":
            ConfigClass   = BotConfigV2
            StrategyClass = ArbitradingV2
            v2_extras = dict(
                promote               = args.promote,
                second_profit_enabled = (args.second_profit == "on"),
                second_profit_percent = args.second_profit_pct,
            )
        else:
            ConfigClass   = BotConfig
            StrategyClass = ArbitradingV1
            v2_extras = {}

        all_reports = []
        for tick_file in files:
            print(f"\n{'='*60}")
            print(f"  FILE: {tick_file}")
            print(f"  STRATEGY: {args.strategy}")
            if args.strategy == "v2":
                print(f"  PROMOTE: {args.promote} | SECOND_PROFIT: {args.second_profit} ({args.second_profit_pct}%)")
            print(f"{'='*60}")
            custom_config = ConfigClass(
                trading_pair       = args.symbol,
                start_base_coin    = args.start_base,
                scale_base_coin    = args.scale,
                min_profit_percent = args.min_profit,
                step_point         = args.step_point,
                trailing_stop      = args.trailing_stop,
                limit_order        = 0.5,
                margin_level       = 1.07,
                **v2_extras,
            )
            report = run_tick_backtest(
                symbol   = args.symbol,
                tick_csv = tick_file,
                config   = custom_config,
                streaming = True,
                strategy_class = StrategyClass,
            )
            all_reports.append(report)

        # Summary table if multiple files
        if len(all_reports) > 1:
            print(f"\n{'='*80}")
            print(f"  SUMMARY - {args.symbol} - {len(all_reports)} months - strategy={args.strategy}")
            print(f"{'='*80}")
            print(f"{'Month':<22} {'Cycles':>7} {'P&L USDT':>12} {'P&L %':>8} {'B&H %':>8}")
            print(f"{'-'*22} {'-'*7} {'-'*12} {'-'*8} {'-'*8}")
            total_pnl = 0
            for r in all_reports:
                if not r:
                    continue
                total_pnl += r.get('pnl_usdt', 0)
                print(f"{r.get('period','?'):<22} "
                      f"{r.get('cycles_completed',0):>7} "
                      f"{r.get('pnl_usdt',0):>12.2f} "
                      f"{r.get('pnl_pct',0):>8.2f} "
                      f"{r.get('buy_hold_pct',0):>8.2f}")
            print(f"{'-'*22} {'-'*7} {'-'*12} {'-'*8} {'-'*8}")
            init_val = all_reports[0].get('initial_value_usdt', 0) if all_reports else 0
            total_pct = (total_pnl / init_val * 100) if init_val > 0 else 0
            print(f"{'TOTAL':<22} {'':>7} {total_pnl:>12.2f} {total_pct:>8.2f}")
            print(f"{'='*80}")
