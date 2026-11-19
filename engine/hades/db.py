from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Engine,
    Float,
    Integer,
    String,
    TypeDecorator,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that always returns tz-aware UTC values.

    SQLite does not persist tzinfo, so values are normalised to UTC on write
    and tagged as UTC on read. Naive inputs are assumed to already be UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


TZDateTime = UTCDateTime


class Base(DeclarativeBase):
    pass


class Token(Base):
    __tablename__ = "tokens"

    mint: Mapped[str] = mapped_column(String, primary_key=True)
    symbol: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    deployer: Mapped[str | None] = mapped_column(String, index=True)
    pool_address: Mapped[str | None] = mapped_column(String)
    pool_created_slot: Mapped[int | None] = mapped_column(Integer)
    pool_created_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    first_seen_at: Mapped[datetime | None] = mapped_column(TZDateTime, default=utcnow)
    peak_mcap_usd: Mapped[float | None] = mapped_column(Float)
    is_runner: Mapped[bool] = mapped_column(Boolean, default=False)


class Swap(Base):
    __tablename__ = "swaps"

    signature: Mapped[str] = mapped_column(String, primary_key=True)
    wallet: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    mint: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    slot: Mapped[int] = mapped_column(Integer, index=True)
    block_time: Mapped[datetime] = mapped_column(TZDateTime)
    side: Mapped[str] = mapped_column(String)  # 'buy' | 'sell'
    token_amount: Mapped[float] = mapped_column(Float)
    sol_amount: Mapped[float | None] = mapped_column(Float)  # None = not SOL-quoted
    source: Mapped[str] = mapped_column(String)  # 'backfill' | 'webhook'
    dex: Mapped[str | None] = mapped_column(String)


class Wallet(Base):
    __tablename__ = "wallets"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    first_funder: Mapped[str | None] = mapped_column(String, index=True)
    funded_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    label: Mapped[str | None] = mapped_column(String)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    last_backfilled_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class WalletStats(Base):
    __tablename__ = "wallet_stats"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    window_days: Mapped[int] = mapped_column(Integer, default=30)
    realized_pnl_sol: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl_sol: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl_sol: Mapped[float] = mapped_column(Float, default=0.0)
    tokens_traded: Mapped[int] = mapped_column(Integer, default=0)
    tokens_closed: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float | None] = mapped_column(Float)
    median_hold_sec: Mapped[float | None] = mapped_column(Float)
    trades_per_day: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class WalletTag(Base):
    __tablename__ = "wallet_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String, index=True)
    # 'deployer_link' | 'sniper' | 'bundle' | 'repeat_early' | 'bot'
    tag: Mapped[str] = mapped_column(String, index=True)
    mint: Mapped[str | None] = mapped_column(String)
    evidence_json: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Watchlist(Base):
    __tablename__ = "watchlist"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    note: Mapped[str | None] = mapped_column(String)
    added_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(String, index=True)
    mint: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    sol_amount: Mapped[float | None] = mapped_column(Float)
    signature: Mapped[str] = mapped_column(String, index=True)
    block_time: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    delivered_tg: Mapped[bool] = mapped_column(Boolean, default=False)


def make_engine(url: str) -> Engine:
    """Create an engine; for SQLite, enable WAL and allow cross-thread use."""
    if not url.startswith("sqlite"):
        return create_engine(url)

    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if url in ("sqlite://", "sqlite:///:memory:"):
        # Single shared connection so every session sees the same in-memory DB.
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on error."""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
