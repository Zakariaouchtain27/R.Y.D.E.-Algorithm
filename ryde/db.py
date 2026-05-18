"""
Async SQLAlchemy engine, session factory, and table schema.

DATABASE_URL is auto-detected from the environment:
  Railway/production  postgres(ql)://...  → postgresql+asyncpg://...
  Local dev (unset)                       → sqlite+aiosqlite:///ryde.db
"""
import logging
import os
import re

from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

log = logging.getLogger(__name__)

_raw_url = os.getenv("DATABASE_URL", "")


def _make_async_url(url: str) -> str:
    if not url:
        return "sqlite+aiosqlite:///ryde.db"
    return re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", url)


ASYNC_URL = _make_async_url(_raw_url)
IS_PG = ASYNC_URL.startswith("postgresql")

_engine_kwargs: dict = {"echo": False, "pool_pre_ping": IS_PG}
if IS_PG:
    _engine_kwargs.update(
        poolclass=AsyncAdaptedQueuePool,
        pool_size=10,
        max_overflow=20,
    )
else:
    _engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(ASYNC_URL, **_engine_kwargs)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)

metadata = MetaData()

bookings_table = Table(
    "bookings", metadata,
    Column("booking_id", String, primary_key=True),
    Column("data", Text, nullable=False),
    Column("active", Integer, nullable=False, default=1),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

audit_log_table = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("booking_id", String, nullable=False, index=True),
    Column("agency", String, nullable=False, default=""),
    Column("event", String, nullable=False),
    Column("detail", Text, nullable=False, default="{}"),
    Column("created_at", String, nullable=False),
)

idempotency_table = Table(
    "idempotency_keys", metadata,
    Column("idem_key", String, primary_key=True),
    Column("tracking_id", String, nullable=False),
    Column("response", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

agencies_table = Table(
    "agencies", metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False),
    Column("api_key", String, unique=True, nullable=False),
    Column("environment", String, nullable=False, default="test"),
    Column("active", Integer, nullable=False, default=1),
    Column("total_calls", Integer, nullable=False, default=0),
    Column("last_call_at", String),
    Column("created_at", String, nullable=False),
    Column("ls_subscription_id", String),
    Column("ls_order_id", String),
    Column("stripe_customer_id", String),
    Column("subscription_tier", String, default="free"),
    Column("notification_config", Text, default="{}"),
)

clients_table = Table(
    "clients", metadata,
    Column("client_id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False),
    Column("stripe_customer_id", String),
    Column("stripe_payment_method", String),
    Column("booking_data", Text, nullable=False),
    Column("monitoring_active", Integer, nullable=False, default=0),
    Column("total_savings", Float, nullable=False, default=0.0),
    Column("created_at", String, nullable=False),
)

client_events_table = Table(
    "client_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("client_id", String, nullable=False),
    Column("event", String, nullable=False),
    Column("data", Text, nullable=False),
    Column("created_at", String, nullable=False),
)


def _dialect_insert(table):
    """Dialect-specific INSERT supporting on_conflict_do_update / on_conflict_do_nothing."""
    if IS_PG:
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table)


async def init_db() -> None:
    """Create all tables (idempotent, never drops data) then run column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all, checkfirst=True)
    await _migrate_schema()
    log.info("Database ready", extra={"backend": "postgresql" if IS_PG else "sqlite"})


async def _migrate_schema() -> None:
    """Add columns introduced after initial deploy without touching existing rows."""
    migrations = [
        ("agencies", "ls_subscription_id", "TEXT"),
        ("agencies", "ls_order_id", "TEXT"),
        ("agencies", "stripe_customer_id", "TEXT"),
        ("agencies", "subscription_tier", "TEXT DEFAULT 'free'"),
        ("agencies", "notification_config", "TEXT DEFAULT '{}'"),
    ]
    async with engine.begin() as conn:
        for tbl, col, col_type in migrations:
            if IS_PG:
                sql = f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {col_type}"
            else:
                sql = f"ALTER TABLE {tbl} ADD COLUMN {col} {col_type}"
            try:
                await conn.execute(text(sql))
            except Exception:
                pass  # column already present
