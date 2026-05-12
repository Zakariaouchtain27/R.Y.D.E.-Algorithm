"""
PriceMonitor — Signal API model (time-decay only).

IMPORTANT: In the Signal API architecture, agencies PUSH price updates via
the B2B API endpoints (POST /monitor, PATCH /bookings/{id}).  We do NOT poll
external airline APIs (Duffel / Amadeus).

The PriceMonitor's only job here is to provide a clean start/stop lifecycle
wrapper that the lifespan context manager in app.py can control.  The actual
PRISM time-decay re-evaluations run in the async _prism_background_scan()
coroutine (api_v1.scan_all_active), which respects per-booking cadence:

    >= 14 days to departure  →  re-evaluate every 60 min
     7–14 days to departure  →  re-evaluate every 30 min
      < 7 days to departure  →  re-evaluate every 15 min

The background scanner (app.py) polls every PRISM_SCAN_INTERVAL_SECONDS
(default 15 min) and scan_all_active() skips bookings that are not yet due
for their cadence tier.
"""
import logging
import threading

from .store import BookingStore

log = logging.getLogger(__name__)


class PriceMonitor:
    """
    Lightweight lifecycle manager for the Signal API time-decay scanner.

    No external API calls are made here.  This class exists solely so that
    the lifespan context manager has a clear start() / stop() interface and
    can log scanner state for the /health endpoint.

    Parameters
    ----------
    store : BookingStore
        Read-only reference used only for logging active-booking counts.
    scan_interval : int
        Interval in seconds between background scans (default 900 = 15 min).
        Stored for health-check reporting; actual cadence is enforced in
        api_v1.scan_all_active().
    """

    def __init__(self, store: BookingStore, scan_interval: int = 900) -> None:
        self.store = store
        self.scan_interval = scan_interval
        self._running = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Mark the monitor as active and log startup state."""
        if self._running:
            log.warning("PriceMonitor.start() called but monitor is already running.")
            return
        self._running = True
        active_count = 0
        try:
            active_count = len(self.store.get_active())
        except Exception:
            pass
        log.info(
            "PriceMonitor started (Signal API mode) — scan_interval=%ds, "
            "active_bookings=%d. Time-decay evaluations via async background task.",
            self.scan_interval,
            active_count,
        )

    def stop(self) -> None:
        """Signal shutdown."""
        if not self._running:
            return
        self._running = False
        log.info("PriceMonitor stopped.")

    # ------------------------------------------------------------------
    # Status helpers
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running
