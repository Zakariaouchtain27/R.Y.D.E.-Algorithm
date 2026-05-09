import logging
from datetime import datetime
from typing import Any, Dict, Optional

import requests

from ..models import Booking, PriceSnapshot
from .base import BaseAdapter

log = logging.getLogger(__name__)


class DuffelAdapter(BaseAdapter):
    """
    Duffel API adapter — full booking lifecycle including API-level holds.

    Duffel aggregates NDC-direct inventory from 300+ airlines.
    Supports: search, price, hold ("hold" order type), book, cancel.

    Docs: https://duffel.com/docs/api
    Sign up: https://app.duffel.com/join
    """

    BASE = "https://api.duffel.com/air"
    API_VERSION = "v2"

    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Duffel-Version": self.API_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # BaseAdapter implementation
    # ------------------------------------------------------------------

    def get_current_price(self, booking: Booking) -> PriceSnapshot:
        offer_request_id = self._create_offer_request(booking)
        offers = self._fetch_offers(offer_request_id, booking.currency)

        if not offers:
            raise ValueError(
                f"No offers found: {booking.origin} → {booking.destination} "
                f"on {booking.departure_date.date()}"
            )

        best = offers[0]  # sorted by total_amount asc by the API
        seats = self._parse_seats(best)

        return PriceSnapshot(
            booking_id=booking.booking_id,
            current_price=float(best["total_amount"]),
            seats_remaining=seats,
            snapshot_time=datetime.now(),
            fare_id=best["id"],
            source="duffel",
        )

    def create_hold(
        self, booking: Booking, fare_id: str
    ) -> Optional[str]:
        payload = {
            "data": {
                "type": "hold",
                "selected_offers": [fare_id],
                "passengers": [self._passenger_payload(booking)],
            }
        }
        try:
            r = self._post("/orders", payload)
            return r["data"]["id"]
        except Exception as exc:
            log.warning("Duffel hold failed: %s", exc)
            return None

    def cancel_booking(self, booking: Booking) -> bool:
        try:
            r = requests.post(
                f"{self.BASE}/orders/{booking.adapter_booking_ref}/actions/cancel",
                headers=self._headers,
                timeout=30,
            )
            r.raise_for_status()
            return True
        except requests.HTTPError as exc:
            log.error("Cancel failed [%s]: %s", booking.booking_id, exc)
            return False

    def create_booking(self, booking: Booking, fare_id: str) -> str:
        payload = {
            "data": {
                "selected_offers": [fare_id],
                "passengers": [self._passenger_payload(booking)],
                "payments": [
                    {
                        "type": "balance",
                        "currency": booking.currency,
                        "amount": "0",  # charged from Duffel balance
                    }
                ],
            }
        }
        r = self._post("/orders", payload)
        return r["data"]["id"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_offer_request(self, booking: Booking) -> str:
        payload = {
            "data": {
                "cabin_class": booking.cabin_class,
                "slices": [
                    {
                        "origin": booking.origin,
                        "destination": booking.destination,
                        "departure_date": booking.departure_date.strftime("%Y-%m-%d"),
                    }
                ],
                "passengers": [{"type": "adult"}],
            }
        }
        r = self._post("/offer_requests", payload)
        return r["data"]["id"]

    def _fetch_offers(self, offer_request_id: str, currency: str):
        r = requests.get(
            f"{self.BASE}/offers",
            params={
                "offer_request_id": offer_request_id,
                "sort": "total_amount",
                "max_connections": 1,
            },
            headers=self._headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("data", [])

    @staticmethod
    def _parse_seats(offer: Dict[str, Any]) -> int:
        slices = offer.get("slices", [])
        if not slices:
            return 9
        min_seats = min(
            seg.get("passengers", [{}])[0].get("cabin", {}).get("available_services", [9])[0]
            if seg.get("passengers")
            else 9
            for seg in slices[0].get("segments", [])
        )
        return min_seats if isinstance(min_seats, int) else 9

    @staticmethod
    def _passenger_payload(booking: Booking) -> Dict[str, Any]:
        p = booking.passenger
        return {
            "title": p.title,
            "given_name": p.given_name,
            "family_name": p.family_name,
            "born_on": p.born_on,
            "gender": p.gender,
            "email": p.email,
            "phone_number": p.phone,
            "type": "adult",
        }

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        r = requests.post(
            f"{self.BASE}{path}",
            json=payload,
            headers=self._headers,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
