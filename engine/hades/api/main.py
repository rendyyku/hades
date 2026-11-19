"""HADES read API: overview, wallets, wallet detail, token forensics, watchlist.

Every ``/api/*`` route requires ``X-Hades-Key`` (constant-time compare). With an
empty ``HADES_API_KEY`` the API fails closed with 503. ``/healthz`` is public.
The read routes only read the DB (no unrealized PnL). Task 9 adds the realtime
side: ``POST /webhook/helius`` (outside /api, authed by the Helius
``Authorization`` header = ``HELIUS_WEBHOOK_SECRET``), ``GET /api/stream`` (SSE)
and watchlist -> Helius webhook sync (the only outbound network call).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse
from starlette.concurrency import run_in_threadpool

from hades.api import schemas as S
from hades.config import Settings, get_settings
from hades.db import (
    Alert,
    Swap,
    Token,
    Wallet,
    WalletStats,
    WalletTag,
    Watchlist,
    init_db,
    make_engine,
)
from hades.pnl import compute_positions
from hades.realtime import AlertBus, alert_events, ingest_webhook, sync_webhook

EARLY_BUYERS_LIMIT = 100
PING_INTERVAL_SEC = 15
MAX_WEBHOOK_TXS = 1000
SYNC_HEADER = "X-Webhook-Sync"
FUNDING_HOPS = 3
SORT_COLUMNS = {
    "score": WalletStats.score,
    "total_pnl_sol": WalletStats.total_pnl_sol,
    "win_rate": WalletStats.win_rate,
}


# -- dependencies -------------------------------------------------------------------

def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


def require_key(request: Request, x_hades_key: str | None = Header(default=None)) -> None:
    expected: str = request.app.state.settings.HADES_API_KEY
    if not expected:
        raise HTTPException(status_code=503, detail="api key not configured")
    if x_hades_key is None or not secrets.compare_digest(
        x_hades_key.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid api key")


# -- helpers ------------------------------------------------------------------------

def _tag_names(session: Session, addresses: list[str]) -> dict[str, list[str]]:
    out: dict[str, set[str]] = {a: set() for a in addresses}
    if addresses:
        rows = session.execute(
            select(WalletTag.address, WalletTag.tag).where(WalletTag.address.in_(addresses))
        )
        for addr, tag in rows:
            out[addr].add(tag)
    return {a: sorted(t) for a, t in out.items()}


def _wallet_rows(session: Session, stmt) -> list[S.WalletRow]:
    rows = session.execute(stmt).all()
    tags = _tag_names(session, [ws.address for ws, _ in rows])
    return [
        S.WalletRow(
            address=ws.address,
            score=ws.score,
            total_pnl_sol=ws.total_pnl_sol,
            realized_pnl_sol=ws.realized_pnl_sol,
            win_rate=ws.win_rate,
            tokens_closed=ws.tokens_closed,
            tags=tags[ws.address],
            is_bot=bool(is_bot),
        )
        for ws, is_bot in rows
    ]


def _wallets_base():
    is_bot = func.coalesce(Wallet.is_bot, False)
    stmt = select(WalletStats, is_bot).outerjoin(Wallet, Wallet.address == WalletStats.address)
    return stmt, is_bot


# -- routes -------------------------------------------------------------------------

router = APIRouter(prefix="/api", dependencies=[Depends(require_key)])


@router.get("/overview", response_model=S.Overview)
def overview(session: Session = Depends(get_session)) -> S.Overview:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt, is_bot = _wallets_base()
    top_stmt = (
        stmt.where(WalletStats.score.is_not(None), is_bot.is_(False))
        .order_by(WalletStats.score.desc(), WalletStats.address)
        .limit(10)
    )
    return S.Overview(
        tracked_wallets=session.scalar(select(func.count()).select_from(WalletStats)) or 0,
        runner_tokens=session.scalar(
            select(func.count()).select_from(Token).where(Token.is_runner.is_(True))
        ) or 0,
        alerts_24h=session.scalar(
            select(func.count()).select_from(Alert).where(Alert.created_at >= since)
        ) or 0,
        last_swap_at=session.scalar(select(func.max(Swap.block_time))),
        top=_wallet_rows(session, top_stmt),
    )


@router.get("/wallets", response_model=S.WalletList)
def list_wallets(
    sort: Literal["score", "total_pnl_sol", "win_rate"] = "score",
    tag: str | None = None,
    hide_bots: bool = True,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> S.WalletList:
    stmt, is_bot = _wallets_base()
    if hide_bots:
        stmt = stmt.where(is_bot.is_(False))
    if tag:
        stmt = stmt.where(
            exists().where(and_(WalletTag.address == WalletStats.address, WalletTag.tag == tag))
        )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    col = SORT_COLUMNS[sort]
    page = stmt.order_by(col.is_(None), col.desc(), WalletStats.address).limit(limit).offset(offset)
    return S.WalletList(items=_wallet_rows(session, page), total=total)


def _funding_chain(session: Session, address: str, wallet: Wallet | None) -> list[S.FundingHop]:
    chain = [S.FundingHop(
        address=address,
        funding_signature=wallet.funding_signature if wallet else None,
        funding_slot=wallet.funding_slot if wallet else None,
    )]
    seen = {address}
    cur = wallet
    for _ in range(FUNDING_HOPS):
        if cur is None or not cur.first_funder or cur.first_funder in seen:
            break
        nxt_addr = cur.first_funder
        seen.add(nxt_addr)
        cur = session.get(Wallet, nxt_addr)
        chain.append(S.FundingHop(
            address=nxt_addr,
            funding_signature=cur.funding_signature if cur else None,
            funding_slot=cur.funding_slot if cur else None,
        ))
    return chain


@router.get("/wallets/{address}", response_model=S.WalletDetail)
def wallet_detail(address: str, session: Session = Depends(get_session)) -> S.WalletDetail:
    wallet = session.get(Wallet, address)
    swaps = list(session.scalars(select(Swap).where(Swap.wallet == address)))
    if wallet is None and not swaps:
        raise HTTPException(status_code=404, detail="wallet not found")

    stats = session.get(WalletStats, address)
    positions = compute_positions(swaps)
    last_activity: dict[str, datetime] = {}
    for s in swaps:
        if s.mint not in last_activity or s.block_time > last_activity[s.mint]:
            last_activity[s.mint] = s.block_time
    symbols = dict(session.execute(
        select(Token.mint, Token.symbol).where(Token.mint.in_(list(positions)))
    ).all()) if positions else {}

    ordered = sorted(positions.values(), key=lambda p: (last_activity[p.mint], p.mint), reverse=True)
    pos_rows = [
        S.PositionRow(
            mint=p.mint, symbol=symbols.get(p.mint), bought_tokens=p.bought_tokens,
            sold_tokens=p.sold_tokens, cost_sol=p.cost_sol, proceeds_sol=p.proceeds_sol,
            realized_pnl_sol=p.realized_pnl_sol, remaining_tokens=p.remaining_tokens,
            untracked_sell_tokens=p.untracked_sell_tokens, closed=p.closed,
            first_buy_time=p.first_buy_time, last_sell_time=p.last_sell_time,
        )
        for p in ordered
    ]

    # Per-position granularity: one point per position, at its last sell.
    curve: list[S.PnlPoint] = []
    cum = 0.0
    for p in sorted((p for p in positions.values() if p.last_sell_time), key=lambda p: p.last_sell_time):
        cum += p.realized_pnl_sol
        curve.append(S.PnlPoint(t=p.last_sell_time, cum_pnl_sol=cum))

    tags = [
        S.TagRow(tag=t.tag, mint=t.mint, evidence=t.evidence_json or {}, created_at=t.created_at)
        for t in session.scalars(
            select(WalletTag).where(WalletTag.address == address).order_by(WalletTag.id)
        )
    ]
    return S.WalletDetail(
        stats=S.WalletStatsOut.model_validate(stats) if stats else None,
        positions=pos_rows,
        tags=tags,
        funding_chain=_funding_chain(session, address, wallet),
        pnl_curve=curve,
    )


@router.get("/tokens/{mint}", response_model=S.TokenForensics)
def token_forensics(mint: str, session: Session = Depends(get_session)) -> S.TokenForensics:
    token = session.get(Token, mint)
    if token is None:
        raise HTTPException(status_code=404, detail="token not found")

    buyers: dict[str, Swap] = {}
    buys = session.scalars(
        select(Swap).where(Swap.mint == mint, Swap.side == "buy").order_by(Swap.slot, Swap.signature)
    )
    for s in buys:
        if s.wallet not in buyers:
            buyers[s.wallet] = s
            if len(buyers) >= EARLY_BUYERS_LIMIT:
                break

    tag_map: dict[str, set[str]] = {a: set() for a in buyers}
    if buyers:
        rows = session.execute(
            select(WalletTag.address, WalletTag.tag).where(
                WalletTag.address.in_(list(buyers)),
                (WalletTag.mint == mint) | WalletTag.mint.is_(None),
            )
        )
        for addr, tag in rows:
            tag_map[addr].add(tag)
    early = [
        S.EarlyBuyer(address=a, slot=s.slot, signature=s.signature, sol_amount=s.sol_amount,
                     tags=sorted(tag_map[a]))
        for a, s in buyers.items()
    ]

    clusters: dict[str, S.Bundle] = {}
    for t in session.scalars(
        select(WalletTag).where(WalletTag.mint == mint, WalletTag.tag == "bundle").order_by(WalletTag.id)
    ):
        ev = t.evidence_json or {}
        cluster = str(ev.get("cluster") or "unknown")
        b = clusters.setdefault(cluster, S.Bundle(cluster=cluster, funder=ev.get("funder"), wallets=[]))
        if t.address not in b.wallets:
            b.wallets.append(t.address)

    return S.TokenForensics(
        token=S.TokenOut.model_validate(token), early_buyers=early, bundles=list(clusters.values())
    )


@router.get("/watchlist", response_model=list[S.WatchlistRow])
def watchlist_list(session: Session = Depends(get_session)) -> list[Watchlist]:
    return list(session.scalars(select(Watchlist).order_by(Watchlist.added_at, Watchlist.address)))


def _watchlist_upsert(session: Session, body: S.WatchlistIn) -> Watchlist:
    row = session.get(Watchlist, body.address)
    if row is None:
        row = Watchlist(address=body.address, note=body.note)
        session.add(row)
    else:
        row.note = body.note
    session.commit()
    session.refresh(row)
    return row


def _watchlist_remove(session: Session, address: str) -> bool:
    row = session.get(Watchlist, address)
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


@router.post("/watchlist", response_model=S.WatchlistRow, status_code=201)
async def watchlist_add(
    body: S.WatchlistIn, request: Request, response: Response,
    session: Session = Depends(get_session),
) -> S.WatchlistRow:
    row = await run_in_threadpool(_watchlist_upsert, session, body)
    out = S.WatchlistRow.model_validate(row)
    response.headers[SYNC_HEADER] = await sync_webhook(request.app)
    return out


@router.delete("/watchlist/{address}", status_code=204)
async def watchlist_delete(
    address: str, request: Request, session: Session = Depends(get_session)
) -> Response:
    if not await run_in_threadpool(_watchlist_remove, session, address):
        raise HTTPException(status_code=404, detail="not in watchlist", headers={SYNC_HEADER: "skipped"})
    return Response(status_code=204, headers={SYNC_HEADER: await sync_webhook(request.app)})


@router.get("/stream")
async def stream(request: Request) -> EventSourceResponse:
    """SSE: ``alert`` events (AlertRow JSON) and ``ping`` every ping_interval seconds."""
    app = request.app
    return EventSourceResponse(alert_events(app.state.bus, request.is_disconnected, app.state.ping_interval))


# -- Helius webhook (outside /api) ------------------------------------------------

webhook_router = APIRouter()


@webhook_router.post("/webhook/helius")
async def helius_webhook(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, int]:
    secret: str = request.app.state.settings.HELIUS_WEBHOOK_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if authorization is None or not secrets.compare_digest(
        authorization.encode("utf-8"), secret.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="invalid webhook secret")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid json") from None
    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="expected a list of transactions")
    if len(body) > MAX_WEBHOOK_TXS:
        raise HTTPException(status_code=413, detail="too many transactions")
    n_swaps, new_rows = await run_in_threadpool(ingest_webhook, request.app.state.engine, body)
    for row in new_rows:  # after commit
        request.app.state.bus.publish(row)
    return {"received": len(body), "swaps": n_swaps, "alerts": len(new_rows)}


# -- app factory --------------------------------------------------------------------

def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.DATABASE_URL)
    init_db(engine)

    app = FastAPI(title="HADES", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.bus = AlertBus()
    app.state.ping_interval = PING_INTERVAL_SEC
    app.state.sync_lock = asyncio.Lock()

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    app.include_router(webhook_router)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """Module-level ``app`` for ``uvicorn hades.api.main:app`` / ``hades serve``.

    Built lazily on first access (PEP 562) so that importing this module (e.g.
    from tests, for ``create_app``) does not open or migrate the configured DB.
    """
    global _app
    if name == "app":
        if _app is None:
            try:
                _app = create_app()
            except AttributeError as e:
                # PEP 562: an AttributeError escaping here would read as "no attribute app".
                raise RuntimeError(f"create_app() failed: {e}") from e
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
