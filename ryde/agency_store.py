"""
Agency and API key management — async SQLAlchemy backend.
All public methods are coroutines; callers must await them.

TIER_LIMITS is exported and used by api_v1 for rate-limiting and booking caps.
seed_dev_keys() is called from the app lifespan (not __init__) since it is async.
"""
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import insert, select, update

from .db import AsyncSessionLocal, _dialect_insert, agencies_table

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subscription tier limits (exported to api_v1 and app)
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _from_row(row) -> Agency:
    try:
        nc = json.loads(row.notification_config or "{}")
    except Exception:
        nc = {}
    return Agency(
        id=row.id,
        name=row.name,
        email=row.email,
        api_key=row.api_key,
        environment=row.environment,
        active=bool(row.active),
        total_calls=row.total_calls,
        last_call_at=row.last_call_at,
        created_at=str(row.created_at),
        ls_subscription_id=row.ls_subscription_id,
        ls_order_id=row.ls_order_id,
        stripe_customer_id=row.stripe_customer_id,
        subscription_tier=row.subscription_tier or "free",
        notification_config=nc,
    )


class AgencyStore:
    def __init__(self, db_path: str = "ryde.db"):
        pass  # engine / session managed globally in ryde.db

    @staticmethod
    def generate_key(name: str, environment: str = "test") -> str:
        slug  = re.sub(r"[^a-z0-9]", "", name.lower())[:12]
        token = secrets.token_hex(16)
        return f"ryde_{environment}_{slug}_{token}"

    async def seed_dev_keys(self) -> None:
        """Insert test seeds if they don't already exist (idempotent)."""
        seeds = [
            ("dev_001", "ACME Travel",         "dev@acmetravel.com",   "ryde_dev_test_key_001"),
            ("dev_002", "Globetrotter Agency",  "dev@globetrotter.com", "ryde_dev_test_key_002"),
        ]
        for agency_id, name, email, key in seeds:
            stmt = (
                _dialect_insert(agencies_table)
                .values(
                    id=agency_id, name=name, email=email,
                    api_key=key, environment="test", created_at=_now(),
                )
                .on_conflict_do_nothing()
            )
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await session.execute(stmt)

    async def create_agency(self, name: str, email: str, environment: str = "test") -> Agency:
        agency_id = str(uuid.uuid4())
        api_key   = self.generate_key(name, environment)
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    insert(agencies_table).values(
                        id=agency_id, name=name, email=email,
                        api_key=api_key, environment=environment, created_at=_now(),
                    )
                )
        return await self.get_by_id(agency_id)  # type: ignore

    async def create_agency_ls(
        self,
        name: str,
        email: str,
        environment: str = "live",
        ls_subscription_id: str = "",
        ls_order_id: str = "",
        subscription_tier: str = "free",
    ) -> Agency:
        agency_id = str(uuid.uuid4())
        api_key   = self.generate_key(name, environment)
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    insert(agencies_table).values(
                        id=agency_id, name=name, email=email,
                        api_key=api_key, environment=environment, created_at=_now(),
                        ls_subscription_id=ls_subscription_id,
                        ls_order_id=ls_order_id,
                        subscription_tier=subscription_tier,
                    )
                )
        return await self.get_by_id(agency_id)  # type: ignore

    async def revoke(self, agency_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(agencies_table)
                    .where(agencies_table.c.id == agency_id)
                    .values(active=0)
                )

    async def reactivate(self, agency_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(agencies_table)
                    .where(agencies_table.c.id == agency_id)
                    .values(active=1)
                )

    async def regenerate_key(self, agency_id: str) -> Optional[Agency]:
        agency = await self.get_by_id(agency_id)
        if not agency:
            return None
        new_key = self.generate_key(agency.name, agency.environment)
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(agencies_table)
                    .where(agencies_table.c.id == agency_id)
                    .values(api_key=new_key)
                )
        return await self.get_by_id(agency_id)

    async def set_stripe_customer(self, agency_id: str, stripe_customer_id: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(agencies_table)
                    .where(agencies_table.c.id == agency_id)
                    .values(stripe_customer_id=stripe_customer_id)
                )

    async def set_subscription_tier(self, agency_id: str, tier: str) -> None:
        tier = tier if tier in TIER_LIMITS else "free"
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(agencies_table)
                    .where(agencies_table.c.id == agency_id)
                    .values(subscription_tier=tier)
                )

    async def set_notification_config(self, agency_id: str, config: dict) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(agencies_table)
                    .where(agencies_table.c.id == agency_id)
                    .values(notification_config=json.dumps(config))
                )

    async def log_call(self, api_key: str) -> None:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    update(agencies_table)
                    .where(agencies_table.c.api_key == api_key)
                    .values(
                        total_calls=agencies_table.c.total_calls + 1,
                        last_call_at=_now(),
                    )
                )

    async def get_by_key(self, api_key: str) -> Optional[Agency]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(agencies_table)
                .where(
                    agencies_table.c.api_key == api_key,
                    agencies_table.c.active  == 1,
                )
            )).fetchone()
        return _from_row(row) if row else None

    async def get_by_id(self, agency_id: str) -> Optional[Agency]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(agencies_table).where(agencies_table.c.id == agency_id)
            )).fetchone()
        return _from_row(row) if row else None

    async def get_by_name(self, name: str) -> Optional[Agency]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(agencies_table)
                .where(
                    agencies_table.c.name   == name,
                    agencies_table.c.active == 1,
                )
                .limit(1)
            )).fetchone()
        return _from_row(row) if row else None

    async def get_by_ls_subscription(self, ls_subscription_id: str) -> Optional[Agency]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(agencies_table)
                .where(agencies_table.c.ls_subscription_id == ls_subscription_id)
            )).fetchone()
        return _from_row(row) if row else None

    async def get_by_ls_order(self, ls_order_id: str) -> Optional[Agency]:
        async with AsyncSessionLocal() as session:
            row = (await session.execute(
                select(agencies_table)
                .where(agencies_table.c.ls_order_id == ls_order_id)
            )).fetchone()
        return _from_row(row) if row else None

    async def list_agencies(self) -> List[Agency]:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(agencies_table).order_by(agencies_table.c.created_at.desc())
            )).fetchall()
        return [_from_row(r) for r in rows]
