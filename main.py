"""
RYDE Bot — entry point.

Configuration is read entirely from environment variables (see .env.example).
Run with:
    pip install -r requirements.txt
    cp .env.example .env   # fill in your API keys
    python main.py
"""
import logging
import os
import time

from dotenv import load_dotenv

from ryde.adapters.amadeus import AmadeusAdapter
from ryde.adapters.duffel import DuffelAdapter
from ryde.bot import RYDEBot
from ryde.price_monitor import PriceMonitor

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-20s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ryde.main")


def build_adapters() -> dict:
    adapters = {}

    duffel_key = os.getenv("DUFFEL_API_KEY")
    if duffel_key:
        adapters["duffel"] = DuffelAdapter(duffel_key)
        log.info("Duffel adapter loaded (full booking).")

    amadeus_id = os.getenv("AMADEUS_CLIENT_ID")
    amadeus_secret = os.getenv("AMADEUS_CLIENT_SECRET")
    if amadeus_id and amadeus_secret:
        adapters["amadeus"] = AmadeusAdapter(amadeus_id, amadeus_secret)
        log.info("Amadeus adapter loaded (price monitoring).")

    if not adapters:
        raise RuntimeError(
            "No adapters configured. Set DUFFEL_API_KEY or AMADEUS_* environment variables."
        )
    return adapters


def main():
    adapters = build_adapters()

    bot = RYDEBot(
        adapters=adapters,
        db_path=os.getenv("RYDE_DB_PATH", "ryde.db"),
        strike_threshold=float(os.getenv("STRIKE_THRESHOLD", "72")),
        phantom_hold_threshold=float(os.getenv("PHANTOM_HOLD_THRESHOLD", "48")),
    )

    monitor = PriceMonitor(
        bot=bot,
        interval_minutes=int(os.getenv("POLL_INTERVAL_MINUTES", "60")),
    )

    monitor.start()
    log.info("RYDE Bot is running. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()
        log.info("RYDE Bot shut down cleanly.")


if __name__ == "__main__":
    main()
