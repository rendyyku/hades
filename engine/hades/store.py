"""SQLite store (spec 10). Sync SQLAlchemy Core; every call is a short transaction.

Datetimes are stored as UTC ISO-8601 strings and come back timezone-aware.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    JSON, Boolean, Column, Float, ForeignKey, Integer, MetaData, String, Table, Text, TypeDecorator,
    UniqueConstraint, create_engine, delete, event, func, insert, select, update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from hades.models import Enrichment, ListingEvent, Signal

MAX_LIMIT = 200
UNHEALTHY_AFTER = 3
STALE_AFTER = 3  # intervals without a success


class UtcDateTime(TypeDecorator):
    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime")
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")

    def process_result_value(self, value, dialect):
        return None if value is None else datetime.fromisoformat(value)


md = MetaData()

watcher_state = Table(
    "watcher_state", md,
    Column("name", String, primary_key=True),
    Column("cursor", JSON),
    Column("last_ok", UtcDateTime),
    Column("last_err", Text),
    Column("last_err_at", UtcDateTime),
    Column("fail_count", Integer, nullable=False, default=0),
)

signals = Table(
    "signals", md,
    Column("id", Integer, primary_key=True),
    Column("dedupe_key", String, nullable=False, unique=True),
    Column("source", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("severity", String, nullable=False),
    Column("project_key", String, nullable=False, index=True),
    Column("chain", String),
    Column("address", String),
    Column("symbol", String, index=True),
    Column("name", String),
    Column("website", String),
    Column("evidence_url", Text, nullable=False),
    Column("observed_at", UtcDateTime, nullable=False, index=True),
    Column("score", Integer),
    Column("score_detail", JSON),
    Column("listed_on", JSON, nullable=False, default=list),
    Column("raw", JSON, nullable=False, default=dict),
)

enrichments = Table(
    "enrichments", md,
    Column("id", Integer, primary_key=True),
    Column("signal_id", Integer, ForeignKey("signals.id"), nullable=False, index=True),
    Column("checked_at", UtcDateTime, nullable=False),
    Column("provider", String, nullable=False),
    Column("buyable", Boolean),
    Column("liquidity_usd", Float),
    Column("dex", String),
    Column("pool_url", Text),
    Column("volume_24h", Float),
    Column("pool_count", Integer, nullable=False, default=0),
    Column("verified", Boolean, nullable=False, default=True),
    Column("candidates", JSON, nullable=False, default=list),
    Column("error", Text),
)

listing_events = Table(
    "listing_events", md,
    Column("id", Integer, primary_key=True),
    Column("exchange", String, nullable=False),
    Column("symbol", String, nullable=False),
    Column("base_asset", String, nullable=False, index=True),
    Column("first_seen_at", UtcDateTime, nullable=False),
    Column("raw", JSON, nullable=False, default=dict),
    UniqueConstraint("exchange", "symbol"),
)

watch_domains = Table(
    "watch_domains", md,
    Column("domain", String, primary_key=True),
    Column("origin", String, nullable=False),
    Column("added_at", UtcDateTime, nullable=False),
)

aliases = Table(
    "aliases", md,
    Column("alias", String, primary_key=True),
    Column("project_key", String, nullable=False, index=True),
)

mutes = Table(
    "mutes", md,
    Column("target", String, primary_key=True),
    Column("until", UtcDateTime, nullable=False),
)

alerts_sent = Table(
    "alerts_sent", md,
    Column("id", Integer, primary_key=True),
    Column("signal_id", Integer, ForeignKey("signals.id"), nullable=False),
    Column("message_id", Integer),
    Column("sent_at", UtcDateTime, nullable=False),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    def __init__(self, url: str) -> None:
        kw: dict[str, Any] = {}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kw = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
        elif url.startswith("sqlite"):
            kw = {"connect_args": {"check_same_thread": False, "timeout": 5}}
        self.engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        md.create_all(self.engine)

    # -- watcher state ------------------------------------------------------

    def get_state(self, name: str) -> dict | None:
        with self.engine.connect() as c:
            return c.execute(select(watcher_state.c.cursor).where(watcher_state.c.name == name)).scalar()

    def _upsert_state(self, name: str, **values: Any) -> None:
        with self.engine.begin() as c:
            n = c.execute(update(watcher_state).where(watcher_state.c.name == name).values(**values)).rowcount
            if not n:
                c.execute(insert(watcher_state).values(**{"name": name, "fail_count": 0, **values}))

    def save_state(self, name: str, state: dict) -> None:
        self._upsert_state(name, cursor=state)

    def record_ok(self, name: str, at: datetime) -> None:
        self._upsert_state(name, last_ok=at, fail_count=0)

    def record_error(self, name: str, err: str, at: datetime) -> None:
        with self.engine.begin() as c:
            n = c.execute(
                update(watcher_state).where(watcher_state.c.name == name)
                .values(last_err=err[:500], last_err_at=at, fail_count=watcher_state.c.fail_count + 1)
            ).rowcount
            if not n:
                c.execute(insert(watcher_state).values(name=name, last_err=err[:500], last_err_at=at, fail_count=1))

    def watchers(self, now: datetime | None = None, intervals: dict[str, int] | None = None) -> list[dict]:
        """Health per watcher. With `now` + `intervals`, a watcher whose last success is older than
        STALE_AFTER intervals is `stale` and unhealthy (its loop may have died without an error)."""
        with self.engine.connect() as c:
            rows = c.execute(select(watcher_state).order_by(watcher_state.c.name)).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("cursor")
            iv = (intervals or {}).get(d["name"])
            d["stale"] = bool(now and iv and d["last_ok"] and now - d["last_ok"] > timedelta(seconds=STALE_AFTER * iv))
            d["healthy"] = d["fail_count"] < UNHEALTHY_AFTER and not d["stale"]
            out.append(d)
        return out

    # -- signals ------------------------------------------------------------

    def insert_signal(self, sig: Signal) -> int | None:
        try:
            with self.engine.begin() as c:
                return c.execute(insert(signals).values(
                    dedupe_key=sig.dedupe_key, source=sig.source, kind=sig.kind, severity=sig.severity,
                    project_key=sig.project_key, chain=sig.chain, address=sig.address, symbol=sig.symbol,
                    name=sig.name, website=sig.website, evidence_url=sig.evidence_url,
                    observed_at=sig.observed_at, raw=sig.raw, listed_on=[],
                )).inserted_primary_key[0]
        except IntegrityError:
            return None

    def upgrade_signal(self, dedupe_key: str, kind: str, evidence_url: str) -> int | None:
        with self.engine.begin() as c:
            row = c.execute(select(signals.c.id, signals.c.kind, signals.c.evidence_url)
                            .where(signals.c.dedupe_key == dedupe_key)).first()
            if row is None or (row.kind == kind and row.evidence_url == evidence_url):
                return None
            c.execute(update(signals).where(signals.c.id == row.id).values(kind=kind, evidence_url=evidence_url))
            return row.id

    def set_score(self, signal_id: int, score: int, detail: dict) -> None:
        with self.engine.begin() as c:
            c.execute(update(signals).where(signals.c.id == signal_id).values(score=score, score_detail=detail))

    def get_signal(self, signal_id: int) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(signals).where(signals.c.id == signal_id)).mappings().first()
        return dict(row) if row else None

    def list_signals(self, *, source: str | None = None, chain: str | None = None, min_score: int | None = None,
                     buyable: bool | None = None, before_id: int | None = None, limit: int = 50) -> list[dict]:
        q = select(signals)
        if source:
            q = q.where(signals.c.source == source)
        if chain:
            q = q.where(signals.c.chain == chain)
        if min_score is not None:
            q = q.where(signals.c.score >= min_score)
        if before_id is not None:
            q = q.where(signals.c.id < before_id)
        if buyable is not None:
            latest = (select(enrichments.c.buyable).where(enrichments.c.signal_id == signals.c.id)
                      .order_by(enrichments.c.id.desc()).limit(1).scalar_subquery())
            q = q.where(latest.is_(buyable))
        q = q.order_by(signals.c.id.desc()).limit(max(1, min(limit, MAX_LIMIT)))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def signals_since(self, since: datetime, severity: str | None = None) -> list[dict]:
        q = select(signals).where(signals.c.observed_at >= since)
        if severity:
            q = q.where(signals.c.severity == severity)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q.order_by(signals.c.id)).mappings()]

    def top_signals_since(self, since: datetime, limit: int = 10) -> list[dict]:
        q = (select(signals).where(signals.c.observed_at >= since)
             .order_by(signals.c.score.desc().nulls_last(), signals.c.id.desc()).limit(limit))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def signals_by_project(self, project_key: str, exclude_id: int | None = None, limit: int = 20) -> list[dict]:
        keys = self._project_keys(project_key)
        q = select(signals).where(signals.c.project_key.in_(keys))
        if exclude_id is not None:
            q = q.where(signals.c.id != exclude_id)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q.order_by(signals.c.id.desc()).limit(limit)).mappings()]

    # -- enrichments --------------------------------------------------------

    def add_enrichment(self, signal_id: int, e: Enrichment) -> None:
        with self.engine.begin() as c:
            c.execute(insert(enrichments).values(
                signal_id=signal_id, checked_at=e.checked_at, provider=e.provider, buyable=e.buyable,
                liquidity_usd=e.liquidity_usd, dex=e.dex, pool_url=e.pool_url, volume_24h=e.volume_24h,
                pool_count=e.pool_count, verified=e.verified, candidates=e.candidates, error=e.error,
            ))

    def enrichments(self, signal_id: int) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(enrichments).where(enrichments.c.signal_id == signal_id)
                             .order_by(enrichments.c.id)).mappings()
            return [dict(r) for r in rows]

    def due_rechecks(self, now: datetime, after_s: int) -> list[int]:
        cutoff = datetime.fromtimestamp(now.timestamp() - after_s, timezone.utc)
        q = (select(enrichments.c.signal_id).group_by(enrichments.c.signal_id)
             .having(func.count() == 1).having(func.min(enrichments.c.checked_at) <= cutoff)
             .order_by(enrichments.c.signal_id))
        with self.engine.connect() as c:
            return list(c.execute(q).scalars())

    # -- listings -----------------------------------------------------------

    def insert_listing(self, ev: ListingEvent) -> bool:
        try:
            with self.engine.begin() as c:
                c.execute(insert(listing_events).values(
                    exchange=ev.exchange, symbol=ev.symbol, base_asset=ev.base_asset,
                    first_seen_at=ev.first_seen_at, raw=ev.raw,
                ))
            return True
        except IntegrityError:
            return False

    def list_listings(self, limit: int = 100, exchange: str | None = None) -> list[dict]:
        q = select(listing_events)
        if exchange:
            q = q.where(listing_events.c.exchange == exchange)
        q = q.order_by(listing_events.c.id.desc()).limit(max(1, min(limit, MAX_LIMIT)))
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def all_listings(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(select(listing_events).order_by(listing_events.c.id)).mappings()]

    def all_signals(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(select(signals).order_by(signals.c.id)).mappings()]

    def mark_listed(self, signal_id: int, exchange: str, at: datetime) -> None:
        with self.engine.begin() as c:
            cur = c.execute(select(signals.c.listed_on).where(signals.c.id == signal_id)).scalar() or []
            if any(x.get("exchange") == exchange for x in cur):
                return
            cur = [*cur, {"exchange": exchange, "at": at.astimezone(timezone.utc).isoformat(timespec="seconds")}]
            c.execute(update(signals).where(signals.c.id == signal_id).values(listed_on=cur))

    # -- projects / aliases -------------------------------------------------

    def add_alias(self, project_key: str, alias: str) -> None:
        if alias == project_key:
            return
        with self.engine.begin() as c:
            if c.execute(select(aliases.c.alias).where(aliases.c.alias == alias)).first() is None:
                c.execute(insert(aliases).values(alias=alias, project_key=project_key))

    def resolve(self, key: str) -> str:
        with self.engine.connect() as c:
            return c.execute(select(aliases.c.project_key).where(aliases.c.alias == key)).scalar() or key

    def _project_keys(self, key: str) -> list[str]:
        canon = self.resolve(key)
        with self.engine.connect() as c:
            al = list(c.execute(select(aliases.c.alias).where(aliases.c.project_key == canon)).scalars())
        return [canon, *al]

    def sources_for_project(self, project_key: str, since: datetime) -> set[str]:
        keys = self._project_keys(project_key)
        q = select(signals.c.source).where(signals.c.project_key.in_(keys), signals.c.observed_at >= since).distinct()
        with self.engine.connect() as c:
            return set(c.execute(q).scalars())

    # -- watch domains ------------------------------------------------------

    def add_domain(self, domain: str, origin: str) -> bool:
        try:
            with self.engine.begin() as c:
                c.execute(insert(watch_domains).values(domain=domain, origin=origin, added_at=_now()))
            return True
        except IntegrityError:
            return False

    def remove_domain(self, domain: str) -> bool:
        with self.engine.begin() as c:
            return c.execute(delete(watch_domains).where(watch_domains.c.domain == domain)).rowcount > 0

    def domains(self) -> list[dict]:
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(select(watch_domains).order_by(watch_domains.c.domain)).mappings()]

    # -- mutes --------------------------------------------------------------

    def add_mute(self, target: str, until: datetime) -> None:
        with self.engine.begin() as c:
            c.execute(delete(mutes).where(mutes.c.target == target))
            c.execute(insert(mutes).values(target=target, until=until))

    def remove_mute(self, target: str) -> bool:
        with self.engine.begin() as c:
            return c.execute(delete(mutes).where(mutes.c.target == target)).rowcount > 0

    def active_mutes(self, now: datetime) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(mutes).order_by(mutes.c.target)).mappings().all()
        return [dict(r) for r in rows if r["until"] > now]

    # -- alerts -------------------------------------------------------------

    def mark_sent(self, signal_id: int, message_id: int | None, at: datetime) -> None:
        with self.engine.begin() as c:
            c.execute(insert(alerts_sent).values(signal_id=signal_id, message_id=message_id, sent_at=at))

    def counts_since(self, since: datetime) -> dict:
        s = since
        with self.engine.connect() as c:
            n_sig = c.execute(select(func.count()).select_from(signals).where(signals.c.observed_at >= s)).scalar()
            n_alert = c.execute(select(func.count()).select_from(alerts_sent).where(alerts_sent.c.sent_at >= s)).scalar()
            n_list = c.execute(select(func.count()).select_from(listing_events)
                               .where(listing_events.c.first_seen_at >= s)).scalar()
        return {"signals": n_sig, "alerts": n_alert, "listings": n_list}


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
