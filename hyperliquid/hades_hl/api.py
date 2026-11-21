"""The Hyperliquid head API (spec 9: the head contract, identical on every head).

Every ``/api/*`` route requires ``X-Hades-Key`` (constant-time compare against
``HL_API_KEY``). With an empty key the API fails closed with 503. Docs and OpenAPI are
disabled. Served on 127.0.0.1:8010 by ``hades-hl serve``.

- ``GET /api/health``: ``HeadHealth``; ``last_ingest_at`` = the newest stored fill.
- ``GET /api/wallets?sort=score|pnl&limit<=200``: ``WalletRow``s (bots and vaults
  hidden unless ``hide_bots=false``). ``winrate`` is never served without ``n > 0``.
- ``GET /api/wallets/{address}``: a ``WalletRow`` whose ``extra`` adds avg_leverage,
  liquidations, funding_usd (T5 stats), tags with evidence (T7), ``pnl_curve`` (daily
  cumulative closedPnl - fees + funding of the stored fills over the stats window) and
  the follow, if any.
- ``GET /api/wallets/{address}/positions``: the wallet's live open positions on the
  main perp dex (Task 14, P64), from HL ``clearinghouseState`` through the shared
  client, cached 5 s per address. 400 on a malformed address, 404 for a wallet this
  head does not know, 502 with a fixed message when the upstream call fails or
  answers an unexpected body (the upstream body is never forwarded).
- ``GET /api/follows``, ``POST/DELETE /api/follows/{address}``: manual follow (409 over
  the cap) and manual removal (remembered: never auto-followed again).
- ``GET /api/paper/{address}``: the paper runtime's snapshot (T9): summary, fills,
  equity curve, both gates with checks, ladder history, ``ladder_cooldown_until`` (a
  promotion held back after a demotion), gap (P49), ``missed_count``, plus
  ``median_delay_ms`` of the follow's observed events. The last curve point and the
  summary equity are the display-only "now" point (P54); the gates read persisted
  points only.
- ``GET /api/alerts?limit=`` and ``GET /api/stream`` (SSE): the contract ``Alert``
  shape. The stream polls the alerts table for rows newer than its start (so alerts
  written by the runtime, in this process or another, reach it) and sends a ``ping``
  after ``heartbeat_s`` (15 s) without one.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import statistics
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from hades_core.contract import Alert as AlertModel
from hades_core.contract import HeadHealth, PaperSummary, WalletRow
from hades_core.gate import GateResult
from hades_hl import HEAD, __version__, alerts, pipeline
from hades_hl import follows as fl
from hades_hl import paper_runtime as pr
from hades_hl import positions as pos
from hades_hl.client import HLClient
from hades_hl.config import Settings, get_settings
from hades_hl.db import Alert, Fill, Follow, LadderHistory, PaperEvent, Tag, Wallet, init_db, make_engine
from hades_hl.normalize import fill_fee, is_spot
from hades_hl.pnl import funding_usdc

HEARTBEAT_S = 15.0
UPSTREAM_DETAIL = "hyperliquid upstream unavailable"  # fixed: never the upstream's words
POLL_S = 1.0
SPARKLINE_POINTS = 50
DAY_MS = 86_400_000
UTC = timezone.utc
LIST_EXTRA = ("roi_on_margin", "daily_sharpe", "max_dd", "avg_leverage", "window_days",
              "history_partial", "score_reason")
DETAIL_EXTRA = ("liquidations", "funding_usd", "realized_usd", "fees_usd", "peak_margin_usd",
                "median_hold_s", "median_fills_per_day", "maker_share", "chain_gaps")


# -- response models (nesting the core contract models) ------------------------------

class FollowRow(BaseModel):
    address: str
    origin: Literal["auto", "manual"]
    started_at: datetime
    ladder: str | None
    summary: PaperSummary | None
    sparkline: list[list[str]] = Field(default_factory=list)


class Unfollowed(BaseModel):
    address: str
    removed: bool


class LadderStep(BaseModel):
    from_: str | None = Field(serialization_alias="from")
    to: str
    at: datetime
    gate: dict[str, Any]


class GateStatus(BaseModel):
    full: GateResult
    rolling: GateResult
    config: dict[str, Any]


class PaperDetail(BaseModel):
    address: str
    active: bool
    follow_started_at: datetime
    computed_at: datetime | None
    summary: PaperSummary | None
    winrate: float | None
    n: int
    fills: list[dict[str, Any]]
    open_positions: list[dict[str, Any]]
    equity_curve: list[list[str]]
    gate: GateStatus | None
    ladder_history: list[LadderStep]
    # set while a promotion is held back after a demotion (I1 hysteresis)
    ladder_cooldown_until: datetime | None = None
    gap_usd: Decimal | None
    leader_realized_usd: Decimal | None
    missed_count: int
    # missed_count by reason (P65): cap | no_price | stale
    missed_reasons: dict[str, int] = {}
    median_delay_ms: int | None
    funding_missing_hours: int | None


# -- dependencies --------------------------------------------------------------------

def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


def require_key(request: Request, x_hades_key: str | None = Header(default=None)) -> None:
    expected: str = request.app.state.settings.API_KEY
    if not expected:
        raise HTTPException(status_code=503, detail="api key not configured")
    if x_hades_key is None or not secrets.compare_digest(
        x_hades_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid api key")


def _address(address: str) -> str:
    try:
        return fl.normalize_address(address)
    except ValueError:
        raise HTTPException(status_code=400, detail="not a Hyperliquid address") from None


# -- helpers -------------------------------------------------------------------------

def _ms_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _tags(session: Session, addresses: list[str]) -> dict[str, list[Tag]]:
    out: dict[str, list[Tag]] = {a: [] for a in addresses}
    if addresses:
        for t in session.scalars(select(Tag).where(Tag.address.in_(addresses))
                                 .order_by(Tag.address, Tag.tag)):
            out[t.address].append(t)
    return out


def _wallet_row(w: Wallet, tags: list[Tag], extra_keys: tuple[str, ...] = LIST_EXTRA,
                extra: dict[str, Any] | None = None) -> WalletRow:
    st = w.stats_json or {}
    n = int(st.get("n") or 0)
    names = [t.tag for t in tags]
    if w.is_vault:
        names.append("vault")
    elif w.is_bot:
        names.append("bot")
    return WalletRow(
        address=w.address, score=w.score,
        net_pnl_usd=Decimal(str(st.get("net_pnl_usd") or 0)),
        trades=int(st.get("trades") or 0),
        winrate=st.get("winrate") if n > 0 else None,  # never a winrate without n
        n=n, tags=names, is_bot=bool(w.is_bot or w.is_vault),
        extra={**{k: st.get(k) for k in extra_keys if k in st}, **(extra or {})},
    )


def pnl_curve(session: Session, w: Wallet) -> list[list[str]]:
    """Daily cumulative net (closedPnl - fees + funding) of the stored perp fills over
    the stats window ``[scored_at - window_days, scored_at]`` (the T5 definition of
    ``net_pnl_usd``); one point per active day, at the day's start."""
    st = w.stats_json or {}
    if w.scored_at is None:
        return []
    end = int(w.scored_at.timestamp() * 1000)
    start = end - int(float(st.get("window_days") or 30) * DAY_MS)
    daily: dict[int, Decimal] = {}
    for f in pipeline.stored_fills(session, w.address, start, end):
        if is_spot(f):
            continue
        d = (int(f["time"]) - start) // DAY_MS
        daily[d] = daily.get(d, Decimal(0)) + Decimal(f["closedPnl"]) - fill_fee(f)
    for r in pipeline.stored_funding(session, w.address, start, end):
        d = (int(r["time"]) - start) // DAY_MS
        daily[d] = daily.get(d, Decimal(0)) + funding_usdc(r)
    out, cum = [], Decimal(0)
    for d in sorted(daily):
        cum += daily[d]
        out.append([_ms_dt(start + d * DAY_MS).isoformat(), str(cum)])
    return out


