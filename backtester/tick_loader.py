"""
Backtester — Tick Data Loader
Φορτώνει tick-by-tick trade data από Binance CSV.

Format CSV (χωρίς header):
  trade_id, price, quantity, quote_qty, timestamp_ms, is_buyer_maker, ?

Χρήση:
  loader = TickLoader()
  ticks = loader.load_csv("data/SOLUSDT-trades-2024-01.csv")
  # ticks = [(datetime, price), (datetime, price), ...]
"""

import csv
import logging
from datetime import datetime, timezone
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Tick tuple: (timestamp: datetime, price: float)
Tick = Tuple[datetime, float]


class TickLoader:
    """Φορτώνει Binance historical trades CSV σε λίστα (timestamp, price)."""

    def load_csv(self, filepath: str, max_ticks: int = 0) -> List[Tick]:
        """
        Φορτώνει tick data από CSV.

        Args:
            filepath: Μονοπάτι CSV αρχείου (Binance format)
            max_ticks: Μέγιστος αριθμός ticks (0 = όλα)

        Returns:
            Λίστα (datetime, price) sorted by timestamp
        """
        ticks: List[Tick] = []
        count = 0

        with open(filepath, "r") as f:
            reader = csv.reader(f)
            for row in reader:
                # Binance format: trade_id, price, qty, quote_qty, timestamp_ms, is_buyer_maker, ?
                if len(row) < 6:
                    continue

                try:
                    price = float(row[1])
                    ts_ms = int(row[4])
                    ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                    ticks.append((ts, price))
                except (ValueError, IndexError):
                    continue

                count += 1
                if max_ticks > 0 and count >= max_ticks:
                    break

                # Progress log κάθε 5 εκατομμύρια
                if count % 5_000_000 == 0:
                    logger.info(f"  Φορτώθηκαν {count:,} ticks ({ts.date()})...")

        logger.info(f"Φορτώθηκαν {len(ticks):,} ticks από {filepath}")
        if ticks:
            logger.info(f"  Περίοδος: {ticks[0][0]} → {ticks[-1][0]}")
        return ticks
