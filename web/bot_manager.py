"""
web/bot_manager.py - Wraps TraderLoop for web UI control.

The bot runs in a background thread within the Flask process.
Single process model: systemd runs Flask/Gunicorn, Flask owns the bot thread.
If Flask dies, bot dies - systemd restarts both.

v5.1: Resume-from-state logic + live config update (LIVE/NEXT_CYCLE/RESTART
categories). Symbol/mode change forces fresh start. Otherwise default is
Resume (unless resume_from_state=False in config).
"""

import os
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


# ---------------------------------------------------------------------------
# Parameter categorization for live config updates.
#
# LIVE:       εφαρμόζεται άμεσα στο trέχον strategy.config
# NEXT_CYCLE: κρατείται σε _pending_config, εφαρμόζεται πριν το επόμενο SETUP
# RESTART:    απαιτεί Soft Stop + Start για να εφαρμοστεί
# ---------------------------------------------------------------------------
PARAM_CATEGORIES = {
    # LIVE — αλλάζουν triggers/limits στον τρέχοντα κύκλο
    "min_profit_percent":    "LIVE",
    "step_point":            "LIVE",
    "trailing_stop":         "LIVE",
    "limit_order":           "LIVE",
    "margin_level":          "LIVE",
    "second_profit_enabled": "LIVE",
    "second_profit_percent": "LIVE",

    # NEXT_CYCLE — εφαρμόζονται στο επόμενο SETUP
    "promote":               "NEXT_CYCLE",
    "scale_base_coin":       "NEXT_CYCLE",
    "ratio_scale":           "NEXT_CYCLE",
    "vip_coins":             "NEXT_CYCLE",
    "vip_allocation_mode":   "NEXT_CYCLE",
    "vip_percentages":       "NEXT_CYCLE",
    "vip_priority_list":     "NEXT_CYCLE",
    "scale_vip_coin":        "NEXT_CYCLE",
    "min_order_usdt":        "NEXT_CYCLE",

    # RESTART — χρειάζεται Soft Stop + Start
    "symbol":                "RESTART",
    "mode":                  "RESTART",
    "start_base_coin":       "RESTART",
    "poll_interval":         "RESTART",
    "resume_from_state":     "RESTART",   # διαβάζεται μόνο στο start()
}


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
        self._pending_config: dict              = {}   # NEXT_CYCLE queue
        self._last_resume_event: Optional[str]  = None  # για debug / UI badge

    # ----- Config persistence -----

    def load_config(self) -> dict:
        if not self.config_file.exists():
            return self._default_config()
        try:
            d = json.loads(self.config_file.read_text())
            # Εφαρμογή defaults για νέα πεδία που δεν υπάρχουν σε παλιά config files
            defaults = self._default_config()
            for k, v in defaults.items():
                d.setdefault(k, v)
            return d
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
            "resume_from_state":     True,
        }

    # ----- Start / Stop -----

    def start(self, cfg_dict: dict) -> dict:
        """Start bot with given config. Performs Resume-from-state if applicable.

        Resume rules:
          - Resume only if resume_from_state=True AND previous snapshot exists
            AND symbol+mode match previous snapshot meta.
          - Symbol or mode mismatch → forced fresh start (log warning).
          - No previous snapshot → fresh start (expected on first run)."""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "Bot already running"}

            self._last_error = None
            self._pending_config = {}
            try:
                # v6.x: auto-derive vip_coins αν είναι κενό αλλά υπάρχουν entries σε
                # vip_percentages ή vip_priority_list. Έτσι αν ο χρήστης συμπληρώσει
                # μόνο τα ποσοστά (ή priority list) — όπως είναι φυσικό — δεν χάνονται
                # τα VIP coins λόγω ξεχασμένου πεδίου.
                vip_coins_input        = list(cfg_dict.get("vip_coins", []))
                vip_percentages_input  = dict(cfg_dict.get("vip_percentages", {}))
                vip_priority_input     = list(cfg_dict.get("vip_priority_list", []))
                vip_allocation_mode    = cfg_dict.get("vip_allocation_mode", "percent")
                if not vip_coins_input:
                    if vip_allocation_mode == "percent" and vip_percentages_input:
                        vip_coins_input = list(vip_percentages_input.keys())
                        logger.info(f"[BotManager] auto-derived vip_coins from vip_percentages: {vip_coins_input}")
                    elif vip_allocation_mode == "priority" and vip_priority_input:
                        vip_coins_input = list(vip_priority_input)
                        logger.info(f"[BotManager] auto-derived vip_coins from vip_priority_list: {vip_coins_input}")

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
                    vip_coins             = vip_coins_input,
                    vip_allocation_mode   = vip_allocation_mode,
                    vip_percentages       = vip_percentages_input,
                    vip_priority_list     = vip_priority_input,
                    scale_vip_coin        = float(cfg_dict.get("scale_vip_coin", 5.0)),
                    min_order_usdt        = float(cfg_dict.get("min_order_usdt", 5.0)),
                )

                self._symbol = cfg_dict["symbol"]
                self._mode   = cfg_dict.get("mode", "paper")
                self._current_config = bot_config
                resume_requested = bool(cfg_dict.get("resume_from_state", True))

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

                # ---- Resume-from-state decision ----
                resume_info = self._maybe_resume(resume_requested)
                self._last_resume_event = resume_info["decision"]
                logger.info(f"[BotManager] Resume decision: {resume_info['decision']} "
                            f"({resume_info['reason']})")

                self.feed = PriceFeed(
                    symbol=self._symbol,
                    on_tick=self._on_tick,
                    exchange_id="kucoin",
                    poll_interval=float(cfg_dict.get("poll_interval", 1.0)),
                    mode=self._mode,  # v6.x: για price injection hard guard (live ignores)
                    env=os.environ.get("ARBITRADING_ENV", "production"),  # per-env injection file
                )

                self._started_at = datetime.utcnow()
                self._running = True

                self.save_config(cfg_dict)
                self.feed.start()
                logger.info(f"[BotManager] Started {self._mode} mode for {self._symbol} "
                            f"(tick_count={self._tick_count})")
                return {
                    "ok":     True,
                    "mode":   self._mode,
                    "symbol": self._symbol,
                    "resume": resume_info,
                }

            except Exception as e:
                self._last_error = str(e)
                logger.exception(f"Start failed: {e}")
                # start() απέτυχε — χρησιμοποιούμε START_FAILED αντί για USER_STOP
                # ώστε να μη μπλοκαριστεί auto-resume σε επόμενο boot
                self._cleanup_locked(stop_event="START_FAILED")
                return {"ok": False, "error": str(e)}

    def _maybe_resume(self, resume_requested: bool) -> dict:
        """Αποφασίζει Resume vs Fresh και εφαρμόζει το state αν Resume.

        Επιστρέφει dict: {'decision': 'resumed'|'fresh', 'reason': str, ...}
        Mετά από αυτή τη μέθοδο:
          - self._tick_count έχει σωστή τιμή (0 για fresh, saved για resume)
          - self._started_strategy = True αν Resume (το _on_tick ΔΕΝ θα κάνει SETUP)
          - self._started_strategy = False αν Fresh (το _on_tick θα κάνει SETUP)
          - self.strategy.state + memory + executor balances restored αν Resume."""
        self._tick_count = 0
        self._started_strategy = False

        if not resume_requested:
            return {"decision": "fresh", "reason": "resume_from_state=False (user chose fresh)"}

        snap = self.state_persistence.restore_latest()
        if snap is None:
            return {"decision": "fresh", "reason": "no previous snapshot found"}

        meta = snap.get("meta") or {}
        prev_symbol = meta.get("symbol")
        prev_mode   = meta.get("mode")

        # v5.2: Legacy snapshots (pre-v5.1) δεν έχουν meta/balances. Δεν
        # μπορούμε να επαληθεύσουμε symbol/mode, ούτε έχουμε balances να
        # κάνουμε restore — άρα force Fresh Start αντί για ημιτελές resume.
        if not meta:
            return {"decision": "fresh",
                    "reason": "legacy snapshot without meta (pre-v5.1) — cannot resume safely"}

        if prev_symbol and prev_symbol != self._symbol:
            return {"decision": "fresh",
                    "reason": f"symbol changed ({prev_symbol} -> {self._symbol})"}
        if prev_mode and prev_mode != self._mode:
            return {"decision": "fresh",
                    "reason": f"mode changed ({prev_mode} -> {self._mode})"}

        # Resume: εφαρμογή memory + balances + metadata
        try:
            # 1) Memory
            self.state_persistence.apply_to_memory(snap["memory"], self.strategy.memory)
            # 2) Executor balances
            if snap.get("balances") and hasattr(self.executor, "restore_balances"):
                self.executor.restore_balances(snap["balances"])
            # 3) State machine state
            try:
                self.strategy.state = BotState(snap["state"])
            except ValueError:
                logger.warning(f"[BotManager] Unknown state '{snap['state']}', falling back IDLE")
                self.strategy.state = BotState.IDLE
            # 4) Tick count
            self._tick_count = int(meta.get("tick_count", 0))
            # 5) Signal to _on_tick: δεν καλείς strategy.start()
            self._started_strategy = True
        except Exception as e:
            logger.exception(f"[BotManager] Resume failed ({e}), fallback to fresh start")
            # Reset strategy instance για να μη έχει μισό state
            self.strategy = ArbitradingV2(config=self._current_config, executor=self.executor)
            self._tick_count = 0
            self._started_strategy = False
            return {"decision": "fresh", "reason": f"restore error: {e}"}

        return {
            "decision":   "resumed",
            "reason":     f"resumed from {snap.get('ts_iso')} ({snap.get('event')})",
            "from_event": snap.get("event"),
            "from_state": snap.get("state"),
            "tick_count": self._tick_count,
        }

    def stop(self) -> dict:
        """Soft Stop: σταματά το loop. ΔΕΝ στέλνει εντολές σε exchange —
        τυχόν ανοιχτές margin θέσεις παραμένουν ως έχουν και χειρίζονται
        χειροκίνητα στο KuCoin."""
        with self._lock:
            if not self._running:
                return {"ok": False, "error": "Bot not running"}
            try:
                self._cleanup_locked()
                logger.info("[BotManager] Soft-stopped (no exchange actions taken)")
                return {"ok": True}
            except Exception as e:
                self._last_error = str(e)
                return {"ok": False, "error": str(e)}

    def _cleanup_locked(self, stop_event: str = "USER_STOP") -> None:
        """Called with self._lock held.

        stop_event σηματοδοτεί ποιος σταμάτησε το bot:
          - 'USER_STOP' (default) — ρητό Soft Stop από UI → δεν κάνουμε auto-resume
          - 'STOP' — άλλος λόγος (κλείσιμο service κλπ) → επιτρέπουμε auto-resume"""
        if self.feed:
            try: self.feed.stop()
            except Exception: pass
        if self.state_persistence and self.strategy:
            try:
                self._save_snapshot(stop_event)
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
                self._save_snapshot("RESSET_INVEST")
                return {"ok": True, "price": price}
            except Exception as e:
                self._last_error = str(e)
                return {"ok": False, "error": str(e)}

    # ----- Live config update -----

    def update_config(self, updates: dict) -> dict:
        """Εφαρμόζει config updates κατηγοριοποιημένα (LIVE/NEXT_CYCLE/RESTART).

        Returns:
          {'applied_live': [...], 'queued_next_cycle': [...], 'rejected_restart': [...], ...}
        """
        applied_live     = []
        queued_next      = []
        rejected_restart = []
        unknown          = []

        with self._lock:
            if not self._running or not self.strategy:
                # Bot is stopped — απλά αποθηκεύουμε στο config file
                saved = self.load_config()
                for k, v in updates.items():
                    if k in saved:
                        saved[k] = v
                    else:
                        unknown.append(k)
                self.save_config(saved)
                return {
                    "running": False,
                    "applied_live": [],
                    "queued_next_cycle": [],
                    "rejected_restart": [],
                    "unknown": unknown,
                    "note": "Bot not running — changes saved to config file only",
                }

            cfg = self.strategy.config
            for k, v in updates.items():
                cat = PARAM_CATEGORIES.get(k)
                if cat is None:
                    unknown.append(k)
                    continue
                if cat == "LIVE":
                    # Εφαρμόζουμε αμέσως στο strategy.config
                    if hasattr(cfg, k):
                        try:
                            current = getattr(cfg, k)
                            if isinstance(current, bool):
                                setattr(cfg, k, bool(v))
                            elif isinstance(current, (int, float)):
                                setattr(cfg, k, type(current)(v))
                            else:
                                setattr(cfg, k, v)
                            applied_live.append(k)
                        except Exception as e:
                            logger.warning(f"update_config: failed setting {k}={v}: {e}")
                elif cat == "NEXT_CYCLE":
                    self._pending_config[k] = v
                    queued_next.append(k)
                elif cat == "RESTART":
                    rejected_restart.append(k)

            # Αποθήκευση ΟΛΩΝ στο config file (ώστε να παραμείνουν στο επόμενο Start)
            saved = self.load_config()
            for k, v in updates.items():
                if k in saved:
                    saved[k] = v
            self.save_config(saved)

        return {
            "running":           True,
            "applied_live":      applied_live,
            "queued_next_cycle": queued_next,
            "rejected_restart":  rejected_restart,
            "unknown":           unknown,
        }

    def get_param_categories(self) -> dict:
        return dict(PARAM_CATEGORIES)

    # ----- Auto-Resume on boot -----

    def try_auto_resume(self) -> dict:
        """Καλείται από τη Flask εκκίνηση. Επιχειρεί να κάνει auto-start αν:
          - Υπάρχει έγκυρο state snapshot με meta (v5.1+ format)
          - Το saved mode είναι 'paper' (live ΔΕΝ auto-resumes — απαιτεί manual)
          - Το τελευταίο event δεν είναι USER_STOP (user explicit stop = respect it)
          - Το symbol του snapshot ταιριάζει με το saved config

        Safety constraints:
          - ΠΟΤΕ δεν κάνει auto-start σε live mode (αξιώνει ρητή ανθρώπινη έγκριση)
          - Αν το bot είναι ήδη running (race condition), skip
          - Αν οποιαδήποτε ασυνέπεια → log warning και skip
        """
        try:
            with self._lock:
                if self._running:
                    return {"auto_started": False, "reason": "already running"}

            cfg = self.load_config()
            mode   = cfg.get("mode", "paper")
            symbol = cfg.get("symbol")

            if mode == "live":
                logger.info("[BotManager] Auto-resume skipped: live mode requires manual Start")
                return {"auto_started": False, "reason": "live mode — manual Start required"}

            db_path = "live_state.db" if mode == "live" else "paper_state.db"
            if not Path(db_path).exists():
                logger.info(f"[BotManager] Auto-resume skipped: {db_path} δεν υπάρχει (καμία προηγ. εκτέλεση)")
                return {"auto_started": False, "reason": "no state db yet"}

            # Peek latest snapshot χωρίς να αρχικοποιήσουμε StatePersistence εδώ
            import sqlite3, json
            try:
                conn = sqlite3.connect(db_path)
                row = conn.execute(
                    "SELECT event, memory_json FROM state_snapshots "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                conn.close()
            except Exception as e:
                logger.warning(f"[BotManager] Auto-resume skipped (DB read error): {e}")
                return {"auto_started": False, "reason": f"db read error: {e}"}

            if not row:
                return {"auto_started": False, "reason": "no snapshots"}

            last_event, mem_json = row

            # User-initiated stops → respect the stop, skip auto-resume
            if last_event == "USER_STOP":
                logger.info("[BotManager] Auto-resume skipped: last stop was user-initiated")
                return {"auto_started": False, "reason": "last stop was USER_STOP"}

            try:
                parsed = json.loads(mem_json)
            except Exception:
                return {"auto_started": False, "reason": "snapshot parse error"}

            meta = (parsed.get("meta") if isinstance(parsed, dict) else None) or {}
            if not meta:
                logger.info("[BotManager] Auto-resume skipped: legacy snapshot (no meta)")
                return {"auto_started": False, "reason": "legacy snapshot (no meta)"}

            prev_symbol = meta.get("symbol")
            prev_mode   = meta.get("mode")
            if prev_symbol != symbol:
                logger.info(f"[BotManager] Auto-resume skipped: symbol mismatch ({prev_symbol} vs {symbol})")
                return {"auto_started": False, "reason": f"symbol mismatch"}
            if prev_mode != mode:
                logger.info(f"[BotManager] Auto-resume skipped: mode mismatch ({prev_mode} vs {mode})")
                return {"auto_started": False, "reason": f"mode mismatch"}

            # Όλα OK → auto-start με resume
            cfg["resume_from_state"] = True
            logger.info(f"[BotManager] AUTO-RESUMING on boot: {symbol} {mode} "
                        f"(last event: {last_event}, tick_count={meta.get('tick_count', '?')})")
            result = self.start(cfg)
            if result.get("ok"):
                return {"auto_started": True, "result": result}
            else:
                logger.warning(f"[BotManager] Auto-resume failed at start(): {result.get('error')}")
                return {"auto_started": False, "reason": f"start() failed: {result.get('error')}"}
        except Exception as e:
            logger.exception(f"[BotManager] try_auto_resume exception: {e}")
            return {"auto_started": False, "reason": f"exception: {e}"}

    def _apply_pending_at_setup(self) -> None:
        """Εφαρμόζει pending NEXT_CYCLE αλλαγές πριν από νέο SETUP."""
        if not self._pending_config or not self.strategy:
            return
        cfg = self.strategy.config
        for k, v in list(self._pending_config.items()):
            if hasattr(cfg, k):
                try:
                    current = getattr(cfg, k)
                    if isinstance(current, bool):
                        setattr(cfg, k, bool(v))
                    elif isinstance(current, int):
                        setattr(cfg, k, int(v))
                    elif isinstance(current, float):
                        setattr(cfg, k, float(v))
                    elif isinstance(current, list):
                        setattr(cfg, k, list(v))
                    elif isinstance(current, dict):
                        setattr(cfg, k, dict(v))
                    else:
                        setattr(cfg, k, v)
                    logger.info(f"[BotManager] Applied NEXT_CYCLE: {k} = {v}")
                except Exception as e:
                    logger.warning(f"[BotManager] Failed applying pending {k}: {e}")
        self._pending_config.clear()

    # ----- Tick handler -----

    def _save_snapshot(self, event: str) -> None:
        if not (self.state_persistence and self.strategy and self.executor):
            return
        try:
            balances = self.executor.get_balance_dict() if hasattr(self.executor, "get_balance_dict") else {}
            meta = {
                "symbol":     self._symbol,
                "mode":       self._mode,
                "tick_count": self._tick_count,
            }
            self.state_persistence.save(
                self.strategy.memory, self.strategy.state,
                event=event, executor_balances=balances, meta=meta,
            )
        except Exception as e:
            logger.warning(f"[BotManager] snapshot save failed: {e}")

    def _on_tick(self, price: float, ts: datetime) -> None:
        try:
            self.executor.current_price = price
            self._tick_count += 1
            prev_state = self.strategy.state

            if not getattr(self, "_started_strategy", False):
                # Fresh start path
                self.strategy.start(price, ts)
                self._started_strategy = True
                self._save_snapshot("INIT")
            else:
                # Πριν από κάθε νέο SETUP, εφαρμόζουμε pending NEXT_CYCLE
                if self.strategy.state == BotState.SETUP:
                    self._apply_pending_at_setup()
                self.strategy.on_price_update(price, ts)

            if self.strategy.state != prev_state:
                self._save_snapshot(f"STATE_{prev_state.value}->{self.strategy.state.value}")
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
                "pending_next_cycle": {},
                "last_resume_event": self._last_resume_event,
            }
        price = (self.feed.get_last_price() or 0.0) if self.feed else 0.0
        stat  = self.strategy.get_status(price)
        snap  = self.executor.snapshot(price) if self.executor else {}
        feed_stats = self.feed.get_stats() if self.feed else {}
        return {
            "running":            True,
            "mode":               self._mode,
            "symbol":             self._symbol,
            "started_at":         self._started_at.isoformat() if self._started_at else None,
            "tick_count":         self._tick_count,
            "feed":               feed_stats,
            "strategy":           stat,
            "snapshot":           snap,
            "last_error":         self._last_error,
            "pending_next_cycle": dict(self._pending_config),
            "last_resume_event":  self._last_resume_event,
        }


# Module-level singleton
_instance: Optional[BotManager] = None


def get_manager() -> BotManager:
    """Return the module-level BotManager singleton (created on first call)."""
    global _instance
    if _instance is None:
        _instance = BotManager()
    return _instance