def _thin(points: list, n: int) -> list:
    if len(points) <= n:
        return list(points)
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


def _follow_row(session: Session, f: Follow) -> FollowRow:
    snap = pr.snapshot(session, f.address)
    return FollowRow(
        address=f.address, origin=f.origin if f.origin in ("auto", "manual") else "manual",
        started_at=f.started_at, ladder=f.ladder,
        summary=PaperSummary(**snap["summary"]) if snap else None,
        sparkline=_thin(snap.get("equity_curve") or [], SPARKLINE_POINTS) if snap else [],
    )


def median_delay_ms(session: Session, addr: str, since: datetime) -> int | None:
    """Median observed delay of ``addr``'s events since ``since`` (the follow start)."""
    delays = [d for d in session.scalars(select(PaperEvent.delay_ms).where(
        PaperEvent.address == addr, PaperEvent.observed_time >= since,
        PaperEvent.delay_ms.is_not(None)))]
    return round(statistics.median(delays)) if delays else None


def _max_alert_id(engine) -> int:
    with Session(engine) as s:
        return int(s.scalar(select(func.max(Alert.id))) or 0)


def _alerts_after(engine, last_id: int) -> list[dict]:
    with Session(engine) as s:
        rows = s.scalars(select(Alert).where(Alert.id > last_id).order_by(Alert.id))
        return [alerts.to_contract(a).model_dump(mode="json") for a in rows]


