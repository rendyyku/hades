"""SQLite schema for the Hyperliquid head (SQLAlchemy 2).

Conventions:
- HL timestamps (fills, funding, prices) are stored as integer epoch **ms**, exactly
  as the API returns them; our own wall-clock columns are tz-aware UTC datetimes.
- HL decimal strings (px, sz, pnl, fee, usdc, mid) are stored as TEXT and read back
  as ``Decimal`` so no precision is lost (SQLite has no exact NUMERIC type).
"""

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Engine,
    Index,
    Integer,
    String,
    TypeDecorator,
    create_engine,
    event,
    false,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """DateTime that always returns tz-aware UTC (SQLite drops tzinfo)."""

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


class DecimalText(TypeDecorator):
    """Exact decimal stored as TEXT; accepts str/int/Decimal, returns Decimal."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("DecimalText refuses float; pass str or Decimal")
        return str(Decimal(value))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    pass


class Wallet(Base):
    __tablename__ = "wallets"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    is_vault: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    score: Mapped[float | None] = mapped_column(index=True)
    stats_json: Mapped[Any] = mapped_column(JSON, default=dict)
    scored_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Fill(Base):
    __tablename__ = "fills"

    tid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    address: Mapped[str] = mapped_column(String, index=True)
    coin: Mapped[str] = mapped_column(String, index=True)
    px: Mapped[Decimal] = mapped_column(DecimalText)
    sz: Mapped[Decimal] = mapped_column(DecimalText)
    side: Mapped[str] = mapped_column(String)  # 'B' | 'A'
    dir: Mapped[str] = mapped_column(String)  # 'Open Long', 'Close Short', ...
    start_position: Mapped[Decimal] = mapped_column(DecimalText)
    closed_pnl: Mapped[Decimal] = mapped_column(DecimalText)
    fee: Mapped[Decimal] = mapped_column(DecimalText)
    time: Mapped[int] = mapped_column(BigInteger)  # epoch ms
    hash: Mapped[str | None] = mapped_column(String)
    crossed: Mapped[bool] = mapped_column(Boolean)  # True = taker
    liquidation: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # liquidation.liquidatedUser (lowercase): tells our own liquidation from one where
    # this wallet was the counterparty / liquidator (normalize.is_own_liquidation)
    liquidated_user: Mapped[str | None] = mapped_column(String)
    builder_fee: Mapped[Decimal | None] = mapped_column(DecimalText)  # ruling P20

    __table_args__ = (Index("ix_fills_address_time", "address", "time"),)


class Funding(Base):
    __tablename__ = "funding"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    coin: Mapped[str] = mapped_column(String, primary_key=True)
    time: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # epoch ms
    usdc: Mapped[Decimal] = mapped_column(DecimalText)  # signed: + received, - paid


class Tag(Base):
    __tablename__ = "tags"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    tag: Mapped[str] = mapped_column(String, primary_key=True)
    evidence_json: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Follow(Base):
    __tablename__ = "follows"

    address: Mapped[str] = mapped_column(String, primary_key=True)
    origin: Mapped[str] = mapped_column(String)  # 'auto' | 'manual'
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    removed_manually: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    ladder: Mapped[str | None] = mapped_column(String)
    ladder_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class LadderHistory(Base):
    __tablename__ = "ladder_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    address: Mapped[str] = mapped_column(String, index=True)
    from_: Mapped[str | None] = mapped_column("from_", String)
    to: Mapped[str] = mapped_column(String)
    gate_json: Mapped[Any] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Price(Base):
    __tablename__ = "prices"

    coin: Mapped[str] = mapped_column(String, primary_key=True)
    time: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # epoch ms
    mid: Mapped[Decimal] = mapped_column(DecimalText)


class PaperEvent(Base):
    __tablename__ = "paper_events"

    source_ref: Mapped[str] = mapped_column(String, primary_key=True)
    address: Mapped[str] = mapped_column(String, index=True)
    json: Mapped[Any] = mapped_column(JSON)
    observed_time: Mapped[datetime] = mapped_column(UTCDateTime)
    delay_ms: Mapped[int | None] = mapped_column(Integer)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String)
    address: Mapped[str] = mapped_column(String, index=True)
    coin: Mapped[str | None] = mapped_column(String)
    source_ref: Mapped[str] = mapped_column(String)
    payload_json: Mapped[Any] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("ux_alerts_kind_address_ref", "kind", "address", "source_ref", unique=True),
    )


class Kv(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


def make_engine(url: str) -> Engine:
    """Create an engine; for SQLite, enable WAL and allow cross-thread use."""
    if not url.startswith("sqlite"):
        return create_engine(url)
    kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
    if url in ("sqlite://", "sqlite:///:memory:"):
        kwargs["poolclass"] = StaticPool  # one shared in-memory DB
    engine = create_engine(url, **kwargs)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


# Columns added after the initial schema: (table, column, SQL type). Empty for v1;
# later tasks append here so old DBs gain columns on init_db.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("fills", "liquidated_user", "VARCHAR"),
    ("fills", "builder_fee", "VARCHAR"),
)


def _existing_columns(conn, table: str) -> set[str]:
    if conn.dialect.name == "sqlite":
        return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
    return {c["name"] for c in inspect(conn).get_columns(table)}


def init_db(engine: Engine) -> None:
    """Create missing tables, then add columns missing from older databases."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, column, sql_type in _ADDED_COLUMNS:
            if column not in _existing_columns(conn, table):
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))


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


