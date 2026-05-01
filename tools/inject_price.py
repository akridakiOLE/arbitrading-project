#!/usr/bin/env python3
"""
tools/inject_price.py — Price injection tool for paper-mode testing.

Forces a synthetic price into the bot's price feed, bypassing the real
exchange API. Used to validate bot logic (triggers, state transitions,
trailing stops, REFERENCE updates, MARGIN_PROTECT, etc.) without waiting
for market volatility.

USAGE:
  python tools/inject_price.py --price 0.0000038                    # PERSISTENT (default)
  python tools/inject_price.py --price 0.0000038 --duration 60      # 60-second timed
  python tools/inject_price.py --status
  python tools/inject_price.py --clear

DEFAULT BEHAVIOR (persistent):
  When --duration is omitted, the injection has NO expiry. The bot uses
  the injected price until you replace it with a new injection or run
  --clear. This is the recommended mode for multi-step scenario testing.

OPTIONAL TIMED:
  When --duration N is provided (1-300 sec), the injection auto-expires
  after N seconds and the bot reverts to real prices.

HARD CONSTRAINTS (cannot be bypassed):
  - REFUSES if bot config has mode == "live"
  - duration must be 1-300 seconds (if provided)
  - price must be > 0

ENVIRONMENT ISOLATION:
  Reads ARBITRADING_ENV (default: 'production'). Uses per-environment
  file: /tmp/arbitrading_injection_{env}.json. This prevents staging
  injections from contaminating production and vice versa.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_FILENAME = "web_bot_config.json"
MAX_DURATION_S = 300


def detect_env() -> str:
    """Detect ARBITRADING_ENV from env var or script path fallback."""
    env = os.environ.get("ARBITRADING_ENV")
    if env:
        return env
    # Fallback: detect from script location (production vs staging install)
    script_path = str(Path(__file__).resolve())
    if "arbitrading-staging" in script_path:
        return "staging"
    return "production"


def injection_file_path(env: str) -> str:
    """Compose per-environment injection file path."""
    return f"/tmp/arbitrading_injection_{env}.json"


def read_bot_config() -> dict:
    """Read bot config to determine current symbol and mode.

    Tries CWD first, then project root (relative to this script).
    """
    candidates = [
        Path.cwd() / CONFIG_FILENAME,
        Path(__file__).resolve().parent.parent / CONFIG_FILENAME,
    ]
    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception as e:
                sys.exit(f"ERROR: failed to read {p}: {e}")
    sys.exit(f"ERROR: {CONFIG_FILENAME} not found in CWD or project root")


def cmd_inject(args, env: str, inj_path: str):
    """Write injection file with given price (persistent or timed)."""
    cfg = read_bot_config()
    symbol = cfg.get("symbol", "")
    mode = cfg.get("mode", "paper")

    # HARD GUARD #1: live mode is FORBIDDEN — non-bypassable
    if mode == "live":
        sys.exit("REFUSED: bot is in 'live' mode. Price injection is paper-only.")

    # Validation
    if args.price <= 0:
        sys.exit(f"REFUSED: price must be > 0 (got {args.price})")
    if args.duration is not None and not (1 <= args.duration <= MAX_DURATION_S):
        sys.exit(f"REFUSED: duration must be 1-{MAX_DURATION_S}s (got {args.duration})")
    if not symbol:
        sys.exit("REFUSED: bot config has no symbol set")

    now = datetime.now(timezone.utc)

    payload = {
        "symbol":     symbol,
        "mode":       mode,
        "env":        env,
        "price":      float(args.price),
        "set_at_iso": now.isoformat(),
    }
    if args.duration is not None:
        expires = now + timedelta(seconds=args.duration)
        payload["expires_at_iso"] = expires.isoformat()
        payload["duration_s"] = int(args.duration)
    # else: no expires_at_iso -> persistent injection

    try:
        Path(inj_path).write_text(json.dumps(payload, indent=2))
    except Exception as e:
        sys.exit(f"ERROR writing {inj_path}: {e}")

    print(f"INJECTED for {symbol} ({mode} mode, env={env}):")
    print(f"  Price:    {args.price}")
    if args.duration is not None:
        print(f"  Duration: {args.duration}s (timed)")
        print(f"  Expires:  {payload['expires_at_iso']}")
    else:
        print(f"  Duration: PERSISTENT (no expiry - clear with --clear or replace)")
    print(f"  File:     {inj_path}")
    print()
    print("The bot will pick up this price on its next price tick (within ~1s).")


def cmd_status(_args, env: str, inj_path: str):
    """Show current injection state."""
    if not os.path.exists(inj_path):
        print(f"No active injection. ({inj_path} does not exist)")
        return
    try:
        data = json.loads(Path(inj_path).read_text())
    except Exception as e:
        print(f"ERROR reading injection file: {e}")
        return

    expires_iso = data.get("expires_at_iso")
    is_persistent = expires_iso is None
    is_active = True
    remaining = None

    if not is_persistent:
        try:
            expires = datetime.fromisoformat(expires_iso)
            now = datetime.now(timezone.utc)
            remaining = (expires - now).total_seconds()
            is_active = remaining > 0
        except Exception:
            is_active = False

    if is_persistent:
        status_label = "ACTIVE (persistent)"
    elif is_active:
        status_label = "ACTIVE (timed)"
    else:
        status_label = "EXPIRED"

    print(f"Injection {status_label}:")
    print(f"  Symbol:    {data.get('symbol')}")
    print(f"  Mode:      {data.get('mode')}")
    print(f"  Env:       {data.get('env', '(legacy file, no env)')}")
    print(f"  Price:     {data.get('price')}")
    print(f"  Set at:    {data.get('set_at_iso')}")
    if is_persistent:
        print(f"  Expires:   never (persistent - use --clear to remove)")
    else:
        print(f"  Expires:   {expires_iso}")
        if is_active and remaining is not None:
            print(f"  Remaining: {remaining:.1f}s")


def cmd_clear(_args, env: str, inj_path: str):
    """Remove injection file."""
    if not os.path.exists(inj_path):
        print(f"No injection file to clear ({inj_path} does not exist).")
        return
    try:
        os.remove(inj_path)
        print(f"Cleared: {inj_path}")
    except Exception as e:
        sys.exit(f"ERROR removing file: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Inject synthetic price for paper-mode testing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--price", type=float,
                        help="Price to inject (must be > 0)")
    parser.add_argument("--duration", type=int, default=None,
                        help=f"Optional timed expiry in seconds (1-{MAX_DURATION_S}). "
                             "If omitted, injection is PERSISTENT (no expiry).")
    parser.add_argument("--status", action="store_true",
                        help="Show current injection state and exit")
    parser.add_argument("--clear", action="store_true",
                        help="Remove active injection and exit")

    args = parser.parse_args()

    env = detect_env()
    inj_path = injection_file_path(env)

    if args.status:
        cmd_status(args, env, inj_path)
    elif args.clear:
        cmd_clear(args, env, inj_path)
    elif args.price is not None:
        cmd_inject(args, env, inj_path)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
