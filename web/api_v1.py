"""
B2B API v1 — lets travel agencies submit bookings for PRISM monitoring.

    POST /api/v1/monitor
    Header: X-API-Key: <agency key>
    Body:   { origin, destination, departure_date, original_price, cancellation_fee }
    Returns: { tracking_id, status }
"""
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from ryde.agency_store import AgencyStore
from ryde.models import Booking, Passenger
from ryde.store import BookingStore

router = APIRouter(prefix="/api/v1", tags=["b2b-v1"])

_db_path = os.getenv("RYDE_DB_PATH", "ryde.db")
_bookings = BookingStore(_db_path)
_agencies = AgencyStore(_db_path)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    """Validates X-API-Key header, logs the call, returns the agency name."""
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
    _agencies.log_call(x_api_key)
    return agency.name


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class MonitorRequest(BaseModel):
    origin: str      = Field(..., min_length=3, max_length=3)
    destination: str = Field(..., min_length=3, max_length=3)
    departure_date: str   = Field(..., description="YYYY-MM-DD")
    original_price: float = Field(..., gt=0)
    cancellation_fee: float = Field(..., ge=0)

    @field_validator("origin", "destination")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("departure_date")
    @classmethod
    def _validate_date(cls, v: str) -> str:
        try:
            dep = datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("departure_date must be YYYY-MM-DD")
        if dep.date() <= datetime.now().date():
            raise ValueError("departure_date must be in the future")
        return v


class MonitorResponse(BaseModel):
    tracking_id: str
    status: str
    agency: str
    monitored_route: str
    departure_date: str


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/monitor", response_model=MonitorResponse, status_code=201)
async def submit_monitor(
    payload: MonitorRequest,
    agency: str = Depends(require_api_key),
) -> MonitorResponse:
    tracking_id = f"b2b_{uuid.uuid4().hex[:16]}"

    booking = Booking(
        booking_id=tracking_id,
        passenger=Passenger(
            title="mr",
            given_name="Pending",
            family_name="Pending",
            born_on="1990-01-01",
            gender="m",
            email=f"noreply+{tracking_id}@b2b.ryde.invalid",
            phone="+10000000000",
        ),
        origin=payload.origin,
        destination=payload.destination,
        departure_date=datetime.strptime(payload.departure_date, "%Y-%m-%d"),
        original_price=payload.original_price,
        currency="USD",
        cancellation_fee=payload.cancellation_fee,
        adapter="amadeus",
        adapter_booking_ref=tracking_id,
        metadata={
            "source": "b2b_api_v1",
            "agency": agency,
            "submitted_at": datetime.utcnow().isoformat() + "Z",
        },
    )
    _bookings.upsert(booking)

    return MonitorResponse(
        tracking_id=tracking_id,
        status="monitoring",
        agency=agency,
        monitored_route=f"{payload.origin}→{payload.destination}",
        departure_date=payload.departure_date,
    )
