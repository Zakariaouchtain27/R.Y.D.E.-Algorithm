import logging
import sqlite3
import threading
from typing import List, Optional

log = logging.getLogger(__name__)


def make_route_key(origin: str, destination: str, departure_date: str) -> str:
    """e.g. 'JFK-CDG-2026-09-15'"""
    return f"{origin.upper()}-{destination.upper()}-{departure_date}"


class PriceHistory:
    """
    Thread-safe SQLite price history store.
    Shares the same ryde.db as BookingStore — no extra setup needed.
    """

    def __init__(self, db_path: str = "ryde.db"):
        self._db   = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db, check_same_thread=False)

    def _init_db(self):
        with self._lock, self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    route_key       TEXT    NOT NULL,
                    captured_at     TEXT    NOT NULL DEFAULT (datetime('now')),
                    price           REAL    NOT NULL,
                    seats_remaining INTEGER,
                    days_to_dep     INTEGER
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ps_route
                ON price_snapshots (route_key, captured_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rebooking_outcomes (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    booking_id      TEXT    NOT NULL,
                    route_key       TEXT    NOT NULL,
                    decided_at      TEXT    NOT NULL DEFAULT (datetime('now')),
                    original_price  REAL    NOT NULL,
                    rebooked_price  REAL    NOT NULL,
                    savings         REAL    NOT NULL,
                    success         INTEGER NOT NULL
                )
            """)
            conn.commit()

    def record_snapshot(
        self,
        route_key: str,
        price: float,
        seats_remaining: Optional[int] = None,
        days_to_dep: Optional[int] = None,
    ):
        """
        Persist one price observation.
        Prices ≤ 0 are silently dropped — one bad API response should not
        corrupt the OU fit for the entire route.
        """
        if price <= 0:
            log.warning("Skipping invalid price %.2f for route %s", price, route_key)
            return

        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO price_snapshots
                       (route_key, price, seats_remaining, days_to_dep)
                   VALUES (?, ?, ?, ?)""",
                (route_key, price, seats_remaining, days_to_dep),
            )
            conn.commit()

    def record_outcome(
        self,
        booking_id: str,
        route_key: str,
        original_price: float,
        rebooked_price: float,
        savings: float,
        success: bool,
    ):
        with self._lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO rebooking_outcomes
                       (booking_id, route_key, original_price,
                        rebooked_price, savings, success)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (booking_id, route_key, original_price,
                 rebooked_price, savings, int(success)),
            )
            conn.commit()

    def get_price_series(self, route_key: str, max_rows: int = 500) -> List[float]:
        """Chronological price list for OU model fitting."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT price FROM price_snapshots
                   WHERE route_key = ?
                   ORDER BY captured_at ASC LIMIT ?""",
                (route_key, max_rows),
            ).fetchall()
        return [r[0] for r in rows]

    def get_reference_price(self, route_key: str, last_n: int = 30) -> Optional[float]:
        """
        Rolling median of the most recent N price snapshots.

        Using all-time median would anchor the OU long-run mean to stale
        prices on routes that have been trending down for months.
        30 snapshots at hourly polling = last 30 hours of market data.
        """
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT price FROM price_snapshots
                   WHERE route_key = ?
                   ORDER BY captured_at DESC LIMIT ?""",
                (route_key, last_n),
            ).fetchall()
        if not rows:
            return None
        prices = sorted(r[0] for r in rows)
        return prices[len(prices) // 2]

    def get_booking_velocity(self, route_key: str, window_days: int = 7) -> Optional[float]:
        """Seats sold per day over recent window (inferred from seat count drop)."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT seats_remaining FROM price_snapshots
                   WHERE route_key = ? AND seats_remaining IS NOT NULL
                   ORDER BY captured_at DESC LIMIT ?""",
                (route_key, window_days * 24),
            ).fetchall()
        if len(rows) < 2:
            return None
        first_seats = rows[-1][0]   # oldest in window
        last_seats  = rows[0][0]    # most recent
        seats_sold  = max(0, first_seats - last_seats)
        return seats_sold / max(window_days, 1)

    def get_historical_max_drop(self, route_key: str) -> Optional[float]:
        """Largest consecutive price drop observed for this route."""
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                """SELECT price FROM price_snapshots
                   WHERE route_key = ? ORDER BY captured_at ASC""",
                (route_key,),
            ).fetchall()
        if len(rows) < 2:
            return None
        prices   = [r[0] for r in rows]
        max_drop = max(
            (prices[i - 1] - prices[i] for i in range(1, len(prices))),
            default=0.0,
        )
        return max_drop if max_drop > 0 else None
