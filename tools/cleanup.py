#!/usr/bin/env python3
"""
tools/cleanup.py — Periodic hygiene cleanup για arbitrading bot (v5.4).

Tasks:
  - Delete state_snapshots > RETENTION_DAYS (default 30)
  - VACUUM όλες τις SQLite DBs για να ανακτηθεί χώρος
  - Trades DBs: NEVER deleted (audit trail)
  - Print disk usage report

Usage:
  python3 tools/cleanup.py                  # one-shot run
  python3 tools/cleanup.py --dry-run        # δείχνει τι θα έκανε χωρίς να αλλάξει τίποτα

Schedule via systemd timer (deployment/arbitrading-cleanup.timer)
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

RETENTION_DAYS = 30

# Paths στον server (override με env var ARBITRADING_DIR αν χρειαστεί)
WORKDIR = Path(os.environ.get("ARBITRADING_DIR", "/opt/arbitrading-project"))

STATE_DBS  = ["paper_state.db",  "live_state.db"]
TRADES_DBS = ["paper_trades.db", "live_trades.db"]


def now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def cleanup_state_snapshots(db_path: Path, dry_run: bool) -> dict:
    """Διαγράφει state_snapshots παλαιότερα από RETENTION_DAYS μέρες.
    VACUUMs τη βάση για να ανακτηθεί χώρος.
    Returns dict με before/after counts και size delta."""
    if not db_path.exists():
        return {"db": str(db_path), "skipped": "not found"}

    size_before = db_path.stat().st_size
    cutoff_iso = (datetime.now(tz=timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        # Confirm table exists (defensive)
        tbl = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='state_snapshots'"
        ).fetchone()
        if not tbl:
            conn.close()
            return {"db": str(db_path), "skipped": "no state_snapshots table"}

        before = cur.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0]
        to_delete = cur.execute(
            "SELECT COUNT(*) FROM state_snapshots WHERE ts_iso < ?", (cutoff_iso,)
        ).fetchone()[0]

        if dry_run:
            conn.close()
            return {
                "db": str(db_path), "dry_run": True,
                "would_delete": to_delete, "kept": before - to_delete,
                "size_kb": size_before // 1024,
            }

        cur.execute("DELETE FROM state_snapshots WHERE ts_iso < ?", (cutoff_iso,))
        conn.commit()
        after = cur.execute("SELECT COUNT(*) FROM state_snapshots").fetchone()[0]
        # VACUUM ΜΟΝΟ έξω από transaction
        conn.isolation_level = None
        cur.execute("VACUUM")
        conn.close()
    except Exception as e:
        try: conn.close()
        except Exception: pass
        return {"db": str(db_path), "error": str(e)}

    size_after = db_path.stat().st_size
    return {
        "db": str(db_path), "before": before, "after": after,
        "deleted": before - after,
        "size_kb_before": size_before // 1024, "size_kb_after": size_after // 1024,
        "reclaimed_kb": (size_before - size_after) // 1024,
    }


def vacuum_trades_db(db_path: Path, dry_run: bool) -> dict:
    """VACUUMs trades DB. ΔΕΝ διαγράφει τίποτα (audit trail)."""
    if not db_path.exists():
        return {"db": str(db_path), "skipped": "not found"}

    size_before = db_path.stat().st_size
    if dry_run:
        return {"db": str(db_path), "dry_run": True, "size_kb": size_before // 1024}

    try:
        conn = sqlite3.connect(str(db_path))
        conn.isolation_level = None
        conn.execute("VACUUM")
        conn.close()
    except Exception as e:
        return {"db": str(db_path), "error": str(e)}

    size_after = db_path.stat().st_size
    return {
        "db": str(db_path),
        "size_kb_before": size_before // 1024, "size_kb_after": size_after // 1024,
        "reclaimed_kb": (size_before - size_after) // 1024,
    }


def disk_usage_report() -> dict:
    """Επιστρέφει disk usage info για το WORKDIR."""
    try:
        total_kb = sum(
            f.stat().st_size for f in WORKDIR.rglob("*") if f.is_file()
        ) // 1024
    except Exception as e:
        total_kb = -1
    return {
        "workdir": str(WORKDIR),
        "total_size_mb": round(total_kb / 1024, 2),
    }


def main() -> int:
    global RETENTION_DAYS
    ap = argparse.ArgumentParser(description="Arbitrading bot cleanup (v5.4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be done without making changes")
    ap.add_argument("--retention-days", type=int, default=RETENTION_DAYS,
                    help=f"Days to keep state_snapshots (default: {RETENTION_DAYS})")
    args = ap.parse_args()
    RETENTION_DAYS = args.retention_days

    print(f"[{now_utc()}] Cleanup started — workdir={WORKDIR} retention={RETENTION_DAYS}d "
          f"dry_run={args.dry_run}")

    if not WORKDIR.exists():
        print(f"[ERROR] Workdir {WORKDIR} does not exist")
        return 2

    os.chdir(WORKDIR)

    # State snapshots cleanup + vacuum
    print("--- State snapshots ---")
    for db_name in STATE_DBS:
        result = cleanup_state_snapshots(Path(db_name), args.dry_run)
        print(f"  {db_name}: {result}")

    # Trades vacuum (no deletes)
    print("--- Trades (vacuum only, no deletes) ---")
    for db_name in TRADES_DBS:
        result = vacuum_trades_db(Path(db_name), args.dry_run)
        print(f"  {db_name}: {result}")

    # Disk usage
    print("--- Disk usage ---")
    print(f"  {disk_usage_report()}")

    print(f"[{now_utc()}] Cleanup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
