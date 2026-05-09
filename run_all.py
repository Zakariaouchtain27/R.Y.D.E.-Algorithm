"""
Starts both the RYDE bot and the web server in one command.

    python3 run_all.py

Then open http://localhost:8000 in your browser.
"""
import logging
import os
import threading
import time

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)-20s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ryde.run_all")


def _start_bot():
    from ryde.bot import RYDEBot
    from ryde.price_monitor import PriceMonitor

    adapters = {}

    duffel_key = os.getenv("DUFFEL_API_KEY")
    if duffel_key:
        from ryde.adapters.duffel import DuffelAdapter
        adapters["duffel"] = DuffelAdapter(duffel_key)
        log.info("Duffel adapter loaded.")

    amadeus_id = os.getenv("AMADEUS_CLIENT_ID")
    amadeus_secret = os.getenv("AMADEUS_CLIENT_SECRET")
    if amadeus_id and amadeus_secret:
        from ryde.adapters.amadeus import AmadeusAdapter
        adapters["amadeus"] = AmadeusAdapter(amadeus_id, amadeus_secret)
        log.info("Amadeus adapter loaded.")

    if not adapters:
        log.warning("No adapters configured — bot will idle until API keys are added.")
        return

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
    log.info("RYDE Bot running.")

    while True:
        time.sleep(60)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=_start_bot, daemon=True)
    bot_thread.start()

    log.info("Starting web server at http://localhost:8000")
    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
