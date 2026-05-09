import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)


class PriceMonitor:
    """
    Background scheduler that drives the bot's polling loop.

    Default interval: 60 minutes.
    The first poll fires immediately on start() so you get instant feedback.

    Scheduling strategy:
      - Most routes: 60-minute interval is sufficient.
      - Hot routes (high volatility_index): reduce to 15-30 minutes.
      - Under 14 days to departure: increase to every 15 minutes
        (last-minute inventory changes fast).

    In production, replace APScheduler with Celery Beat + Redis
    for distributed, crash-resilient scheduling.
    """

    def __init__(self, bot, interval_minutes: int = 60):
        self.bot = bot
        self.interval = interval_minutes
        self._scheduler = BackgroundScheduler()

    def start(self):
        self._scheduler.add_job(
            self._poll_all,
            trigger=IntervalTrigger(minutes=self.interval),
            id="ryde_poll",
            replace_existing=True,
            next_run_time=datetime.now(),  # fire immediately
        )
        self._scheduler.start()
        log.info("RYDE PriceMonitor started — polling every %d min.", self.interval)

    def stop(self):
        self._scheduler.shutdown(wait=False)
        log.info("RYDE PriceMonitor stopped.")

    def _poll_all(self):
        bookings = self.bot.store.get_active()
        if not bookings:
            log.debug("No active bookings to poll.")
            return

        log.info("Polling %d booking(s)...", len(bookings))
        for booking in bookings:
            try:
                self.bot.process(booking)
            except Exception as exc:
                log.error("Unhandled error [%s]: %s", booking.booking_id, exc)
