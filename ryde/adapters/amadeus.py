import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from ..models import Booking, PriceSnapshot
from .base import BaseAdapter

log = logging.getLogger(__name__)


class AmadeusAdapter(BaseAdapter):
    """
    Amadeus for Developers adapter — PRICE MONITORING ONLY.

    Full booking via Amadeus requires IATA Travel Agency accreditation
    and a signed content agreement. This adapter is ideal as the
    price-watch layer while Duffel handles actual booking.

    Free tier: 2,000 API calls/month on test environment.
    Production requires a paid plan.

    Sign up: https://developers.amadeus.com
    """

    AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
    BASE_URL = "https://test.api.amadeus.com"
    # Swap to production:
    # AUTH_URL = "https://api.amadeus.com/v1/security/oauth2/token"
    # BASE_URL = "https://api.amadeus.com"

    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: Optional[str] = None
        self._token_expires: Optional[datetime] = None

    # ------------------------------------------------------------------
    # BaseAdapter implementation
    # ------------------------------------------------------------------

    def get_current_price(self, booking: Booking) -> PriceSnapshot:
        params = {
            "originLocationCode": booking.origin,
            "destinationLocationCode": booking.destination,
            "departureDate": booking.departure_date.strftime("%Y-%m-%d"),
            "adults": 1,
            "currencyCode": booking.currency,
            "travelClass": booking.cabin_class.upper(),
            "max": 5,
        }
        r = requests.get(
            f"{self.BASE_URL}/v2/shopping/flight-offers",
            params=params,
            headers=self._auth_headers(),
            timeout=30,
        )
        r.raise_for_status()
        offers = r.json().get("data", [])

        if not offers:
            raise ValueError(
                f"No Amadeus offers: {booking.origin} → {booking.destination}"
            )

        best = min(offers, key=lambda o: float(o["price"]["grandTotal"]))
        seats = int(best.get("numberOfBookableSeats", 9))

        return PriceSnapshot(
            booking_id=booking.booking_id,
            current_price=float(best["price"]["grandTotal"]),
            seats_remaining=seats,
            snapshot_time=datetime.now(),
            fare_id=best["id"],
            source="amadeus",
        )

    def create_hold(self, booking: Booking, fare_id: str) -> Optional[str]:
        # Amadeus does not support API-level holds without NDC accreditation
        return None

    def cancel_booking(self, booking: Booking) -> bool:
        raise NotImplementedError(
            "Amadeus booking requires IATA accreditation. "
            "Use AmadeusAdapter for price monitoring; DuffelAdapter for booking."
        )

    def create_booking(self, booking: Booking, fare_id: str) -> str:
        raise NotImplementedError(
            "Amadeus booking requires IATA accreditation. "
            "Use AmadeusAdapter for price monitoring; DuffelAdapter for booking."
        )

    # ------------------------------------------------------------------
    # OAuth2 token management
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def _get_token(self) -> str:
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token

        r = requests.post(
            self.AUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = datetime.now() + timedelta(seconds=body["expires_in"] - 60)
        return self._token
