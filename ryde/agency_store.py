"""
Agency and API key management.

Uses PostgreSQL when DATABASE_URL is set, SQLite otherwise.
Keys stored in plaintext for MVP. Production: store HMAC-SHA256(key).

PostgreSQL connection is deferred to the first actual query so that
module import (and therefore uvicorn startup) never blocks waiting
for the database to become available.
"""
import json
import os
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Dict, List, Optional

_DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---------------------------------------------------------------------------
# Subscription tier limits
# ---------------------------------------------------------------------------

TIER_LIMITS: dict = {
    "free":    {"max_bookings": 3,   "rate_limit": 20},
    "starter": {"max_bookings": 25,  "rate_limit": 60},
    "pro":     {"max_bookings": -1,  "rate_limit": 120},
}


@dataclass
class Agency:
    id: str
    name: str
    email: str
    api_key: str
    environment: str
    active: bool
    total_calls: int
    last_call_at: Optional[str]
    created_at: str
    ls_subscription_id: Optional[str] = None
    ls_order_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    subscription_tier: str = "free"
    notification_config: Dict = field(default_factory=dict)


class AgencyStore:
    def __init__(self, db_path: str = "ryde.db"):
        self._lock  = RLock()
        self._pg    = bool(_DATABASE_URL)
        self._ready = False
        self._conn  = None

        if self._pg:
            import psycopg2.extras
            self._dict_cursor = psycopg2.extras.DictCursor
        else:
            from pathlib import Path
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._dict_cursor = None
            self._init_schema()
            self._seed_dev_keys()
            self._ready = True

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        import psycopg2
        import psycopg2.extras
        self._conn = psycopg2.connect(_DATABASE_URL)
        self._conn.autocommit = False
        self._conn.cursor_factory = self._dict_cursor
        self._ready = True
        self._init_schema()
        self._seed_dev_keys()

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._pg else sql

    def _execute(self, sql: str, params=()):
        if self._pg:
            self._ensure_ready()
            cur = self._conn.cursor(cursor_factory=self._dict_cursor)
        else:
            cur = self._conn.cursor()
        cur.execute(self._q(sql), params)
        return cur

    def _commit(self):
        self._conn.commit()

    def _insert_or_ignore(self, table: str, cols: List[str]) -> str:
        ph = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        if self._pg:
            return f"INSERT INTO {table} ({col_str}) VALUES ({ph}) ON CONFLICT DO NOTHING"
        return f"INSERT OR IGNORE INTO {table} ({col_str}) VALUES ({ph})"

    def _init_schema(self) -> None:
        with self._lock:
            self._execute("""
                CREATE TABLE IF NOT EXISTS agencies (
                    id                   TEXT PRIMARY KEY,
                    name                 TEXT NOT NULL,
                    email                TEXT NOT NULL,
                    api_key              TEXT UNIQUE NOT NULL,
                    environment          TEXT NOT NULL DEFAULT 'test',
                    active               INTEGER NOT NULL DEFAULT 1,
                    total_calls          INTEGER NOT NULL DEFAULT 0,
                    last_call_at         TEXT,
                    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    ls_subscription_id   TEXT,
                    ls_order_id          TEXT,
                    stripe_customer_id   TEXT,
                    subscription_tier    TEXT DEFAULT 'free',
                    notification_config  TEXT DEFAULT '{}'
                )
            """)
            self._add_column_if_missing("agencies", "ls_subscription_id", "TEXT")
            self._add_column_if_missing("agencies", "ls_order_id", "TEXT")
            self._add_column_if_missing("agencies", "stripe_customer_id", "TEXT")
            self._add_column_if_missing("agencies", "subscription_tier", "TEXT DEFAULT 'free'")
            self._add_column_if_missing("agencies", "notification_config", "TEXT DEFAULT '{}'")
            self._commit()

    def _add_column_if_missing(self, table: str, column: str, col_type: str) -> None:
        if self._pg:
            self._execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}"
            )
        else:
            try:
                self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            except Exception:
                pass

    def _seed_dev_keys(self) -> None:
        seeds = [
            ("dev_001", "ACME Travel",        "dev@acmetravel.com",   "ryde_dev_test_key_001"),
            ("dev_002", "Globetrotter Agency", "dev@globetrotter.com", "ryde_dev_test_key_002"),
        ]
        sql = self._insert_or_ignore("agencies", ["id", "name", "email", "api_key", "environment"])
        with self._lock:
            for agency_id, name, email, key in seeds:
                self._execute(sql, (agency_id, name, email, key, "test"))
            self._commit()

    @staticmethod
    def generate_key(name: str, environment: str = "test") -> str:
        slug = re.sub(r"[^a-z0-9]", "", name.lower())[:12]
        token = secrets.token_hex(16)
        return f"ryde_{environment}_{slug}_{token}"

    def create_agency(self, name: str, email: str, environment: str = "test") -> "Agency":
        agency_id = str(uuid.uuid4())
        api_key   = self.generate_key(name, environment)
        now       = datetime.utcnow().isoformat() + "Z"
        with self._lock:
            self._execute(
                "INSERT INTO agencies (id, name, email, api_key, environment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (agency_id, name, email, api_key, environment, now),
            )
            self._commit()
        return self.get_by_id(agency_id)  # type: ignore

    def create_agency_ls(
        self,
        name: str,
        email: str,
        environment: str = "live",
        ls_subscription_id: str = "",
        ls_order_id: str = "",
        subscription_tier: str = "free",
    ) -> "Agency":
        agency_id = str(uuid.uuid4())
        api_key   = self.generate_key(name, environment)
        now       = datetime.utcnow().isoformat() + "Z"
        with self._lock:
            self._execute(
                """
                INSERT INTO agencies
                  (id, name, email, api_key, environment, created_at,
                   ls_subscription_id, ls_order_id, subscription_tier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (agency_id, name, email, api_key, environment, now,
                 ls_subscription_id, ls_order_id, subscription_tier),
            )
            self._commit()
        return self.get_by_id(agency_id)  # type: ignore

    def revoke(self, agency_id: str) -> None:
        with self._lock:
            self._execute("UPDATE agencies SET active = 0 WHERE id = ?", (agency_id,))
            self._commit()

    def reactivate(self, agency_id: str) -> None:
        with self._lock:
            self._execute("UPDATE agencies SET active = 1 WHERE id = ?", (agency_id,))
            self._commit()

    def regenerate_key(self, agency_id: str) -> Optional["Agency"]:
        agency = self.get_by_id(agency_id)
        if not agency:
            return None
        new_key = self.generate_key(agency.name, agency.environment)
        with self._lock:
            self._execute("UPDATE agencies SET api_key = ? WHERE id = ?", (new_key, agency_id))
            self._commit()
        return self.get_by_id(agency_id)

    def set_stripe_customer(self, agency_id: str, stripe_customer_id: str) -> None:
        with self._lock:
            self._execute(
                "UPDATE agencies SET stripe_customer_id = ? WHERE id = ?",
                (stripe_customer_id, agency_id),
            )
            self._commit()

    def set_subscription_tier(self, agency_id: str, tier: str) -> None:
        tier = tier if tier in TIER_LIMITS else "free"
        with self._lock:
            self._execute(
                "UPDATE agencies SET subscription_tier = ? WHERE id = ?",
                (tier, agency_id),
            )
            self._commit()

    def set_notification_config(self, agency_id: str, config: dict) -> None:
        """Persist the agency's notification channel settings (JSON blob)."""
        with self._lock:
            self._execute(
                "UPDATE agencies SET notification_config = ? WHERE id = ?",
                (json.dumps(config), agency_id),
            )
            self._commit()

    def log_call(self, api_key: str) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        with self._lock:
            self._execute(
                "UPDATE agencies SET total_calls = total_calls + 1, last_call_at = ? WHERE api_key = ?",
                (now, api_key),
            )
            self._commit()

    def get_by_key(self, api_key: str) -> Optional["Agency"]:
        with self._lock:
            row = self._execute(
                "SELECT * FROM agencies WHERE api_key = ? AND active = 1", (api_key,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_id(self, agency_id: str) -> Optional["Agency"]:
        with self._lock:
            row = self._execute(
                "SELECT * FROM agencies WHERE id = ?", (agency_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_name(self, name: str) -> Optional["Agency"]:
        with self._lock:
            row = self._execute(
                "SELECT * FROM agencies WHERE name = ? AND active = 1 LIMIT 1", (name,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_ls_subscription(self, ls_subscription_id: str) -> Optional["Agency"]:
        with self._lock:
            row = self._execute(
                "SELECT * FROM agencies WHERE ls_subscription_id = ?", (ls_subscription_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_ls_order(self, ls_order_id: str) -> Optional["Agency"]:
        with self._lock:
            row = self._execute(
                "SELECT * FROM agencies WHERE ls_order_id = ?", (ls_order_id,)
            ).fetchone()
        return self._from_row(row) if row else None

    def list_agencies(self) -> List["Agency"]:
        with self._lock:
            rows = self._execute(
                "SELECT * FROM agencies ORDER BY created_at DESC"
            ).fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row) -> "Agency":
        try:
            nc = json.loads(row["notification_config"] or "{}")
        except Exception:
            nc = {}
        return Agency(
            id=row["id"],
            name=row["name"],
            email=row["email"],
            api_key=row["api_key"],
            environment=row["environment"],
            active=bool(row["active"]),
            total_calls=row["total_calls"],
            last_call_at=row["last_call_at"],
            created_at=str(row["created_at"]),
            ls_subscription_id=row["ls_subscription_id"],
            ls_order_id=row["ls_order_id"],
            stripe_customer_id=row["stripe_customer_id"],
            subscription_tier=row["subscription_tier"] or "free",
            notification_config=nc,
        )
