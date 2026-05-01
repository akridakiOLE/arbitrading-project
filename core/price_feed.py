"""
core/price_feed.py — Real-time price feed via ccxt REST polling.

MVP design (v4 Φάση 3α):
  - Poll ccxt.kucoin().fetch_ticker(symbol) κάθε POLL_INTERVAL δευτερόλεπτα
  - Callback-based — καλεί on_tick(price, timestamp) για κάθε νέα τιμή
  - Graceful reconnection σε network errors (exponential backoff)
  - Thread-safe stop() για καθαρή διακοπή

Για το arbitrading strategy (MIN_PROFIT=10% thresholds), 1s polling είναι
υπεραρκετό — τα triggers είναι minutes/hours apart. Upgrade σε WebSocket
είναι trivial αργότερα μέσω ccxt.pro ή raw websockets library.
"""

import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import ccxt

# v6.x: Phase 0 testing — price injection (single-symbol bot).
# Multi-symbol Phase 2+ will use per-slot files.
# Per-environment isolation: production and staging share /tmp on same host
# but use different files to avoid cross-contamination.
def _injection_file_for(env: str) -> str:
    return f"/tmp/arbitrading_injection_{env}.json"

logger = logging.getLogger(__name__)


class PriceFeed:
    """Polls a crypto exchange for price updates και τα στέλνει σε callback."""

    def __init__(self,
                 symbol:        str,
                 on_tick:       Callable[[float, datetime], None],
                 exchange_id:   str   = "kucoin",
                 poll_interval: float = 1.0,
                 max_backoff:   float = 60.0,
                 mode:          str   = "paper",
                 env:           str   = "production"):
        self.symbol        = symbol
        self.on_tick       = on_tick
        self.exchange_id   = exchange_id
        self.poll_interval = poll_interval
        self.max_backoff   = max_backoff
        # v6.x: bot mode (paper|live). Live mode IGNORES injections (hard guard).
        self.mode          = mode
        # v6.x: ARBITRADING_ENV (production|staging). Used για per-env injection file.
        self.env           = env

        self._exchange = self._init_exchange()
        self._stop     = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_price: Optional[float] = None
        self._tick_count  = 0
        self._error_count = 0

    def _init_exchange(self) -> ccxt.Exchange:
        """Δημιουργεί ccxt exchange instance. ΜΟΝΟ public market data — όχι auth."""
        klass = getattr(ccxt, self.exchange_id)
        return klass({
            'enableRateLimit': True,
            'timeout':         10000,  # 10s
        })

    def start(self) -> None:
        """Ξεκινάει το polling loop σε background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning(f"PriceFeed already running for {self.symbol}")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name=f"PriceFeed-{self.symbol}")
        self._thread.start()
        logger.info(f"[PriceFeed] Started for {self.symbol} @ {self.exchange_id} "
                    f"(poll every {self.poll_interval}s)")

    def stop(self, timeout: float = 5.0) -> None:
        """Σταματάει το polling loop καθαρά."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        logger.info(f"[PriceFeed] Stopped for {self.symbol} "
                    f"(ticks: {self._tick_count}, errors: {self._error_count})")

    def _run_loop(self) -> None:
        """Κύριος polling loop με exponential backoff σε errors."""
        backoff = self.poll_interval
        while not self._stop.is_set():
            try:
                price, ts = self._fetch_price()
                if price is not None and price > 0:
                    # Deduplicate αν η τιμή δεν άλλαξε (reduce strategy overhead)
                    # Σημείωση: για το arbitrading strategy δεν πειράζει να στέλνουμε
                    # duplicate prices — απλά δεν θα κάνει τίποτα στο on_price_update
                    self._last_price = price
                    self._tick_count += 1
                    try:
                        self.on_tick(price, ts)
                    except Exception as e:
                        logger.exception(f"[PriceFeed] on_tick callback error: {e}")
                backoff = self.poll_interval  # reset backoff σε επιτυχία
            except ccxt.NetworkError as e:
                self._error_count += 1
                logger.warning(f"[PriceFeed] Network error: {e} — retry in {backoff:.1f}s")
                time.sleep(min(backoff, self.max_backoff))
                backoff = min(backoff * 2, self.max_backoff)
                continue
            except ccxt.ExchangeError as e:
                self._error_count += 1
                logger.error(f"[PriceFeed] Exchange error: {e} — retry in {backoff:.1f}s")
                time.sleep(min(backoff, self.max_backoff))
                backoff = min(backoff * 2, self.max_backoff)
                continue
            except Exception as e:
                self._error_count += 1
                logger.exception(f"[PriceFeed] Unexpected error: {e}")
                time.sleep(self.max_backoff)
                continue

            # Normal poll interval
            self._stop.wait(self.poll_interval)

    def _fetch_price(self):
        """Fetch latest ticker και επιστρέφει (price, timestamp).

        v6.x: σε paper mode ελέγχει πρώτα αν υπάρχει active price injection
        (από tools/inject_price.py). Σε live mode το injection αγνοείται
        πάντα (hard guard).
        """
        # Phase 0 testing: check for active price injection (paper mode only).
        if self.mode != "live":
            injection = self._read_injection()
            if injection is not None:
                return injection
        # Normal path: real exchange ticker.
        ticker = self._exchange.fetch_ticker(self.symbol)
        price  = ticker.get('last') or ticker.get('close')
        ts_ms  = ticker.get('timestamp') or int(time.time() * 1000)
        ts     = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return (price, ts)

    def _read_injection(self):
        """Returns (injected_price, ts) if active injection for self.symbol, else None.

        Reads /tmp/arbitrading_injection_{env}.json. Per-env file isolates
        production from staging. The injection is honored only if:
          - File exists and parseable
          - injection.symbol matches self.symbol
          - injection.mode is not 'live' (defensive double-check)
          - If 'expires_at_iso' present → now < expires_at_iso
          - If 'expires_at_iso' missing → PERSISTENT, no expiry check

        Expired files are auto-cleaned. Any error returns None (fall-through to
        real exchange API).
        """
        inj_path = _injection_file_for(self.env)
        try:
            if not os.path.exists(inj_path):
                return None
            with open(inj_path, 'r') as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"[PriceFeed] Failed to read injection file: {e}")
            return None

        # Symbol mismatch → ignore (different bot's injection, e.g. multi-symbol future)
        if data.get("symbol") != self.symbol:
            return None
        # Defensive: never honor live-mode injection even if file says so
        if data.get("mode") == "live":
            logger.warning(f"[PriceFeed] Injection has mode=live, ignoring (paper-only feature)")
            return None
        # Expiry check (only if expires_at_iso is set; otherwise persistent)
        expires_iso = data.get("expires_at_iso")
        if expires_iso is not None:
            try:
                expires = datetime.fromisoformat(expires_iso)
                now = datetime.now(timezone.utc)
                if now >= expires:
                    # Auto-cleanup expired file
                    try: os.remove(inj_path)
                    except Exception: pass
                    return None
            except Exception:
                return None
        # Valid → return injected price with current timestamp
        try:
            price = float(data["price"])
            if price <= 0:
                return None
        except Exception:
            return None
        return (price, datetime.now(timezone.utc))

    def get_last_price(self) -> Optional[float]:
        """Επιστρέφει την τελευταία παρατηρηθείσα τιμή (για executor)."""
        return self._last_price

    def get_stats(self) -> dict:
        return {
            "symbol":      self.symbol,
            "exchange":    self.exchange_id,
            "running":     bool(self._thread and self._thread.is_alive()),
            "tick_count":  self._tick_count,
            "error_count": self._error_count,
            "last_price":  self._last_price,
        }


# ── CLI test mode ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import argparse

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S",
                        stream=sys.stdout)

    parser = argparse.ArgumentParser(description="Test price feed")
    parser.add_argument("--symbol", type=str, default="PEPE/USDT")
    parser.add_argument("--exchange", type=str, default="kucoin")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--duration", type=int, default=10,
                        help="Διάρκεια test σε seconds")
    args = parser.parse_args()

    def on_tick(price, ts):
        print(f"[{ts.isoformat()}] {args.symbol}: {price}")

    feed = PriceFeed(symbol=args.symbol,
                     on_tick=on_tick,
                     exchange_id=args.exchange,
                     poll_interval=args.interval)
    feed.start()
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        pass
    feed.stop()
    print(f"Stats: {feed.get_stats()}")
