"""
api/kucoin_client.py — KuCoin margin API wrapper (v4 Φάση 3γ).

Encapsulates all KuCoin margin endpoints χρησιμοποιώντας ccxt.

CROSS MARGIN ΜΟΝΟ — per v4 §11 (Promote 2 συμβατότητα).

Operations:
  - fetch_ticker(symbol)                   → last price
  - fetch_balance_margin()                 → current margin account state
  - borrow(currency, amount)               → cross margin borrow
  - repay(currency, amount)                → cross margin repay
  - place_market_order(symbol, side, amt)  → market order (συγχρονισμός fill)
  - fetch_order(order_id, symbol)          → order status
  - cancel_all_orders(symbol)              → cancel open orders
  - get_fill_details(order_id, symbol)     → actual executed qty/price

ΟΛΕΣ οι write operations κάνουν blocking wait for fill (market orders) και
επιστρέφουν τα ΠΡΑΓΜΑΤΙΚΑ executed values, ΟΧΙ τα requested.

Για errors, raise ccxt exceptions — ο caller χειρίζεται halt/retry logic.
"""

import time
import logging
from typing import Optional, Tuple, Dict, Any

import ccxt

logger = logging.getLogger(__name__)


class KuCoinMarginClient:
    """KuCoin cross-margin client με ccxt."""

    MARGIN_TRADE_TYPE = "MARGIN_TRADE"  # cross margin per KuCoin docs
    ORDER_POLL_INTERVAL = 0.5            # seconds
    ORDER_POLL_TIMEOUT  = 30.0           # seconds max wait for fill

    def __init__(self,
                 api_key:    str,
                 api_secret: str,
                 api_passphrase: str,
                 sandbox:    bool = False):
        self._exchange = ccxt.kucoin({
            'apiKey':     api_key,
            'secret':     api_secret,
            'password':   api_passphrase,   # KuCoin-specific passphrase
            'enableRateLimit': True,
            'timeout':    15000,
            'options': {
                'defaultType': 'margin',    # cross margin default
            },
        })
        if sandbox:
            self._exchange.set_sandbox_mode(True)
        self._exchange.load_markets()
        logger.info(f"[KuCoinClient] Initialized (sandbox={sandbox})")

    # ── Public / read operations ─────────────────────────────────────────────

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        return self._exchange.fetch_ticker(symbol)

    def fetch_last_price(self, symbol: str) -> float:
        t = self._exchange.fetch_ticker(symbol)
        return float(t.get('last') or t.get('close') or 0.0)

    def fetch_balance_margin(self) -> Dict[str, Any]:
        """Returns cross margin account balance."""
        return self._exchange.fetch_balance({'type': 'margin'})

    # ── Borrow / Repay (cross margin) ────────────────────────────────────────

    def borrow(self, currency: str, amount: float) -> Dict[str, Any]:
        """Cross margin borrow. Returns API response.
        Tries ccxt's borrow_cross_margin() first, falls back σε raw endpoint."""
        logger.info(f"  [KUCOIN] borrow {amount} {currency}")
        try:
            if hasattr(self._exchange, 'borrow_cross_margin'):
                return self._exchange.borrow_cross_margin(currency, amount)
            # Fallback για παλαιότερα ccxt
            return self._exchange.privatePostMarginBorrow({
                'currency':   currency,
                'size':       str(amount),
                'timeInForce': 'FOK',
            })
        except ccxt.InsufficientFunds as e:
            logger.error(f"  [KUCOIN] borrow FAILED insufficient margin collateral: {e}")
            raise
        except Exception as e:
            logger.error(f"  [KUCOIN] borrow FAILED: {e}")
            raise

    def repay(self, currency: str, amount: float) -> Dict[str, Any]:
        """Cross margin repay."""
        logger.info(f"  [KUCOIN] repay {amount} {currency}")
        try:
            if hasattr(self._exchange, 'repay_cross_margin'):
                return self._exchange.repay_cross_margin(currency, amount)
            return self._exchange.privatePostMarginRepay({
                'currency': currency,
                'size':     str(amount),
            })
        except Exception as e:
            logger.error(f"  [KUCOIN] repay FAILED: {e}")
            raise

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_market_order(self, symbol: str, side: str, amount: float) -> Dict[str, Any]:
        """Place market order σε cross margin. Blocks μέχρι fill ή timeout.
        Επιστρέφει το filled order dict με actual filled qty/price."""
        if side not in ("buy", "sell"):
            raise ValueError(f"Invalid side: {side}")

        logger.info(f"  [KUCOIN] market {side} {amount} {symbol}")
        params = {
            'tradeType':  self.MARGIN_TRADE_TYPE,
            'marginMode': 'cross',
        }
        try:
            order = self._exchange.create_order(
                symbol=symbol, type='market', side=side, amount=amount,
                price=None, params=params,
            )
            order_id = order.get('id')
            if not order_id:
                raise RuntimeError(f"No order_id returned: {order}")

            filled = self._wait_for_fill(order_id, symbol)
            return filled
        except ccxt.InsufficientFunds as e:
            logger.error(f"  [KUCOIN] {side} FAILED insufficient funds: {e}")
            raise
        except Exception as e:
            logger.error(f"  [KUCOIN] {side} FAILED: {e}")
            raise

    def _wait_for_fill(self, order_id: str, symbol: str) -> Dict[str, Any]:
        """Poll fetch_order μέχρι closed/canceled ή timeout."""
        deadline = time.time() + self.ORDER_POLL_TIMEOUT
        while time.time() < deadline:
            try:
                order = self._exchange.fetch_order(order_id, symbol)
                status = order.get('status')
                if status in ('closed', 'filled'):
                    logger.info(f"  [KUCOIN] order {order_id} filled: "
                                f"qty={order.get('filled')} @ {order.get('average')}")
                    return order
                if status in ('canceled', 'rejected', 'expired'):
                    raise RuntimeError(f"Order {order_id} {status}: {order}")
            except ccxt.OrderNotFound:
                pass  # may need a moment after creation
            time.sleep(self.ORDER_POLL_INTERVAL)
        raise TimeoutError(f"Order {order_id} didn't fill within {self.ORDER_POLL_TIMEOUT}s")

    def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return self._exchange.fetch_order(order_id, symbol)

    def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        params = {'tradeType': self.MARGIN_TRADE_TYPE} if symbol else {}
        try:
            if symbol:
                self._exchange.cancel_all_orders(symbol=symbol, params=params)
            else:
                self._exchange.cancel_all_orders(params={'tradeType': self.MARGIN_TRADE_TYPE})
            logger.info(f"  [KUCOIN] cancel_all_orders done (symbol={symbol})")
        except Exception as e:
            logger.warning(f"  [KUCOIN] cancel_all_orders warning: {e}")
