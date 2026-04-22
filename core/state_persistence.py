"""
core/state_persistence.py — SQLite BotMemory + executor balances snapshot/restore.

Κρατάει snapshots της πλήρους bot κατάστασης σε SQLite ώστε:
  - Αν το process πέσει, μπορούμε να το ξανασηκώσουμε στην ίδια κατάσταση
  - Στο Stop → Start, το bot συνεχίζει από εκεί που ήταν (Resume)
  - Έχουμε historical audit trail του state machine

Snapshot schema (v5.1 extended):
  Κάθε snapshot είναι ένα JSON blob με τα εξής top-level keys:
    - memory:    BotMemory fields (όπως πριν)
    - balances:  Executor balances (base_coin, usdt, debts, vip_*)
    - meta:      tick_count, symbol, mode (για fresh-start decisions)

Backward compatibility:
  Παλιά snapshots χωρίς "memory"/"balances"/"meta" keys αντιμετωπίζονται ως
  "legacy flat" (όλα τα fields στο top level είναι BotMemory). Σε αυτή την
  περίπτωση ΔΕΝ γίνεται balance restore.
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
             event: str, notes: str = "",
             executor_balances: Optional[dict] = None,
             meta: Optional[dict] = None) -> None:
        """Serialize πλήρες state σε JSON και αποθηκεύει.

        executor_balances: dict από executor.get_balance_dict() — optional αλλά
                           απαραίτητο για σωστό Resume.
        meta: dict με {symbol, mode, tick_count} — optional αλλά απαραίτητο
              για σωστή fresh-start απόφαση."""
        try:
            mem_dict = asdict(memory)
            ts = mem_dict.get('current_timestamp')
            if ts is not None and hasattr(ts, 'isoformat'):
                mem_dict['current_timestamp'] = ts.isoformat()

            full_state = {
                "memory":   mem_dict,
                "balances": executor_balances or {},
                "meta":     meta or {},
            }
            memory_json = json.dumps(full_state, default=str)
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

        Returns:
          {'state':    str,
           'memory':   dict (BotMemory fields),
           'balances': dict (executor balances) | {} (αν legacy snapshot),
           'meta':     dict ({symbol, mode, tick_count}) | {} (αν legacy),
           'ts_iso':   str,
           'event':    str}
        """
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
            parsed = json.loads(mem_json)
        except Exception as e:
            logger.error(f"[StatePersistence] restore deserialize failed: {e}")
            return None

        # Υποστήριξη 2 schemas: νέο (nested) vs legacy (flat).
        if isinstance(parsed, dict) and "memory" in parsed:
            memory_dict   = parsed.get("memory")   or {}
            balances_dict = parsed.get("balances") or {}
            meta_dict     = parsed.get("meta")     or {}
        else:
            # Legacy flat: όλο το dict είναι BotMemory
            memory_dict   = parsed if isinstance(parsed, dict) else {}
            balances_dict = {}
            meta_dict     = {}

        return {
            'ts_iso':   ts,
            'event':    event,
            'state':    state,
            'memory':   memory_dict,
            'balances': balances_dict,
            'meta':     meta_dict,
        }

    def apply_to_memory(self, memory_dict: dict, target: BotMemory) -> None:
        """Εφαρμόζει ένα restored dict πάνω σε BotMemory instance.
        Skip fields που δεν υπάρχουν στην τρέχουσα BotMemory (schema compatibility)."""
        valid_fields = {f.name for f in fields(BotMemory)}
        applied = 0
        for key, value in memory_dict.items():
            if key not in valid_fields:
                continue
            if key == 'current_timestamp':
                # Θα ανατεθεί από το πρώτο tick
                continue
            setattr(target, key, value)
            applied += 1
        logger.info(f"[StatePersistence] Applied {applied}/{len(valid_fields)} memory fields")

    def close(self) -> None:
        if self._db:
            self._db.close()
