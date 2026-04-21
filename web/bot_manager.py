"""
web/bot_manager.py - Wraps TraderLoop for web UI control.

The bot runs in a background thread within the Flask process.
Single process model: systemd runs Flask/Gunicorn, Flask owns the bot thread.
If Flask dies, bot dies - systemd restarts both.
"""

import json
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from core.price_feed        import PriceFeed
from core.paper_executor    import PaperExecutor
from core.state_persistence import StatePersistence
from strategies.arbitrading_v2 import ArbitradingV2, BotConfig, BotState

logger = logging.getLogger(__name__)


class BotManager:
    """Manages a single bot instance inside the web server process."""

    def __init__(self, config_file: str = "web_bot_config.json"):
        self.config_file = Path(config_file)
        self.strategy: Optional[ArbitradingV2] = None
        self.executor: Optional[object]        = None
        self.feed:     Optional[PriceFeed]     = None
        self.state_persistence: Optional[StatePersistence] = None

        self._lock          = threading.Lock()
        self._running       = False
        self._mode          = "paper"  # paper | live
        self._symbol        = "PEPE/USDT"
        self._tick_count    = 0
        self._started_at:   Optional[datetime] = None
        self._last_error:   Optional[str]      = None
        self._current_config: Optional[BotConfig] = None

    # ----- Config persistence -----

    def load_config(self) -> dict:
        if not self.config_file.exists():
            return self._default_config()
        try:
            return json.loads(self.config_file.read_text())
        except Exception as e:
            logger.warning(f"config load failed: {e}")
            return self._default_config()

    def save_config(self, cfg: dict) -> None:
        self.config_file.write_text(json.dumps(cfg, indent=2))

    def _default_config(self) -> dict:
        return {
            "symbol":                "PEPE/USDT",
            "start_base_coin":       26_000_000.0,
            "scale_base_coin":       4.0,
            "ratio_scale":           0.005,
            "min_profit_percent":    10.0,
            "step_point":            0.5,
            "trailing_stop":         2.0,
            "limit_order":           0.5,
            "margin_level":          1.07,
            "promote":               1,
            "second_profit_enabled": False,
            "second_profit_percent": 4.0,
            "vip_coins":             [],
            "vip_allocation_mode":   "percent",
            "vip_percentages":       {},
            "vip_priority_list":     [],
            "scale_vip_coin":        5.0,
            "min_order_usdt":        5.0,
            "poll_interval":         1.0,
            "mode":                  "paper",
        }

    # ----- Start / Stop -----

    def start(self, cfg_dict: dict) -> dict:
        """Start bot with given config. Returns status dict."""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "Bot already running"}

            self._last_error = None
            try:
                bot_config = BotConfig(
                    trading_pair          = cfg_dict["symbol"],
                    start_base_coin       = float(cfg_dict["start_base_coin"]),
                    scale_base_coin       = float(cfg_dict["scale_base_coin"]),
                    ratio_scale           = float(cfg_dict.get("ratio_scale", 0.005)),
                    min_profit_percent    = float(cfg_dict["min_profit_percent"]),
                    step_point            = float(cfg_dict["step_point"]),
                    trailing_stop         = float(cfg_dict["trailing_stop"]),
                    limit_order           = float(cfg_dict.get("limit_order", 0.5)),
                    margin_level          = float(cfg_dict["margin_level"]),
                    promote               = int(cfg_dict["promote"]),
                    second_profit_enabled = bool(cfg_dict["second_profit_enabled"]),
                    second_profit_percent = float(cfg_dict["second_profit_percent"]),
                    vip_coins             = list(cfg_dict.get("vip_coins", [])),
                    vip_allocation_mode   = cfg_dict.get("vip_allocation_mode", "percent"),
                    vip_percentages       = dict(cfg_dict.get("vip_percentages", {})),
                    vip_priority_list     = list(cfg_dict.get("vip_priority_list", [])),
                    scale_vip_coin        = float(cfg_dict.get("scale_vip_coin", 5.0)),
                    min_order_usdt        = float(cfg_dict.get("min_order_usdt", 5.0)),
                )

                self._symbol = cfg_dict["symbol"]
                self._mode   = cfg_dict.get("mode", "paper")
                self._current_config = bot_config

                # Executor selection
                if self._mode == "live":
                    from api.kucoin_client import KuCoinMarginClient
                    from core.live_executor import LiveExecutor
                    from config import settings as s
                    if not (s.KUCOIN_API_KEY and s.KUCOIN_API_SECRET and s.KUCOIN_API_PASSPHRASE):
                        raise RuntimeError("KuCoin API keys not configured in secrets.env")
                    client = KuCoinMarginClient(
                        api_key=s.KUCOIN_API_KEY,
                        api_secret=s.KUCOIN_API_SECRET,
                        api_passphrase=s.KUCOIN_API_PASSPHRASE,
                    )
                    base_ccy = self._symbol.split('/')[0]
                    self.executor = LiveExecutor(
                        client=client,
                        symbol=self._symbol,
                        base_ccy=base_ccy,
                        db_path="live_trades.db",
                    )
                else:
                    self.executor = PaperExecutor(
                        start_base_coin=bot_config.start_base_coin,
                        db_path="paper_trades.db",
                        exchange_id="kucoin",
                        slippage_pct=0.0,
                    )

                self.state_persistence = StatePersistence(
                    "live_state.db" if self._mode == "live" else "paper_state.db"
                )

                self.strategy = ArbitradingV2(config=bot_config, executor=self.executor)

                self.feed = PriceFeed(
                    symbol=self._symbol,
                    on_tick=self._on_tick,
                    exchange_id="kucoin",
                    poll_interval=float(cfg_dict.get("poll_interval", 1.0)),
                )

                self._tick_count = 0
                self._started_at = datetime.utcnow()
                self._started_strategy = False
                self._running = True

                self.save_config(cfg_dict)
                self.feed.start()
                logger.info(f"[BotManager] Started {self._mode} mode for {self._symbol}")
                return {"ok": True, "mode": self._mode, "symbol": self._symbol}

            except Exception as e:
                self._last_error = str(e)
                logger.exception(f"Start failed: {e}")
                self._cleanup_locked()
                return {"ok": False, "error": str(e)}

    def stop(self) -> dict:
        with self._lock:
            if not self._running:
                return {"ok": False, "error": "Bot not running"}
            try:
                self._cleanup_locked()
                logger.info("[BotManager] Stopped cleanly")
                return {"ok": True}
            except Exception as e:
                self._last_error = str(e)
                return {"ok": False, "error": str(e)}

    def _cleanup_locked(self) -> None:
        """Called with self._lock held."""
        if self.feed:
            try: self.feed.stop()
            except Exception: pass
        if self.state_persistence and self.strategy:
            try:
                self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                            event="STOP")
                self.state_persistence.close()
            except Exception: pass
        if self.executor:
            try: self.executor.close()
            except Exception: pass
        self._running = False

    def resset_invest(self) -> dict:
        with self._lock:
            if not self._running or not self.strategy or not self.feed:
                return {"ok": False, "error": "Bot not running"}
            try:
                price = self.feed.get_last_price() or 0.0
                self.strategy.execute_resset_invest(price, datetime.utcnow())
                if self.state_persistence:
                    self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                                event="RESSET_INVEST")
                return {"ok": True, "price": price}
            except Exception as e:
                self._last_error = str(e)
                return {"ok": False, "error": str(e)}

    # ----- Tick handler -----

    def _on_tick(self, price: float, ts: datetime) -> None:
        try:
            self.executor.current_price = price
            self._tick_count += 1
            prev_state = self.strategy.state

            if not getattr(self, "_started_strategy", False):
                self.strategy.start(price, ts)
                self._started_strategy = True
                self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                            event="INIT")
            else:
                self.strategy.on_price_update(price, ts)

            if self.strategy.state != prev_state:
                self.state_persistence.save(self.strategy.memory, self.strategy.state,
                                            event=f"STATE_{prev_state.value}->{self.strategy.state.value}")
        except Exception as e:
            self._last_error = str(e)
            logger.exception(f"tick error: {e}")

    # ----- Status -----

    def status(self) -> dict:
        if not self._running or not self.strategy:
            cfg = self.load_config()
            return {
                "running":    False,
                "mode":       self._mode,
                "symbol":     self._symbol,
                "last_error": self._last_error,
                "config":     cfg,
            }
        price = (self.feed.get_last_price() or 0.0) if self.feed else 0.0
        stat  = self.strategy.get_status(price)
        snap  = self.executor.snapshot(price) if self.executor else {}
        feed_stats = self.feed.get_stats() if self.feed else {}
        return {
            "running":     True,
            "mode":        self._mode,
            "symbol":      self._symbol,
            "started_at":  self._started_at.isoformat() if self._started_at else None,
            "tick_count":  self._tick_count,
            "feed":        feed_stats,
            "strategy":    stat,
            "snapshot":    snap,
            "last_error":  self._last_error,
        }


# Module-level singleton
_instance: Optional[BotManager] = None

def get_manager() -> BotManager:
    global _instance
    if _instance is None:
        _instance = BotManager()
    return _instance
