"""
core/paper_executor.py — Paper Trading Executor (v4 Φάση 3α).

Ταυτόσημο interface με BacktestExecutor (backtester/engine.py):
  - borrow_usdt / repay_usdt
  - borrow_base_coin / repay_base_coin
  - buy_base_coin / sell_base_coin
  - cancel_all_orders
  - VIP methods (v4): get_vip_price / buy_vip / borrow_usdt_vip / repay_usdt_vip

Διαφορές από backtester:
  - Χρησιμοποιεί ΠΡΑΓΜΑΤΙΚΗ τιμή από price_feed (μέσω current_price)
  - Κάθε virtual trade αποθηκεύεται σε SQLite audit DB (για recovery / review)
  - Slippage simulation (configurable, default 0%)
  - VIP prices φορτώνονται από ccxt fetch_ticker (lazy)

ΔΕΝ στέλνονται πραγματικές εντολές σε exchange. Αυτό το κάνει μόνο ο
LiveExecutor (μελλοντικό — Φάση 3γ).
"""

import sqlite3
import logging
import time
from datetime import datetime, timezone
from typing import Tuple, Optional, Dict, Any

import ccxt

logger = logging.getLogger(__name__)


class PaperExecutor:
    """Virtual executor για paper trading με real-time prices."""

    def __init__(self,
                 start_base_coin: float,
                 db_path:         str   = "paper_trades.db",
                 exchange_id:     str   = "kucoin",
                 slippage_pct:    float = 0.0,
                 vip_symbols:     Optional[Dict[str, str]] = None):
        """
        start_base_coin: Αρχική ποσότητα BASE_COIN (δικά μας κεφάλαια)
        db_path:         SQLite audit log path
        exchange_id:     Για VIP price fetches μέσω ccxt
        slippage_pct:    Προσομοίωση slippage (0.1 = 10bps πάνω στην αγορά, κάτω στην πώληση)
        vip_symbols:     Mapping coin → trading pair για VIP price fetches
                         π.χ. {"BTC": "BTC/USDT", "ETH": "ETH/USDT", "SOL": "SOL/USDT"}
        """
        # Virtual balance (ίδιο με BacktestExecutor)
        self.base_coin       = start_base_coin
        self.usdt            = 0.0
        self.usdt_debt       = 0.0
        self.base_debt       = 0.0
        self.current_price   = 0.0  # ενημερώνεται από trader_loop

        # v4 VIP support
        self.vip_holdings    = {}
        self.vip_debt_usdt   = 0.0
        self.vip_symbols     = vip_symbols or {
            "BTC": "BTC/USDT", "ETH": "ETH/USDT", "SOL": "SOL/USDT",
        }
        self._vip_price_cache: Dict[str, tuple] = {}  # coin -> (price, ts)
        self._vip_price_ttl = 5.0   # seconds

        # ccxt για VIP prices
        self._exchange = None
        self._exchange_id = exchange_id

        # Slippage
        self.slippage_pct = slippage_pct

        # Audit log
        self.db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

        logger.info(f"[PaperExecutor] Initialized | start_base={start_base_coin} | "
                    f"db={db_path} | slippage={slippage_pct}%")

    def _init_db(self) -> None:
        """Δημιουργεί audit schema αν δεν υπάρχει."""
        cur = self._db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_iso       TEXT    NOT NULL,
                action       TEXT    NOT NULL,
                symbol       TEXT,
                quantity     REAL,
                price        REAL,
                usdt_value   REAL,
                note         TEXT,
                base_coin    REAL,
                usdt         REAL,
                usdt_debt    REAL,
                base_debt    REAL,
                vip_debt     REAL
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_paper_trades_ts
            ON paper_trades(ts_iso)
        """)
        self._db.commit()

    def _log(self, action: str,
             quantity: float = 0.0, price: float = 0.0,
             usdt_value: float = 0.0, symbol: str = "",
             note: str = "") -> None:
        cur = self._db.cursor()
        cur.execute("""
            INSERT INTO paper_trades
            (ts_iso, action, symbol, quantity, price, usdt_value, note,
             base_coin, usdt, usdt_debt, base_debt, vip_debt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(tz=timezone.utc).isoformat(),
            action, symbol, quantity, price, usdt_value, note,
            self.base_coin, self.usdt, self.usdt_debt, self.base_debt,
            self.vip_debt_usdt,
        ))
        self._db.commit()

    # ── Δανεισμός ─────────────────────────────────────────────────────────────

    def borrow_usdt(self, amount: float) -> float:
        self.usdt      += amount
        self.usdt_debt += amount
        self._log("BORROW_USDT", usdt_value=amount,
                  note=f"debt total: {self.usdt_debt:.2f}")
        logger.info(f"  [PAPER] borrow_usdt {amount:.2f}")
        return amount

    def repay_usdt(self, amount: float) -> None:
        actual = min(amount, self.usdt)
        self.usdt      -= actual
        self.usdt_debt  = max(0.0, self.usdt_debt - actual)
        self._log("REPAY_USDT", usdt_value=actual,
                  note=f"debt remaining: {self.usdt_debt:.2f}")
        logger.info(f"  [PAPER] repay_usdt {actual:.2f}")

    def borrow_base_coin(self, quantity: float) -> float:
        self.base_coin += quantity
        self.base_debt += quantity
        self._log("BORROW_BASE", quantity=quantity,
                  note=f"debt total: {self.base_debt:.4f}")
        logger.info(f"  [PAPER] borrow_base {quantity:.4f}")
        return quantity

    def repay_base_coin(self, quantity: float) -> None:
        actual = min(quantity, self.base_coin)
        self.base_coin -= actual
        self.base_debt  = max(0.0, self.base_debt - actual)
        self._log("REPAY_BASE", quantity=actual,
                  note=f"debt remaining: {self.base_debt:.4f}")
        logger.info(f"  [PAPER] repay_base {actual:.4f}")

    # ── Αγορά / Πώληση ────────────────────────────────────────────────────────

    def buy_base_coin(self, quantity: float, limit_price: float) -> Tuple[float, float]:
        """Αγορά με slippage-adjusted price."""
        exec_price = limit_price * (1 + self.slippage_pct / 100.0)
        cost = quantity * exec_price
        if cost > self.usdt:
            quantity = self.usdt / exec_price if exec_price > 0 else 0
            cost     = self.usdt
        self.usdt      -= cost
        self.base_coin += quantity
        self._log("BUY", quantity=quantity, price=exec_price, usdt_value=cost,
                  note=f"limit={limit_price:.6f} slippage={self.slippage_pct}%")
        logger.info(f"  [PAPER] BUY {quantity:.4f} @ {exec_price:.6f} = {cost:.2f} USDT")
        return (quantity, exec_price)

    def sell_base_coin(self, quantity: float,
                       limit_price: float) -> Tuple[float, float, float]:
        """Πώληση με slippage-adjusted price (χαμηλότερη από limit)."""
        exec_price = limit_price * (1 - self.slippage_pct / 100.0)
        actual_qty = min(quantity, self.base_coin)
        usdt_received = actual_qty * exec_price
        self.base_coin -= actual_qty
        self.usdt      += usdt_received
        self._log("SELL", quantity=actual_qty, price=exec_price, usdt_value=usdt_received,
                  note=f"limit={limit_price:.6f} slippage={self.slippage_pct}%")
        logger.info(f"  [PAPER] SELL {actual_qty:.4f} @ {exec_price:.6f} = {usdt_received:.2f} USDT")
        return (actual_qty, exec_price, usdt_received)

    def cancel_all_orders(self) -> None:
        """Στο paper mode δεν υπάρχουν pending orders (synchronous fills)."""
        self._log("CANCEL_ALL", note="no pending orders in paper mode")
        logger.debug("  [PAPER] cancel_all_orders (no-op)")

    # ── v4: VIP support (Promote 2) ───────────────────────────────────────────

    def _get_exchange(self):
        if self._exchange is None:
            klass = getattr(ccxt, self._exchange_id)
            self._exchange = klass({'enableRateLimit': True, 'timeout': 10000})
        return self._exchange

    def get_vip_price(self, coin: str) -> float:
        """Επιστρέφει τρέχουσα τιμή VIP coin σε USDT από ccxt.
        Caching 5s για να μην κάνουμε API call για κάθε κλήση."""
        now = time.time()
        if coin in self._vip_price_cache:
            cached_price, cached_ts = self._vip_price_cache[coin]
            if now - cached_ts < self._vip_price_ttl:
                return cached_price

        symbol = self.vip_symbols.get(coin)
        if not symbol:
            logger.warning(f"  [PAPER] get_vip_price({coin}): no symbol mapping — using 1.0")
            return 1.0

        try:
            ticker = self._get_exchange().fetch_ticker(symbol)
            price  = ticker.get('last') or ticker.get('close') or 0.0
            if price <= 0:
                logger.warning(f"  [PAPER] get_vip_price({coin}) zero from ccxt — using 1.0")
                return 1.0
            self._vip_price_cache[coin] = (price, now)
            return price
        except Exception as e:
            logger.warning(f"  [PAPER] get_vip_price({coin}) ccxt error: {e} — using 1.0")
            return 1.0

    def buy_vip(self, coin: str, usdt_amount: float) -> Tuple[float, float]:
        if usdt_amount > self.usdt:
            logger.warning(f"  [PAPER] buy_vip({coin}, {usdt_amount}) insufficient USDT ({self.usdt:.2f})")
            usdt_amount = max(0, self.usdt)
        price = self.get_vip_price(coin) * (1 + self.slippage_pct / 100.0)
        qty = usdt_amount / price if price > 0 else 0
        self.usdt -= usdt_amount
        self.vip_holdings[coin] = self.vip_holdings.get(coin, 0.0) + qty
        self._log("BUY_VIP", quantity=qty, price=price, usdt_value=usdt_amount,
                  symbol=coin,
                  note=f"total holdings {coin}: {self.vip_holdings[coin]:.8f}")
        logger.info(f"  [PAPER] BUY_VIP {qty:.8f} {coin} @ {price:.4f} = {usdt_amount:.2f} USDT")
        return (qty, price)

    def borrow_usdt_vip(self, amount: float) -> float:
        self.usdt         += amount
        self.vip_debt_usdt += amount
        self._log("BORROW_VIP_USDT", usdt_value=amount,
                  note=f"vip_debt total: {self.vip_debt_usdt:.2f}")
        logger.info(f"  [PAPER] borrow_usdt_vip {amount:.2f}")
        return amount

    def repay_usdt_vip(self, amount: float) -> None:
        actual = min(amount, self.usdt, self.vip_debt_usdt)
        self.usdt          -= actual
        self.vip_debt_usdt  = max(0.0, self.vip_debt_usdt - actual)
        self._log("REPAY_VIP_USDT", usdt_value=actual,
                  note=f"vip_debt remaining: {self.vip_debt_usdt:.2f}")
        logger.info(f"  [PAPER] repay_usdt_vip {actual:.2f}")

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, price: float) -> dict:
        vip_market = sum(qty * self.get_vip_price(c) for c, qty in self.vip_holdings.items() if qty > 0)
        total_assets = (self.base_coin * price) + self.usdt + vip_market
        total_debt   = self.usdt_debt + (self.base_debt * price) + self.vip_debt_usdt
        ratio        = total_assets / total_debt if total_debt > 0 else 0
        return {
            "base_coin":     round(self.base_coin, 4),
            "usdt":          round(self.usdt, 2),
            "usdt_debt":     round(self.usdt_debt, 2),
            "base_debt":     round(self.base_debt, 4),
            "vip_holdings":  dict(self.vip_holdings),
            "vip_debt_usdt": round(self.vip_debt_usdt, 2),
            "vip_market":    round(vip_market, 2),
            "total_assets":  round(total_assets, 2),
            "total_debt":    round(total_debt, 2),
            "margin_ratio":  round(ratio, 4),
        }

    def get_balance_dict(self) -> dict:
        """Raw (μη-στρογγυλοποιημένα) balance fields για state persistence.
        Πρέπει να περιλαμβάνει ΟΛΑ τα mutable balance fields του executor."""
        return {
            "base_coin":     self.base_coin,
            "usdt":          self.usdt,
            "usdt_debt":     self.usdt_debt,
            "base_debt":     self.base_debt,
            "vip_holdings":  dict(self.vip_holdings),
            "vip_debt_usdt": self.vip_debt_usdt,
        }

    def restore_balances(self, d: dict) -> None:
        """Εφαρμόζει saved balances πάνω στον executor (resume from state)."""
        if not isinstance(d, dict):
            return
        if "base_coin"     in d: self.base_coin     = float(d["base_coin"])
        if "usdt"          in d: self.usdt          = float(d["usdt"])
        if "usdt_debt"     in d: self.usdt_debt     = float(d["usdt_debt"])
        if "base_debt"     in d: self.base_debt     = float(d["base_debt"])
        if "vip_holdings"  in d: self.vip_holdings  = dict(d["vip_holdings"] or {})
        if "vip_debt_usdt" in d: self.vip_debt_usdt = float(d["vip_debt_usdt"])
        logger.info(f"[PaperExecutor] Restored balances | base={self.base_coin} | "
                    f"usdt={self.usdt} | debt={self.usdt_debt} | borrow={self.base_debt}")

    def close(self) -> None:
        if self._db:
            self._db.close()
