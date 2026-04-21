"""
Arbitrading Bot - Strategy v1 (NEW RULES)

NEW CYCLE LOGIC:
  SELL trigger (price UP)  → REPAY borrow with owned SOL (USDT unchanged)
  BUY  trigger (price DOWN) → Buy SOL with USDT + Repay borrow (has_bought=True)
  Cycle closes ONLY when: has_bought=True AND SELL trigger fires

NEW SELL formula (repay):
  qty = total_base_coin × (trailing - reference) / reference
  → Use owned SOL to repay that qty of borrow
  → BASE_COIN value = BORROW value (balance maintained)

CLOSING SELL:
  1. Repay ALL borrow with owned SOL (buy more if needed)
  2. Convert ALL USDT to SOL at trailing price
  3. New START = total SOL → SETUP restart
"""

from enum import Enum
from dataclasses import dataclass
from typing import List
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class BotState(Enum):
    IDLE           = "idle"
    SETUP          = "setup"
    MONITORING     = "monitoring"
    CLOSING_SELL   = "closing_sell"
    MARGIN_PROTECT = "margin_protect"


@dataclass
class BotConfig:
    trading_pair:            str   = "SOL/USDT"
    profit_coin:             str   = "USDT"
    account_type:            str   = "margin"
    start_base_coin:         float = 10.0
    scale_base_coin:         float = 4.0
    ratio_scale:             float = 0.005
    borrow_base_coin_factor: float = 1.0
    min_profit_percent:      float = 5.0
    step_point:              float = 0.5
    trailing_stop:           float = 2.0
    limit_order:             float = 0.5
    margin_level:            float = 1.07


@dataclass
class BotMemory:
    sel_price_base_coin:     float = 0.0
    buy_price:               float = 0.0
    sel_price:               float = 0.0
    reference_price:         float = 0.0
    total_base_coin:         float = 0.0
    borrow_base_coin:        float = 0.0
    available_usdt:          float = 0.0
    usdt_debt:               float = 0.0
    # NEW: has_bought replaces has_sold
    # True after first BUY — next SELL trigger = CLOSING SELL
    has_bought:              bool  = False
    cycle_count:             int   = 0
    current_timestamp:       object = None  # τρέχον candle timestamp
    cumulative_profit_sol:   float = 0.0
    last_cycle_price:        float = 0.0
    last_cycle_sol:          float = 0.0
    buy_activated:           bool  = False
    buy_lowest_activation:   float = 0.0
    buy_trailing_stop:       float = 0.0
    sell_activated:          bool  = False
    sell_highest_activation: float = 0.0
    sell_trailing_stop:      float = 0.0


@dataclass
class TradeRecord:
    timestamp:        datetime
    action:           str
    price:            float
    quantity:         float
    usdt_value:       float
    borrow_remaining: float
    state:            str
    notes:            str = ""


