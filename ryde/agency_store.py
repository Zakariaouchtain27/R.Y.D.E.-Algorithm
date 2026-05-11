"""
Agency and API key management.

Uses PostgreSQL when DATABASE_URL is set, SQLite otherwise.
Keys stored in plaintext for MVP. Production: store HMAC-SHA256(key).
"""
import os
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import List, Optional

_DATABASE_URL = os.getenv("DATABASE_URL", "")


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


class AgencyStore:
    def __init__(self, db_path: str = "ryde.db"):
        self._lock = Lock()
        self._pg = bool(_DATABASE_URL)

        if self._pg:
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(_DATABASE_URL)
            self._conn.autocommit = False
            self._dict_cursor = psycopg2.extras.DictCursor
        else:
            from pathlib import Path
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._dict_cursor = None

        self._init_schema()
        self._seed_dev_keys()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._pg else sql

    def _execute(self, sql: str, params=()):
        """Always call under self._lock."""
        if self._pg:
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

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._execute("""
                CREATE TABLE IF NOT EXISTS agencies (
                    id           TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    email        TEXT NOT NULL,
                    api_key      TEXT UNIQUE NOT NULL,
                    environment  TEXT NOT NULL DEFAULT 'test',
                    active       INTEGER NOT NULL DEFAULT 1,
                    total_calls  INTEGER NOT NULL DEFAULT 0,
                    last_call_at TEXT,
                    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._commit()

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

    # ------------------------------------------------------------------
    # Key generation
    # ------------------------------------------------------------------

    @staticmethod
    def generate_key(name: str, environment: str = "test") -> str:
        slug = re.sub(r"[^a-z0-9]", "", name.lower())[:12]
        token = secrets.token_hex(16)
        return f"ryde_{environment}_{slug}_{token}"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create_agency(self, name: str, email: str, environment: str = "test") -> Agency:
        agency_id = str(uuid.uuid4())
        api_key = self.generate_key(name, environment)
        now = datetime.utcnow().isoformat() + "Z"
        with self._lock:
            self._execute(
                """
                INSERT INTO agencies (id, name, email, api_key, environment, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (agency_id, name, email, api_key, environment, now),
            )
            self._commit()
        return self.get_by_id(agency_id)  # type: ignore[return-value]

    def revoke(self, agency_id: str) -> None:
        with self._lock:
            self._execute("UPDATE agencies SET active = 0 WHERE id = ?", (agency_id,))
            self._commit()

    def reactivate(self, agency_id: str) -> None:
        with self._lock:
            self._execute("UPDATE agencies SET active = 1 WHERE id = ?", (agency_id,))
            self._commit()

    def regenerate_key(self, agency_id: str) -> Optional[Agency]:
        agency = self.get_by_id(agency_id)
        if not agency:
            return None
        new_key = self.generate_key(agency.name, agency.environment)
        with self._lock:
            self._execute(
                "UPDATE agencies SET api_key = ? WHERE id = ?",
                (new_key, agency_id),
            )
            self._commit()
        return self.get_by_id(agency_id)

    def log_call(self, api_key: str) -> None:
        now = datetime.utcnow().isoformat() + "Z"
        with self._lock:
            self._execute(
                """
                UPDATE agencies
                SET total_calls = total_calls + 1, last_call_at = ?
                WHERE api_key = ?
                """,
                (now, api_key),
            )
            self._commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_by_key(self, api_key: str) -> Optional[Agency]:
        with self._lock:
            row = self._execute(
                "SELECT * FROM agencies WHERE api_key = ? AND active = 1",
                (api_key,),
            ).fetchone()
        return self._from_row(row) if row else None

    def get_by_id(self, agency_id: str) -> Optional[Agency]:
        with self._lock:
            row = self._execute(
                "SELECT * FROM agencies WHERE id = ?",
                (agency_id,),
            ).fetchone()
        return self._from_row(row) if row else None

    def list_agencies(self) -> List[Agency]:
        with self._lock:
            rows = self._execute(
                "SELECT * FROM agencies ORDER BY created_at DESC"
            ).fetchall()
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(row) -> Agency:
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
        )
