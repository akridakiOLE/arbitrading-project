"""
Arbitrading Bot — Global Settings
Φορτώνει τα API keys από config/secrets.env (ΠΟΤΕ στο GitHub)
Έκδοση: v1 | Master_arbitrading-project_v1.md
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "secrets.env"))

# ── API KEYS (από secrets.env) ────────────────────────────────────────────────
KUCOIN_API_KEY        = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET     = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")  # KuCoin απαιτεί passphrase

BINANCE_API_KEY       = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET    = os.getenv("BINANCE_API_SECRET", "")

# ── TRADING PAIR SETTINGS ─────────────────────────────────────────────────────
TRADING_PAIR     = os.getenv("TRADING_PAIR", "SOL/USDT")   # Ζεύγος συναλλαγών
PROFIT_COIN      = os.getenv("PROFIT_COIN", "USDT")        # Νόμισμα κέρδους
ACCOUNT_TYPE     = os.getenv("ACCOUNT_TYPE", "margin")     # spot / margin / futures

# ── BOT CORE VARIABLES ────────────────────────────────────────────────────────
# Αυτές εφαρμόζονται ΜΕΤΑ την ολοκλήρωση κύκλου
START_BASE_COIN  = float(os.getenv("START_BASE_COIN", "10"))   # Αρχικό υπόλοιπο BASE_COIN
SCALE_BASE_COIN  = float(os.getenv("SCALE_BASE_COIN", "4"))    # Πολλαπλασιαστής αγοράς
RATIO_SCALE      = float(os.getenv("RATIO_SCALE", "0.005"))    # Buffer % δανεισμού USDT (0.5%)
BORROW_BASE_COIN = float(os.getenv("BORROW_BASE_COIN", "1"))   # Συντελεστής δανεισμού BASE_COIN (= TOTAL)

# ── BOT PROFIT VARIABLES (*) ──────────────────────────────────────────────────
# (*) Εφαρμόζονται ΑΜΕΣΑ, ακόμα και εντός κύκλου
MIN_PROFIT_PERCENT = float(os.getenv("MIN_PROFIT_PERCENT", "10"))  # % ενεργοποίησης (v4: 10%)
STEP_POINT         = float(os.getenv("STEP_POINT", "0.5"))         # % μετακίνησης activation
TRAILING_STOP      = float(os.getenv("TRAILING_STOP", "2"))        # % trailing stop
LIMIT_ORDER        = float(os.getenv("LIMIT_ORDER", "0.5"))        # % buffer limit order

# ── SAFETY ────────────────────────────────────────────────────────────────────
MARGIN_LEVEL = float(os.getenv("MARGIN_LEVEL", "1.07"))  # Ratio trigger προστασίας λογαριασμού

# ── MODE ──────────────────────────────────────────────────────────────────────
# live       → πραγματικές εντολές με API keys
# paper      → live τιμές, virtual balance, μηδέν πραγματικές εντολές
# backtest   → ιστορικά OHLCV δεδομένα, simulation
BOT_MODE = os.getenv("BOT_MODE", "paper")

# ── LOGGING ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE  = os.path.join(os.path.dirname(__file__), "../logs/arbitrading.log")

# ── BACKTESTER ────────────────────────────────────────────────────────────────
BACKTEST_SYMBOL    = os.getenv("BACKTEST_SYMBOL", "SOL/USDT")
BACKTEST_TIMEFRAME = os.getenv("BACKTEST_TIMEFRAME", "1m")      # 1-λεπτό candles
BACKTEST_START     = os.getenv("BACKTEST_START", "2024-01-01")  # Ημερομηνία έναρξης
BACKTEST_END       = os.getenv("BACKTEST_END", "2024-03-01")    # Ημερομηνία λήξης
