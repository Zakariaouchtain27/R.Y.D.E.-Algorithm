from abc import ABC, abstractmethod
from typing import Optional

from ..models import Booking, PriceSnapshot


class BaseAdapter(ABC):
    """
    Contract every airline/GDS integration must fulfil.

    Third-party access paths (most → least permissive):
      1. Duffel API        — modern NDC aggregator, supports holds
      2. Amadeus for Devs  — great for price monitoring; booking needs IATA cert
      3. Sabre / Travelport — full GDS access, requires travel agency agreement
      4. Airline NDC direct — airline-by-airline, e.g. BA, AA, LH developer portals
    """

    @abstractmethod
    def get_current_price(self, booking: Booking) -> PriceSnapshot:
        """Fetch the current best available price for the same itinerary."""
        ...

    @abstractmethod
    def create_hold(self, booking: Booking, fare_id: str) -> Optional[str]:
        """
        Attempt a 24 h API-level hold on a specific fare.
        Returns the hold reference on success, None if unsupported.
        """
        ...

    @abstractmethod
    def cancel_booking(self, booking: Booking) -> bool:
        """Cancel the existing booking. Returns True on success."""
        ...

    @abstractmethod
    def create_booking(self, booking: Booking, fare_id: str) -> str:
        """Complete a new booking for the given fare. Returns new booking reference."""
        ...
