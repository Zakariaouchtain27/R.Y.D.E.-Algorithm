"""
RYDE B2B API v1
===============
All endpoints require:  X-Agency-Key: <agency_key>

How PRISM evaluation works
--------------------------
PRISM (Longstaff-Schwartz Monte Carlo) runs automatically whenever a
current_price is provided:

  POST /monitor           — include current_price to evaluate on submit
  PATCH /bookings/{id}    — push a new current_price to re-evaluate

Duplicate-webhook protection
-----------------------------
Each PRISM run records last_evaluated_price + last_evaluated_at in the
booking metadata. A subsequent PATCH with the same price is silently
skipped unless at least 5 minutes have passed (price unchanged but time
decay could shift the decision). Different price always re-evaluates.

Webhook security
-----------------
Every webhook POST is signed:
  X-RYDE-Signature: sha256=<hmac-sha256-hex>
Verify with: hmac.new(RYDE_WEBHOOK_SECRET, body, sha256).hexdigest()

Time-decay scan frequency
--------------------------
Background scheduler runs every 15 minutes.
Per booking, re-evaluation frequency scales with urgency:
  >= 14 days to departure  →  re-evaluate every 60 minutes
   7–14 days to departure  →  re-evaluate every 30 minutes
    < 7 days to departure  →  re-evaluate every 15 minutes

Idempotency
-----------
POST /monitor accepts an optional  Idempotency-Key: <uuid>  header.
Returns the original 201 response on duplicate keys — safe to retry.

Endpoints
---------
POST   /api/v1/monitor              Submit a booking for PRISM monitoring
GET    /api/v1/bookings             List all bookings for your agency
GET    /api/v1/bookings/{id}        Get a single booking + status
GET    /api/v1/bookings/{id}/audit  Immutable decision + lifecycle trail
PATCH  /api/v1/bookings/{id}        Push new price / update booking details
DELETE /api/v1/bookings/{id}        Stop monitoring a booking
GET    /api/v1/analytics            Agency-level savings + usage stats
GET    /api/v1/account              Your agency profile and quota info
"""
import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from ryde.agency_store import AgencyStore
from ryde.models import Booking, Passenger, PriceSnapshot, RYDEAction
from ryde.notifier import Notifier
from ryde.prism import PRISMEngine
from ryde.store import BookingStore

router = APIRouter(prefix="/api/v1", tags=["B2B API v1"])
log    = logging.getLogger(__name__)

_db_path  = os.getenv("RYDE_DB_PATH", "ryde.db")
_bookings = BookingStore(_db_path)
_agencies = AgencyStore(_db_path)
_engine   = PRISMEngine(_db_path)
_notifier = Notifier()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prism")

# Seconds between re-evaluations per booking based on days to departure.
# Background scan runs every 15 min; this controls per-booking cadence.
_EVAL_INTERVAL = {"far": 3600, "close": 1800, "urgent": 900}
_PATCH_COOLDOWN = 300   # seconds: skip PATCH-triggered eval at same price


def _scan_interval(days: int) -> int:
    """How often (seconds) PRISM should re-evaluate a booking."""
    if days >= 14:
        return _EVAL_INTERVAL["far"]     # 60 min
    if days >= 7:
        return _EVAL_INTERVAL["close"]   # 30 min
    return _EVAL_INTERVAL["urgent"]      # 15 min


# ---------------------------------------------------------------------------
# Rate limiting  (60 requests / 60 seconds per key, in-memory)
# ---------------------------------------------------------------------------

_call_log: dict = defaultdict(list)
_RATE_LIMIT  = 60
_RATE_WINDOW = 60


def _check_rate(api_key: str) -> None:
    now = time.monotonic()
    _call_log[api_key] = [t for t in _call_log[api_key] if now - t < _RATE_WINDOW]
    if len(_call_log[api_key]) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit: {_RATE_LIMIT} requests / {_RATE_WINDOW}s.",
            headers={"Retry-After": "60"},
        )
    _call_log[api_key].append(now)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def require_api_key(
    x_agency_key: Optional[str] = Header(default=None),
    x_api_key:    Optional[str] = Header(default=None),
) -> tuple:
    key = x_agency_key or x_api_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Agency-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    agency = _agencies.get_by_key(key)
    if not agency:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or revoked API key.",
        )
    _check_rate(key)
    _agencies.log_call(key)
    return agency.name, key


# ---------------------------------------------------------------------------
# PRISM evaluation
# ---------------------------------------------------------------------------

