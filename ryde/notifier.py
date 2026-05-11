import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict

import requests

from .models import Booking, RebookingResult, RYDEDecision

log = logging.getLogger(__name__)

_WEBHOOK_SECRET = os.getenv("RYDE_WEBHOOK_SECRET", "")


def _sign(body: str) -> str:
    """Return HMAC-SHA256 signature for the given JSON body string."""
    if not _WEBHOOK_SECRET:
        return ""
    mac = hmac.new(_WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


class Notifier:
    """
    Dispatches signed webhook payloads to the URL registered on each booking.

    Every POST includes:
      X-RYDE-Signature: sha256=<hmac-sha256>

    Agencies verify the signature with:
      expected = hmac.new(RYDE_WEBHOOK_SECRET, request.body, sha256).hexdigest()
      assert request.headers["X-RYDE-Signature"] == f"sha256={expected}"
    """

    TIMEOUT = 10

    def decision(self, booking: Booking, d: RYDEDecision) -> None:
        if not booking.notify_webhook:
            return
        self._post(
            booking.notify_webhook,
            {
                "event":                       "ryde.decision",
                "booking_id":                  booking.booking_id,
                "action":                      d.action,
                "confidence_score":            d.confidence_score,
                "net_savings":                 d.net_savings,
                "probability_of_future_drop":  d.probability_of_future_drop,
                "seat_urgency_multiplier":     d.seat_urgency_multiplier,
                "reasoning":                   d.reasoning,
            },
        )

    def rebooking(self, booking: Booking, result: RebookingResult) -> None:
        if not booking.notify_webhook:
            return
        self._post(
            booking.notify_webhook,
            {
                "event":             "ryde.rebooking",
                "booking_id":        booking.booking_id,
                "success":           result.success,
                "old_ref":           result.old_ref,
                "new_ref":           result.new_ref,
                "savings_realized":  result.savings_realized,
                "error":             result.error,
                "timestamp":         result.timestamp.isoformat(),
            },
        )

    def _post(self, url: str, payload: Dict[str, Any]) -> None:
        # Serialize once so the signature covers the exact bytes we send
        body = json.dumps(payload, default=str)
        headers = {
            "Content-Type": "application/json",
            "User-Agent":   "RYDE-Notifier/1.0",
        }
        sig = _sign(body)
        if sig:
            headers["X-RYDE-Signature"] = sig

        try:
            r = requests.post(url, data=body, headers=headers, timeout=self.TIMEOUT)
            r.raise_for_status()
            log.debug("Webhook delivered to %s (status %s)", url, r.status_code)
        except Exception as exc:
            log.warning("Webhook failed [%s]: %s", url, exc)
