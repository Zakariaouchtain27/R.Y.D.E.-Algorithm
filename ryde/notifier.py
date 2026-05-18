"""
Notifier — dispatches STRIKE alerts to all configured channels.

Channels (all optional, skip gracefully when unconfigured):
  Webhook   — HMAC-signed HTTP POST to booking.notify_webhook
  Slack     — Incoming Webhook URL (per-agency)
  Email     — SendGrid (SENDGRID_API_KEY env + per-agency to-address)
  WhatsApp  — Twilio WA Business API (TWILIO_* env + per-agency to-number)
  Telegram  — Bot API (TELEGRAM_BOT_TOKEN env + per-agency chat_id)

STRIKE message format:
  🚨 RYDE STRIKE — LHR → JFK
  Current fare: $487 (was $634)
  Net savings after rebook: $132 (confidence 87%)
  Rebook now — 3 seats remaining at this price.
"""
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

import requests

from .models import Booking, RebookingResult, RYDEDecision

log = logging.getLogger(__name__)

_WEBHOOK_SECRET = os.getenv("RYDE_WEBHOOK_SECRET", "")

# ---------------------------------------------------------------------------
# SendGrid global config
# ---------------------------------------------------------------------------
_SENDGRID_KEY  = os.getenv("SENDGRID_API_KEY", "")
_FROM_EMAIL    = os.getenv("RYDE_FROM_EMAIL", "strikes@ryde.io")
_FROM_NAME     = os.getenv("RYDE_FROM_NAME", "RYDE PRISM")

# ---------------------------------------------------------------------------
# Twilio WA global config
# ---------------------------------------------------------------------------
_TWILIO_SID    = os.getenv("TWILIO_ACCOUNT_SID", "")
_TWILIO_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "")
_TWILIO_WA_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")  # Twilio sandbox default

# ---------------------------------------------------------------------------
# Telegram global config
# ---------------------------------------------------------------------------
_TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