def fill_row(address: str, f: dict) -> dict:
    """Map one HL fill dict (userFills / userFillsByTime / WS userFills) to a fills row."""
    return {
        "tid": int(f["tid"]),
        "address": address,
        "coin": f["coin"],
        "px": f["px"],
        "sz": f["sz"],
        "side": f["side"],
        "dir": f["dir"],
        "start_position": f["startPosition"],
        "closed_pnl": f["closedPnl"],
        "fee": f["fee"],
        "time": int(f["time"]),
        "hash": f.get("hash"),
        "crossed": bool(f["crossed"]),
        # HL attaches a ``liquidation`` object only to liquidation fills.
        "liquidation": bool(f.get("liquidation")),
        "liquidated_user": _liquidated_user(f),
        "builder_fee": f.get("builderFee"),
    }


def _liquidated_user(f: dict) -> str | None:
    liq = f.get("liquidation")
    victim = liq.get("liquidatedUser") if isinstance(liq, dict) else None
    return str(victim).lower() if victim else None


def funding_row(address: str, r: dict) -> dict:
    """Map one HL ``userFunding`` row to a funding row."""
    delta = r.get("delta") or {}
    return {"address": address, "coin": delta["coin"], "time": int(r["time"]),
            "usdc": str(delta.get("usdc") or "0")}


def insert_funding(session: Session, address: str, rows: Iterable[dict]) -> int:
    """Insert funding payments, skipping any (address, coin, time) already stored."""
    mapped: dict[tuple[str, int], dict] = {}
    for r in rows:
        m = funding_row(address, r)
        mapped.setdefault((m["coin"], m["time"]), m)
    if not mapped:
        return 0
    times = sorted({t for _, t in mapped})
    existing: set[tuple[str, int]] = set()
    for i in range(0, len(times), 500):
        chunk = times[i : i + 500]
        existing.update(
            (c, t) for c, t in session.execute(
                select(Funding.coin, Funding.time).where(
                    Funding.address == address, Funding.time.in_(chunk)))
        )
    new_rows = [m for k, m in mapped.items() if k not in existing]
    if new_rows:
        _insert_ignore(session, Funding.__table__, new_rows)
    return len(new_rows)


def _insert_ignore(session: Session, table, rows: list[dict]) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise NotImplementedError(f"insert-ignore not implemented for {dialect}")
    session.execute(insert(table).on_conflict_do_nothing(), rows)


def insert_fills(session: Session, address: str, fills: Iterable[dict]) -> int:
    """Insert fills, silently skipping any ``tid`` already stored. Returns rows inserted."""
    seen: set[int] = set()
    rows = []
    for f in fills:
        r = fill_row(address, f)
        if r["tid"] in seen:
            continue
        seen.add(r["tid"])
        rows.append(r)
    if not rows:
        return 0
    existing: set[int] = set()
    tids = [r["tid"] for r in rows]
    for i in range(0, len(tids), 500):  # stay under SQLite's bound-parameter limit
        chunk = tids[i : i + 500]
        existing.update(session.scalars(select(Fill.tid).where(Fill.tid.in_(chunk))))
    new_rows = [r for r in rows if r["tid"] not in existing]
    if new_rows:
        # ON CONFLICT DO NOTHING still guards against a concurrent writer.
        _insert_ignore(session, Fill.__table__, new_rows)
    return len(new_rows)
