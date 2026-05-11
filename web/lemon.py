"""
LemonSqueezy integration.

Flow:
  1. Agency visits /pricing and clicks a plan
  2. LemonSqueezy hosted checkout collects payment
  3. LS fires POST /webhook/lemonsqueeze  (subscription_created)
  4. We auto-create the agency + generate API key
  5. LS redirects buyer to /welcome?order={ls_order_id}
  6. Welcome page polls /api/welcome-status until key is ready, shows it once
  7. On subscription_cancelled / subscription_expired → key revoked
  8. On subscription_resumed → key reactivated
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
_db_path = os.getenv("RYDE_DB_PATH", "ryde.db")
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

    log.info("LemonSqueezy event", extra={"event": event})

    if event == "subscription_created":
        _on_subscription_created(data, attrs)
    elif event in ("subscription_cancelled", "subscription_expired"):
        _on_subscription_cancelled(attrs)
    elif event == "subscription_resumed":
        _on_subscription_resumed(attrs)

    return {"ok": True}


@router.get("/api/welcome-status")
async def welcome_status(order_id: str):
    """Polled by the welcome page until the webhook has created the agency."""
    agency = _agencies.get_by_ls_order(order_id)
    if not agency:
        return JSONResponse({"ready": False})
    return JSONResponse({
        "ready":   True,
        "agency":  agency.name,
        "email":   agency.email,
        "key":     agency.api_key,
        "env":     agency.environment,
    })


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _on_subscription_created(data: dict, attrs: dict) -> None:
    ls_subscription_id = str(data.get("id", ""))
    ls_order_id        = str(attrs.get("order_id", ""))
    customer_name      = attrs.get("user_name") or "Agency"
    customer_email     = attrs.get("user_email", "")
    variant_name       = (attrs.get("variant_name") or "").lower()

    # Treat any paid plan as "live"; test/sandbox variants stay "test"
    environment = "test" if "test" in variant_name or "sandbox" in variant_name else "live"

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


def _on_subscription_cancelled(attrs: dict) -> None:
    ls_sub_id = str(attrs.get("id", ""))
    agency = _agencies.get_by_ls_subscription(ls_sub_id)
    if agency:
        _agencies.revoke(agency.id)
        log.info("Agency revoked (subscription cancelled)", extra={"agency": agency.name})


def _on_subscription_resumed(attrs: dict) -> None:
    ls_sub_id = str(attrs.get("id", ""))
    agency = _agencies.get_by_ls_subscription(ls_sub_id)
    if agency:
        _agencies.reactivate(agency.id)
        log.info("Agency reactivated (subscription resumed)", extra={"agency": agency.name})
