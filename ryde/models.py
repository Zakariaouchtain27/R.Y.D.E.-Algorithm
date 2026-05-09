from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class RYDEAction(str, Enum):
    STRIKE = "STRIKE"
    PHANTOM_HOLD = "PHANTOM_HOLD"
    WAIT = "WAIT"
    IGNORE = "IGNORE"


@dataclass
class Passenger:
    title: str          # "mr" | "ms" | "mrs"
    given_name: str
    family_name: str
    born_on: str        # ISO date: "1990-01-01"
    gender: str         # "m" | "f"
    email: str
    phone: str          # E.164 format: "+15551234567"


@dataclass
class Booking:
    booking_id: str
    passenger: Passenger
    origin: str                  # IATA code e.g. "JFK"
    destination: str             # IATA code e.g. "CDG"
    departure_date: datetime
    original_price: float
    currency: str                # "USD", "EUR", etc.
    cancellation_fee: float
    adapter: str                 # "duffel" | "amadeus"
    adapter_booking_ref: str     # API-specific order/PNR reference
    cabin_class: str = "economy"
    volatility_index: float = 1.0   # 0.5 (stable) → 2.0 (highly volatile route)
    notify_webhook: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PriceSnapshot:
    booking_id: str
    current_price: float
    seats_remaining: int
    snapshot_time: datetime
    fare_id: str    # Opaque fare token needed to lock/book this exact offer
    source: str     # Which adapter produced this snapshot


@dataclass
class RYDEDecision:
    action: RYDEAction
    confidence_score: float
    net_savings: float
    expected_future_gain: float
    probability_of_future_drop: float   # Percentage 0-100
    seat_urgency_multiplier: float
    reasoning: str


@dataclass
class PhantomHold:
    booking_id: str
    fare_id: str
    locked_price: float
    created_at: datetime
    expires_at: datetime
    hold_ref: Optional[str] = None   # API-side hold reference if the airline supports it


@dataclass
class RebookingResult:
    booking_id: str
    success: bool
    old_ref: str
    new_ref: Optional[str]
    savings_realized: float
    timestamp: datetime
    error: Optional[str] = None
