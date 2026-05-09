import json
import sqlite3
from threading import Lock
from typing import List, Optional


class ClientStore:
    """
    Stores client profiles and payment state alongside the bot's booking store.
    Both use the same ryde.db so the bot auto-picks up newly registered bookings.
    """

    def __init__(self, db_path: str = "ryde.db"):
        self._lock = Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
                client_id              TEXT PRIMARY KEY,
                name                   TEXT NOT NULL,
                email                  TEXT NOT NULL,
                stripe_customer_id     TEXT,
                stripe_payment_method  TEXT,
                booking_data           TEXT NOT NULL,
                monitoring_active      INTEGER NOT NULL DEFAULT 0,
                total_savings          REAL NOT NULL DEFAULT 0.0,
                created_at             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS client_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id  TEXT NOT NULL,
                event      TEXT NOT NULL,
                data       TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self._conn.commit()

    def create_client(
        self,
        client_id: str,
        name: str,
        email: str,
        stripe_customer_id: str,
        booking_data: dict,
    ):
        with self._lock:
            self._conn.execute(
                """INSERT INTO clients
                   (client_id, name, email, stripe_customer_id, booking_data)
                   VALUES (?, ?, ?, ?, ?)""",
                (client_id, name, email, stripe_customer_id, json.dumps(booking_data)),
            )
            self._conn.commit()

    def get_client(self, client_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return self._parse(row) if row else None

    def get_client_by_booking_ref(self, booking_ref: str) -> Optional[dict]:
        rows = self._conn.execute(
            "SELECT * FROM clients WHERE monitoring_active = 1"
        ).fetchall()
        for row in rows:
            d = self._parse(row)
            if d["booking_data"].get("booking_ref") == booking_ref:
                return d
        return None

    def save_payment_method(self, client_id: str, payment_method_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE clients SET stripe_payment_method = ? WHERE client_id = ?",
                (payment_method_id, client_id),
            )
            self._conn.commit()

    def activate_monitoring(self, client_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE clients SET monitoring_active = 1 WHERE client_id = ?",
                (client_id,),
            )
            self._conn.commit()

    def add_savings(self, client_id: str, amount: float):
        with self._lock:
            self._conn.execute(
                "UPDATE clients SET total_savings = total_savings + ? WHERE client_id = ?",
                (amount, client_id),
            )
            self._conn.commit()

    def log_event(self, client_id: str, event: str, data: dict):
        with self._lock:
            self._conn.execute(
                "INSERT INTO client_events (client_id, event, data) VALUES (?, ?, ?)",
                (client_id, event, json.dumps(data)),
            )
            self._conn.commit()

    def get_events(self, client_id: str) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM client_events WHERE client_id = ? ORDER BY created_at DESC LIMIT 30",
            (client_id,),
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["data"] = json.loads(d["data"])
            result.append(d)
        return result

    @staticmethod
    def _parse(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["booking_data"] = json.loads(d["booking_data"])
        return d
