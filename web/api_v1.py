"""
RYDE B2B API v1
===============
All endpoints require:  X-API-Key: <agency_key>

Idempotency
-----------
POST /api/v1/monitor accepts an optional  Idempotency-Key: <uuid>  header.
Sending the same key a second time returns the original 201 response from
cache — no duplicate booking is created.  Safe to retry on network errors.

Endpoints
---------
POST   /api/v1/monitor              Submit a booking for PRISM monitoring
GET    /api/v1/bookings             List all bookings for your agency
GET    /api/v1/bookings/{id}        Get a single booking + status
GET    /api/v1/bookings/{id}/audit  Immutable decision + lifecycle trail
PATCH  /api/v1/bookings/{id}        Update webhook URL, passenger, or fee
DELETE /api/v1/bookings/{id}        Stop monitoring a booking
GET    /api/v1/analytics            Agency-level savings + usage stats
GET    /api/v1/account              Your agency profile and quota info
"""
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from ryde.agency_store import AgencyStore
from ryde.models import Booking, Passenger
from ryde.store import BookingStore

router = APIRouter(prefix="/api/v1", tags=["B2B API v1"])

_db_path  = os.getenv("RYDE_DB_PATH", "ryde.db")
_bookings = BookingStore(_db_path)
_agencies = AgencyStore(_db_path)

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
    x_api_key: Optional[str] = Header(default=None),
) -> tuple:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    agency = _agencies.get_by_key(x_api_key)
    if not agency:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or revoked API key.",
        )
    _check_rate(x_api_key)
    _agencies.log_call(x_api_key)
    return agency.name, x_api_key


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _booking_to_response(d: dict) -> dict:
    return {
        "tracking_id":      d["booking_id"],
        "route":            f"{d['origin']}-{d['destination']}",
        "origin":           d["origin"],
        "destination":      d["destination"],
        "departure_date":   d["departure_date"][:10],
        "original_price":   d["original_price"],
        "cancellation_fee": d["cancellation_fee"],
        "currency":         d["currency"],
        "cabin_class":      d.get("cabin_class", "economy"),
        "fare_type":        d.get("metadata", {}).get("fare_type", "refundable"),
        "status":           "monitoring" if d.get("_active", True) else "stopped",
        "webhook_url":      d.get("notify_webhook"),
        "passenger_set":    d["passenger"]["given_name"] != "Pending",
        "submitted_at":     d.get("_created_at"),
        "updated_at":       d.get("_updated_at"),
        "metadata":         d.get("metadata", {}),
    }


# ---------------------------------------------------------------------------
# POST /monitor  — submit a booking
# ---------------------------------------------------------------------------

class MonitorRequest(BaseModel):
    origin:           str   = Field(..., min_length=3, max_length=3, description="IATA origin, e.g. JFK")
    destination:      str   = Field(..., min_length=3, max_length=3, description="IATA destination, e.g. CDG")
    departure_date:   str   = Field(..., description="YYYY-MM-DD")
    original_price:   float = Field(..., gt=0, description="Price paid in USD")
    cancellation_fee: float = Field(..., ge=0, description="Airline fee to cancel and rebook")
    cabin_class:      Literal["economy", "premium_economy", "business", "first"] = "economy"
    fare_type:        Literal["refundable", "partially_refundable"] = Field(
        "refundable",
        description="refundable | partially_refundable. Non-refundable fares are rejected.",
    )
    webhook_url:  Optional[str] = Field(None, description="POST target for PRISM decisions")
    reference:    Optional[str] = Field(None, description="Your internal booking reference")

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
        # Rebooking can only save money if the fee is lower than the ticket price
        if self.cancellation_fee >= self.original_price:
            raise ValueError(
                f"cancellation_fee ({self.cancellation_fee}) must be less than "
                f"original_price ({self.original_price}). "
                "Rebooking would never produce a saving."
            )
        return self


@router.post("/monitor", status_code=201, summary="Submit booking for monitoring")
async def submit_monitor(
    payload: MonitorRequest,
    auth: tuple = Depends(require_api_key),
    idempotency_key: Optional[str] = Header(default=None),
):
    agency, _ = auth

    # Idempotency: return cached response on duplicate key
    if idempotency_key:
        cached = _bookings.get_idempotency(idempotency_key)
        if cached:
            return cached["response"]

    tracking_id = f"b2b_{uuid.uuid4().hex[:16]}"
    now_iso = datetime.utcnow().isoformat() + "Z"

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
        adapter="amadeus",
        adapter_booking_ref=payload.reference or tracking_id,
        notify_webhook=payload.webhook_url,
        metadata={
            "source":       "b2b_api_v1",
            "agency":       agency,
            "fare_type":    payload.fare_type,
            "submitted_at": now_iso,
        },
    )
    _bookings.upsert(booking)

    _bookings.log_audit(tracking_id, agency, "submitted", {
        "route":            f"{payload.origin}-{payload.destination}",
        "original_price":   payload.original_price,
        "cancellation_fee": payload.cancellation_fee,
        "cabin_class":      payload.cabin_class,
        "fare_type":        payload.fare_type,
        "departure_date":   payload.departure_date,
        "webhook_url":      payload.webhook_url,
        "reference":        payload.reference,
    })

    response = {
        "tracking_id":    tracking_id,
        "status":         "monitoring",
        "agency":         agency,
        "route":          f"{payload.origin}-{payload.destination}",
        "fare_type":      payload.fare_type,
        "departure_date": payload.departure_date,
        "webhook_url":    payload.webhook_url,
        "submitted_at":   now_iso,
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
    rows = _bookings.get_by_agency(agency)
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
# PATCH /bookings/{id}
# ---------------------------------------------------------------------------

class PatchBookingRequest(BaseModel):
    webhook_url:      Optional[str]   = None
    cancellation_fee: Optional[float] = Field(None, ge=0)
    cabin_class:      Optional[str]   = None
    passenger:        Optional[dict]  = None


@router.patch("/bookings/{tracking_id}", summary="Update booking details")
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

    updated_at = datetime.utcnow().isoformat() + "Z"
    booking.metadata["updated_at"] = updated_at
    _bookings.upsert(booking)
    _bookings.log_audit(tracking_id, agency, "updated", {"changes": changes, "updated_at": updated_at})

    return {"ok": True, "tracking_id": tracking_id, "updated": updated_at}


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
