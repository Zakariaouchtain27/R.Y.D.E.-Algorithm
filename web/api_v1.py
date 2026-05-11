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

The engine runs 5,000 Monte Carlo price paths in a thread pool and fires
a webhook to webhook_url if the decision is STRIKE or PHANTOM_HOLD.
WAIT/IGNORE decisions are silent — no webhook, no action needed.

A background scheduler in app.py re-evaluates all active bookings hourly
using each booking's last stored current_price, so even agencies that
don't push updates will eventually receive decisions.

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
from datetime import datetime
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
# PRISM evaluation (runs in thread pool — CPU-bound Monte Carlo)
# ---------------------------------------------------------------------------

def _run_prism_sync(
    booking: Booking,
    current_price: float,
    seats_remaining: int,
    agency: str,
) -> str:
    """Runs 5,000 Monte Carlo paths. Always called via run_in_executor."""
    snapshot = PriceSnapshot(
        booking_id=booking.booking_id,
        current_price=current_price,
        seats_remaining=seats_remaining,
        snapshot_time=datetime.utcnow(),
        fare_id="agency-provided",
        source="b2b_api",
    )
    try:
        decision = _engine.evaluate(booking, snapshot)
    except Exception as exc:
        log.error("PRISM evaluation failed [%s]: %s", booking.booking_id, exc)
        return "error"

    log.info(
        "PRISM [%s] → %s (score=%.1f, net_savings=$%.2f)",
        booking.booking_id, decision.action.value,
        decision.confidence_score, decision.net_savings,
    )

    _bookings.log_audit(booking.booking_id, agency, "decision", {
        "action":           decision.action.value,
        "confidence_score": decision.confidence_score,
        "net_savings":      round(decision.net_savings, 2),
        "current_price":    current_price,
        "seats_remaining":  seats_remaining,
        "reasoning":        decision.reasoning,
    })

    # Fire webhook only when agency needs to act
    if decision.action in (RYDEAction.STRIKE, RYDEAction.PHANTOM_HOLD):
        _notifier.decision(booking, decision)

    return decision.action.value


