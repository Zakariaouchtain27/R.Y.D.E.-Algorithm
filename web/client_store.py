"""
Async ClientStore — SQLAlchemy + asyncpg (prod) / aiosqlite (dev).
All public methods are coroutines; callers must await them.
"""
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import delete, insert, select, update

from ryde.db import (
    AsyncSessionLocal, _dialect_insert,
    clients_table, client_events_table,
)

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(row) -> dict:
    d = dict(row._mapping)
    d["booking_data"] = json.loads(d["booking_data"])
    return d


class ClientStore:
    def __init__(self, db_path: str = "ryde.db"):
        pass  # engine / session managed globally in ryde.db

    async def create_client(
        self,
        client_id: str,
        name: str,
        email: str,
        stripe_customer_id: Optional[str],
        booking_data: dict,
    ) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    insert(clients_table).values(
                        client_id=client_id,
                        name=name,
                        email=email,
                        stripe_customer_id=stripe_customer_id,
                        booking_data=json.dumps(booking_data),
                        created_at=_now(),
                    )
                )

    async def get_client(self, client_id: str) -> Optional[dict]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(clients_table)
                .where(clients_table.c.client_id == client_id)
            )).fetchone()
        return _parse(row) if row else None

    async def get_client_by_booking_ref(self, booking_ref: str) -> Optional[dict]:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(clients_table)
                .where(clients_table.c.monitoring_active == 1)
            )).fetchall()
        for row in rows:
            d = _parse(row)
            if d["booking_data"].get("booking_ref") == booking_ref:
                return d
        return None

    async def update_stripe_customer(self, client_id: str, stripe_customer_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(clients_table)
                    .where(clients_table.c.client_id == client_id)
                    .values(stripe_customer_id=stripe_customer_id)
                )

    async def delete_client(self, client_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    delete(clients_table)
                    .where(clients_table.c.client_id == client_id)
                )

    async def save_payment_method(self, client_id: str, payment_method_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(clients_table)
                    .where(clients_table.c.client_id == client_id)
                    .values(stripe_payment_method=payment_method_id)
                )

    async def activate_monitoring(self, client_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(clients_table)
                    .where(clients_table.c.client_id == client_id)
                    .values(monitoring_active=1)
                )

    async def add_savings(self, client_id: str, amount: float) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(clients_table)
                    .where(clients_table.c.client_id == client_id)
                    .values(total_savings=clients_table.c.total_savings + amount)
                )

    async def log_event(self, client_id: str, event: str, data: dict) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    insert(client_events_table).values(
                        client_id=client_id,
                        event=event,
                        data=json.dumps(data),
                        created_at=_now(),
                    )
                )

    async def get_events(self, client_id: str) -> List[dict]:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(client_events_table)
                .where(client_events_table.c.client_id == client_id)
                .order_by(client_events_table.c.created_at.desc())
                .limit(30)
            )).fetchall()
        result = []
        for row in rows:
            d = dict(row._mapping)
            d["data"] = json.loads(d["data"])
            result.append(d)
        return result
