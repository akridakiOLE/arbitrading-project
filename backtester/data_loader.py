"""
Backtester — Data Loader
Κατεβάζει ιστορικά OHLCV δεδομένα από exchange (ccxt) ή φορτώνει από CSV.

Χρήση:
  loader = DataLoader(exchange_id="kucoin")
  candles = loader.fetch("SOL/USDT", "1m", "2024-01-01", "2024-03-01")
  loader.save_csv(candles, "data/SOLUSDT_1m.csv")
  candles = loader.load_csv("data/SOLUSDT_1m.csv")
"""

import csv
import os
import time
import logging
from datetime import datetime, timezone
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Candle tuple: (timestamp: datetime, open, high, low, close, volume)
Candle = Tuple[datetime, float, float, float, float, float]


class DataLoader:

    def __init__(self, exchange_id: str = "kucoin"):
        self.exchange_id = exchange_id
        self._exchange   = None  # lazy init — μόνο αν χρειαστεί

    # ─────────────────────────────────────────────────────────────────────────
    # FETCH από exchange μέσω ccxt
    # ─────────────────────────────────────────────────────────────────────────

    def fetch(self, symbol: str, timeframe: str,
              start: str, end: str) -> List[Candle]:
        """
        Κατεβάζει OHLCV δεδομένα σε batches.
        Παράμετροι start/end: "YYYY-MM-DD" string.
        Επιστρέφει λίστα από Candle tuples.
        """
        try:
            import ccxt
        except ImportError:
            raise ImportError("Εγκατέστησε το ccxt: pip install ccxt")

        if self._exchange is None:
            self._exchange = getattr(ccxt, self.exchange_id)({"enableRateLimit": True})

        start_ts = int(datetime.strptime(start, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)
        end_ts   = int(datetime.strptime(end,   "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)

        all_candles: List[Candle] = []
        current_ts = start_ts
        limit = 1500  # KuCoin max per request

        logger.info(f"Λήψη {symbol} {timeframe} από {start} έως {end}...")

        while current_ts < end_ts:
            try:
                raw = self._exchange.fetch_ohlcv(
                    symbol, timeframe, since=current_ts, limit=limit
                )
            except Exception as e:
                logger.error(f"Σφάλμα λήψης: {e}")
                time.sleep(2)
                continue

            if not raw:
                break

            for r in raw:
                ts_dt = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc)
                if r[0] >= end_ts:
                    break
                all_candles.append((ts_dt, float(r[1]), float(r[2]),
                                    float(r[3]), float(r[4]), float(r[5])))

            current_ts = raw[-1][0] + 1
            logger.info(f"  Λήφθηκαν {len(all_candles)} candles ({all_candles[-1][0].date()})")
            time.sleep(self._exchange.rateLimit / 1000)

        logger.info(f"Σύνολο: {len(all_candles)} candles")
        return all_candles

    # ─────────────────────────────────────────────────────────────────────────
    # ΑΠΟΘΗΚΕΥΣΗ / ΦΟΡΤΩΣΗ CSV
    # ─────────────────────────────────────────────────────────────────────────

    def save_csv(self, candles: List[Candle], filepath: str) -> None:
        """Αποθηκεύει candles σε CSV για επαναχρησιμοποίηση."""
        os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
            for c in candles:
                writer.writerow([c[0].isoformat(), c[1], c[2], c[3], c[4], c[5]])
        logger.info(f"Αποθηκεύτηκαν {len(candles)} candles → {filepath}")

    def load_csv(self, filepath: str) -> List[Candle]:
        """Φορτώνει candles από CSV αρχείο."""
        candles = []
        with open(filepath, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts = datetime.fromisoformat(row["timestamp"])
                candles.append((
                    ts,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"])
                ))
        logger.info(f"Φορτώθηκαν {len(candles)} candles από {filepath}")
        return candles
