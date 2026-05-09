import logging
from datetime import datetime
from typing import Dict, Optional

from .adapters.base import BaseAdapter
from .models import Booking, PriceSnapshot, RYDEAction, RebookingResult
from .notifier import Notifier
from .phantom_hold import PhantomHoldManager
from .prism import PRISMEngine
from .prism.price_history import PriceHistory, make_route_key
from .store import BookingStore

log = logging.getLogger(__name__)


class RYDEBot:
    """
    Orchestrator: PRISM engine + adapters + hold manager + store + notifier.

    Usage:
        bot = RYDEBot(adapters={"duffel": DuffelAdapter(key)})
        bot.register(booking)
        # PriceMonitor calls bot.process(booking) on schedule
    """

    def __init__(
        self,
        adapters: Dict[str, BaseAdapter],
        db_path: str = "ryde.db",
    ):
        self.adapters = adapters
        self.engine = PRISMEngine(db_path=db_path)
        self.holds = PhantomHoldManager()
        self.notifier = Notifier()
        self.store = BookingStore(db_path)
        self._history = PriceHistory(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, booking: Booking):
        """Add a booking to the monitoring queue."""
        self.store.upsert(booking)
        log.info("Registered %s for monitoring.", booking.booking_id)

    def process(self, booking: Booking, n_competitors_dropped: int = 0):
        """
        Single evaluation cycle for one booking.
        Called by PriceMonitor on every poll tick.
        """
        adapter = self._get_adapter(booking)
        if adapter is None:
            return

        if self.holds.is_active(booking.booking_id):
            self._handle_hold_cycle(booking, adapter, n_competitors_dropped)
            return

        snapshot = self._fetch_price(booking, adapter)
        if snapshot is None:
            return

        decision = self.engine.evaluate(
            booking, snapshot, n_competitors_dropped=n_competitors_dropped
        )
        log.info(
            "%s → %s (score=%.1f, savings=$%.2f)",
            booking.booking_id, decision.action,
            decision.confidence_score, decision.net_savings,
        )
        self.notifier.decision(booking, decision)

        if decision.action == RYDEAction.STRIKE:
            self._execute_rebooking(booking, snapshot, adapter)
        elif decision.action == RYDEAction.PHANTOM_HOLD:
            hold_ref = None
            try:
                hold_ref = adapter.create_hold(booking, snapshot.fare_id)
            except Exception:
                log.warning("%s: API hold unsupported, using soft hold.", booking.booking_id)
            self.holds.create(
                booking.booking_id,
                snapshot.fare_id,
                snapshot.current_price,
                hold_ref,
            )
            log.info("%s: Phantom hold created (ref: %s).", booking.booking_id, hold_ref)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_hold_cycle(
        self,
        booking: Booking,
        adapter: BaseAdapter,
        n_competitors_dropped: int,
    ):
        hold = self.holds.get(booking.booking_id)
        if hold is None:
            return

        log.info("%s: On phantom hold until %s.", booking.booking_id, hold.expires_at)
        snapshot = self._fetch_price(booking, adapter)
        if snapshot is None:
            return

        decision = self.engine.evaluate(
            booking, snapshot, n_competitors_dropped=n_competitors_dropped
        )

        # Only STRIKE escalates to rebooking; PHANTOM_HOLD keeps the clock
        # ticking; WAIT/IGNORE releases the hold (price moved unfavorably).
        if decision.action == RYDEAction.STRIKE:
            self._execute_rebooking(booking, snapshot, adapter)
        elif decision.action == RYDEAction.PHANTOM_HOLD:
            log.info("%s: Market stable. Continuing phantom hold.", booking.booking_id)
        else:
            self.holds.release(booking.booking_id)
            log.info("%s: Hold released — price moved unfavorably.", booking.booking_id)

    def _execute_rebooking(
        self,
        booking: Booking,
        snapshot: PriceSnapshot,
        adapter: BaseAdapter,
    ) -> RebookingResult:
        result = RebookingResult(
            booking_id=booking.booking_id,
            success=False,
            old_ref=booking.adapter_booking_ref,
            new_ref=None,
            savings_realized=0.0,
            timestamp=datetime.now(),
        )
        try:
            if not adapter.cancel_booking(booking):
                raise RuntimeError("Cancellation returned False.")

            new_ref = adapter.create_booking(booking, snapshot.fare_id)
            result.success = True
            result.new_ref = new_ref
            result.savings_realized = round(
                booking.original_price - snapshot.current_price - booking.cancellation_fee, 2
            )

            route_key = make_route_key(
                booking.origin,
                booking.destination,
                booking.departure_date.strftime("%Y-%m-%d"),
            )
            self._history.record_outcome(
                booking_id=booking.booking_id,
                route_key=route_key,
                original_price=booking.original_price,
                rebooked_price=snapshot.current_price,
                savings=result.savings_realized,
                success=True,
            )

            booking.adapter_booking_ref = new_ref
            booking.original_price = snapshot.current_price
            self.store.upsert(booking)
            self.holds.release(booking.booking_id)

            log.info(
                "REBOOKING SUCCESS %s → %s  saved=$%.2f",
                result.old_ref, new_ref, result.savings_realized,
            )
        except Exception as exc:
            result.error = str(exc)
            log.error("REBOOKING FAILED %s: %s", booking.booking_id, exc)

        self.notifier.rebooking(booking, result)
        return result

    def _fetch_price(
        self, booking: Booking, adapter: BaseAdapter
    ) -> Optional[PriceSnapshot]:
        try:
            return adapter.get_current_price(booking)
        except Exception as exc:
            log.error("Price fetch failed [%s]: %s", booking.booking_id, exc)
            return None

    def _get_adapter(self, booking: Booking) -> Optional[BaseAdapter]:
        adapter = self.adapters.get(booking.adapter)
        if adapter is None:
            log.error("No adapter registered for '%s'.", booking.adapter)
        return adapter
