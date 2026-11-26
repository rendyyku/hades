"""HADES v2 API (spec 9b). Every /api route needs X-Hades-Key (constant-time compare); with no key
configured the API fails closed with 503. /healthz is public. Served in the engine process.
"""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from hades.bot import MAX_MUTE_H, normalize_domain
from hades.bus import Bus
from hades.config import Settings
from hades.evaluate import WINDOW_DAYS, earliest_signal, evaluate
from hades.store import Store

PING_INTERVAL_S = 15.0


def _json_default(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat()
    raise TypeError(type(v).__name__)


async def signal_events(bus: Bus, is_disconnected: Callable[[], Awaitable[bool]],
                        ping_interval: float = PING_INTERVAL_S) -> AsyncIterator[dict[str, str]]:
    """SSE events: signal / signal_updated (signal row JSON), ping ({"t": iso}) when idle."""
    q = bus.subscribe()
    try:
        while not await is_disconnected():
            try:
                event, data = await asyncio.wait_for(q.get(), timeout=ping_interval)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": json.dumps({"t": datetime.now(timezone.utc).isoformat()})}
                continue
            yield {"event": event, "data": json.dumps(data, default=_json_default)}
    finally:
        bus.unsubscribe(q)


def require_key(request: Request, x_hades_key: str | None = Header(default=None)) -> None:
    expected: str = request.app.state.settings.HADES_API_KEY
    if not expected:
        raise HTTPException(status_code=503, detail="api key not configured")
    if x_hades_key is None or not secrets.compare_digest(x_hades_key.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid api key")


class DomainIn(BaseModel):
    domain: str


class MuteIn(BaseModel):
    target: str = Field(min_length=1, max_length=200)
    hours: int = Field(default=24, ge=1)


def _list_row(store: Store, row: dict) -> dict:
    row = {k: v for k, v in row.items() if k != "raw"}
    enrs = store.enrichments(row["id"])
    row["enrichment"] = enrs[-1] if enrs else None
    return row


def create_app(store: Store, bus: Bus, settings: Settings,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> FastAPI:
    app = FastAPI(title="HADES", version="2.0.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    router = APIRouter(prefix="/api", dependencies=[Depends(require_key)])

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @router.get("/overview")
    def overview() -> dict:
        since = clock() - timedelta(days=1)
        return {"counts": store.counts_since(since), "watchers": store.watchers(),
                "top": [_list_row(store, r) for r in store.top_signals_since(since, 10)]}

    @router.get("/signals")
    def list_signals(source: str | None = None, chain: str | None = None, min_score: int | None = None,
                     buyable: bool | None = None, before: int | None = None,
                     limit: int = Query(50, ge=1, le=200)) -> list[dict]:
        rows = store.list_signals(source=source, chain=chain, min_score=min_score, buyable=buyable,
                                  before_id=before, limit=limit)
        return [_list_row(store, r) for r in rows]

    @router.get("/signals/{signal_id}")
    def signal_detail(signal_id: int) -> dict:
        row = store.get_signal(signal_id)
        if row is None:
            raise HTTPException(status_code=404, detail="signal not found")
        fusion = [{k: v for k, v in r.items() if k != "raw"}
                  for r in store.signals_by_project(row["project_key"], exclude_id=signal_id)]
        return {"signal": row, "enrichments": store.enrichments(signal_id), "fusion": fusion}

    @router.get("/listings")
    def listings(exchange: str | None = None, limit: int = Query(100, ge=1, le=200)) -> list[dict]:
        rows = store.list_listings(limit=limit, exchange=exchange)
        if not rows:
            return []
        oldest = min(r["first_seen_at"] for r in rows)
        sigs = store.signals_since(oldest - timedelta(days=WINDOW_DAYS))
        return [{**r, "earliest_signal": earliest_signal(sigs, r)} for r in rows]

    @router.get("/watchers")
    def watchers() -> list[dict]:
        return store.watchers()

    @router.get("/watchlist")
    def watchlist() -> list[dict]:
        return store.domains()

    @router.post("/watchlist")
    def watchlist_add(body: DomainIn) -> dict:
        d = normalize_domain(body.domain)
        if d is None:
            raise HTTPException(status_code=400, detail="invalid domain")
        return {"domain": d, "added": store.add_domain(d, "manual")}

    @router.delete("/watchlist/{domain}")
    def watchlist_rm(domain: str) -> dict:
        if not store.remove_domain(domain.lower()):
            raise HTTPException(status_code=404, detail="domain not in watchlist")
        return {"domain": domain.lower(), "removed": True}

    @router.get("/mutes")
    def mutes() -> list[dict]:
        return store.active_mutes(clock())

    @router.post("/mutes")
    def mute_add(body: MuteIn) -> dict:
        until = clock() + timedelta(hours=min(body.hours, MAX_MUTE_H))
        store.add_mute(body.target.strip(), until)
        return {"target": body.target.strip(), "until": until}

    @router.delete("/mutes/{target}")
    def mute_rm(target: str) -> dict:
        if not store.remove_mute(target):
            raise HTTPException(status_code=404, detail="not muted")
        return {"target": target, "removed": True}

    @router.get("/eval")
    def eval_report() -> dict:
        return evaluate(store, clock())

    @router.get("/stream")
    async def stream(request: Request) -> EventSourceResponse:
        return EventSourceResponse(signal_events(bus, request.is_disconnected))

    app.include_router(router)
    return app
