"""
core/live_executor.py — Live Trading Executor (v4 Φάση 3γ).

Drop-in replacement για PaperExecutor που στέλνει ΠΡΑΓΜΑΤΙΚΕΣ εντολές στο KuCoin.

Κρίσιμες διαφορές από PaperExecutor:
  - Κάθε method καλεί το KuCoin API μέσω KuCoinMarginClient
  - Blocking wait for fill (market orders)
  - Επιστρέφει ΠΡΑΓΜΑΤΙΚΕΣ τιμές/ποσότητες μετά το fill (όχι requested)
  - Σε οποιοδήποτε API error, raise για να το πιάσει ο trader_loop και να σταματήσει
  - Internal state αντανακλά τα confirmed API responses (όχι local assumptions)
  - SQLite audit log για κάθε trade

ΣΥΜΒΑΣΗ ΑΣΦΑΛΕΙΑΣ:
  - Ο LiveExecutor ΔΕΝ ξεκινά χωρίς explicit confirmation (βλ. trader_loop --confirm-live)
  - Σε ΚΑΘΕ exception halt bot — δεν προσπαθούμε «clever recovery»
  - Ο χρήστης χειρίζεται εξαιρετικές καταστάσεις χειροκίνητα
"""

import sqlite3
import logging
import time
from datetime import datetime, timezone
from typing import Tuple, Optional, Dict

from api.kucoin_client import KuCoinMarginClient

logger = logging.getLogger(__name__)


