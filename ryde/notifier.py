import logging
from typing import Any, Dict

import requests

from .models import Booking, RebookingResult, RYDEDecision

log = logging.getLogger(__name__)


class Notifier:
    """
    Dispatches webhook payloads to the URL registered on each booking.

    Extend this class to add email (SendGrid), SMS (Twilio),
    or push (Firebase) notifications alongside webhooks.
    """

    TIMEOUT = 10  # seconds

    def decision(self, booking: Booking, d: RYDEDecision):
        if not booking.notify_webhook:
            return
        self._post(
            booking.notify_webhook,
            {
                "event": "ryde.decision",
                "booking_id": booking.booking_id,
                "action": d.action,
                "confidence_score": d.confidence_score,
                "net_savings": d.net_savings,
                "probability_of_future_drop": d.probability_of_future_drop,
                "seat_urgency_multiplier": d.seat_urgency_multiplier,
                "reasoning": d.reasoning,
            },
        )

    def rebooking(self, booking: Booking, result: RebookingResult):
        if not booking.notify_webhook:
            return
        self._post(
            booking.notify_webhook,
            {
                "event": "ryde.rebooking",
                "booking_id": booking.booking_id,
                "success": result.success,
                "old_ref": result.old_ref,
                "new_ref": result.new_ref,
                "savings_realized": result.savings_realized,
                "error": result.error,
                "timestamp": result.timestamp.isoformat(),
            },
        )

    def _post(self, url: str, payload: Dict[str, Any]):
        try:
            r = requests.post(url, json=payload, timeout=self.TIMEOUT)
            r.raise_for_status()
            log.debug("Webhook delivered to %s", url)
        except Exception as exc:
            log.warning("Webhook failed [%s]: %s", url, exc)