def _sign(body: str) -> str:
    if not _WEBHOOK_SECRET:
        return ""
    mac = hmac.new(_WEBHOOK_SECRET.encode(), body.encode(), hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _strike_text(booking: Booking, d: RYDEDecision) -> str:
    """Plain-text STRIKE alert used by WA, Telegram, and Slack fallback text."""
    route = f"{booking.origin} → {booking.destination}"
    curr  = booking.metadata.get("current_price") or "?"
    seats = booking.metadata.get("seats_remaining", "")
    seat_str = f"  {seats} seats at this price." if seats else ""
    lines = [
        f"🚨 RYDE STRIKE — {route}",
        f"Current fare:  ${curr}  (was ${booking.original_price:.2f})",
        f"Net savings:   ${d.net_savings:.2f}  (confidence {d.confidence_score:.0f}%)",
        f"Cancel fee:    ${booking.cancellation_fee:.2f}",
        f"Action:        REBOOK NOW — cancel original, book current fare.{seat_str}",
        f"Booking ID:    {booking.booking_id}",
    ]
    return "\n".join(lines)


class Notifier:
    TIMEOUT = 10

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def decision(
        self,
        booking: Booking,
        d: RYDEDecision,
        notification_config: Optional[dict] = None,
    ) -> None:
        """Fire all configured channels for a PRISM decision."""
        # Always try the booking-level webhook first
        if booking.notify_webhook:
            self._post_webhook(
                booking.notify_webhook,
                {
                    "event":                      "ryde.decision",
                    "booking_id":                 booking.booking_id,
                    "action":                     d.action,
                    "confidence_score":           d.confidence_score,
                    "net_savings":                d.net_savings,
                    "probability_of_future_drop": d.probability_of_future_drop,
                    "seat_urgency_multiplier":    d.seat_urgency_multiplier,
                    "reasoning":                  d.reasoning,
                },
            )

        # Extra channels only for STRIKE / PHANTOM_HOLD decisions
        from .models import RYDEAction
        if d.action not in (RYDEAction.STRIKE, RYDEAction.PHANTOM_HOLD):
            return

        cfg = notification_config or {}
        text = _strike_text(booking, d)

        if cfg.get("slack_webhook"):
            self._send_slack(cfg["slack_webhook"], booking, d, text)

        if cfg.get("email"):
            self._send_email(cfg["email"], booking, d, text)

        if cfg.get("whatsapp_to"):
            self._send_whatsapp(cfg["whatsapp_to"], text)

        if cfg.get("telegram_chat_id"):
            self._send_telegram(cfg["telegram_chat_id"], text)

    def rebooking(self, booking: Booking, result: RebookingResult) -> None:
        if not booking.notify_webhook:
            return
        self._post_webhook(
            booking.notify_webhook,
            {
                "event":            "ryde.rebooking",
                "booking_id":       booking.booking_id,
                "success":          result.success,
                "old_ref":          result.old_ref,
                "new_ref":          result.new_ref,
                "savings_realized": result.savings_realized,
                "error":            result.error,
                "timestamp":        result.timestamp.isoformat(),
            },
        )

    def billing_error(self, booking: Booking, fee_usd: float, error: str) -> None:
        if not booking.notify_webhook:
            return
        self._post_webhook(
            booking.notify_webhook,
            {
                "event":      "ryde.billing_error",
                "booking_id": booking.booking_id,
                "fee_usd":    round(fee_usd, 2),
                "error":      error,
            },
        )

    # ------------------------------------------------------------------
    # Slack
    # ------------------------------------------------------------------

    def _send_slack(self, webhook_url: str, booking: Booking, d: RYDEDecision, fallback: str) -> None:
        route = f"{booking.origin} → {booking.destination}"
        curr  = booking.metadata.get("current_price") or "?"
        seats = booking.metadata.get("seats_remaining", "")
        color = "#10b981" if str(d.action) in ("RYDEAction.STRIKE", "STRIKE") else "#f59e0b"

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*🚨 RYDE STRIKE — {route}*",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Current fare*\n${curr}"},
                    {"type": "mrkdwn", "text": f"*Original fare*\n${booking.original_price:.2f}"},
                    {"type": "mrkdwn", "text": f"*Net savings*\n${d.net_savings:.2f}"},
                    {"type": "mrkdwn", "text": f"*Confidence*\n{d.confidence_score:.0f}%"},
                    {"type": "mrkdwn", "text": f"*Cancel fee*\n${booking.cancellation_fee:.2f}"},
                    {"type": "mrkdwn", "text": f"*Seats left*\n{seats or '—'}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Action:* Cancel original booking and rebook at ${curr}."},
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Booking `{booking.booking_id}`"}],
            },
        ]
        payload = {"text": fallback, "attachments": [{"color": color, "blocks": blocks}]}
        try:
            r = requests.post(webhook_url, json=payload, timeout=self.TIMEOUT)
            r.raise_for_status()
            log.info("Slack STRIKE alert sent for %s", booking.booking_id)
        except Exception as exc:
            log.warning("Slack notification failed [%s]: %s", booking.booking_id, exc)

    # ------------------------------------------------------------------
    # Email (SendGrid)
    # ------------------------------------------------------------------

    def _send_email(self, to_email: str, booking: Booking, d: RYDEDecision, text: str) -> None:
        if not _SENDGRID_KEY:
            log.debug("SENDGRID_API_KEY not set — skipping email notification")
            return
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail, Content

            route = f"{booking.origin} → {booking.destination}"
            curr  = booking.metadata.get("current_price") or "?"
            seats = booking.metadata.get("seats_remaining", "")
            seat_str = f"<p><b>Seats at this price:</b> {seats}</p>" if seats else ""

            html = f"""
<h2 style="color:#10b981">🚨 RYDE STRIKE &mdash; {route}</h2>
<table style="font-family:monospace;font-size:14px;border-collapse:collapse">
  <tr><td style="padding:4px 16px 4px 0"><b>Current fare</b></td><td>${curr}</td></tr>
  <tr><td style="padding:4px 16px 4px 0"><b>Original fare</b></td><td>${booking.original_price:.2f}</td></tr>
  <tr><td style="padding:4px 16px 4px 0"><b>Net savings</b></td><td>${d.net_savings:.2f}</td></tr>
  <tr><td style="padding:4px 16px 4px 0"><b>Cancel fee</b></td><td>${booking.cancellation_fee:.2f}</td></tr>
  <tr><td style="padding:4px 16px 4px 0"><b>Confidence</b></td><td>{d.confidence_score:.0f}%</td></tr>
  {seat_str}
</table>
<p style="color:#ef4444"><b>Action: Cancel your original booking and rebook at ${curr} immediately.</b></p>
<p style="color:#6b7280;font-size:12px">Booking ID: {booking.booking_id}</p>
<hr/>
<p style="font-size:11px;color:#9ca3af">Powered by RYDE PRISM &mdash; reply to this email to contact support.</p>
"""
            message = Mail(
                from_email=(_FROM_EMAIL, _FROM_NAME),
                to_emails=to_email,
                subject=f"RYDE STRIKE — {route} — Save ${d.net_savings:.2f}",
                html_content=html,
            )
            message.plain_text_content = Content("text/plain", text)
            sg = SendGridAPIClient(_SENDGRID_KEY)
            sg.send(message)
            log.info("Email STRIKE alert sent to %s for %s", to_email, booking.booking_id)
        except Exception as exc:
            log.warning("Email notification failed [%s]: %s", booking.booking_id, exc)

    # ------------------------------------------------------------------
    # WhatsApp (Twilio)
    # ------------------------------------------------------------------

    def _send_whatsapp(self, to_number: str, text: str) -> None:
        if not (_TWILIO_SID and _TWILIO_TOKEN):
            log.debug("TWILIO_* env vars not set — skipping WhatsApp notification")
            return
        try:
            from twilio.rest import Client
            client = Client(_TWILIO_SID, _TWILIO_TOKEN)
            to_wa  = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"
            client.messages.create(
                from_=_TWILIO_WA_FROM,
                to=to_wa,
                body=text,
            )
            log.info("WhatsApp STRIKE alert sent to %s", to_number)
        except Exception as exc:
            log.warning("WhatsApp notification failed [%s]: %s", to_number, exc)

    # ------------------------------------------------------------------
    # Telegram
    # ------------------------------------------------------------------

    def _send_telegram(self, chat_id: str, text: str) -> None:
        if not _TELEGRAM_TOKEN:
            log.debug("TELEGRAM_BOT_TOKEN not set — skipping Telegram notification")
            return
        try:
            url = f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage"
            r = requests.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
            }, timeout=self.TIMEOUT)
            r.raise_for_status()
            log.info("Telegram STRIKE alert sent to chat %s", chat_id)
        except Exception as exc:
            log.warning("Telegram notification failed [chat=%s]: %s", chat_id, exc)

    # ------------------------------------------------------------------
    # Generic signed webhook
    # ------------------------------------------------------------------

    def _post_webhook(self, url: str, payload: Dict[str, Any]) -> None:
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
