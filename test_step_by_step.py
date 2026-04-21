"""
Test Step-by-Step - Vima 1: MONO SETUP
Kathe mera dimiourgei SETUP kai emfanizei REFERENCE.
Trexe: python test_step_by_step.py
"""

import sys
import logging
from collections import defaultdict
from datetime import datetime, timezone

from backtester.data_loader import DataLoader
from backtester.engine import BacktestExecutor
from strategies.arbitrading_v1 import ArbitradingV1, BotConfig, BotState

# -- RYTHMISEIS --
config = BotConfig(
    trading_pair            = "SOL/USDT",
    profit_coin             = "USDT",
    start_base_coin         = 10.0,
    scale_base_coin         = 4.0,
    ratio_scale             = 0.005,
    borrow_base_coin_factor = 1.0,
    min_profit_percent      = 5.0,
    step_point              = 0.5,
    trailing_stop           = 2.0,
    limit_order             = 0.5,
    margin_level            = 1.07,
)

CSV_PATH = "data/SOLUSDT_1m.csv"

# -- LOGGING --
logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
    stream=sys.stdout,
)

# -- FORTOSI DEDOMENON --
loader = DataLoader(exchange_id="kucoin")
candles = loader.load_csv(CSV_PATH)
print(f"Loaded {len(candles)} candles")
print(f"Period: {candles[0][0].date()} -> {candles[-1][0].date()}")
print()

# -- OMADOPOIISI ANA MERA --
days = defaultdict(list)
for c in candles:
    days[c[0].date()].append(c)

# -- EKTELESI SETUP ANA MERA --
print(f"{'DAY':<14} {'OPEN':>10} {'REFERENCE':>12} {'TOTAL_SOL':>12} "
      f"{'BORROW_SOL':>12} {'CASH_USDT':>12} {'USDT_DEBT':>12} "
      f"{'BUY_ACT':>10} {'SELL_ACT':>10}")
print("-" * 110)

for day in sorted(days.keys()):
    day_candles = days[day]
    open_price = day_candles[0][1]

    executor = BacktestExecutor(start_base_coin=config.start_base_coin)
    strategy = ArbitradingV1(config=config, executor=executor)

    ts = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
    strategy.start(open_price, ts)

    m = strategy.memory
    buy_act = m.reference_price * (1 - config.min_profit_percent / 100)
    sell_act = m.reference_price * (1 + config.min_profit_percent / 100)
    print(f"{str(day):<14} {open_price:>10.4f} {m.reference_price:>12.4f} "
          f"{m.total_base_coin:>12.4f} {m.borrow_base_coin:>12.4f} "
          f"{m.available_usdt:>12.2f} {m.usdt_debt:>12.2f} "
          f"{buy_act:>10.4f} {sell_act:>10.4f}")

print("-" * 110)
print("DONE")
