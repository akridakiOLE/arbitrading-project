"""
arbitrading - Crypto Arbitrage Bot
Entry point
"""

from loguru import logger
from config.settings import LOG_LEVEL, LOG_FILE, DRY_RUN

# ── Logging Setup ─────────────────────────────────────────────────────────────
logger.add(LOG_FILE, level=LOG_LEVEL, rotation="10 MB", retention="7 days")

def main():
    logger.info("=== arbitrading bot starting ===")

    if DRY_RUN:
        logger.warning("DRY RUN mode active - no real orders will be placed")

    # TODO: Initialize exchange connections
    # TODO: Start price monitoring loop
    # TODO: Execute arbitrage strategy

    logger.info("Bot initialized. Ready.")

if __name__ == "__main__":
    main()