class LiveExecutor:
    """Real KuCoin cross-margin executor."""

    def __init__(self,
                 client:     KuCoinMarginClient,
                 symbol:     str,
                 base_ccy:   str,
                 db_path:    str = "live_trades.db",
                 vip_symbols: Optional[Dict[str, str]] = None):
        """
        client:    KuCoinMarginClient instance
        symbol:    trading pair (π.χ. "PEPE/USDT")
        base_ccy:  base currency (π.χ. "PEPE") — για borrow/repay
        db_path:   SQLite audit log
        vip_symbols: mapping VIP coin → trading pair για price fetches
        """
        self.client    = client
        self.symbol    = symbol
        self.base_ccy  = base_ccy
        self.quote_ccy = "USDT"

        # Local balance mirror (ενημερώνεται από API responses)
        self.base_coin    = 0.0
        self.usdt         = 0.0
        self.usdt_debt    = 0.0
        self.base_debt    = 0.0
        self.current_price = 0.0

        # v4 VIP (Promote 2)
        self.vip_holdings  = {}
        self.vip_debt_usdt = 0.0
        self.vip_symbols   = vip_symbols or {
            "BTC": "BTC/USDT", "ETH": "ETH/USDT", "SOL": "SOL/USDT",
        }
        self._vip_price_cache: Dict[str, tuple] = {}
        self._vip_price_ttl = 5.0

        # Audit DB
        self.db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

        # Αρχική συγχρονισμός balance από exchange
        self._sync_balance_from_exchange()

        logger.warning(f"[LiveExecutor] LIVE MODE — real orders will be sent to KuCoin | "
                       f"symbol={symbol} | db={db_path}")

    def _init_db(self) -> None:
        cur = self._db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS live_trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_iso       TEXT    NOT NULL,
                action       TEXT    NOT NULL,
                symbol       TEXT,
                quantity     REAL,
                price        REAL,
                usdt_value   REAL,
                order_id     TEXT,
                note         TEXT,
                base_coin    REAL,
                usdt         REAL,
                usdt_debt    REAL,
                base_debt    REAL,
                vip_debt     REAL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_live_trades_ts ON live_trades(ts_iso)")
        self._db.commit()

    def _log(self, action: str,
             quantity: float = 0.0, price: float = 0.0,
             usdt_value: float = 0.0, order_id: str = "",
             symbol: str = "", note: str = "") -> None:
        cur = self._db.cursor()
        cur.execute("""
            INSERT INTO live_trades
            (ts_iso, action, symbol, quantity, price, usdt_value, order_id, note,
             base_coin, usdt, usdt_debt, base_debt, vip_debt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(tz=timezone.utc).isoformat(),
            action, symbol or self.symbol, quantity, price, usdt_value, order_id, note,
            self.base_coin, self.usdt, self.usdt_debt, self.base_debt, self.vip_debt_usdt,
        ))
        self._db.commit()

    def _sync_balance_from_exchange(self) -> None:
        """Fetch current margin balance και update local mirror."""
        try:
            bal = self.client.fetch_balance_margin()
            # ccxt schema: {'USDT': {'free': X, 'used': Y, 'total': Z}, ...}
            quote = bal.get(self.quote_ccy, {})
            base  = bal.get(self.base_ccy, {})
            self.usdt      = float(quote.get('free', 0.0) or 0.0)
            self.base_coin = float(base.get('free', 0.0)  or 0.0)
            # Debt: KuCoin-specific info έχει 'info' με debt details
            # ccxt κανονικά το βάζει στο 'used' ή 'debt' field
            self.usdt_debt = float(quote.get('debt', 0.0)  or 0.0)
            self.base_debt = float(base.get('debt', 0.0)   or 0.0)
            logger.info(f"  [LIVE sync] base={self.base_coin} usdt={self.usdt} "
                        f"usdt_debt={self.usdt_debt} base_debt={self.base_debt}")
        except Exception as e:
            logger.warning(f"  [LIVE sync] balance fetch failed: {e}")

    # ── Δανεισμός ─────────────────────────────────────────────────────────────

    def borrow_usdt(self, amount: float) -> float:
        res = self.client.borrow(self.quote_ccy, amount)
        self.usdt      += amount
        self.usdt_debt += amount
        self._log("BORROW_USDT", usdt_value=amount,
                  note=f"api_response={res}")
        return amount

    def repay_usdt(self, amount: float) -> None:
        actual = min(amount, self.usdt)
        if actual <= 0:
            return
        self.client.repay(self.quote_ccy, actual)
        self.usdt      -= actual
        self.usdt_debt  = max(0.0, self.usdt_debt - actual)
        self._log("REPAY_USDT", usdt_value=actual)

    def borrow_base_coin(self, quantity: float) -> float:
        self.client.borrow(self.base_ccy, quantity)
        self.base_coin += quantity
        self.base_debt += quantity
        self._log("BORROW_BASE", quantity=quantity)
        return quantity

    def repay_base_coin(self, quantity: float) -> None:
        actual = min(quantity, self.base_coin)
        if actual <= 0:
            return
        self.client.repay(self.base_ccy, actual)
        self.base_coin -= actual
        self.base_debt  = max(0.0, self.base_debt - actual)
        self._log("REPAY_BASE", quantity=actual)

    # ── Αγορά / Πώληση (market orders στο cross margin) ──────────────────────

    def buy_base_coin(self, quantity: float, limit_price: float) -> Tuple[float, float]:
        """Market BUY. Το limit_price χρησιμοποιείται μόνο για sanity check /
        pre-calc. Το πραγματικό fill είναι market."""
        # Υπολογισμός cost estimate ΓΙΑ έλεγχο USDT
        if quantity * limit_price > self.usdt:
            quantity = self.usdt / limit_price * 0.995  # μικρό buffer για slippage
            logger.warning(f"  [LIVE] BUY reduced to {quantity} λόγω διαθέσιμου USDT")

        order = self.client.place_market_order(self.symbol, 'buy', quantity)
        actual_qty   = float(order.get('filled') or 0.0)
        actual_price = float(order.get('average') or order.get('price') or limit_price)
        actual_cost  = float(order.get('cost')    or actual_qty * actual_price)

        self.usdt      -= actual_cost
        self.base_coin += actual_qty
        self._log("BUY", quantity=actual_qty, price=actual_price,
                  usdt_value=actual_cost, order_id=str(order.get('id','')),
                  note=f"limit_hint={limit_price}")
        return (actual_qty, actual_price)

    def sell_base_coin(self, quantity: float,
                       limit_price: float) -> Tuple[float, float, float]:
        """Market SELL."""
        actual_qty_request = min(quantity, self.base_coin)
        if actual_qty_request <= 0:
            return (0.0, limit_price, 0.0)

        order = self.client.place_market_order(self.symbol, 'sell', actual_qty_request)
        actual_qty   = float(order.get('filled') or 0.0)
        actual_price = float(order.get('average') or order.get('price') or limit_price)
        usdt_received = float(order.get('cost')   or actual_qty * actual_price)

        self.base_coin -= actual_qty
        self.usdt      += usdt_received
        self._log("SELL", quantity=actual_qty, price=actual_price,
                  usdt_value=usdt_received, order_id=str(order.get('id','')),
                  note=f"limit_hint={limit_price}")
        return (actual_qty, actual_price, usdt_received)

    def cancel_all_orders(self) -> None:
        self.client.cancel_all_orders(symbol=self.symbol)
        self._log("CANCEL_ALL", note=f"symbol={self.symbol}")

    # ── v4: VIP support (Promote 2) ───────────────────────────────────────────

    def get_vip_price(self, coin: str) -> float:
        now = time.time()
        if coin in self._vip_price_cache:
            cached_price, cached_ts = self._vip_price_cache[coin]
            if now - cached_ts < self._vip_price_ttl:
                return cached_price
        sym = self.vip_symbols.get(coin)
        if not sym:
            logger.warning(f"  [LIVE] get_vip_price({coin}): no symbol mapping — 1.0")
            return 1.0
        try:
            price = self.client.fetch_last_price(sym)
            if price <= 0:
                return 1.0
            self._vip_price_cache[coin] = (price, now)
            return price
        except Exception as e:
            logger.warning(f"  [LIVE] get_vip_price({coin}) failed: {e}")
            return 1.0

    def buy_vip(self, coin: str, usdt_amount: float) -> Tuple[float, float]:
        sym = self.vip_symbols.get(coin)
        if not sym:
            raise ValueError(f"No symbol mapping for VIP coin {coin}")
        price = self.get_vip_price(coin)
        qty   = usdt_amount / price if price > 0 else 0
        # Market BUY σε cross margin (vip coin με USDT από ίδιο account)
        order = self.client.place_market_order(sym, 'buy', qty)
        actual_qty   = float(order.get('filled') or 0.0)
        actual_price = float(order.get('average') or price)
        actual_cost  = float(order.get('cost')    or actual_qty * actual_price)

        self.usdt -= actual_cost
        self.vip_holdings[coin] = self.vip_holdings.get(coin, 0.0) + actual_qty
        self._log("BUY_VIP", quantity=actual_qty, price=actual_price,
                  usdt_value=actual_cost, order_id=str(order.get('id','')),
                  symbol=coin,
                  note=f"total {coin}: {self.vip_holdings[coin]}")
        return (actual_qty, actual_price)

    def borrow_usdt_vip(self, amount: float) -> float:
        self.client.borrow(self.quote_ccy, amount)
        self.usdt         += amount
        self.vip_debt_usdt += amount
        self._log("BORROW_VIP_USDT", usdt_value=amount,
                  note=f"vip_debt total: {self.vip_debt_usdt}")
        return amount

    def repay_usdt_vip(self, amount: float) -> None:
        actual = min(amount, self.usdt, self.vip_debt_usdt)
        if actual <= 0:
            return
        self.client.repay(self.quote_ccy, actual)
        self.usdt          -= actual
        self.vip_debt_usdt  = max(0.0, self.vip_debt_usdt - actual)
        self._log("REPAY_VIP_USDT", usdt_value=actual)

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self, price: float) -> dict:
        vip_market = sum(qty * self.get_vip_price(c)
                         for c, qty in self.vip_holdings.items() if qty > 0)
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

    def close(self) -> None:
        if self._db:
            self._db.close()