async def _trigger_prism(
    booking: Booking,
    current_price: float,
    seats_remaining: int,
    agency: str,
) -> None:
    """Non-blocking: offloads CPU-bound PRISM to thread pool."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor,
        _run_prism_sync,
        booking, current_price, seats_remaining, agency,
    )


async def scan_all_active() -> int:
    """
    Re-evaluate every active B2B booking using its stored current_price.
    Called by the hourly background task in app.py.
    Returns number of bookings evaluated.
    """
    rows = _bookings.get_active()
    b2b  = [b for b in rows if b.metadata.get("source") == "b2b_api_v1"]
    if not b2b:
        log.debug("Background scan: no active B2B bookings.")
        return 0

    log.info("Background PRISM scan: %d active booking(s)", len(b2b))
    count = 0
    for booking in b2b:
        current_price = float(booking.metadata.get("current_price") or 0)
        if current_price <= 0:
            continue  # agency hasn't provided a price yet
        seats  = int(booking.metadata.get("seats_remaining") or 9)
        agency = booking.metadata.get("agency", "unknown")
        try:
            await _trigger_prism(booking, current_price, seats, agency)
            count += 1
        except Exception as exc:
            log.error("Scan failed [%s]: %s", booking.booking_id, exc)

    log.info("Background PRISM scan complete: %d evaluated", count)
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
        "submitted_at":     d.get("_created_at"),
        "updated_at":       d.get("_updated_at"),
        "metadata":         meta,
    }


# ---------------------------------------------------------------------------
# POST /monitor  — submit a booking
# ---------------------------------------------------------------------------

class MonitorRequest(BaseModel):
    origin:           str   = Field(..., min_length=3, max_length=3, description="IATA origin code, e.g. JFK")
    destination:      str   = Field(..., min_length=3, max_length=3, description="IATA destination code, e.g. CDG")
    departure_date:   str   = Field(..., description="YYYY-MM-DD")
    original_price:   float = Field(..., gt=0, description="Price paid at booking time (USD)")
    cancellation_fee: float = Field(..., ge=0, description="Airline cancellation/rebooking fee (USD)")
    current_price:    Optional[float] = Field(
        None, gt=0,
        description="Current market price (USD). If lower than original_price minus cancellation_fee, PRISM evaluates immediately and fires a webhook if it's time to rebook.",
    )
    seats_remaining:  int   = Field(9, ge=0, description="Seats available at current_price. Lower counts increase PRISM urgency score.")
    cabin_class:      Literal["economy", "premium_economy", "business", "first"] = "economy"
    fare_type:        Literal["refundable", "partially_refundable"] = Field(
        "refundable",
        description="Non-refundable fares are rejected at submission time.",
    )
    webhook_url:  Optional[str] = Field(None, description="HTTPS endpoint that receives PRISM decision events")
    reference:    Optional[str] = Field(None, description="Your internal PNR or booking reference")

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
                f"original_price ({self.original_price}). "
                "Rebooking would never produce a saving."
            )
        return self


@router.post("/monitor", status_code=201, summary="Submit booking for PRISM monitoring")
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
    now_iso = datetime.utcnow().isoformat() + "Z"

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
        "cabin_class":      payload.cabin_class,
        "fare_type":        payload.fare_type,
        "departure_date":   payload.departure_date,
        "webhook_url":      payload.webhook_url,
        "reference":        payload.reference,
    })

    # Trigger PRISM immediately if the price is already lower than break-even
    prism_triggered = False
    if payload.current_price is not None:
        net_savings = payload.original_price - payload.current_price - payload.cancellation_fee
        if net_savings > 0:
            asyncio.create_task(_trigger_prism(
                booking, payload.current_price, payload.seats_remaining, agency,
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

@router.get("/bookings", summary="List all monitored bookings")
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

@router.get("/bookings/{tracking_id}", summary="Get booking status")
async def get_booking(
    tracking_id: str,
    auth: tuple = Depends(require_api_key),
):
    agency, _ = auth
    rows  = _bookings.get_by_agency(agency)
    match = next((r for r in rows if r["booking_id"] == tracking_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return _booking_to_response(match)


# ---------------------------------------------------------------------------
# GET /bookings/{id}/audit
# ---------------------------------------------------------------------------

@router.get("/bookings/{tracking_id}/audit", summary="Full audit trail for a booking")
async def get_booking_audit(
    tracking_id: str,
    auth: tuple = Depends(require_api_key),
):
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
        description="New market price observed in your GDS. Triggers PRISM evaluation if net_savings > 0.",
    )
    seats_remaining:  Optional[int]   = Field(None, ge=0)
    webhook_url:      Optional[str]   = None
    cancellation_fee: Optional[float] = Field(None, ge=0)
    cabin_class:      Optional[str]   = None
    passenger:        Optional[dict]  = None


@router.patch("/bookings/{tracking_id}", summary="Push a new price or update booking details")
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
            raise HTTPException(
                status_code=422,
                detail="cancellation_fee must be less than original_price.",
            )
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

    # Run PRISM if a new price was pushed and there are positive savings
    prism_triggered = False
    if payload.current_price is not None:
        net_savings = booking.original_price - payload.current_price - booking.cancellation_fee
        if net_savings > 0:
            seats = (
                payload.seats_remaining
                if payload.seats_remaining is not None
                else int(booking.metadata.get("seats_remaining") or 9)
            )
            asyncio.create_task(_trigger_prism(
                booking, payload.current_price, seats, agency,
            ))
            prism_triggered = True

    return {
        "ok":              True,
        "tracking_id":     tracking_id,
        "updated":         updated_at,
        "prism_triggered": prism_triggered,
    }


# ---------------------------------------------------------------------------
# DELETE /bookings/{id}
# ---------------------------------------------------------------------------

@router.delete("/bookings/{tracking_id}", summary="Stop monitoring a booking")
async def delete_booking(
    tracking_id: str,
    auth: tuple = Depends(require_api_key),
):
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

@router.get("/analytics", summary="Agency savings and usage analytics")
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

@router.get("/account", summary="Agency profile and quota")
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
