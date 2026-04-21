"""
core/state_persistence.py — SQLite BotMemory snapshot/restore (v4 Φάση 3γ).

Κρατάει snapshots της BotMemory σε SQLite ώστε:
  - Αν το process πέσει, μπορούμε να το ξανασηκώσουμε στην ίδια κατάσταση
  - Έχουμε historical audit trail του state machine

Design:
  - Κάθε snapshot είναι ένα JSON blob με όλα τα BotMemory fields + BotState
  - Αποθηκεύεται αυτόματα μετά από:
    - Κάθε SETUP completion
    - Κάθε BUY / REPAY_SELL / CLOSING_SELL trigger
    - Κάθε MARGIN_PROTECT / RESSET_INVEST
  - restore_latest() επαναφέρει την πιο πρόσφατη εγγραφή
"""

import json
import sqlite3
import logging
from dataclasses import fields, asdict
from datetime import datetime, timezone
from typing import Optional

from strategies.arbitrading_v2 import BotMemory, BotState

logger = logging.getLogger(__name__)


class StatePersistence:

    def __init__(self, db_path: str = "bot_state.db"):
        self.db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()

    def _init_db(self) -> None:
        cur = self._db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS state_snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_iso     TEXT    NOT NULL,
                event      TEXT    NOT NULL,
                state      TEXT    NOT NULL,
                memory_json TEXT   NOT NULL,
                notes      TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_state_snapshots_ts
            ON state_snapshots(ts_iso DESC)
        """)
        self._db.commit()

    def save(self, memory: BotMemory, state: BotState,
             event: str, notes: str = "") -> None:
        """Serialize BotMemory σε JSON και αποθηκεύει."""
        try:
            d = asdict(memory)
            # datetime fields -> iso string
            ts = d.get('current_timestamp')
            if ts is not None and hasattr(ts, 'isoformat'):
                d['current_timestamp'] = ts.isoformat()
            memory_json = json.dumps(d, default=str)
        except Exception as e:
            logger.warning(f"[StatePersistence] serialize failed: {e}")
            return

        cur = self._db.cursor()
        cur.execute("""
            INSERT INTO state_snapshots (ts_iso, event, state, memory_json, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (
            datetime.now(tz=timezone.utc).isoformat(),
            event, state.value, memory_json, notes,
        ))
        self._db.commit()

    def restore_latest(self) -> Optional[dict]:
        """Επιστρέφει το πιο πρόσφατο state ή None αν δεν υπάρχει.
        Returns: {'state': str, 'memory': dict, 'ts_iso': str, 'event': str}"""
        cur = self._db.cursor()
        row = cur.execute("""
            SELECT ts_iso, event, state, memory_json
            FROM state_snapshots
            ORDER BY id DESC
            LIMIT 1
        """).fetchone()
        if not row:
            return None
        ts, event, state, mem_json = row
        try:
            memory_dict = json.loads(mem_json)
        except Exception as e:
            logger.error(f"[StatePersistence] restore deserialize failed: {e}")
            return None
        return {'ts_iso': ts, 'event': event, 'state': state, 'memory': memory_dict}

    def apply_to_memory(self, memory_dict: dict, target: BotMemory) -> None:
        """Εφαρμόζει ένα restored dict πάνω σε BotMemory instance.
        ΠΡΟΣΟΧΗ: skip fields που δεν υπάρχουν στην τρέχουσα BotMemory (schema compatibility)."""
        valid_fields = {f.name for f in fields(BotMemory)}
        for key, value in memory_dict.items():
            if key not in valid_fields:
                continue
            # current_timestamp: skip (θα ανατεθεί από το πρώτο tick)
            if key == 'current_timestamp':
                continue
            setattr(target, key, value)
        logger.info(f"[StatePersistence] Applied {len(valid_fields & set(memory_dict.keys()))} fields")

    def close(self) -> None:
        if self._db:
            self._db.close()