def _seconds_since(iso_str: str) -> float:
    """Seconds elapsed since an ISO-8601 UTC timestamp string."""
    try:
        ts = datetime.fromisoformat(iso_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return float("inf")


def _run_prism_sync(
    booking_id: str,
    current_price: float,
    seats_remaining: int,
    agency: str,
) -> str:
    """
    CPU-bound: runs 5,000 Monte Carlo paths. Always called via run_in_executor.
    Re-fetches the booking from DB for freshest data, then writes back
    last_evaluated_price / last_evaluated_at to prevent duplicate webhooks.
    """
    booking = _bookings.get_by_id(booking_id)
    if not booking:
        log.error("PRISM: booking %s not found", booking_id)
        return "error"

    snapshot = PriceSnapshot(
        booking_id=booking_id,
        current_price=current_price,
        seats_remaining=seats_remaining,
        snapshot_time=datetime.utcnow(),
        fare_id="agency-provided",
        source="b2b_api",
    )
    try:
        decision = _engine.evaluate(booking, snapshot)
    except Exception as exc:
        log.error("PRISM evaluation failed [%s]: %s", booking_id, exc)
        return "error"

    log.info(
        "PRISM [%s] → %s (score=%.1f, net_savings=$%.2f)",
        booking_id, decision.action.value,
        decision.confidence_score, decision.net_savings,
    )

    now_iso = datetime.utcnow().isoformat() + "Z"

    _bookings.log_audit(booking_id, agency, "decision", {
        "action":           decision.action.value,
        "confidence_score": decision.confidence_score,
        "net_savings":      round(decision.net_savings, 2),
        "current_price":    current_price,
        "seats_remaining":  seats_remaining,
        "reasoning":        decision.reasoning,
        "evaluated_at":     now_iso,
    })

    # Record evaluation so duplicates are suppressed
    booking.metadata["last_evaluated_price"] = current_price
    booking.metadata["last_evaluated_at"]    = now_iso
    _bookings.upsert(booking)

    # Fire webhook only when agency needs to act
    if decision.action in (RYDEAction.STRIKE, RYDEAction.PHANTOM_HOLD):
        _notifier.decision(booking, decision)

    return decision.action.value


async def _trigger_prism(
    booking_id: str,
    current_price: float,
    seats_remaining: int,
    agency: str,
) -> None:
    """Non-blocking: offloads CPU-bound PRISM to thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor,
        _run_prism_sync,
        booking_id, current_price, seats_remaining, agency,
    )


async def scan_all_active() -> int:
    """
    Re-evaluate active B2B bookings using stored current_price.
    Called by the 15-minute background task in app.py.

    Each booking is evaluated at a frequency determined by its days to
    departure (60 min / 30 min / 15 min). Bookings evaluated too recently
    are skipped to avoid duplicate decision webhooks.

    Returns number of bookings evaluated this pass.
    """
    rows = _bookings.get_active()
    b2b  = [b for b in rows if b.metadata.get("source") == "b2b_api_v1"]
    if not b2b:
        return 0

    log.info("PRISM scan: %d active B2B booking(s) to check", len(b2b))
    count = 0
    now   = datetime.utcnow()

    for booking in b2b:
        current_price = float(booking.metadata.get("current_price") or 0)
        if current_price <= 0:
            continue  # agency hasn't provided a price yet

        days   = max(0, (booking.departure_date.replace(tzinfo=None) - now).days)
        needed = _scan_interval(days)  # how often we should re-evaluate this booking

        last_eval = booking.metadata.get("last_evaluated_at", "")
        if last_eval and _seconds_since(last_eval) < needed:
            continue  # not due yet

        seats  = int(booking.metadata.get("seats_remaining") or 9)
        agency = booking.metadata.get("agency", "unknown")
        try:
            await _trigger_prism(booking.booking_id, current_price, seats, agency)
            count += 1
        except Exception as exc:
            log.error("Scan failed [%s]: %s", booking.booking_id, exc)

    if count:
        log.info("PRISM scan complete: %d evaluated", count)
    return count


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _booking_to_response(d: dict) -> dict:
    meta = d.get("metadata", {})
    return {
        "tracking_id":      d["booking_id"],
        "route":            f"{d['origin']}-{d['destination']}",
        "origin":           d["origin"],
        "destination":      d["destination"],
        "departure_date":   d["departure_date"][:10],
        "original_price":   d["original_price"],
        "current_price":    meta.get("current_price"),
        "cancellation_fee": d["cancellation_fee"],
        "currency":         d["currency"],
        "cabin_class":      d.get("cabin_class", "economy"),
        "fare_type":        meta.get("fare_type", "refundable"),
        "status":           "monitoring" if d.get("_active", True) else "stopped",
        "webhook_url":      d.get("notify_webhook"),
        "passenger_set":    d["passenger"]["given_name"] != "Pending",
        "last_decision":    meta.get("last_evaluated_at"),
        "submitted_at":     d.get("_created_at"),
        "updated_at":       d.get("_updated_at"),
        "metadata":         meta,
    }


# ---------------------------------------------------------------------------
# POST /monitor  — submit a booking
# ---------------------------------------------------------------------------

class MonitorRequest(BaseModel):
    origin:           str   = Field(..., min_length=3, max_length=3)
    destination:      str   = Field(..., min_length=3, max_length=3)
    departure_date:   str   = Field(..., description="YYYY-MM-DD")
    original_price:   float = Field(..., gt=0)
    cancellation_fee: float = Field(..., ge=0)
    current_price:    Optional[float] = Field(
        None, gt=0,
        description="Current market price. If net_savings > 0, PRISM evaluates immediately.",
    )
    seats_remaining:  int   = Field(9, ge=0)
    cabin_class:      Literal["economy", "premium_economy", "business", "first"] = "economy"
    fare_type:        Literal["refundable", "partially_refundable"] = "refundable"
    webhook_url:  Optional[str] = None
    reference:    Optional[str] = None

    @field_validator("origin", "destination")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("departure_date")
    @classmethod
    def _future(cls, v: str) -> str:
        try:
            dep = datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("departure_date must be YYYY-MM-DD")
        if dep.date() <= datetime.now().date():
            raise ValueError("departure_date must be in the future")
        return v

    @model_validator(mode="after")
    def _economics_check(self) -> "MonitorRequest":
        if self.cancellation_fee >= self.original_price:
            raise ValueError(
                f"cancellation_fee ({self.cancellation_fee}) must be less than "
                f"original_price ({self.original_price})."
            )
        return self


@router.post("/monitor", status_code=201)
async def submit_monitor(
    payload: MonitorRequest,
    auth: tuple = Depends(require_api_key),
    idempotency_key: Optional[str] = Header(default=None),
):
    agency, _ = auth

    if idempotency_key:
        cached = _bookings.get_idempotency(idempotency_key)
        if cached:
            return cached["response"]

    tracking_id = f"b2b_{uuid.uuid4().hex[:16]}"
    now_iso     = datetime.utcnow().isoformat() + "Z"

    meta: dict = {
        "source":          "b2b_api_v1",
        "agency":          agency,
        "fare_type":       payload.fare_type,
        "submitted_at":    now_iso,
        "seats_remaining": payload.seats_remaining,
    }
    if payload.current_price is not None:
        meta["current_price"] = payload.current_price

    booking = Booking(
        booking_id=tracking_id,
        passenger=Passenger(
            title="mr", given_name="Pending", family_name="Pending",
            born_on="1990-01-01", gender="m",
            email=f"noreply+{tracking_id}@b2b.ryde.invalid",
            phone="+10000000000",
        ),
        origin=payload.origin,
        destination=payload.destination,
        departure_date=datetime.strptime(payload.departure_date, "%Y-%m-%d"),
        original_price=payload.original_price,
        currency="USD",
        cancellation_fee=payload.cancellation_fee,
        cabin_class=payload.cabin_class,
        adapter="b2b",
        adapter_booking_ref=payload.reference or tracking_id,
        notify_webhook=payload.webhook_url,
        metadata=meta,
    )
    _bookings.upsert(booking)

    _bookings.log_audit(tracking_id, agency, "submitted", {
        "route":            f"{payload.origin}-{payload.destination}",
        "original_price":   payload.original_price,
        "current_price":    payload.current_price,
        "cancellation_fee": payload.cancellation_fee,
        "fare_type":        payload.fare_type,
        "departure_date":   payload.departure_date,
        "webhook_url":      payload.webhook_url,
    })

    prism_triggered = False
    if payload.current_price is not None:
        net_savings = payload.original_price - payload.current_price - payload.cancellation_fee
        if net_savings > 0:
            asyncio.create_task(_trigger_prism(
                tracking_id, payload.current_price, payload.seats_remaining, agency,
            ))
            prism_triggered = True

    response = {
        "tracking_id":      tracking_id,
        "status":           "monitoring",
        "agency":           agency,
        "route":            f"{payload.origin}-{payload.destination}",
        "fare_type":        payload.fare_type,
        "departure_date":   payload.departure_date,
        "original_price":   payload.original_price,
        "current_price":    payload.current_price,
        "cancellation_fee": payload.cancellation_fee,
        "webhook_url":      payload.webhook_url,
        "prism_triggered":  prism_triggered,
        "submitted_at":     now_iso,
    }

    if idempotency_key:
        _bookings.set_idempotency(idempotency_key, tracking_id, response)

    return response


# ---------------------------------------------------------------------------
# GET /bookings
# ---------------------------------------------------------------------------

@router.get("/bookings")
async def list_bookings(
    status_filter: Optional[str] = None,
    auth: tuple = Depends(require_api_key),
):
    agency, _ = auth
    rows = _bookings.get_by_agency(agency)
    if status_filter == "monitoring":
        rows = [r for r in rows if r.get("_active")]
    elif status_filter == "stopped":
        rows = [r for r in rows if not r.get("_active")]
    return {"agency": agency, "count": len(rows), "bookings": [_booking_to_response(r) for r in rows]}


# ---------------------------------------------------------------------------
# GET /bookings/{id}
# ---------------------------------------------------------------------------

@router.get("/bookings/{tracking_id}")
async def get_booking(tracking_id: str, auth: tuple = Depends(require_api_key)):
    agency, _ = auth
    rows  = _bookings.get_by_agency(agency)
    match = next((r for r in rows if r["booking_id"] == tracking_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return _booking_to_response(match)


# ---------------------------------------------------------------------------
# GET /bookings/{id}/audit
# ---------------------------------------------------------------------------

@router.get("/bookings/{tracking_id}/audit")
async def get_booking_audit(tracking_id: str, auth: tuple = Depends(require_api_key)):
    agency, _ = auth
    booking = _bookings.get_by_id(tracking_id)
    if not booking or booking.metadata.get("agency") != agency:
        raise HTTPException(status_code=404, detail="Booking not found.")
    trail = _bookings.get_audit(tracking_id)
    return {"tracking_id": tracking_id, "count": len(trail), "trail": trail}


# ---------------------------------------------------------------------------
# PATCH /bookings/{id}  — push a new price or update booking details
# ---------------------------------------------------------------------------

class PatchBookingRequest(BaseModel):
    current_price:    Optional[float] = Field(
        None, gt=0,
        description=(
            "New market price from your GDS. Triggers PRISM if net_savings > 0. "
            "Silently skipped if same as last evaluated price and < 5 min have passed."
        ),
    )
    seats_remaining:  Optional[int]   = Field(None, ge=0)
    webhook_url:      Optional[str]   = None
    cancellation_fee: Optional[float] = Field(None, ge=0)
    cabin_class:      Optional[str]   = None
    passenger:        Optional[dict]  = None


@router.patch("/bookings/{tracking_id}")
async def patch_booking(
    tracking_id: str,
    payload: PatchBookingRequest,
    auth: tuple = Depends(require_api_key),
):
    agency, _ = auth
    booking = _bookings.get_by_id(tracking_id)
    if not booking or booking.metadata.get("agency") != agency:
        raise HTTPException(status_code=404, detail="Booking not found.")

    changes: dict = {}

    if payload.webhook_url is not None:
        changes["webhook_url"] = payload.webhook_url
        booking.notify_webhook = payload.webhook_url

    if payload.cancellation_fee is not None:
        if payload.cancellation_fee >= booking.original_price:
            raise HTTPException(status_code=422, detail="cancellation_fee must be less than original_price.")
        changes["cancellation_fee"] = payload.cancellation_fee
        booking.cancellation_fee = payload.cancellation_fee

    if payload.cabin_class is not None:
        changes["cabin_class"] = payload.cabin_class
        booking.cabin_class = payload.cabin_class

    if payload.passenger:
        p = payload.passenger
        changes["passenger"] = p
        booking.passenger = Passenger(
            title=p.get("title", booking.passenger.title),
            given_name=p.get("given_name", booking.passenger.given_name),
            family_name=p.get("family_name", booking.passenger.family_name),
            born_on=p.get("born_on", booking.passenger.born_on),
            gender=p.get("gender", booking.passenger.gender),
            email=p.get("email", booking.passenger.email),
            phone=p.get("phone", booking.passenger.phone),
        )

    if payload.current_price is not None:
        changes["current_price"] = payload.current_price
        booking.metadata["current_price"] = payload.current_price

    if payload.seats_remaining is not None:
        changes["seats_remaining"] = payload.seats_remaining
        booking.metadata["seats_remaining"] = payload.seats_remaining

    updated_at = datetime.utcnow().isoformat() + "Z"
    booking.metadata["updated_at"] = updated_at
    _bookings.upsert(booking)
    _bookings.log_audit(tracking_id, agency, "updated", {"changes": changes, "updated_at": updated_at})

    # Trigger PRISM only if net_savings > 0 and not a duplicate call
    prism_triggered = False
    prism_skipped_reason = None
    if payload.current_price is not None:
        net_savings = booking.original_price - payload.current_price - booking.cancellation_fee
        if net_savings <= 0:
            prism_skipped_reason = "no_savings"
        else:
            last_price    = float(booking.metadata.get("last_evaluated_price") or 0)
            last_eval_at  = booking.metadata.get("last_evaluated_at", "")
            same_price    = abs(payload.current_price - last_price) < 0.01
            recent        = last_eval_at and _seconds_since(last_eval_at) < _PATCH_COOLDOWN

            if same_price and recent:
                # Same price, evaluated < 5 min ago — suppress duplicate
                prism_skipped_reason = "duplicate_price_cooldown"
            else:
                seats = (
                    payload.seats_remaining
                    if payload.seats_remaining is not None
                    else int(booking.metadata.get("seats_remaining") or 9)
                )
                asyncio.create_task(_trigger_prism(
                    tracking_id, payload.current_price, seats, agency,
                ))
                prism_triggered = True

    return {
        "ok":                  True,
        "tracking_id":         tracking_id,
        "updated":             updated_at,
        "prism_triggered":     prism_triggered,
        "prism_skipped_reason": prism_skipped_reason,
    }


# ---------------------------------------------------------------------------
# DELETE /bookings/{id}
# ---------------------------------------------------------------------------

@router.delete("/bookings/{tracking_id}")
async def delete_booking(tracking_id: str, auth: tuple = Depends(require_api_key)):
    agency, _ = auth
    booking = _bookings.get_by_id(tracking_id)
    if not booking or booking.metadata.get("agency") != agency:
        raise HTTPException(status_code=404, detail="Booking not found.")
    _bookings.deactivate(tracking_id)
    _bookings.log_audit(tracking_id, agency, "stopped", {"stopped_at": datetime.utcnow().isoformat() + "Z"})
    return {"ok": True, "tracking_id": tracking_id, "status": "stopped"}


# ---------------------------------------------------------------------------
# GET /analytics
# ---------------------------------------------------------------------------

@router.get("/analytics")
async def analytics(auth: tuple = Depends(require_api_key)):
    agency, api_key = auth
    rows    = _bookings.get_by_agency(agency)
    active  = [r for r in rows if r.get("_active")]
    stopped = [r for r in rows if not r.get("_active")]
    savings = _bookings.get_agency_savings(agency)
    ag_obj  = _agencies.get_by_key(api_key)
    return {
        "agency":               agency,
        "total_monitored":      len(rows),
        "currently_monitoring": len(active),
        "stopped":              len(stopped),
        "total_savings_usd":    round(savings, 2),
        "ryde_fees_usd":        round(savings * 0.20, 2),
        "net_savings_usd":      round(savings * 0.80, 2),
        "total_api_calls":      ag_obj.total_calls if ag_obj else 0,
        "last_api_call":        ag_obj.last_call_at if ag_obj else None,
        "rate_limit":           f"{_RATE_LIMIT} requests / {_RATE_WINDOW}s",
    }


# ---------------------------------------------------------------------------
# GET /account
# ---------------------------------------------------------------------------

@router.get("/account")
async def account(auth: tuple = Depends(require_api_key)):
    agency, api_key = auth
    ag = _agencies.get_by_key(api_key)
    return {
        "agency":       ag.name,
        "email":        ag.email,
        "environment":  ag.environment,
        "key_prefix":   api_key[:20] + "...",
        "member_since": ag.created_at,
        "total_calls":  ag.total_calls,
        "last_call":    ag.last_call_at,
        "rate_limit":   f"{_RATE_LIMIT} req / {_RATE_WINDOW}s",
        "endpoints": [
            "POST   /api/v1/monitor",
            "GET    /api/v1/bookings",
            "GET    /api/v1/bookings/{id}",
            "GET    /api/v1/bookings/{id}/audit",
            "PATCH  /api/v1/bookings/{id}",
            "DELETE /api/v1/bookings/{id}",
            "GET    /api/v1/analytics",
            "GET    /api/v1/account",
        ],
    }
