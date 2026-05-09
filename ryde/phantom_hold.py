from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .models import PhantomHold


class PhantomHoldManager:
    """
    Tracks 24-hour fare locks.

    Holds two layers:
      1. A soft hold tracked here in memory (always available).
      2. An optional hard hold reference returned by the airline API
         (e.g. Duffel order with type="hold"), stored in hold.hold_ref.

    In production, swap self._store for a Redis hash with TTL so the
    state survives restarts and works across multiple bot instances.
    """

    HOLD_HOURS = 24

    def __init__(self):
        self._store: Dict[str, PhantomHold] = {}

    def create(
        self,
        booking_id: str,
        fare_id: str,
        locked_price: float,
        hold_ref: Optional[str] = None,
    ) -> PhantomHold:
        now = datetime.now()
        hold = PhantomHold(
            booking_id=booking_id,
            fare_id=fare_id,
            locked_price=locked_price,
            created_at=now,
            expires_at=now + timedelta(hours=self.HOLD_HOURS),
            hold_ref=hold_ref,
        )
        self._store[booking_id] = hold
        return hold

    def get(self, booking_id: str) -> Optional[PhantomHold]:
        hold = self._store.get(booking_id)
        if hold and datetime.now() > hold.expires_at:
            del self._store[booking_id]
            return None
        return hold

    def release(self, booking_id: str) -> bool:
        return self._store.pop(booking_id, None) is not None

    def is_active(self, booking_id: str) -> bool:
        return self.get(booking_id) is not None

    def expired_holds(self) -> List[PhantomHold]:
        """Returns holds past their TTL (useful for cleanup jobs)."""
        now = datetime.now()
        return [h for h in self._store.values() if now > h.expires_at]