def _frame(event: str, data: Any, id_: int | None = None) -> str:
    head = f"id: {id_}\n" if id_ is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data)}\n\n"


async def alert_events(engine, is_disconnected: Callable[[], Awaitable[bool]], last_id: int,
                       heartbeat_s: float, poll_s: float) -> AsyncIterator[str]:
    """SSE frames: ``alert`` (contract Alert JSON, ``id`` = alert id) for every alert row
    newer than ``last_id``, and ``ping`` after ``heartbeat_s`` without an alert."""
    loop = asyncio.get_running_loop()
    idle_since = loop.time()
    while not await is_disconnected():
        rows = await run_in_threadpool(_alerts_after, engine, last_id)
        for a in rows:
            last_id = a["id"]
            yield _frame("alert", a, last_id)
        if rows:
            idle_since = loop.time()
        elif loop.time() - idle_since >= heartbeat_s:
            yield _frame("ping", {"t": datetime.now(UTC).isoformat()})
            idle_since = loop.time()
        await asyncio.sleep(poll_s)


# -- routes --------------------------------------------------------------------------

router = APIRouter(prefix="/api", dependencies=[Depends(require_key)])


@router.get("/health", response_model=HeadHealth)
def health(session: Session = Depends(get_session)) -> HeadHealth:
    try:
        last = session.scalar(select(func.max(Fill.time)))
        ok = True
    except SQLAlchemyError:
        last, ok = None, False
    return HeadHealth(head=HEAD, ok=ok, last_ingest_at=None if last is None else _ms_dt(last),
                      version=__version__)


