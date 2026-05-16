import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import List, Optional

from .models import Booking, Passenger

_DATABASE_URL = os.getenv("DATABASE_URL", "")


class _AuditEncoder(json.JSONEncoder):
    """Safely serialise audit detail dicts that may contain datetime / Decimal objects."""
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


class BookingStore:
    """
    SQLite-backed store for local dev; PostgreSQL in production.
    Set DATABASE_URL to switch engines — no other changes needed.

    PostgreSQL connection is deferred to the first actual query so that
    module import (and therefore uvicorn startup) never blocks waiting
    for the database to become available.
    """

    def __init__(self, db_path: str = "ryde.db"):
        self._lock  = RLock()   # RLock: same thread can re-enter during lazy init
        self._pg    = bool(_DATABASE_URL)
        self._ready = False     # True after connection + schema are initialised
        self._conn  = None

        if self._pg:
            import psycopg2.extras
            self._dict_cursor = psycopg2.extras.DictCursor
            # Connection deferred to first _execute call.  Connecting at
            # import time would block the whole Python process for up to the
            # OS TCP timeout (~120s) if PostgreSQL isn't ready yet, which
            # prevents Railway's healthcheck from ever getting a response.
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._dict_cursor = None
            self._init_schema()
            self._ready = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        """Connect to PostgreSQL and init schema on first use.
        Must be called while holding self._lock."""
        if self._ready:
            return
        import psycopg2
        self._conn = psycopg2.connect(_DATABASE_URL)
        self._conn.autocommit = False
        # Set _ready before calling _init_schema so that _execute calls
        # inside _init_schema don't recurse back into this method.
        self._ready = True
        self._init_schema()

    def _q(self, sql: str) -> str:
        """Swap SQLite ? placeholders for PostgreSQL %s."""
        return sql.replace("?", "%s") if self._pg else sql

    def _execute(self, sql: str, params=()):
        """Always call under self._lock."""
        if self._pg:
            self._ensure_ready()
            cur = self._conn.cursor(cursor_factory=self._dict_cursor)
        else:
            cur = self._conn.cursor()
        cur.execute(self._q(sql), params)
        return cur

    def _commit(self):
        """Always call under self._lock."""
        self._conn.commit()

    def _agency_filter(self) -> str:
        """SQL expression: JSON field metadata.agency as text."""
        if self._pg:
            return "data::json->'metadata'->>'agency'"
        return "json_extract(data, '$.metadata.agency')"

    def _insert_or_ignore(self, table: str, cols: List[str]) -> str:
        ph = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        if self._pg:
            return f"INSERT INTO {table} ({col_str}) VALUES ({ph}) ON CONFLICT DO NOTHING"
        return f"INSERT OR IGNORE INTO {table} ({col_str}) VALUES ({ph})"

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self):
        audit_id = "SERIAL PRIMARY KEY" if self._pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        with self._lock:
            self._execute("""
                CREATE TABLE IF NOT EXISTS bookings (
                    booking_id  TEXT PRIMARY KEY,
                    data        TEXT NOT NULL,
                    active      INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._execute(f"""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id          {audit_id},
                    booking_id  TEXT NOT NULL,
                    agency      TEXT NOT NULL DEFAULT '',
                    event       TEXT NOT NULL,
                    detail      TEXT NOT NULL DEFAULT '{{}}',
                    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._execute(
                "CREATE INDEX IF NOT EXISTS audit_log_booking_idx ON audit_log (booking_id)"
            )
            self._execute("""
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idem_key    TEXT PRIMARY KEY,
                    tracking_id TEXT NOT NULL,
                    response    TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._commit()

    # ------------------------------------------------------------------
    # Write — bookings
    # ------------------------------------------------------------------

    def upsert(self, booking: Booking) -> None:
        data = json.dumps(self._to_dict(booking))
        with self._lock:
            self._execute(
                """
                INSERT INTO bookings (booking_id, data) VALUES (?, ?)
                ON CONFLICT (booking_id) DO UPDATE SET
                    data       = excluded.data,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (booking.booking_id, data),
            )
            self._commit()

    def deactivate(self, booking_id: str) -> None:
        with self._lock:
            self._execute(
                "UPDATE bookings SET active = 0, updated_at = CURRENT_TIMESTAMP WHERE booking_id = ?",
                (booking_id,),
            )
            self._commit()

    # ------------------------------------------------------------------
    # Read — bookings
    # ------------------------------------------------------------------

    def get_active(self) -> List[Booking]:
        with self._lock:
            rows = self._execute("SELECT data FROM bookings WHERE active = 1").fetchall()
        return [self._from_dict(json.loads(r[0])) for r in rows]

    def get_by_id(self, booking_id: str) -> Optional[Booking]:
        with self._lock:
            row = self._execute(
                "SELECT data FROM bookings WHERE booking_id = ?",
                (booking_id,),
            ).fetchone()
        return self._from_dict(json.loads(row[0])) if row else None

    def get_by_agency(self, agency: str) -> List[dict]:
        af = self._agency_filter()
        with self._lock:
            rows = self._execute(
                f"""
                SELECT data, active, created_at, updated_at
                FROM bookings
                WHERE {af} = ?
                ORDER BY created_at DESC
                """,
                (agency,),
            ).fetchall()
        result = []
        for row in rows:
            d = json.loads(row[0])
            d["_active"] = bool(row[1])
            d["_created_at"] = str(row[2])
            d["_updated_at"] = str(row[3])
            result.append(d)
        return result

    def get_agency_savings(self, agency: str) -> float:
        af = self._agency_filter()
        with self._lock:
            try:
                row = self._execute(
                    f"""
                    SELECT COALESCE(SUM(ro.savings), 0)
                    FROM rebooking_outcomes ro
                    JOIN bookings b ON b.booking_id = ro.booking_id
                    WHERE {af} = ?
                    AND ro.success = 1
                    """,
                    (agency,),
                ).fetchone()
                return float(row[0]) if row else 0.0
            except Exception:
                self._conn.rollback()
                return 0.0

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def log_audit(self, booking_id: str, agency: str, event: str, detail: dict) -> None:
        with self._lock:
            self._execute(
                "INSERT INTO audit_log (booking_id, agency, event, detail) VALUES (?, ?, ?, ?)",
                (booking_id, agency, event, json.dumps(detail, cls=_AuditEncoder)),
            )
            self._commit()

    def get_audit(self, booking_id: str) -> List[dict]:
        with self._lock:
            rows = self._execute(
                """
                SELECT id, agency, event, detail, created_at
                FROM audit_log WHERE booking_id = ?
                ORDER BY id ASC
                """,
                (booking_id,),
            ).fetchall()
        return [
            {
                "seq":       row[0],
                "agency":    row[1],
                "event":     row[2],
                "detail":    json.loads(row[3]),
                "timestamp": str(row[4]),
            }
            for row in rows
        ]

    def get_last_decision_by_agency(self, agency: str) -> dict:
        """
        Returns {booking_id: decision_detail_dict} for the most recent
        PRISM 'decision' audit event per booking owned by the agency.

        Single-pass scan ordered newest-first; keeps the first (latest)
        entry seen per booking_id to avoid an N+1 per-booking pattern.
        """
        with self._lock:
            rows = self._execute(
                """
                SELECT booking_id, detail
                FROM audit_log
                WHERE event = 'decision' AND agency = ?
                ORDER BY id DESC
                """,
                (agency,),
            ).fetchall()
        seen: dict = {}
        for row in rows:
            bid = row[0]
            if bid not in seen:
                seen[bid] = json.loads(row[1])
        return seen

    def get_billing_events(self, agency: str) -> list:
        """Return all billing audit events for this agency, newest first."""
        with self._lock:
            rows = self._execute(
                """
                SELECT booking_id, event, detail, created_at
                FROM audit_log
                WHERE agency = ?
                  AND event IN ('billing_charged', 'billing_error', 'billing_skipped')
                ORDER BY id DESC
                """,
                (agency,),
            ).fetchall()
        result = []
        for row in rows:
            result.append({
                "booking_id": row[0],
                "event":      row[1],
                "detail":     json.loads(row[2]),
                "timestamp":  str(row[3]),
            })
        return result

    # ------------------------------------------------------------------
    # Idempotency cache
    # ------------------------------------------------------------------

    def get_idempotency(self, idem_key: str) -> Optional[dict]:
        with self._lock:
            row = self._execute(
                "SELECT tracking_id, response FROM idempotency_keys WHERE idem_key = ?",
                (idem_key,),
            ).fetchone()
        if row:
            return {"tracking_id": row[0], "response": json.loads(row[1])}
        return None

    def set_idempotency(self, idem_key: str, tracking_id: str, response: dict) -> None:
        sql = self._insert_or_ignore(
            "idempotency_keys", ["idem_key", "tracking_id", "response"]
        )
        with self._lock:
            self._execute(sql, (idem_key, tracking_id, json.dumps(response)))
            self._commit()

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
