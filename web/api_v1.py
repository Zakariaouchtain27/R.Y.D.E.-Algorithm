"""
B2B API v1 — lets travel agencies submit bookings for PRISM monitoring
over HTTP instead of going through the consumer web flow.

    POST /api/v1/monitor
    Header: X-API-Key: <agency key>
    Body:   { origin, destination, departure_date, original_price, cancellation_fee }
    Returns: { tracking_id, status }

The monitored booking is written into the same BookingStore the bot polls,
so PriceMonitor picks it up on the next cycle — no engine changes needed.
"""
import os
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from ryde.models import Booking, Passenger
from ryde.store import BookingStore

router = APIRouter(prefix="/api/v1", tags=["b2b-v1"])

_db_path = os.getenv("RYDE_DB_PATH", "ryde.db")
_bookings = BookingStore(_db_path)


# ---------------------------------------------------------------------------
# Mock API key auth
# ---------------------------------------------------------------------------

_DEV_KEYS = {
    "ryde_dev_test_key_001": "acme-travel",
    "ryde_dev_test_key_002": "globetrotter-agency",
}


def _load_keys() -> dict:
    """
    Production keys come from RYDE_API_KEYS env var — comma-separated
    "key:agency_name" pairs. Falls back to dev keys when unset.
    """
    raw = os.getenv("RYDE_API_KEYS", "").strip()
    if not raw:
        return dict(_DEV_KEYS)
    keys = {}
    for entry in raw.split(","):
        if ":" in entry:
            k, agency = entry.split(":", 1)
            keys[k.strip()] = agency.strip()
    return keys or dict(_DEV_KEYS)


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency: validates X-API-Key header, returns the agency name."""
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    agency = _load_keys().get(x_api_key)
    if not agency:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key.",
        )
    return agency


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class MonitorRequest(BaseModel):
    origin: str = Field(..., min_length=3, max_length=3, description="IATA origin code, e.g. JFK")
    destination: str = Field(..., min_length=3, max_length=3, description="IATA destination code, e.g. CDG")
    departure_date: str = Field(..., description="ISO date YYYY-MM-DD")
    original_price: float = Field(..., gt=0, description="Price the passenger paid, in USD")
    cancellation_fee: float = Field(..., ge=0, description="Fee to cancel + rebook, in USD")

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

@router.post("/monitor", response_model=MonitorResponse, status_code=status.HTTP_201_CREATED)
async def submit_monitor(
    payload: MonitorRequest,
    agency: str = Depends(require_api_key),
) -> MonitorResponse:
    """
    Register a booking for PRISM monitoring.

    The bot's PriceMonitor will start polling this route on its next cycle
    and emit STRIKE/PHANTOM_HOLD/WAIT decisions. Passenger details and a
    real adapter booking reference can be patched in later via a follow-up
    endpoint (TODO) before any rebooking can actually execute.
    """
    tracking_id = f"b2b_{uuid.uuid4().hex[:16]}"

    booking = Booking(
        booking_id=tracking_id,
        passenger=Passenger(
            title="mr",
            given_name="Pending",
            family_name="Pending",
            born_on="1990-01-01",
            gender="m",
            email=f"noreply+{tracking_id}@{agency}.b2b.invalid",
            phone="+10000000000",
        ),
        origin=payload.origin,
        destination=payload.destination,
        departure_date=datetime.strptime(payload.departure_date, "%Y-%m-%d"),
        original_price=payload.original_price,
        currency="USD",
        cancellation_fee=payload.cancellation_fee,
        adapter="amadeus",        # B2B starts as monitor-only; agency owns booking lifecycle
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