@router.get("/wallets", response_model=list[WalletRow])
def list_wallets(
    sort: Literal["score", "pnl"] = "score",
    hide_bots: bool = True,
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> list[WalletRow]:
    col = Wallet.score if sort == "score" else Wallet.stats_json["net_pnl_usd"].as_float()
    stmt = select(Wallet)
    if hide_bots:
        stmt = stmt.where(Wallet.is_bot.is_(False), Wallet.is_vault.is_(False))
    ws = list(session.scalars(stmt.order_by(col.is_(None), col.desc(), Wallet.address)
                              .limit(limit)))
    tags = _tags(session, [w.address for w in ws])
    return [_wallet_row(w, tags[w.address]) for w in ws]


@router.get("/wallets/{address}", response_model=WalletRow)
def wallet_detail(address: str, session: Session = Depends(get_session)) -> WalletRow:
    addr = _address(address)
    w = session.get(Wallet, addr)
    if w is None:
        raise HTTPException(status_code=404, detail="wallet not found")
    tags = _tags(session, [addr])[addr]
    f = session.get(Follow, addr)
    follow = None if f is None or f.ladder is None else {
        "origin": f.origin, "started_at": f.started_at.isoformat(), "ladder": f.ladder,
        "active": f.removed_at is None}
    st = w.stats_json or {}
    extra = {
        "avg_leverage": st.get("avg_leverage"),
        "tags": [{"tag": t.tag, "evidence": t.evidence_json,
                  "created_at": t.created_at.isoformat() if t.created_at else None}
                 for t in tags],
        "pnl_curve": pnl_curve(session, w),
        "follow": follow,
    }
    return _wallet_row(w, tags, LIST_EXTRA + DETAIL_EXTRA, extra)


def hl_client(app: FastAPI) -> Any:
    """The HL client of this app: the runtime's (``serve`` shares it, so the API is
    charged to the same rate weight budget), else one made on first use and closed
    with the app."""
    if app.state.hl_client is None:
        app.state.hl_client = HLClient.from_settings(app.state.settings)
        app.state.owns_hl_client = True
    return app.state.hl_client


def _wallet_known(engine, addr: str) -> bool:
    with Session(engine) as s:
        return s.get(Wallet, addr) is not None


@router.get("/wallets/{address}/positions", response_model=pos.WalletPositions)
async def wallet_positions(address: str, request: Request) -> pos.WalletPositions:
    """Live open positions (P64): one upstream call per address per 5 s at most."""
    addr = _address(address)
    app = request.app
    if not await run_in_threadpool(_wallet_known, app.state.engine, addr):
        raise HTTPException(status_code=404, detail="wallet not found")

    async def fetch() -> pos.WalletPositions:
        state = await asyncio.wait_for(hl_client(app).clearinghouse_state(addr),
                                       pos.UPSTREAM_TIMEOUT_S)
        return pos.positions_from_state(addr, state, app.state.clock())

    try:
        return await app.state.positions.get(addr, fetch)
    except pos.PositionsUnavailable:
        raise HTTPException(status_code=502, detail=UPSTREAM_DETAIL) from None


@router.get("/follows", response_model=list[FollowRow])
def list_follows(session: Session = Depends(get_session)) -> list[FollowRow]:
    return [_follow_row(session, f) for f in fl.active_follows(session)]


@router.post("/follows/{address}", response_model=FollowRow)
def follow(address: str, request: Request,
           session: Session = Depends(get_session)) -> FollowRow:
    addr = _address(address)
    try:
        f = fl.add_follow(session, addr, request.app.state.clock(),
                          cap=request.app.state.settings.MAX_FOLLOWS)
    except fl.FollowCapError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    session.commit()
    return _follow_row(session, f)


@router.delete("/follows/{address}", response_model=Unfollowed)
def unfollow(address: str, request: Request,
             session: Session = Depends(get_session)) -> Unfollowed:
    addr = _address(address)
    removed = fl.remove_follow(session, addr, request.app.state.clock())
    session.commit()
    return Unfollowed(address=addr, removed=removed)


@router.get("/paper/{address}", response_model=PaperDetail, response_model_by_alias=True)
def paper(address: str, session: Session = Depends(get_session)) -> PaperDetail:
    addr = _address(address)
    f = session.get(Follow, addr)
    if f is None or f.ladder is None:  # never followed (a bare removal record has no ladder)
        raise HTTPException(status_code=404, detail="never followed")
    snap = pr.snapshot(session, addr) or {}
    history = [LadderStep(from_=h.from_, to=h.to, at=h.at, gate=h.gate_json or {})
               for h in session.scalars(select(LadderHistory)
                                        .where(LadderHistory.address == addr)
                                        .order_by(LadderHistory.at, LadderHistory.id))]
    n = int(snap.get("n") or 0)
    gate = None
    if snap.get("gate") and snap.get("rolling"):
        gate = GateStatus(full=GateResult(**snap["gate"]), rolling=GateResult(**snap["rolling"]),
                          config=pr.gate_config())

    def dec(k: str) -> Decimal | None:
        return None if snap.get(k) is None else Decimal(str(snap[k]))

    return PaperDetail(
        address=addr, active=f.removed_at is None, follow_started_at=f.started_at,
        computed_at=snap.get("computed_at"),
        summary=PaperSummary(**snap["summary"]) if snap.get("summary") else None,
        winrate=snap.get("winrate") if n > 0 else None, n=n,
        fills=snap.get("fills") or [], open_positions=snap.get("open_positions") or [],
        equity_curve=snap.get("equity_curve") or [], gate=gate, ladder_history=history,
        ladder_cooldown_until=snap.get("ladder_cooldown_until"),
        gap_usd=dec("gap_usd"), leader_realized_usd=dec("leader_realized_usd"),
        missed_count=int(snap.get("missed") or 0),
        missed_reasons=snap.get("missed_reasons") or {},
        median_delay_ms=median_delay_ms(session, addr, f.started_at),
        funding_missing_hours=snap.get("funding_missing_hours"),
    )


@router.get("/alerts", response_model=list[AlertModel])
def list_alerts(limit: int = Query(50, ge=1, le=500),
                session: Session = Depends(get_session)) -> list[AlertModel]:
    rows = session.scalars(select(Alert).order_by(Alert.id.desc()).limit(limit))
    return [alerts.to_contract(a) for a in rows]


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """SSE: alerts newer than the connection, and a ``ping`` every heartbeat_s idle."""
    app = request.app
    last_id = await run_in_threadpool(_max_alert_id, app.state.engine)
    events = alert_events(app.state.engine, request.is_disconnected, last_id,
                          app.state.heartbeat_s, app.state.poll_s)
    return StreamingResponse(events, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.DATABASE_URL)
    init_db(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if app.state.owns_hl_client and app.state.hl_client is not None:
            await app.state.hl_client.aclose()

    app = FastAPI(title="HADES Hyperliquid", version=__version__, docs_url=None,
                  redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.heartbeat_s = HEARTBEAT_S
    app.state.poll_s = POLL_S
    app.state.clock = lambda: datetime.now(UTC)
    app.state.hl_client = None  # set by ``serve`` (the runtime's client); else lazy
    app.state.owns_hl_client = False
    app.state.positions = pos.PositionsCache()
    app.include_router(router)
    return app


__all__ = ["HEARTBEAT_S", "UPSTREAM_DETAIL", "alert_events", "create_app", "hl_client",
           "median_delay_ms", "pnl_curve", "require_key"]
