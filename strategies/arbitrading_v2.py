"""
Arbitrading Bot - Strategy v2 (per Master_arbitrading-project_v4.md)

ΠΡΟΣΘΕΤΕΙ ΣΤΟ v1:
  - Promote Routes (1=Up_Investment, 2=Buy_Choice_Coins, 3=resset_invest)
  - Second-tier Profit (ON/OFF switch, μετά το 1ο trigger εντός κύκλου)
  - Grand_amount αποθήκευση σε κάθε SETUP
  - LAST_VIP_COIN cumulative purchase cost tracking
  - trigger_count_in_cycle (για SECOND_PROFIT dispatcher)
  - Επέκταση MARGIN_PROTECT τύπου με VIP assets/debts

DEFAULT CONFIG = 100% ΤΑΥΤΟΣΗΜΟ ΜΕ v1:
  promote=1, second_profit_enabled=False, vip_coins=[] → v1 behavior
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import List, Dict
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class BotState(Enum):
    IDLE           = "idle"
    SETUP          = "setup"
    MONITORING     = "monitoring"
    CLOSING_SELL   = "closing_sell"
    MARGIN_PROTECT = "margin_protect"
    USER_RESET     = "user_reset"      # v4: Promote 3 manual trigger
    STOPPED        = "stopped"         # v4: τελική κατάσταση μετά resset_invest


@dataclass
class BotConfig:
    # ── v1 fields ─────────────────────────────────────────────────────────────
    trading_pair:            str   = "SOL/USDT"
    profit_coin:             str   = "USDT"
    account_type:            str   = "margin"
    start_base_coin:         float = 10.0
    scale_base_coin:         float = 4.0
    ratio_scale:             float = 0.005
    borrow_base_coin_factor: float = 1.0
    min_profit_percent:      float = 10.0   # v4: default 10 (was 5 in v1)
    step_point:              float = 0.5
    trailing_stop:           float = 2.0
    limit_order:             float = 0.5
    margin_level:            float = 1.07

    # ── v4: Second-tier Profit ────────────────────────────────────────────────
    second_profit_percent:   float = 4.0
    second_profit_enabled:   bool  = False   # default OFF → v1 συμπεριφορά

    # ── v4: Promote Routes ────────────────────────────────────────────────────
    # 1 = Up_Investment (v1 default)
    # 2 = Buy_Choice_Coins
    # 3 = resset_invest (manual trigger — δεν ρυθμίζεται εδώ)
    promote:                 int   = 1

    # ── v4: VIP Config (Promote 2) ────────────────────────────────────────────
    vip_coins:               List[str]        = field(default_factory=list)
    vip_allocation_mode:     str              = "percent"   # percent | priority
    vip_percentages:         Dict[str, float] = field(default_factory=dict)
    vip_priority_list:       List[str]        = field(default_factory=list)
    scale_vip_coin:          float            = 5.0
    min_order_usdt:          float            = 5.0


@dataclass
class BotMemory:
    # ── v1 fields ─────────────────────────────────────────────────────────────
    sel_price_base_coin:     float = 0.0
    buy_price:               float = 0.0
    sel_price:               float = 0.0
    reference_price:         float = 0.0
    total_base_coin:         float = 0.0
    borrow_base_coin:        float = 0.0
    available_usdt:          float = 0.0
    usdt_debt:               float = 0.0
    has_bought:              bool  = False
    cycle_count:             int   = 0
    current_timestamp:       object = None
    cumulative_profit_sol:   float = 0.0
    last_cycle_price:        float = 0.0
    last_cycle_sol:          float = 0.0
    buy_activated:           bool  = False
    buy_lowest_activation:   float = 0.0
    buy_trailing_stop:       float = 0.0
    sell_activated:          bool  = False
    sell_highest_activation: float = 0.0
    sell_trailing_stop:      float = 0.0

    # ── v4: Second-tier Profit tracking (per-direction counters) ─────────────
    # Κάθε direction έχει δικό του counter. Στον 1ο trigger κάθε direction
    # χρησιμοποιείται ΠΑΝΤΑ MIN_PROFIT_PERCENT. Στους επόμενους triggers της
    # ΙΔΙΑΣ κατεύθυνσης (αν SECOND_PROFIT_ENABLED=ON) χρησιμοποιείται
    # SECOND_PROFIT_PERCENT. Όταν αλλάζει κατεύθυνση (π.χ. fires SELL μετά
    # από BUYs), ο counter της αντίθετης κατεύθυνσης μηδενίζεται ώστε
    # ο επόμενος trigger σε εκείνη την κατεύθυνση να χρησιμοποιήσει ξανά MIN.
    buy_trigger_count:       int   = 0
    sell_trigger_count:      int   = 0

    # ── v6.x: cycle-cumulative counters (UI display only) ────────────────────
    # Δεν μηδενίζονται σε αλλαγή κατεύθυνσης — μηδενίζονται ΜΟΝΟ σε νέο cycle
    # (SETUP), RESSET_INVEST ή MARGIN_PROTECT. Δεν επηρεάζουν τη λογική
    # SECOND_PROFIT — αυτή χρησιμοποιεί τα παλιά per-direction counters παραπάνω.
    buy_count_total:         int   = 0
    sell_count_total:        int   = 0

    # ── v4: Promote 2 state ───────────────────────────────────────────────────
    grand_amount:            float = 0.0         # Αποθηκεύεται σε κάθε SETUP
    last_vip_coin:           float = 0.0         # Cumulative VIP purchase cost
    priority_rotation_ix:    int   = 0           # Δείκτης rotating priority
    vip_holdings:            Dict[str, float] = field(default_factory=dict)
    vip_borrow_usdt:         float = 0.0         # Αθροιστικός VIP-based δανεισμός USDT
    # v6.x: per-coin cumulative purchase cost (για display purchase value στο UI)
    vip_purchase_cost:       Dict[str, float] = field(default_factory=dict)


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


class ArbitradingV2:

    def __init__(self, config: BotConfig, executor):
        self.config    = config
        self.executor  = executor
        self.state     = BotState.IDLE
        self.memory    = BotMemory()
        self.trade_log: List[TradeRecord] = []

    def on_price_update(self, price: float, timestamp: datetime) -> None:
        self.memory.current_timestamp = timestamp

        if self.state == BotState.STOPPED:
            return  # v4: Μετά resset_invest, αγνόηση price updates

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
        logger.info(f"[{timestamp}] === Bot START (v2) === τιμή: {current_price}")
        self.state = BotState.SETUP
        self.on_price_update(current_price, timestamp)

    def update_immediate_settings(self, min_profit=None, step_point=None,
                                   trailing_stop=None, limit_order=None,
                                   second_profit_percent=None,
                                   second_profit_enabled=None):
        """v4: Όλες οι immediate (*) μεταβλητές ενημερώνονται ΑΜΕΣΑ.

        Το SECOND_PROFIT_ENABLED ON/OFF toggle επηρεάζει τον ενεργό tracker
        αμέσως όταν trigger_count_in_cycle >= 1 (βλ. §7.5 του v4 doc)."""
        if min_profit             is not None: self.config.min_profit_percent    = min_profit
        if step_point             is not None: self.config.step_point            = step_point
        if trailing_stop          is not None: self.config.trailing_stop         = trailing_stop
        if limit_order            is not None: self.config.limit_order           = limit_order
        if second_profit_percent  is not None: self.config.second_profit_percent = second_profit_percent
        if second_profit_enabled  is not None: self.config.second_profit_enabled = second_profit_enabled

    # =========================================================================
    # v4 §7.5: Second-tier Profit dispatcher
    # =========================================================================

    def _get_active_profit_pct(self, direction: str) -> float:
        """Επιστρέφει το ενεργό profit % ανά κατεύθυνση (per-direction logic).

        Κανόνας: Ο 1ος trigger ΚΑΘΕ κατεύθυνσης χρησιμοποιεί ΠΑΝΤΑ MIN_PROFIT_PERCENT.
        Subsequent triggers της ΙΔΙΑΣ κατεύθυνσης (αν SECOND_PROFIT_ENABLED=ON)
        χρησιμοποιούν SECOND_PROFIT_PERCENT.

        Σε αλλαγή κατεύθυνσης (π.χ. SELL μετά από BUYs), ο counter της αντίθετης
        κατεύθυνσης μηδενίζεται — επιστροφή σε MIN για τον 1ο trigger της νέας φοράς.

        direction = 'buy' ή 'sell'
        """
        if not self.config.second_profit_enabled:
            return self.config.min_profit_percent
        if direction == 'buy':
            count = self.memory.buy_trigger_count
        elif direction == 'sell':
            count = self.memory.sell_trigger_count
        else:
            raise ValueError(f"Invalid direction: {direction!r}")
        if count >= 1:
            return self.config.second_profit_percent
        return self.config.min_profit_percent

    # =========================================================================
    # SETUP (v4: + grand_amount αποθήκευση, trigger_count reset)
    # =========================================================================

    def _execute_setup(self, price: float, timestamp: datetime) -> None:
        cfg = self.config
        m   = self.memory
        logger.info(f"[{timestamp}] === SETUP === τιμή: {price}")

        if m.usdt_debt == 0:
            # ΠΡΩΤΗ ΕΝΑΡΞΗ: Βήματα 1-6 (LONG + SHORT)
            logger.info(f"  Πρώτη εκκίνηση | START = {cfg.start_base_coin} {cfg.trading_pair.split('/')[0]}")
            buy_qty = cfg.scale_base_coin * cfg.start_base_coin
            logger.info(f"  Βήμα 1 | BUY = {cfg.scale_base_coin} x {cfg.start_base_coin} = {buy_qty:.4f}")
            borrow_usdt = buy_qty * price * (1 + cfg.ratio_scale)
            logger.info(f"  Βήμα 2 | BORROW_USDT = {borrow_usdt:.2f} USDT")
            actual_borrowed_usdt = self.executor.borrow_usdt(borrow_usdt)
            m.usdt_debt = actual_borrowed_usdt
            actual_qty, actual_price = self.executor.buy_base_coin(buy_qty, price)
            cost = actual_qty * actual_price
            logger.info(f"  Βήμα 3 | Αγορά {actual_qty:.4f} @ {actual_price:.6f} | κόστος: {cost:.2f} USDT")
            excess_usdt = actual_borrowed_usdt - cost
            if excess_usdt > 0:
                self.executor.repay_usdt(excess_usdt)
                m.usdt_debt -= excess_usdt
                logger.info(f"  Βήμα 3 | Repay excess USDT: {excess_usdt:.2f} | debt: {m.usdt_debt:.2f}")
            m.total_base_coin = cfg.start_base_coin + actual_qty
            logger.info(f"  Βήμα 4 | TOTAL = {cfg.start_base_coin} + {actual_qty:.4f} = {m.total_base_coin:.4f}")
        else:
            # ΕΠΑΝΕΚΚΙΝΗΣΗ: Δύο sub-cases:
            #  (a) Promote 1 path: ο closing_sell έχει ήδη μετατρέψει USDT→BASE,
            #      άρα TOTAL > 0 και CASH = 0. Δεν χρειάζεται να αγοράσουμε BASE.
            #  (b) Promote 2 path: ο closing_sell πούλησε ΟΛΟ το BASE (Step 2)
            #      και αγόρασε VIP από surplus. TOTAL = 0 αλλά CASH > 0.
            #      Πρέπει να αγοράσουμε BASE με ΟΛΟ το CASH ΠΡΙΝ συνεχίσουμε.
            if m.total_base_coin == 0 and m.available_usdt > 0:
                buy_qty = m.available_usdt / price
                actual_qty, actual_price = self.executor.buy_base_coin(buy_qty, price)
                actual_cost = actual_qty * actual_price
                m.total_base_coin += actual_qty
                m.available_usdt  -= actual_cost
                logger.info(f"  Promote 2 re-entry | BUY {actual_qty:.4f} @ {actual_price:.6f} ({actual_cost:.2f} USDT)")
            logger.info(f"  Επανεκκίνηση | LONG αμετάβλητο | USDT debt: {m.usdt_debt:.2f}")
            logger.info(f"  TOTAL_BASE_COIN = {m.total_base_coin:.4f}")

        # Βήμα 5: Δανεισμός TOTAL_BASE_COIN
        borrow_qty = m.total_base_coin * cfg.borrow_base_coin_factor
        actual_borrowed_base = self.executor.borrow_base_coin(borrow_qty)
        m.borrow_base_coin = actual_borrowed_base
        logger.info(f"  Βήμα 5 | BORROW = {actual_borrowed_base:.4f}")

        # Βήμα 6: Πώληση δανεισμένου BASE_COIN
        sell_qty, sell_price, usdt_received = self.executor.sell_base_coin(actual_borrowed_base, price)
        m.available_usdt      = usdt_received
        m.sel_price_base_coin = sell_price
        m.reference_price     = sell_price
        logger.info(f"  Βήμα 6 | Πώληση {sell_qty:.4f} @ {sell_price:.6f} -> {usdt_received:.2f} USDT")
        logger.info(f"  REFERENCE = {m.reference_price:.6f}")

        # v6.x §6 Βήμα 7: Αποθήκευση Grand_amount (per-promote semantics).
        # Promote 1: ενημερώνεται σε ΚΑΘΕ νέο SETUP (= TOTAL_BASE × REFERENCE)
        #            ώστε το display να δείχνει την τρέχουσα BASE position value.
        # Promote 2: παραμένει στην αρχική αξία (set μόνο στον πρώτο SETUP).
        #            Η αρχική αξία είναι το baseline για το surplus calculation
        #            σε κάθε επόμενο cycle. Έτσι surplus = cycle profit κάθε φορά.
        if m.grand_amount == 0 or cfg.promote != 2:
            m.grand_amount = m.total_base_coin * price
            logger.info(f"  Grand_amount UPDATED = {m.grand_amount:.2f} USDT (= TOTAL_BASE × REFERENCE)")
        else:
            logger.info(f"  Grand_amount KEPT = {m.grand_amount:.2f} USDT (Promote 2 — fixed since first SETUP)")

        logger.info("  === ΔΟΜΗ ===")
        logger.info(f"  ASSET:  {m.total_base_coin:.4f} ({m.total_base_coin * price:.2f} USDT)")
        logger.info(f"  CASH:   {m.available_usdt:.2f} USDT")
        logger.info(f"  BORROW: {m.borrow_base_coin:.4f}")
        logger.info(f"  DEBT:   {m.usdt_debt:.2f} USDT")

        # v4: Reset cycle-scoped state (BOTH per-direction counters)
        m.has_bought              = False
        m.buy_trigger_count       = 0
        m.sell_trigger_count      = 0
        # v6.x: reset cycle-cumulative counters σε νέο cycle
        m.buy_count_total         = 0
        m.sell_count_total        = 0

        self._reset_buy_tracker()
        self._reset_sell_tracker()

        pct_buy  = self._get_active_profit_pct('buy')
        pct_sell = self._get_active_profit_pct('sell')
        exp_buy  = round(m.reference_price * (1 - pct_buy / 100), 6)
        exp_sell = round(m.reference_price * (1 + pct_sell / 100), 6)
        logger.info(f"  BUY activation: {exp_buy} (buy%={pct_buy}) | SELL activation: {exp_sell} (sell%={pct_sell})")

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
            if self.memory.has_bought:
                self.state = BotState.CLOSING_SELL
                self._execute_closing_sell(price, timestamp)
            else:
                self._execute_repay_sell(price, timestamp)
        elif buy_triggered:
            self._execute_buy(price, timestamp)

    # =========================================================================
    # BUY TRACKER (v4: χρήση dispatcher profit%)
    # =========================================================================

    def _reset_buy_tracker(self) -> None:
        m   = self.memory
        pct = self._get_active_profit_pct('buy')
        m.buy_activated         = False
        # v6.x: round στα 10 δεκαδικά (matching UI display) για να αποφεύγεται
        # floating-point precision mismatch όταν ο χρήστης στέλνει την εμφανιζόμενη
        # τιμή στο price injection.
        m.buy_lowest_activation = round(m.reference_price * (1 - pct / 100), 10)
        m.buy_trailing_stop     = round(m.buy_lowest_activation * (1 + self.config.trailing_stop / 100), 10)

    def _update_buy_tracker(self, price: float) -> bool:
        m   = self.memory
        cfg = self.config
        pct = self._get_active_profit_pct('buy')
        # v6.x: round threshold στα 10 δεκαδικά (όσα δείχνει το UI) ώστε όταν
        # ο χρήστης στέλνει την εμφανιζόμενη τιμή να μην αποτυγχάνει η σύγκριση
        # λόγω floating-point precision (π.χ. 92.577*0.90 = 83.31929999999999).
        initial_activation = round(m.reference_price * (1 - pct / 100), 10)

        if not m.buy_activated:
            if price <= initial_activation:
                m.buy_activated         = True
                m.buy_lowest_activation = price
                # v6.x: round trailing στα 10 δεκαδικά (matching UI display)
                m.buy_trailing_stop     = round(price * (1 + cfg.trailing_stop / 100), 10)
                logger.info(f"  [{m.current_timestamp}] BUY ACTIVATED @ {price:.6f} | threshold: {initial_activation:.6f} | trailing: {m.buy_trailing_stop:.6f}")
        else:
            next_step = round(m.buy_lowest_activation * (1 - cfg.step_point / 100), 10)
            if price <= next_step:
                m.buy_lowest_activation = price
                # v6.x: round trailing στα 10 δεκαδικά (matching UI display)
                m.buy_trailing_stop     = round(price * (1 + cfg.trailing_stop / 100), 10)
                logger.info(f"  [{m.current_timestamp}] BUY STEP_POINT ↓ @ {price:.6f} | trailing: {m.buy_trailing_stop:.6f}")
            elif price >= m.buy_trailing_stop:
                logger.info(f"  [{m.current_timestamp}] BUY TRIGGER @ {price:.6f} | activation: {m.buy_lowest_activation:.6f} | trailing: {m.buy_trailing_stop:.6f}")
                m.buy_activated = False
                return True
        return False

    # =========================================================================
    # SELL TRACKER (v4: χρήση dispatcher profit%)
    # =========================================================================

    def _reset_sell_tracker(self) -> None:
        m   = self.memory
        pct = self._get_active_profit_pct('sell')
        m.sell_activated           = False
        # v6.x: round στα 10 δεκαδικά (matching UI display) για να αποφεύγεται
        # floating-point precision mismatch όταν ο χρήστης στέλνει την εμφανιζόμενη
        # τιμή στο price injection.
        m.sell_highest_activation  = round(m.reference_price * (1 + pct / 100), 10)
        m.sell_trailing_stop       = round(m.sell_highest_activation * (1 - self.config.trailing_stop / 100), 10)

    def _update_sell_tracker(self, price: float) -> bool:
        m   = self.memory
        cfg = self.config
        pct = self._get_active_profit_pct('sell')
        # v6.x: round threshold στα 10 δεκαδικά (όσα δείχνει το UI) ώστε όταν
        # ο χρήστης στέλνει την εμφανιζόμενη τιμή να μην αποτυγχάνει η σύγκριση
        # λόγω floating-point precision (π.χ. 92.577*1.10 = 101.83470000000001).
        initial_activation = round(m.reference_price * (1 + pct / 100), 10)

        if not m.sell_activated:
            if price >= initial_activation:
                m.sell_activated          = True
                m.sell_highest_activation = price
                # v6.x: round trailing στα 10 δεκαδικά (matching UI display)
                m.sell_trailing_stop      = round(price * (1 - cfg.trailing_stop / 100), 10)
                logger.info(f"  [{m.current_timestamp}] SELL ACTIVATED @ {price:.6f} | threshold: {initial_activation:.6f} | trailing: {m.sell_trailing_stop:.6f}")
        else:
            next_step = round(m.sell_highest_activation * (1 + cfg.step_point / 100), 10)
            if price >= next_step:
                m.sell_highest_activation = price
                # v6.x: round trailing στα 10 δεκαδικά (matching UI display)
                m.sell_trailing_stop      = round(price * (1 - cfg.trailing_stop / 100), 10)
                logger.info(f"  [{m.current_timestamp}] SELL STEP_POINT ↑ @ {price:.6f} | trailing: {m.sell_trailing_stop:.6f}")
            elif price <= m.sell_trailing_stop:
                logger.info(f"  [{m.current_timestamp}] SELL TRIGGER @ {price:.6f} | activation: {m.sell_highest_activation:.6f} | trailing: {m.sell_trailing_stop:.6f}")
                m.sell_activated = False
                return True
        return False

    # =========================================================================
    # EXECUTE BUY (v4: + trigger_count_in_cycle += 1)
    # =========================================================================

    def _execute_buy(self, price: float, timestamp: datetime) -> None:
        m   = self.memory
        pct_buy     = (m.reference_price - price) / m.reference_price
        usdt_spent  = m.available_usdt * pct_buy

        if usdt_spent <= 0 or m.available_usdt <= 0:
            return

        buy_qty = usdt_spent / price
        actual_qty, actual_price = self.executor.buy_base_coin(buy_qty, price)
        actual_cost = actual_qty * actual_price

        logger.info(f"  BUY | {actual_qty:.4f} @ {actual_price:.6f} | USDT: {actual_cost:.2f} ({pct_buy*100:.2f}%)")

        self.executor.repay_base_coin(actual_qty)
        m.borrow_base_coin -= actual_qty
        m.available_usdt   -= actual_cost

        m.buy_price               = price
        m.reference_price         = price
        m.has_bought              = True
        # v4: Per-direction counter — BUY count +1, SELL count reset
        m.buy_trigger_count  += 1
        m.sell_trigger_count  = 0
        # v6.x: cumulative cycle counter (UI display, δεν μηδενίζεται)
        m.buy_count_total    += 1

        self._reset_buy_tracker()
        self._reset_sell_tracker()

        logger.info(f"  BORROW: {m.borrow_base_coin:.4f} | USDT: {m.available_usdt:.2f} | REF: {m.reference_price:.6f} | buy_cnt: {m.buy_trigger_count} sell_cnt: {m.sell_trigger_count}")
        self._log_trade(timestamp, "BUY", price, actual_qty,
                        actual_cost, m.borrow_base_coin, "MONITORING")

    # =========================================================================
    # EXECUTE REPAY SELL (v4: + trigger_count_in_cycle += 1)
    # =========================================================================

    def _execute_repay_sell(self, price: float, timestamp: datetime) -> None:
        m   = self.memory
        pct_repay   = (price - m.reference_price) / m.reference_price
        qty_repay   = m.total_base_coin * (price - m.reference_price) / price

        if qty_repay <= 0 or m.total_base_coin <= 0:
            return

        actual_repay = min(qty_repay, m.borrow_base_coin)
        self.executor.repay_base_coin(actual_repay)
        m.total_base_coin  -= actual_repay
        m.borrow_base_coin -= actual_repay

        m.sel_price               = price
        m.reference_price         = price
        # v4: Per-direction counter — SELL count +1, BUY count reset
        m.sell_trigger_count += 1
        m.buy_trigger_count   = 0
        # v6.x: cumulative cycle counter (UI display, δεν μηδενίζεται)
        m.sell_count_total   += 1
        # has_bought ΠΑΡΑΜΕΝΕΙ False — ΔΕΝ κλείνει κύκλος

        self._reset_buy_tracker()
        self._reset_sell_tracker()

        logger.info(f"  REPAY SELL | {actual_repay:.4f} repaid @ {price:.6f} | pct: {pct_repay*100:.2f}% | buy_cnt: {m.buy_trigger_count} sell_cnt: {m.sell_trigger_count}")
        logger.info(f"  ASSET: {m.total_base_coin:.4f} ({m.total_base_coin*price:.2f} USDT) | BORROW: {m.borrow_base_coin:.4f} ({m.borrow_base_coin*price:.2f} USDT)")
        logger.info(f"  CASH: {m.available_usdt:.2f} USDT (αμετάβλητο) | REF: {m.reference_price:.6f}")
        self._log_trade(timestamp, "REPAY_SELL", price, actual_repay,
                        actual_repay * price, m.borrow_base_coin, "MONITORING",
                        notes=f"Repay {actual_repay:.4f} borrow (USDT unchanged)")

    # =========================================================================
    # CLOSING SELL (v4: dispatcher βάσει promote)
    # =========================================================================

    def _execute_closing_sell(self, price: float, timestamp: datetime) -> None:
        """Dispatcher στο ανάλογο Promote route."""
        p = self.config.promote
        if p == 1:
            self._execute_closing_sell_up_investment(price, timestamp)
        elif p == 2:
            self._execute_closing_sell_buy_choice_coins(price, timestamp)
        elif p == 3:
            # Promote 3 εφαρμόζεται ΑΜΕΣΑ μέσω execute_resset_invest(),
            # ΟΧΙ μέσω closing sell trigger. Αν φτάσουμε εδώ, θεωρείται
            # misconfiguration — fallback σε Promote 1.
            logger.warning("  PROMOTE=3 δεν ενεργοποιείται μέσω CLOSING_SELL — fallback σε Promote 1")
            self._execute_closing_sell_up_investment(price, timestamp)
        else:
            logger.error(f"  Άγνωστο PROMOTE={p} — fallback σε Promote 1")
            self._execute_closing_sell_up_investment(price, timestamp)

    def _execute_closing_sell_up_investment(self, price: float, timestamp: datetime) -> None:
        """PROMOTE 1 — v1 συμπεριφορά (compound growth του BASE_COIN)."""
        m = self.memory
        logger.info(f"[{timestamp}] === CLOSING SELL (Promote 1: Up_Investment) === price: {price:.6f}")

        # Βήμα 1: Repay ΟΛΟ το BORROW με owned BASE_COIN
        if m.total_base_coin >= m.borrow_base_coin:
            repay_qty = m.borrow_base_coin
            self.executor.repay_base_coin(repay_qty)
            m.total_base_coin  -= repay_qty
            m.borrow_base_coin  = 0.0
            logger.info(f"  Repay {repay_qty:.4f} borrow (από owned)")
        else:
            shortfall    = m.borrow_base_coin - m.total_base_coin
            extra_qty, extra_price = self.executor.buy_base_coin(shortfall, price)
            m.available_usdt  -= extra_qty * extra_price
            logger.info(f"  Αγορά {extra_qty:.4f} επιπλέον για repay (κόστος {extra_qty*extra_price:.2f} USDT)")
            total_repay = m.total_base_coin + extra_qty
            self.executor.repay_base_coin(total_repay)
            m.total_base_coin  = 0.0
            m.borrow_base_coin = 0.0

        # Βήμα 2: Convert USDT → BASE_COIN
        if m.available_usdt > 0:
            buy_qty      = m.available_usdt / price
            actual_qty, actual_price = self.executor.buy_base_coin(buy_qty, price)
            actual_cost  = actual_qty * actual_price
            m.total_base_coin += actual_qty
            m.available_usdt  -= actual_cost
            logger.info(f"  Convert USDT: {actual_cost:.2f} → {actual_qty:.4f} @ {price:.6f}")

        # Στατιστικά κύκλου
        m.cycle_count           += 1
        m.last_cycle_price       = price
        m.last_cycle_sol         = m.total_base_coin
        m.cumulative_profit_sol  = m.total_base_coin

        logger.info(f"  ╔══ ΚΥΚΛΟΣ #{m.cycle_count} ΟΛΟΚΛΗΡΩΘΗΚΕ ══════════════════╗")
        logger.info(f"  ║  Νέο TOTAL: {m.total_base_coin:.4f}")
        logger.info(f"  ║  Τιμή κλεισίματος: {price:.6f} USDT")
        logger.info(f"  ║  Αξία assets: {m.total_base_coin * price:.2f} USDT")
        logger.info(f"  ╚══════════════════════════════════════════════╝")

        self._log_trade(timestamp, "CLOSING_SELL", price, m.total_base_coin,
                        m.total_base_coin * price, 0.0, "CLOSING_SELL",
                        notes=f"Κύκλος #{m.cycle_count} (Promote 1) | Start={m.total_base_coin:.4f}")

        # v6.x: Set state σε SETUP χωρίς inline _execute_setup. Έτσι ο επόμενος
        # tick θα επιτρέψει στο BotManager να εφαρμόσει NEXT_CYCLE pending config
        # (π.χ. αλλαγή promote 1→2) πριν τρέξει το νέο SETUP. Αν ήταν inline, το
        # _execute_setup θα έτρεχε με το παλιό config.
        logger.info(f"  Νέο SETUP θα εκτελεστεί στον επόμενο tick (pending config εφαρμόζεται πρώτα)")
        self.state = BotState.SETUP

    def _execute_closing_sell_buy_choice_coins(self, price: float, timestamp: datetime) -> None:
        """PROMOTE 2 — Buy_Choice_Coins (v4 §11.2).

        Ροή:
          1. Repay BORROW_BASE_COIN (με owned BASE_COIN, shortfall αν χρειαστεί)
          2. Sell ALL remaining BASE_COIN → USDT
          3. surplus = available_usdt - grand_amount
          4. Αν surplus > min_order_usdt: VIP purchase (percent ή priority mode)
             → ενημέρωση LAST_VIP_COIN (cumulative purchase cost)
          5. appreciation = TOTAL_VIP_market - LAST_VIP_COIN
             IF > 0: new borrow USDT = appreciation × SCALE_VIP_COIN
             IF < 0: repay |appreciation| USDT (από υπάρχον VIP debt)
             IF = 0: no action
          6. Restart SETUP με όλα τα διαθέσιμα USDT

        ΣΗΜΑΝΤΙΚΟ: Ο executor πρέπει να έχει μεθόδους get_vip_price / buy_vip /
        borrow_usdt_vip / repay_usdt_vip. Το BacktestExecutor v4 υλοποιεί
        stubs με mock prices (αρκετά για code path validation).
        Για ακριβές backtest σε Promote 2 χρειάζεται πραγματικό parallel VIP
        price feed — μελλοντική επέκταση.
        """
        m   = self.memory
        cfg = self.config
        logger.info(f"[{timestamp}] === CLOSING SELL (Promote 2: Buy_Choice_Coins) === price: {price:.6f}")

        # Βήμα 1: Repay ΟΛΟ το BORROW_BASE_COIN
        if m.total_base_coin >= m.borrow_base_coin:
            repay_qty = m.borrow_base_coin
            self.executor.repay_base_coin(repay_qty)
            m.total_base_coin  -= repay_qty
            m.borrow_base_coin  = 0.0
            logger.info(f"  [Promote2 βήμα 1] Repay {repay_qty:.4f} borrow (από owned)")
        else:
            # Θεωρητικά δεν συμβαίνει — safety fallback
            shortfall = m.borrow_base_coin - m.total_base_coin
            extra_qty, extra_price = self.executor.buy_base_coin(shortfall, price)
            m.available_usdt -= extra_qty * extra_price
            total_repay = m.total_base_coin + extra_qty
            self.executor.repay_base_coin(total_repay)
            m.total_base_coin  = 0.0
            m.borrow_base_coin = 0.0
            logger.warning(f"  [Promote2 βήμα 1] Shortfall repay: αγορά {extra_qty:.4f} extra")

        # Βήμα 2: Sell ΟΛΟ το remaining BASE_COIN
        if m.total_base_coin > 0:
            sell_qty, sell_price, usdt_received = self.executor.sell_base_coin(m.total_base_coin, price)
            m.available_usdt  += usdt_received
            m.total_base_coin  = 0.0
            logger.info(f"  [Promote2 βήμα 2] Sold {sell_qty:.4f} → {usdt_received:.2f} USDT")

        # Βήμα 3: Surplus vs Grand_amount
        surplus = m.available_usdt - m.grand_amount
        logger.info(f"  [Promote2 βήμα 3] available_usdt={m.available_usdt:.2f} | grand={m.grand_amount:.2f} | surplus={surplus:.2f}")

        # v6.x: αποθήκευση last_vip_coin ΠΡΙΝ από το Step 4 buy. Έτσι στο Step 5
        # μπορούμε να υπολογίσουμε appreciation που περιλαμβάνει το ΝΕΟ VIP που
        # μόλις αγοράσαμε (γιατί total_vip_market_AFTER_step4 ήδη το περιλαμβάνει).
        last_vip_coin_pre_step4 = m.last_vip_coin

        # Βήμα 4: VIP purchase (αν surplus > min_order)
        if surplus > cfg.min_order_usdt:
            self._buy_vip_coins_from_surplus(surplus)
        else:
            logger.info(f"  [Promote2 βήμα 4] surplus ({surplus:.2f}) ≤ min_order ({cfg.min_order_usdt}) — skip VIP purchase")

        # v6.x Βήμα 5: VIP_BORROW με extended formula:
        #   appreciation_extended = total_vip_market_AFTER_step4 - last_vip_coin_PRE_step4
        #   Δηλαδή: (παλιά appreciation) + (αξία ΝΕΟΥ VIP από Step 4)
        # Άρα borrow/repay εφαρμόζεται και στις δύο πλευρές με SCALE_VIP_COIN.
        total_vip_market = self._calc_vip_market_value()
        appreciation = total_vip_market - last_vip_coin_pre_step4
        borrow_or_repay = appreciation * cfg.scale_vip_coin
        logger.info(f"  [Promote2 βήμα 5] VIP market={total_vip_market:.2f} | LAST_VIP_pre={last_vip_coin_pre_step4:.2f} | appreciation_ext={appreciation:.2f} | scaled={borrow_or_repay:.2f}")

        if borrow_or_repay > 0:
            actual_borrowed = self.executor.borrow_usdt_vip(borrow_or_repay)
            m.vip_borrow_usdt += actual_borrowed
            m.available_usdt  += actual_borrowed
            logger.info(f"  [Promote2 βήμα 5] +BORROW USDT via VIP: {actual_borrowed:.2f} (SCALE={cfg.scale_vip_coin})")
        elif borrow_or_repay < 0:
            repay_amount = min(abs(borrow_or_repay), m.vip_borrow_usdt, m.available_usdt)
            if repay_amount > 0:
                self.executor.repay_usdt_vip(repay_amount)
                m.vip_borrow_usdt -= repay_amount
                m.available_usdt  -= repay_amount
                logger.info(f"  [Promote2 βήμα 5] -REPAY VIP USDT: {repay_amount:.2f}")
            else:
                logger.info(f"  [Promote2 βήμα 5] VIP depreciated αλλά δεν υπάρχει VIP debt / USDT για repay")
        else:
            logger.info(f"  [Promote2 βήμα 5] appreciation=0 — καμία ενέργεια")

        # Στατιστικά κύκλου
        m.cycle_count           += 1
        m.last_cycle_price       = price
        m.last_cycle_sol         = m.total_base_coin

        logger.info(f"  ╔══ ΚΥΚΛΟΣ #{m.cycle_count} (Promote 2) ΟΛΟΚΛΗΡΩΘΗΚΕ ═══╗")
        logger.info(f"  ║  USDT cash:     {m.available_usdt:.2f}")
        logger.info(f"  ║  VIP holdings:  {dict(m.vip_holdings)}")
        logger.info(f"  ║  VIP borrow:    {m.vip_borrow_usdt:.2f}")
        logger.info(f"  ║  LAST_VIP:      {m.last_vip_coin:.2f}")
        logger.info(f"  ╚═══════════════════════════════════════════╝")

        self._log_trade(timestamp, "CLOSING_SELL", price, 0.0,
                        m.available_usdt, 0.0, "CLOSING_SELL",
                        notes=f"Κύκλος #{m.cycle_count} (Promote 2) | surplus={surplus:.2f} | VIP_holdings={dict(m.vip_holdings)}")

        # v6.x: Set state σε SETUP χωρίς inline _execute_setup (δες σχόλιο στο
        # Promote 1 closing sell). Έτσι το BotManager θα εφαρμόσει NEXT_CYCLE
        # pending config πριν το νέο SETUP.
        logger.info(f"  Νέο SETUP θα εκτελεστεί στον επόμενο tick (pending config εφαρμόζεται πρώτα)")
        self.state = BotState.SETUP

    def _buy_vip_coins_from_surplus(self, surplus: float) -> None:
        """VIP allocation logic (§11.2.1):
        - percent mode: split surplus με VIP_PERCENTAGES
        - priority mode (auto-switch αν split < min_order ή explicit): 100% σε ένα coin
          με rotating logic μέσω VIP_PRIORITY_LIST
        """
        m   = self.memory
        cfg = self.config

        if not cfg.vip_coins:
            logger.warning(f"  VIP purchase skipped: vip_coins empty")
            return

        use_priority = (cfg.vip_allocation_mode == 'priority')
        allocation: Dict[str, float] = {}

        if cfg.vip_allocation_mode == 'percent':
            if not cfg.vip_percentages:
                logger.warning(f"  percent mode αλλά vip_percentages empty — fallback priority")
                use_priority = True
            else:
                # Υπολογισμός splits
                for coin, pct in cfg.vip_percentages.items():
                    allocation[coin] = surplus * pct / 100.0
                # Auto-switch αν ΕΣΤΩ και ένα split < min_order
                if any(amt < cfg.min_order_usdt for amt in allocation.values()):
                    logger.info(f"  Auto-switch priority mode (split < {cfg.min_order_usdt} USDT)")
                    use_priority = True
                    allocation = {}

        if use_priority:
            if not cfg.vip_priority_list:
                logger.warning(f"  priority mode αλλά vip_priority_list empty — skip VIP purchase")
                return
            ix   = m.priority_rotation_ix % len(cfg.vip_priority_list)
            coin = cfg.vip_priority_list[ix]
            allocation = {coin: surplus}
            m.priority_rotation_ix = (ix + 1) % len(cfg.vip_priority_list)
            logger.info(f"  Priority rotation ix={ix} -> 100% {coin}")

        # Execute purchases
        for coin, usdt_amount in allocation.items():
            if usdt_amount < cfg.min_order_usdt:
                logger.info(f"  Skip {coin}: {usdt_amount:.2f} < min_order {cfg.min_order_usdt}")
                continue
            try:
                vip_price = self.executor.get_vip_price(coin)
                if vip_price <= 0:
                    logger.warning(f"  Skip {coin}: invalid price {vip_price}")
                    continue
                qty, actual_price = self.executor.buy_vip(coin, usdt_amount)
                m.vip_holdings[coin] = m.vip_holdings.get(coin, 0.0) + qty
                m.available_usdt    -= usdt_amount
                m.last_vip_coin     += usdt_amount   # cumulative purchase cost (όλων των coins)
                # v6.x: per-coin cumulative cost για display purchase value στο UI
                m.vip_purchase_cost[coin] = m.vip_purchase_cost.get(coin, 0.0) + usdt_amount
                logger.info(f"  VIP BUY: {qty:.8f} {coin} @ {actual_price:.2f} = {usdt_amount:.2f} USDT")
            except AttributeError as e:
                logger.warning(f"  Executor does not support VIP methods: {e}")
                return
            except Exception as e:
                logger.warning(f"  VIP BUY {coin} failed: {e}")

    # =========================================================================
    # PROMOTE 3 - resset_invest (manual trigger, immediate)
    # =========================================================================

    def execute_resset_invest(self, price: float, timestamp: datetime) -> None:
        """PROMOTE 3 - Manual reset (v4 §11.3).

        Called from UI/API, NOT from price tick. Applied IMMEDIATELY regardless
        of cycle state.

        Flow:
          1. VIP_COINS NOT touched - remain as cross-margin collateral
          2. Cancel open orders
          3. Repay FULL BORROW_BASE_COIN using ASSET
             (invariant §6 guarantees asset >= borrow - safety fallback if not)
          4. If remaining ASSET -> sell for USDT
          5. Bot -> STOPPED. USDT debt, VIP, VIP debt handled manually by user.
        """
        m = self.memory
        logger.warning(f"[{timestamp}] === PROMOTE 3: RESSET_INVEST === price: {price:.6f}")

        preserved_vip      = dict(m.vip_holdings)
        preserved_vip_debt = m.vip_borrow_usdt

        # Step 2: Cancel open orders
        self.executor.cancel_all_orders()
        logger.info(f"  [Promote3 step 2] Cancelled all open orders")

        # Step 3: Repay FULL BORROW_BASE_COIN
        if m.borrow_base_coin > 0:
            if m.total_base_coin >= m.borrow_base_coin:
                repay_qty = m.borrow_base_coin
                self.executor.repay_base_coin(repay_qty)
                m.total_base_coin  -= repay_qty
                m.borrow_base_coin  = 0.0
                logger.info(f"  [Promote3 step 3] Repay {repay_qty:.4f} borrow (from owned asset)")
            else:
                logger.warning(f"  [Promote3 step 3] asset ({m.total_base_coin:.4f}) < borrow ({m.borrow_base_coin:.4f}) - partial repay only")
                repay_qty = m.total_base_coin
                self.executor.repay_base_coin(repay_qty)
                m.borrow_base_coin -= repay_qty
                m.total_base_coin   = 0.0
        else:
            logger.info(f"  [Promote3 step 3] No BORROW_BASE_COIN to repay")

        # Step 4: Sell remaining ASSET
        if m.total_base_coin > 0:
            sell_qty, sell_price, usdt_received = self.executor.sell_base_coin(m.total_base_coin, price)
            m.available_usdt  += usdt_received
            m.total_base_coin  = 0.0
            logger.info(f"  [Promote3 step 4] Sold {sell_qty:.4f} remaining @ {sell_price:.6f} -> {usdt_received:.2f} USDT")
        else:
            logger.info(f"  [Promote3 step 4] No remaining asset")

        # Reset cycle-scoped state
        m.has_bought         = False
        m.buy_trigger_count  = 0
        m.sell_trigger_count = 0
        # v6.x: reset cycle-cumulative counters
        m.buy_count_total    = 0
        m.sell_count_total   = 0

        logger.info(f"  === RESSET_INVEST COMPLETE ===")
        logger.info(f"    USDT cash:        {m.available_usdt:.2f}")
        logger.info(f"    USDT debt (init): {m.usdt_debt:.2f}")
        logger.info(f"    USDT debt (VIP):  {preserved_vip_debt:.2f}")
        logger.info(f"    VIP holdings:     {preserved_vip}")
        logger.info(f"    BASE_COIN:        0 | BORROW: 0")
        logger.info(f"    STATE -> STOPPED (user takes over)")

        self._log_trade(timestamp, "RESSET_INVEST", price, 0.0,
                        m.available_usdt, 0.0, "STOPPED",
                        notes=f"Promote 3 reset | VIP preserved: {preserved_vip} | VIP_debt: {preserved_vip_debt:.2f}")
        self.state = BotState.STOPPED

    # =========================================================================
    # MARGIN PROTECT (v4: extended formula with VIP assets/debts)
    # =========================================================================

    def _check_margin_level(self, price: float) -> bool:
        """v4 §12: ratio = TOTAL_ALL_ASSETS / TOTAL_ALL_DEBT

        TOTAL_ALL_ASSETS = asset_base_coin + cash + sum(vip_market_value)
        TOTAL_ALL_DEBT   = usdt_debt + vip_borrow_usdt + borrow_base_coin_value

        For Promote 1 (vip_holdings={}, vip_borrow_usdt=0), the sum is
        identical to v1 formula.
        """
        m   = self.memory
        cfg = self.config

        total_assets = (m.total_base_coin * price) + m.available_usdt
        total_debt   = m.usdt_debt + (m.borrow_base_coin * price)

        if m.vip_holdings:
            vip_market_value = self._calc_vip_market_value()
            total_assets += vip_market_value
        if m.vip_borrow_usdt > 0:
            total_debt += m.vip_borrow_usdt

        if total_debt <= 0:
            return False
        ratio = total_assets / total_debt
        if ratio <= cfg.margin_level:
            logger.warning(f"  MARGIN PROTECT! ratio={ratio:.4f} <= {cfg.margin_level}")
            self.state = BotState.MARGIN_PROTECT
            return True
        return False

    def _calc_vip_market_value(self) -> float:
        """Calculate market value of VIP holdings via executor."""
        total = 0.0
        m = self.memory
        for coin, qty in m.vip_holdings.items():
            if qty > 0 and hasattr(self.executor, 'get_vip_price'):
                price = self.executor.get_vip_price(coin)
                total += qty * price
        return total

    def _execute_margin_protect(self, price: float, timestamp: datetime) -> None:
        """v4 §12: Cancel all -> Repay BASE_COIN borrow -> Restart SETUP.
        Does NOT touch VIP_COINS."""
        m = self.memory
        logger.warning(f"[{timestamp}] === MARGIN PROTECT ===")
        self.executor.cancel_all_orders()
        repay_qty = min(m.total_base_coin, m.borrow_base_coin)
        self.executor.repay_base_coin(repay_qty)
        m.total_base_coin  -= repay_qty
        m.borrow_base_coin -= repay_qty
        self._log_trade(timestamp, "MARGIN_PROTECT", price, repay_qty,
                        repay_qty * price, m.borrow_base_coin, "MARGIN_PROTECT")
        # v4: Reset per-direction counters
        m.buy_trigger_count  = 0
        m.sell_trigger_count = 0
        # v6.x: reset cycle-cumulative counters (νέος cycle θα ξεκινήσει σε SETUP)
        m.buy_count_total    = 0
        m.sell_count_total   = 0
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
        if m.vip_holdings:
            total_assets += self._calc_vip_market_value()
        if m.vip_borrow_usdt > 0:
            total_debt += m.vip_borrow_usdt
        ratio = total_assets / total_debt if total_debt > 0 else 0

        # v6.x: enriched VIP holdings — quantity + purchase_cost + current_value
        vip_enriched: Dict[str, dict] = {}
        for coin, qty in m.vip_holdings.items():
            try:
                cur_price = self.executor.get_vip_price(coin) if hasattr(self.executor, 'get_vip_price') else 0.0
            except Exception:
                cur_price = 0.0
            cur_value = qty * cur_price if cur_price > 0 else 0.0
            vip_enriched[coin] = {
                "quantity":      round(qty, 8),
                "purchase_cost": round(m.vip_purchase_cost.get(coin, 0.0), 2),
                "current_value": round(cur_value, 2),
                "current_price": round(cur_price, 4) if cur_price > 0 else None,
            }
        return {
            "state":                   self.state.value,
            "cycle_count":             m.cycle_count,
            # v5.2: price fields σε 10 δεκαδικά — τα 6 δεκαδικά έχαναν
            # σημαντικά ψηφία σε low-priced coins (π.χ. PEPE ~3e-6 → 4e-6)
            "reference_price":         round(m.reference_price, 10),
            "buy_price":               round(m.buy_price, 10),
            "sel_price":               round(m.sel_price, 10),
            "total_base_coin":         round(m.total_base_coin, 4),
            "borrow_base_coin":        round(m.borrow_base_coin, 4),
            "available_usdt":          round(m.available_usdt, 2),
            "usdt_debt":               round(m.usdt_debt, 2),
            "margin_ratio":            round(ratio, 4),
            "has_bought":              m.has_bought,
            "buy_trigger_count":       m.buy_trigger_count,
            "sell_trigger_count":      m.sell_trigger_count,
            # v6.x: cycle-cumulative counters (UI display)
            "buy_count_total":         m.buy_count_total,
            "sell_count_total":        m.sell_count_total,
            "buy_activated":           m.buy_activated,
            "sell_activated":          m.sell_activated,
            "buy_trailing_stop":       round(m.buy_trailing_stop, 10),
            "sell_trailing_stop":      round(m.sell_trailing_stop, 10),
            "grand_amount":            round(m.grand_amount, 2),
            "last_vip_coin":           round(m.last_vip_coin, 2),
            "vip_holdings":            dict(m.vip_holdings),
            "vip_holdings_enriched":   vip_enriched,
            "vip_borrow_usdt":         round(m.vip_borrow_usdt, 2),
            "promote":                 self.config.promote,
            "second_profit_enabled":   self.config.second_profit_enabled,
            "active_profit_pct_buy":   self._get_active_profit_pct('buy'),
            "active_profit_pct_sell":  self._get_active_profit_pct('sell'),
        }
