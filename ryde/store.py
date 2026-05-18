"""
Async BookingStore — SQLAlchemy + asyncpg (prod) / aiosqlite (dev).
All public methods are coroutines; callers must await them.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import func, insert, select, update

from .db import (
    AsyncSessionLocal, IS_PG,
    audit_log_table, bookings_table, idempotency_table, _dialect_insert,
)
from .models import Booking, Passenger

log = logging.getLogger(__name__)


class _AuditEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        try:
            from decimal import Decimal
            if isinstance(obj, Decimal):
                return float(obj)
        except ImportError:
            pass
        return str(obj)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _agency_filter(agency: str):
    """Cross-dialect expression: JSON field metadata.agency."""
    if IS_PG:
        from sqlalchemy import text
        return text("data::json->'metadata'->>'agency' = :ag").bindparams(ag=agency)
    return func.json_extract(bookings_table.c.data, "$.metadata.agency") == agency


class BookingStore:
    def __init__(self, db_path: str = "ryde.db"):
        pass  # engine / session managed globally in ryde.db

    # ------------------------------------------------------------------
    # Bookings
    # ------------------------------------------------------------------

    async def upsert(self, booking: Booking) -> None:
        data = json.dumps(self._to_dict(booking))
        now  = _now()
        stmt = (
            _dialect_insert(bookings_table)
            .values(
                booking_id=booking.booking_id, data=data, active=1,
                created_at=now, updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["booking_id"],
                set_={"data": data, "updated_at": now},
            )
        )
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(stmt)

    async def deactivate(self, booking_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(bookings_table)
                    .where(bookings_table.c.booking_id == booking_id)
                    .values(active=0, updated_at=_now())
                )

    async def get_active(self) -> List[Booking]:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(bookings_table).where(bookings_table.c.active == 1)
            )).fetchall()
        return [self._from_dict(json.loads(r.data)) for r in rows]

    async def get_by_id(self, booking_id: str) -> Optional[Booking]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(bookings_table)
                .where(bookings_table.c.booking_id == booking_id)
            )).fetchone()
        return self._from_dict(json.loads(row.data)) if row else None

    async def get_by_agency(self, agency: str) -> List[dict]:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(
                    bookings_table.c.data,
                    bookings_table.c.active,
                    bookings_table.c.created_at,
                    bookings_table.c.updated_at,
                )
                .where(_agency_filter(agency))
                .order_by(bookings_table.c.created_at.desc())
            )).fetchall()
        result = []
        for row in rows:
            d = json.loads(row.data)
            d["_active"]     = bool(row.active)
            d["_created_at"] = str(row.created_at)
            d["_updated_at"] = str(row.updated_at)
            result.append(d)
        return result

    async def get_agency_savings(self, agency: str) -> float:
        try:
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(
                    select(audit_log_table.c.detail)
                    .where(
                        audit_log_table.c.agency == agency,
                        audit_log_table.c.event  == "decision",
                    )
                )).fetchall()
            total = 0.0
            for row in rows:
                d = json.loads(row.detail)
                if d.get("action") == "STRIKE":
                    total += float(d.get("net_savings", 0))
            return total
        except Exception:
            return 0.0

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def log_audit(self, booking_id: str, agency: str, event: str, detail: dict) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    insert(audit_log_table).values(
                        booking_id=booking_id,
                        agency=agency,
                        event=event,
                        detail=json.dumps(detail, cls=_AuditEncoder),
                        created_at=_now(),
                    )
                )

    async def get_audit(self, booking_id: str) -> List[dict]:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(
                    audit_log_table.c.id,
                    audit_log_table.c.agency,
                    audit_log_table.c.event,
                    audit_log_table.c.detail,
                    audit_log_table.c.created_at,
                )
                .where(audit_log_table.c.booking_id == booking_id)
                .order_by(audit_log_table.c.id.asc())
            )).fetchall()
        return [
            {
                "seq":       row.id,
                "agency":    row.agency,
                "event":     row.event,
                "detail":    json.loads(row.detail),
                "timestamp": str(row.created_at),
            }
            for row in rows
        ]

    async def get_last_decision_by_agency(self, agency: str) -> dict:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(audit_log_table.c.booking_id, audit_log_table.c.detail)
                .where(
                    audit_log_table.c.event  == "decision",
                    audit_log_table.c.agency == agency,
                )
                .order_by(audit_log_table.c.id.desc())
            )).fetchall()
        seen: dict = {}
        for row in rows:
            if row.booking_id not in seen:
                seen[row.booking_id] = json.loads(row.detail)
        return seen

    async def get_billing_events(self, agency: str) -> list:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(
                    audit_log_table.c.booking_id,
                    audit_log_table.c.event,
                    audit_log_table.c.detail,
                    audit_log_table.c.created_at,
                )
                .where(
                    audit_log_table.c.agency == agency,
                    audit_log_table.c.event.in_(
                        ["billing_charged", "billing_error", "billing_skipped"]
                    ),
                )
                .order_by(audit_log_table.c.id.desc())
            )).fetchall()
        return [
            {
                "booking_id": row.booking_id,
                "event":      row.event,
                "detail":     json.loads(row.detail),
                "timestamp":  str(row.created_at),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    async def get_idempotency(self, idem_key: str) -> Optional[dict]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(idempotency_table.c.tracking_id, idempotency_table.c.response)
                .where(idempotency_table.c.idem_key == idem_key)
            )).fetchone()
        if row:
            return {"tracking_id": row.tracking_id, "response": json.loads(row.response)}
        return None

    async def set_idempotency(self, idem_key: str, tracking_id: str, response: dict) -> None:
        stmt = (
            _dialect_insert(idempotency_table)
            .values(
                idem_key=idem_key,
                tracking_id=tracking_id,
                response=json.dumps(response),
                created_at=_now(),
            )
            .on_conflict_do_nothing()
        )
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(stmt)

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(b: Booking) -> dict:
        return {
            "booking_id": b.booking_id,
            "passenger": {
                "title":       b.passenger.title,
                "given_name":  b.passenger.given_name,
                "family_name": b.passenger.family_name,
                "born_on":     b.passenger.born_on,
                "gender":      b.passenger.gender,
                "email":       b.passenger.email,
                "phone":       b.passenger.phone,
            },
            "origin":              b.origin,
            "destination":         b.destination,
            "departure_date":      b.departure_date.isoformat(),
            "original_price":      b.original_price,
            "currency":            b.currency,
            "cancellation_fee":    b.cancellation_fee,
            "adapter":             b.adapter,
            "adapter_booking_ref": b.adapter_booking_ref,
            "cabin_class":         b.cabin_class,
            "volatility_index":    b.volatility_index,
            "notify_webhook":      b.notify_webhook,
            "metadata":            b.metadata,
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
