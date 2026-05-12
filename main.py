"""
RYDE — Signal API entry point.

Configuration is read from environment variables (see .env.example).

In the Signal API model, agencies push price updates via:
    POST  /api/v1/monitor          — submit a booking for monitoring
    PATCH /api/v1/bookings/{id}    — push a new current_price

PRISM decisions are returned via agency-registered webhook_url.

This file is used only for local development / CLI invocation.
On Railway, use:  uvicorn web.app:app --host 0.0.0.0 --port $PORT
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from ryde.logging_config import setup_logging
setup_logging(os.getenv("LOG_LEVEL", "INFO"))

log = logging.getLogger("ryde.main")


def main() -> None:
    log.info(
        "RYDE Signal API mode. "
        "Run with:  uvicorn web.app:app --host 0.0.0.0 --port 8000"
    )
    log.info(
        "No external adapters required. "
        "Agencies push price updates via POST /api/v1/monitor and "
        "PATCH /api/v1/bookings/{id}."
    )


if __name__ == "__main__":
    main()
