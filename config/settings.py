"""
Global settings for the arbitrading bot.
Loads sensitive values (API keys) from config/secrets.env
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "secrets.env"))

# ── Exchange API Keys (loaded from secrets.env) ──────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

KRAKEN_API_KEY     = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET  = os.getenv("KRAKEN_API_SECRET", "")

# ── Bot Settings ─────────────────────────────────────────────────────────────
MIN_PROFIT_PERCENT = float(os.getenv("MIN_PROFIT_PERCENT", "0.5"))  # Minimum profit % to execute
TRADE_AMOUNT_USDT  = float(os.getenv("TRADE_AMOUNT_USDT", "100"))   # Amount per trade in USDT
DRY_RUN            = os.getenv("DRY_RUN", "true").lower() == "true" # If True, no real orders

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = os.path.join(os.path.dirname(__file__), "../logs/arbitrading.log")