class ArbitradingV1:

    def __init__(self, config: BotConfig, executor):
        self.config    = config
        self.executor  = executor
        self.state     = BotState.IDLE
        self.memory    = BotMemory()
        self.trade_log: List[TradeRecord] = []

    def on_price_update(self, price: float, timestamp: datetime) -> None:
        # Αποθηκεύουμε το τρέχον candle timestamp για χρήση στα logs
        self.memory.current_timestamp = timestamp

        if self.state == BotState.MONITORING:
            if self._check_margin_level(price):
                return
        if self.state == BotState.IDLE:
            pass
        elif self.state == BotState.SETUP:
            self._execute_setup(price, timestamp)
        elif self.state == BotState.MONITORING:
            self._monitor(price, timestamp)
        elif self.state == BotState.CLOSING_SELL:
            self._execute_closing_sell(price, timestamp)
        elif self.state == BotState.MARGIN_PROTECT:
            self._execute_margin_protect(price, timestamp)

    def start(self, current_price: float, timestamp: datetime) -> None:
        logger.info(f"[{timestamp}] === Bot START === τιμή: {current_price}")
        self.state = BotState.SETUP
        self.on_price_update(current_price, timestamp)

    def update_immediate_settings(self, min_profit=None, step_point=None,
                                   trailing_stop=None, limit_order=None):
        if min_profit    is not None: self.config.min_profit_percent = min_profit
        if step_point    is not None: self.config.step_point         = step_point
        if trailing_stop is not None: self.config.trailing_stop      = trailing_stop
        if limit_order   is not None: self.config.limit_order        = limit_order

    # =========================================================================
    # SETUP
    # Πρώτη εκκίνηση: LONG (borrow USDT→buy SOL) + SHORT (borrow SOL→sell)
    # Επανεκκίνηση:   Μόνο SHORT (LONG+USDT debt αμετάβλητα)
    # =========================================================================

    def _execute_setup(self, price: float, timestamp: datetime) -> None:
        cfg = self.config
        m   = self.memory
        logger.info(f"[{timestamp}] === SETUP === τιμή: {price}")

        if m.usdt_debt == 0:
            # ΠΡΩΤΗ ΕΝΑΡΞΗ: Βήματα 1-6 (LONG + SHORT)
            logger.info(f"  Πρώτη εκκίνηση | START = {cfg.start_base_coin} SOL")
            buy_qty = cfg.scale_base_coin * cfg.start_base_coin
            logger.info(f"  Βήμα 1 | BUY = {cfg.scale_base_coin} x {cfg.start_base_coin} = {buy_qty:.4f} SOL")
            borrow_usdt = buy_qty * price * (1 + cfg.ratio_scale)
            logger.info(f"  Βήμα 2 | BORROW_USDT = {borrow_usdt:.2f} USDT")
            actual_borrowed_usdt = self.executor.borrow_usdt(borrow_usdt)
            m.usdt_debt = actual_borrowed_usdt
            actual_qty, actual_price = self.executor.buy_base_coin(buy_qty, price)
            cost = actual_qty * actual_price
            logger.info(f"  Βήμα 3 | Αγορά {actual_qty:.4f} SOL @ {actual_price:.4f} | κόστος: {cost:.2f} USDT")
            excess_usdt = actual_borrowed_usdt - cost
            if excess_usdt > 0:
                self.executor.repay_usdt(excess_usdt)
                m.usdt_debt -= excess_usdt
                logger.info(f"  Βήμα 3 | Repay excess USDT: {excess_usdt:.2f} | debt: {m.usdt_debt:.2f}")
            m.total_base_coin = cfg.start_base_coin + actual_qty
            logger.info(f"  Βήμα 4 | TOTAL = {cfg.start_base_coin} + {actual_qty:.4f} = {m.total_base_coin:.4f}")
        else:
            # ΕΠΑΝΕΚΚΙΝΗΣΗ (Κανόνας 1): Μόνο SHORT
            logger.info(f"  Κανόνας 1 | LONG αμετάβλητο | USDT debt: {m.usdt_debt:.2f}")
            logger.info(f"  Κανόνας 1 | TOTAL_BASE_COIN = {m.total_base_coin:.4f} SOL")

        # Βήμα 5: Δανεισμός TOTAL_BASE_COIN
        borrow_qty = m.total_base_coin * cfg.borrow_base_coin_factor
        actual_borrowed_base = self.executor.borrow_base_coin(borrow_qty)
        m.borrow_base_coin = actual_borrowed_base
        logger.info(f"  Βήμα 5 | BORROW = {actual_borrowed_base:.4f} SOL")

        # Βήμα 6: Πώληση δανεισμένου BASE_COIN
        sell_qty, sell_price, usdt_received = self.executor.sell_base_coin(actual_borrowed_base, price)
        m.available_usdt      = usdt_received
        m.sel_price_base_coin = sell_price
        m.reference_price     = sell_price
        logger.info(f"  Βήμα 6 | Πώληση {sell_qty:.4f} SOL @ {sell_price:.4f} -> {usdt_received:.2f} USDT")
        logger.info(f"  REFERENCE = {m.reference_price:.4f}")

        logger.info("  === ΔΟΜΗ ===")
        logger.info(f"  ASSET:  {m.total_base_coin:.4f} SOL  ({m.total_base_coin * price:.2f} USDT)")
        logger.info(f"  CASH:   {m.available_usdt:.2f} USDT")
        logger.info(f"  BORROW: {m.borrow_base_coin:.4f} SOL")
        logger.info(f"  DEBT:   {m.usdt_debt:.2f} USDT")
        exp_buy  = round(m.reference_price * (1 - cfg.min_profit_percent / 100), 4)
        exp_sell = round(m.reference_price * (1 + cfg.min_profit_percent / 100), 4)
        logger.info(f"  BUY activation: {exp_buy} | SELL activation: {exp_sell}")

        m.has_bought = False
        self._reset_buy_tracker()
        self._reset_sell_tracker()
        self._log_trade(timestamp, "SETUP_SHORT", sell_price, sell_qty,
                        usdt_received, m.borrow_base_coin, "SETUP")
        self.state = BotState.MONITORING

    # =========================================================================
    # MONITORING
    # =========================================================================

    def _monitor(self, price: float, timestamp: datetime) -> None:
        buy_triggered  = self._update_buy_tracker(price)
        sell_triggered = self._update_sell_tracker(price)

        if sell_triggered:
            # Με tick data: price = πραγματική τιμή trigger (ακριβής)
            if self.memory.has_bought:
                # BUY έχει γίνει → CLOSING SELL (κλείσιμο κύκλου)
                self.state = BotState.CLOSING_SELL
                self._execute_closing_sell(price, timestamp)
            else:
                # Δεν έχει γίνει BUY → REPAY operation (ΟΧΙ κλείσιμο κύκλου)
                self._execute_repay_sell(price, timestamp)
        elif buy_triggered:
            self._execute_buy(price, timestamp)

    # =========================================================================
    # BUY TRACKER (τιμή πάει ΚΑΤΩ)
    # activation = πραγματική χαμηλότερη τιμή
    # STEP_POINT = ελάχιστη μεταβολή για να μετακινηθεί το activation
    # =========================================================================

    def _reset_buy_tracker(self) -> None:
        m = self.memory
        m.buy_activated         = False
        m.buy_lowest_activation = m.reference_price * (1 - self.config.min_profit_percent / 100)
        m.buy_trailing_stop     = m.buy_lowest_activation * (1 + self.config.trailing_stop / 100)

    def _update_buy_tracker(self, price: float) -> bool:
        m   = self.memory
        cfg = self.config
        initial_activation = m.reference_price * (1 - cfg.min_profit_percent / 100)

        if not m.buy_activated:
            if price <= initial_activation:
                m.buy_activated         = True
                m.buy_lowest_activation = price
                m.buy_trailing_stop     = price * (1 + cfg.trailing_stop / 100)
                logger.info(f"  [{m.current_timestamp}] BUY ACTIVATED @ {price:.4f} | threshold: {initial_activation:.4f} | trailing: {m.buy_trailing_stop:.4f}")
        else:
            next_step = m.buy_lowest_activation * (1 - cfg.step_point / 100)
            if price <= next_step:
                m.buy_lowest_activation = price
                m.buy_trailing_stop     = price * (1 + cfg.trailing_stop / 100)
                logger.info(f"  [{m.current_timestamp}] BUY STEP_POINT ↓ @ {price:.4f} | trailing: {m.buy_trailing_stop:.4f}")
            elif price >= m.buy_trailing_stop:
                logger.info(f"  [{m.current_timestamp}] BUY TRIGGER @ {price:.4f} | activation: {m.buy_lowest_activation:.4f} | trailing: {m.buy_trailing_stop:.4f}")
                m.buy_activated = False
                return True
        return False

    # =========================================================================
    # SELL TRACKER (τιμή πάει ΠΑΝΩ)
    # activation = πραγματική υψηλότερη τιμή
    # =========================================================================

    def _reset_sell_tracker(self) -> None:
        m = self.memory
        m.sell_activated           = False
        m.sell_highest_activation  = m.reference_price * (1 + self.config.min_profit_percent / 100)
        m.sell_trailing_stop       = m.sell_highest_activation * (1 - self.config.trailing_stop / 100)

    def _update_sell_tracker(self, price: float) -> bool:
        m   = self.memory
        cfg = self.config
        initial_activation = m.reference_price * (1 + cfg.min_profit_percent / 100)

        if not m.sell_activated:
            if price >= initial_activation:
                m.sell_activated          = True
                m.sell_highest_activation = price
                m.sell_trailing_stop      = price * (1 - cfg.trailing_stop / 100)
                logger.info(f"  [{m.current_timestamp}] SELL ACTIVATED @ {price:.4f} | threshold: {initial_activation:.4f} | trailing: {m.sell_trailing_stop:.4f}")
        else:
            next_step = m.sell_highest_activation * (1 + cfg.step_point / 100)
            if price >= next_step:
                m.sell_highest_activation = price
                m.sell_trailing_stop      = price * (1 - cfg.trailing_stop / 100)
                logger.info(f"  [{m.current_timestamp}] SELL STEP_POINT ↑ @ {price:.4f} | trailing: {m.sell_trailing_stop:.4f}")
            elif price <= m.sell_trailing_stop:
                logger.info(f"  [{m.current_timestamp}] SELL TRIGGER @ {price:.4f} | activation: {m.sell_highest_activation:.4f} | trailing: {m.sell_trailing_stop:.4f}")
                m.sell_activated = False
                return True
        return False

    # =========================================================================
    # EXECUTE BUY (τιμή πάει ΚΑΤΩ)
    # Αγορά SOL με USDT + Repay borrow
    # REFERENCE = trailing (per Master doc)
    # =========================================================================

    def _execute_buy(self, price: float, timestamp: datetime) -> None:
        m   = self.memory
        cfg = self.config
        # price = πραγματική τιμή BUY_TRIGGER (ΟΧΙ trailing/limit)
        pct_buy     = (m.reference_price - price) / m.reference_price
        usdt_spent  = m.available_usdt * pct_buy

        if usdt_spent <= 0 or m.available_usdt <= 0:
            return

        buy_qty = usdt_spent / price
        actual_qty, actual_price = self.executor.buy_base_coin(buy_qty, price)
        actual_cost = actual_qty * actual_price

        logger.info(f"  BUY | {actual_qty:.4f} SOL @ {actual_price:.4f} | USDT: {actual_cost:.2f} ({pct_buy*100:.2f}%)")

        # Repay borrow με αγορασμένα SOL
        self.executor.repay_base_coin(actual_qty)
        m.borrow_base_coin -= actual_qty
        m.available_usdt   -= actual_cost

        # REFERENCE = BUY_TRIGGER (πραγματική τιμή)
        m.buy_price       = price
        m.reference_price = price
        m.has_bought      = True  # ← κύκλος μπορεί πλέον να κλείσει

        self._reset_buy_tracker()
        self._reset_sell_tracker()

        logger.info(f"  BORROW: {m.borrow_base_coin:.4f} SOL | USDT: {m.available_usdt:.2f} | REF: {m.reference_price:.4f}")
        logger.info(f"  ASSET value: {m.total_base_coin * price:.2f} USDT | BORROW value: {m.borrow_base_coin * price:.2f} USDT")
        self._log_trade(timestamp, "BUY", price, actual_qty,
                        actual_cost, m.borrow_base_coin, "MONITORING")

    # =========================================================================
    # EXECUTE REPAY SELL (τιμή πάει ΠΑΝΩ, has_bought=False)
    # ΔΕΝ πωλούμε SOL — κάνουμε Repay borrow με owned SOL
    # CASH αμετάβλητο, BASE_COIN value = BORROW value (ισορροπία)
    # =========================================================================

    def _execute_repay_sell(self, price: float, timestamp: datetime) -> None:
        m   = self.memory
        cfg = self.config
        # price = πραγματική τιμή SEL_TRIGGER (ΟΧΙ trailing)
        pct_repay   = (price - m.reference_price) / m.reference_price
        qty_repay   = m.total_base_coin * (price - m.reference_price) / price

        if qty_repay <= 0 or m.total_base_coin <= 0:
            return

        # Repay borrow με owned SOL (ΔΕΝ πωλούμε — USDT αμετάβλητο)
        actual_repay = min(qty_repay, m.borrow_base_coin)
        self.executor.repay_base_coin(actual_repay)
        m.total_base_coin  -= actual_repay
        m.borrow_base_coin -= actual_repay

        # REFERENCE = SEL_TRIGGER (πραγματική τιμή)
        m.sel_price       = price
        m.reference_price = price
        # has_bought ΠΑΡΑΜΕΝΕΙ False — ΔΕΝ κλείνει κύκλος

        self._reset_buy_tracker()
        self._reset_sell_tracker()

        logger.info(f"  REPAY SELL | {actual_repay:.4f} SOL repaid @ {price:.4f} | pct: {pct_repay*100:.2f}%")
        logger.info(f"  ASSET: {m.total_base_coin:.4f} SOL ({m.total_base_coin*price:.2f} USDT) | BORROW: {m.borrow_base_coin:.4f} SOL ({m.borrow_base_coin*price:.2f} USDT)")
        logger.info(f"  CASH: {m.available_usdt:.2f} USDT (αμετάβλητο) | REF: {m.reference_price:.4f}")
        self._log_trade(timestamp, "REPAY_SELL", price, actual_repay,
                        actual_repay * price, m.borrow_base_coin, "MONITORING",
                        notes=f"Repay {actual_repay:.4f} SOL borrow (USDT unchanged)")

    # =========================================================================
    # EXECUTE CLOSING SELL (SELL trigger + has_bought=True)
    # 1. Repay ΟΛΟ το BORROW με owned SOL
    # 2. Convert ΟΛΟ το USDT → SOL
    # 3. Νέο START = total SOL → SETUP
    # =========================================================================

    def _execute_closing_sell(self, price: float, timestamp: datetime) -> None:
        m = self.memory
        # price = πραγματική τιμή SELL_TRIGGER (ΟΧΙ trailing)
        logger.info(f"[{price}] === CLOSING SELL === price: {price:.4f}")

        # Βήμα 1: Repay ΟΛΟ το BORROW με owned SOL
        if m.total_base_coin >= m.borrow_base_coin:
            # Αρκούν τα owned SOL
            repay_qty = m.borrow_base_coin
            self.executor.repay_base_coin(repay_qty)
            m.total_base_coin  -= repay_qty
            m.borrow_base_coin  = 0.0
            logger.info(f"  Repay {repay_qty:.4f} SOL borrow (από owned SOL)")
        else:
            # Δεν αρκούν — αγορά επιπλέον SOL με USDT
            shortfall    = m.borrow_base_coin - m.total_base_coin
            extra_qty, extra_price = self.executor.buy_base_coin(shortfall, price)
            m.available_usdt  -= extra_qty * extra_price
            logger.info(f"  Αγορά {extra_qty:.4f} SOL επιπλέον για repay (κόστος {extra_qty*extra_price:.2f} USDT)")
            # Repay ΟΛΟ
            total_repay = m.total_base_coin + extra_qty
            self.executor.repay_base_coin(total_repay)
            m.total_base_coin  = 0.0
            m.borrow_base_coin = 0.0

        # Βήμα 2: Convert USDT → SOL
        if m.available_usdt > 0:
            buy_qty      = m.available_usdt / price
            actual_qty, actual_price = self.executor.buy_base_coin(buy_qty, price)
            actual_cost  = actual_qty * actual_price
            m.total_base_coin += actual_qty
            m.available_usdt  -= actual_cost
            logger.info(f"  Convert USDT: {actual_cost:.2f} → {actual_qty:.4f} SOL @ {price:.4f}")

        # Βήμα 3: Στατιστικά κύκλου
        m.cycle_count          += 1
        m.last_cycle_price      = price
        m.last_cycle_sol        = m.total_base_coin
        m.cumulative_profit_sol  = m.total_base_coin  # αθροιστικά SOL

        logger.info(f"  ╔══ ΚΥΚΛΟΣ #{m.cycle_count} ΟΛΟΚΛΗΡΩΘΗΚΕ ══════════════════╗")
        logger.info(f"  ║  Νέο TOTAL SOL: {m.total_base_coin:.4f}")
        logger.info(f"  ║  Τιμή κλεισίματος: {price:.4f} USDT")
        logger.info(f"  ║  Αξία assets: {m.total_base_coin * price:.2f} USDT")
        logger.info(f"  ╚══════════════════════════════════════════════╝")

        self._log_trade(timestamp, "CLOSING_SELL", price, m.total_base_coin,
                        m.total_base_coin * price, 0.0, "CLOSING_SELL",
                        notes=f"Κύκλος #{m.cycle_count} | Start={m.total_base_coin:.4f} SOL")

        # SETUP ξεκινά ΑΜΕΣΑ στην πραγματική τιμή
        logger.info(f"  Επανεκκίνηση SETUP @ τιμή: {price:.4f}")
        self.state = BotState.SETUP
        self._execute_setup(price, timestamp)

    # =========================================================================
    # MARGIN PROTECT
    # =========================================================================

    def _check_margin_level(self, price: float) -> bool:
        m   = self.memory
        cfg = self.config
        total_assets = (m.total_base_coin * price) + m.available_usdt
        total_debt   = m.usdt_debt + (m.borrow_base_coin * price)
        if total_debt <= 0:
            return False
        ratio = total_assets / total_debt
        if ratio <= cfg.margin_level:
            logger.warning(f"  MARGIN PROTECT! ratio={ratio:.4f} <= {cfg.margin_level}")
            self.state = BotState.MARGIN_PROTECT
            return True
        return False

    def _execute_margin_protect(self, price: float, timestamp: datetime) -> None:
        m = self.memory
        logger.warning(f"[{timestamp}] === MARGIN PROTECT ===")
        self.executor.cancel_all_orders()
        repay_qty = min(m.total_base_coin, m.borrow_base_coin)
        self.executor.repay_base_coin(repay_qty)
        m.total_base_coin  -= repay_qty
        m.borrow_base_coin -= repay_qty
        self._log_trade(timestamp, "MARGIN_PROTECT", price, repay_qty,
                        repay_qty * price, m.borrow_base_coin, "MARGIN_PROTECT")
        self.state = BotState.SETUP

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _log_trade(self, timestamp, action, price, qty, usdt_val,
                   borrow_rem, state, notes="") -> None:
        self.trade_log.append(TradeRecord(
            timestamp=timestamp, action=action, price=price,
            quantity=qty, usdt_value=usdt_val,
            borrow_remaining=borrow_rem, state=state, notes=notes
        ))

    def get_status(self, current_price: float) -> dict:
        m = self.memory
        total_assets = (m.total_base_coin * current_price) + m.available_usdt
        total_debt   = m.usdt_debt + (m.borrow_base_coin * current_price)
        ratio        = total_assets / total_debt if total_debt > 0 else 0
        return {
            "state":                   self.state.value,
            "cycle_count":             m.cycle_count,
            "reference_price":         round(m.reference_price, 4),
            "buy_price":               round(m.buy_price, 4),
            "sel_price":               round(m.sel_price, 4),
            "total_base_coin":         round(m.total_base_coin, 4),
            "borrow_base_coin":        round(m.borrow_base_coin, 4),
            "available_usdt":          round(m.available_usdt, 2),
            "usdt_debt":               round(m.usdt_debt, 2),
            "margin_ratio":            round(ratio, 4),
            "has_bought":              m.has_bought,
            "buy_activated":           m.buy_activated,
            "sell_activated":          m.sell_activated,
            "buy_trailing_stop":       round(m.buy_trailing_stop, 4),
            "sell_trailing_stop":      round(m.sell_trailing_stop, 4),
            "cumulative_profit_sol":   round(m.cumulative_profit_sol, 4),
            "last_cycle_sol":          round(m.last_cycle_sol, 4),
        }
