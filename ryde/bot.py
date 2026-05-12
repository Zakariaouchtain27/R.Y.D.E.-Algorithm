"""
RYDEBot — Signal API orchestrator.

In the Signal API model, agencies push price updates via the B2B API.
RYDEBot no longer requires or calls external adapters (Duffel / Amadeus).

process() is kept for compatibility with any legacy B2C paths but the
primary evaluation pipeline runs through api_v1._run_prism_sync() which
calls PRISMEngine.evaluate() directly with agency-supplied prices.

Hold cycle logic:
    STRIKE       → escalate to decision webhook  (no rebook in Signal mode)
    PHANTOM_HOLD → maintain hold state, do NOT rebook, keep watching
    WAIT / IGNORE → release hold if price moved unfavorably
"""
import logging
from datetime import datetime
from typing import Dict, Optional

from . import events
from .models import Booking, PriceSnapshot, RYDEAction, RYDEDecision
from .notifier import Notifier
from .phantom_hold import PhantomHoldManager
from .prism import PRISMEngine
from .prism.price_history import PriceHistory, make_route_key
from .store import BookingStore

log = logging.getLogger(__name__)


class RYDEBot:
    """
    Orchestrator: PRISM engine + hold manager + store + notifier.

    Signal API model: no external adapters are used.  Price updates arrive
    via agency webhooks (PATCH /api/v1/bookings/{id}) and PRISM decisions
    are returned to the agency via their registered webhook_url.

    Usage:
        bot = RYDEBot(db_path="ryde.db")
        bot.register(booking)
        # PRISMEngine is invoked by api_v1._run_prism_sync on each price push
    """

    def __init__(self, db_path: str = "ryde.db") -> None:
        self.engine   = PRISMEngine(db_path=db_path)
        self.holds    = PhantomHoldManager()
        self.notifier = Notifier()
        self.store    = BookingStore(db_path)
        self._history = PriceHistory(db_path)
        self._last_prices: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, booking: Booking) -> None:
        """Add a booking to the monitoring queue."""
        self.store.upsert(booking)
        log.info("Registered %s for PRISM monitoring.", booking.booking_id)

    def process(self, booking: Booking, snapshot: Optional[PriceSnapshot] = None) -> Optional[RYDEDecision]:
        """
        Single evaluation cycle for one booking.

        In Signal API mode, snapshot must be provided (agency-supplied price).
        Returns None if the booking is on hold and no escalation occurs.
        """
        if snapshot is None:
            log.warning(
                "%s: process() called without snapshot (Signal API mode). "
                "Use api_v1._run_prism_sync for agency-pushed prices.",
                booking.booking_id,
            )
            return None

        if self.holds.is_active(booking.booking_id):
            return self._handle_hold_cycle(booking, snapshot)

        decision = self.engine.evaluate(booking, snapshot)
        log.info(
            "%s → %s (score=%.1f, savings=$%.2f)",
            booking.booking_id, decision.action,
            decision.confidence_score, decision.net_savings,
        )
        self._emit_price_event(booking, snapshot, decision)
        self.notifier.decision(booking, decision)

        if decision.action == RYDEAction.STRIKE:
            # Signal API: notify agency to act — do not self-rebook
            log.info(
                "%s: STRIKE decision — webhook fired. Agency must execute rebook.",
                booking.booking_id,
            )
        elif decision.action == RYDEAction.PHANTOM_HOLD:
            self.holds.create(
                booking.booking_id,
                snapshot.fare_id,
                snapshot.current_price,
                hold_ref=None,  # no external hold in Signal API mode
            )
            log.info("%s: Phantom hold created.", booking.booking_id)

        return decision

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _emit_price_event(
        self,
        booking: Booking,
        snapshot: PriceSnapshot,
        decision: RYDEDecision,
    ) -> None:
        """Publish a ticker event whenever the observed price moves."""
        old_price = self._last_prices.get(booking.booking_id)
        new_price = float(snapshot.current_price)

        if old_price is not None and abs(old_price - new_price) < 1e-6:
            return

        events.publish({
            "booking_id": booking.booking_id,
            "route": f"{booking.origin}-{booking.destination}",
            "old_price": round(old_price, 2) if old_price is not None else None,
            "new_price": round(new_price, 2),
            "action": decision.action.value,
            "score": round(decision.confidence_score, 1),
            "savings": round(decision.net_savings, 2),
            "seats_remaining": snapshot.seats_remaining,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        })
        self._last_prices[booking.booking_id] = new_price

    def _handle_hold_cycle(
        self,
        booking: Booking,
        snapshot: PriceSnapshot,
    ) -> Optional[RYDEDecision]:
        """
        Re-evaluate a booking that is currently on phantom hold.

        Decision matrix:
          STRIKE       → notify agency (webhook); release hold
          PHANTOM_HOLD → maintain hold state; do NOT trigger a rebook
          WAIT / IGNORE → release hold (price moved unfavorably)
        """
        hold = self.holds.get(booking.booking_id)
        if hold is None:
            return None

        log.info("%s: On phantom hold until %s.", booking.booking_id, hold.expires_at)
        decision = self.engine.evaluate(booking, snapshot)
        self._emit_price_event(booking, snapshot, decision)

        if decision.action == RYDEAction.STRIKE:
            # Notify agency — do not self-rebook in Signal API mode
            self.notifier.decision(booking, decision)
            self.holds.release(booking.booking_id)
            log.info(
                "%s: STRIKE during hold — agency notified; hold released.",
                booking.booking_id,
            )
        elif decision.action == RYDEAction.PHANTOM_HOLD:
            # Hold remains active — market stable, keep watching
            log.info("%s: Market stable. Maintaining phantom hold.", booking.booking_id)
        else:
            # WAIT / IGNORE — conditions worsened; release hold
            self.holds.release(booking.booking_id)
            log.info("%s: Hold released — price moved unfavorably.", booking.booking_id)

        return decision
