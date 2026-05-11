"""
LemonSqueezy integration.

Subscription lifecycle → API key lifecycle:

  subscription_created          →  create agency + generate API key
  subscription_payment_failed   →  suspend key immediately
  subscription_payment_recovery →  reactivate key (payment recovered)
  subscription_cancelled        →  revoke key
  subscription_expired          →  revoke key
  subscription_resumed          →  reactivate key

Flow after checkout:
  1. Agency pays on LemonSqueezy hosted checkout
  2. LS fires POST /webhook/lemonsqueeze (subscription_created)
  3. We auto-create agency + API key in DB
  4. LS redirects buyer to /welcome?order={ls_order_id}
  5. Welcome page polls /api/welcome-status, shows key once
"""
import hashlib
import hmac
import json
import logging
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ryde.agency_store import AgencyStore

log = logging.getLogger(__name__)
router = APIRouter(tags=["LemonSqueezy"])

_LS_WEBHOOK_SECRET = os.getenv("LEMONSQUEEZE_WEBHOOK_SECRET", "")
_db_path  = os.getenv("RYDE_DB_PATH", "ryde.db")
_agencies = AgencyStore(_db_path)


def _verify_ls_signature(body: bytes, signature: str) -> bool:
    if not _LS_WEBHOOK_SECRET:
        log.warning("LEMONSQUEEZE_WEBHOOK_SECRET not set — skipping signature check")
        return True
    expected = hmac.new(_LS_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhook/lemonsqueeze")
async def ls_webhook(request: Request):
    body = await request.body()
    sig  = request.headers.get("X-Signature", "")

    if not _verify_ls_signature(body, sig):
        log.warning("LemonSqueezy webhook signature mismatch")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event = payload.get("meta", {}).get("event_name", "")
    data  = payload.get("data", {})
    attrs = data.get("attributes", {})

    # Subscription ID lives at data["id"], not inside attributes
    ls_subscription_id = str(data.get("id", ""))

    log.info("LemonSqueezy event received", extra={"event": event, "ls_sub": ls_subscription_id})

    if event == "subscription_created":
        _on_subscription_created(ls_subscription_id, attrs)

    elif event == "subscription_payment_failed":
        # Card declined on renewal — suspend key immediately.
        # LS will retry; if recovered, subscription_payment_recovery fires.
        _on_payment_failed(ls_subscription_id)

    elif event == "subscription_payment_recovery":
        # Retry succeeded — reactivate the key.
        _on_payment_recovered(ls_subscription_id)

    elif event in ("subscription_cancelled", "subscription_expired"):
        _on_subscription_cancelled(ls_subscription_id)

    elif event == "subscription_resumed":
        _on_subscription_resumed(ls_subscription_id)

    return {"ok": True}


@router.get("/api/welcome-status")
async def welcome_status(order_id: str):
    """Polled by the welcome page until the webhook has created the agency."""
    agency = _agencies.get_by_ls_order(order_id)
    if not agency:
        return JSONResponse({"ready": False})
    return JSONResponse({
        "ready":  True,
        "agency": agency.name,
        "email":  agency.email,
        "key":    agency.api_key,
        "env":    agency.environment,
    })


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _on_subscription_created(ls_subscription_id: str, attrs: dict) -> None:
    ls_order_id    = str(attrs.get("order_id", ""))
    customer_name  = attrs.get("user_name") or "Agency"
    customer_email = attrs.get("user_email", "")
    variant_name   = (attrs.get("variant_name") or "").lower()
    environment    = "test" if "test" in variant_name or "sandbox" in variant_name else "live"

    agency = _agencies.create_agency_ls(
        name=customer_name,
        email=customer_email,
        environment=environment,
        ls_subscription_id=ls_subscription_id,
        ls_order_id=ls_order_id,
    )
    log.info(
        "Agency created via LemonSqueezy",
        extra={"agency": agency.name, "email": agency.email, "ls_sub": ls_subscription_id},
    )


def _on_payment_failed(ls_subscription_id: str) -> None:
    """Renewal payment failed — suspend key until payment is recovered."""
    agency = _agencies.get_by_ls_subscription(ls_subscription_id)
    if agency:
        _agencies.revoke(agency.id)
        log.info(
            "Agency key suspended (payment failed)",
            extra={"agency": agency.name, "ls_sub": ls_subscription_id},
        )


def _on_payment_recovered(ls_subscription_id: str) -> None:
    """Retry payment succeeded — restore key access."""
    agency = _agencies.get_by_ls_subscription(ls_subscription_id)
    if agency:
        _agencies.reactivate(agency.id)
        log.info(
            "Agency key reactivated (payment recovered)",
            extra={"agency": agency.name, "ls_sub": ls_subscription_id},
        )


def _on_subscription_cancelled(ls_subscription_id: str) -> None:
    agency = _agencies.get_by_ls_subscription(ls_subscription_id)
    if agency:
        _agencies.revoke(agency.id)
        log.info(
            "Agency key revoked (subscription cancelled/expired)",
            extra={"agency": agency.name, "ls_sub": ls_subscription_id},
        )


def _on_subscription_resumed(ls_subscription_id: str) -> None:
    agency = _agencies.get_by_ls_subscription(ls_subscription_id)
    if agency:
        _agencies.reactivate(agency.id)
        log.info(
            "Agency key reactivated (subscription resumed)",
            extra={"agency": agency.name, "ls_sub": ls_subscription_id},
        )
