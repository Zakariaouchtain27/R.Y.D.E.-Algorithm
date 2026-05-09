import json
import sqlite3
from datetime import datetime
from threading import Lock
from typing import List, Optional

from .models import Booking, Passenger


class BookingStore:
    """
    SQLite-backed store for active bookings.

    Swap this for Postgres + SQLAlchemy in production.
    The interface is intentionally minimal so the swap is trivial.
    """

    def __init__(self, db_path: str = "ryde.db"):
        self._lock = Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self):
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                booking_id  TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.commit()

    # ------------------------------------------------------------------

    def upsert(self, booking: Booking):
        data = json.dumps(self._to_dict(booking))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bookings (booking_id, data) VALUES (?, ?)
                ON CONFLICT(booking_id) DO UPDATE SET
                    data       = excluded.data,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (booking.booking_id, data),
            )
            self._conn.commit()

    def get_active(self) -> List[Booking]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM bookings WHERE active = 1"
            ).fetchall()
        return [self._from_dict(json.loads(r[0])) for r in rows]

    def deactivate(self, booking_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE bookings SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE booking_id = ?",
                (booking_id,),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(b: Booking) -> dict:
        return {
            "booking_id": b.booking_id,
            "passenger": {
                "title": b.passenger.title,
                "given_name": b.passenger.given_name,
                "family_name": b.passenger.family_name,
                "born_on": b.passenger.born_on,
                "gender": b.passenger.gender,
                "email": b.passenger.email,
                "phone": b.passenger.phone,
            },
            "origin": b.origin,
            "destination": b.destination,
            "departure_date": b.departure_date.isoformat(),
            "original_price": b.original_price,
            "currency": b.currency,
            "cancellation_fee": b.cancellation_fee,
            "adapter": b.adapter,
            "adapter_booking_ref": b.adapter_booking_ref,
            "cabin_class": b.cabin_class,
            "volatility_index": b.volatility_index,
            "notify_webhook": b.notify_webhook,
            "metadata": b.metadata,
        }

    @staticmethod
    def _from_dict(d: dict) -> Booking:
        return Booking(
            booking_id=d["booking_id"],
            passenger=Passenger(**d["passenger"]),
            origin=d["origin"],
            destination=d["destination"],
            departure_date=datetime.fromisoformat(d["departure_date"]),
            original_price=d["original_price"],
            currency=d["currency"],
            cancellation_fee=d["cancellation_fee"],
            adapter=d["adapter"],
            adapter_booking_ref=d["adapter_booking_ref"],
            cabin_class=d.get("cabin_class", "economy"),
            volatility_index=d.get("volatility_index", 1.0),
            notify_webhook=d.get("notify_webhook"),
            metadata=d.get("metadata", {}),
        )
