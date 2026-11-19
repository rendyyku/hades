"""Realtime: Helius webhook ingest -> alerts, in-process AlertBus (SSE fan-out),
and watchlist -> Helius webhook sync.

Nothing here logs the Helius API key or the webhook secret; failures are logged
by exception type only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from hades.api import schemas as S
from hades.db import Alert, Kv, Swap, Token, WalletStats, Watchlist
from hades.ingest.parse import parse_swaps
from hades.sources.helius import HeliusClient

log = logging.getLogger(__name__)

ALERT_MIN_SCORE = 70
QUEUE_MAXSIZE = 100
KV_WEBHOOK_ID = "helius_webhook_id"
WEBHOOK_PATH = "/webhook/helius"
SYNC_MAX_RETRIES = 1  # POST /v0/webhooks is not idempotent: keep retries minimal


# -- bus ----------------------------------------------------------------------------

class AlertBus:
    """In-process fan-out of alert rows to SSE subscribers (one event loop)."""

    def __init__(self, maxsize: int = QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subs: set[asyncio.Queue] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, alert_row: dict) -> None:
        """Non-blocking: a full subscriber queue drops its oldest item."""
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - race-free in one loop
                    pass
            try:
                q.put_nowait(alert_row)
            except asyncio.QueueFull:  # pragma: no cover
                pass


async def alert_events(
    bus: AlertBus,
    is_disconnected: Callable[[], Awaitable[bool]],
    ping_interval: float,
) -> AsyncIterator[dict[str, str]]:
    """SSE events: ``alert`` (AlertRow JSON) and ``ping`` ({"t": iso}) when idle."""
    q = bus.subscribe()
    try:
        while not await is_disconnected():
            try:
                item = await asyncio.wait_for(q.get(), timeout=ping_interval)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": json.dumps({"t": datetime.now(timezone.utc).isoformat()})}
                continue
            yield {"event": "alert", "data": json.dumps(item)}
    finally:
        bus.unsubscribe(q)


# -- webhook ingest -----------------------------------------------------------------

def _alert_rows(session: Session, alerts: list[Alert]) -> list[dict]:
    mints = list({a.mint for a in alerts})
    symbols = dict(session.execute(
        select(Token.mint, Token.symbol).where(Token.mint.in_(mints))
    ).all()) if mints else {}
    return [
        S.AlertRow(
            id=a.id, wallet=a.wallet, mint=a.mint, symbol=symbols.get(a.mint), side=a.side,
            sol_amount=a.sol_amount, signature=a.signature, block_time=a.block_time,
        ).model_dump(mode="json")
        for a in alerts
    ]


def _ingest_once(engine, txs: list[Any]) -> tuple[int, list[dict]]:
    n_swaps = 0
    with Session(engine) as session:
        swaps = []
        for tx in txs:
            if not isinstance(tx, dict):
                log.warning("webhook: skipping non-object tx")
                continue
            try:
                swaps.extend(parse_swaps(tx))
            except Exception as e:  # poison tx must not make Helius retry forever
                log.warning("webhook: unparseable tx skipped (%s)", type(e).__name__)
        wallets = list({sw.wallet for sw in swaps})
        watched: set[str] = set()
        high: set[str] = set()
        if wallets:
            watched = set(session.scalars(select(Watchlist.address).where(Watchlist.address.in_(wallets))))
            high = set(session.scalars(select(WalletStats.address).where(
                WalletStats.address.in_(wallets), WalletStats.score >= ALERT_MIN_SCORE)))

        new: list[Alert] = []
        seen: set[tuple[str, str, str]] = set()
        for sw in swaps:
            n_swaps += 1
            session.merge(Swap(
                signature=sw.signature, wallet=sw.wallet, mint=sw.mint, slot=sw.slot,
                block_time=sw.block_time, side=sw.side, token_amount=sw.token_amount,
                sol_amount=sw.sol_amount, source="webhook", dex=sw.dex,
            ))
            key = (sw.signature, sw.wallet, sw.mint)
            if key in seen or (sw.wallet not in watched and sw.wallet not in high):
                continue
            seen.add(key)
            exists = session.scalar(select(Alert.id).where(
                Alert.signature == sw.signature, Alert.wallet == sw.wallet, Alert.mint == sw.mint))
            if exists is not None:
                continue
            a = Alert(wallet=sw.wallet, mint=sw.mint, side=sw.side, sol_amount=sw.sol_amount,
                      signature=sw.signature, block_time=sw.block_time)
            session.add(a)
            new.append(a)
        session.commit()
        return n_swaps, _alert_rows(session, new)


def ingest_webhook(engine, txs: list[Any]) -> tuple[int, list[dict]]:
    """Store swaps (source='webhook') and NEW alerts; return (n_swaps, new AlertRows).

    Idempotent on (signature, wallet, mint). A concurrent duplicate delivery that
    trips the unique index is retried once; the retry sees the committed alert.
    """
    try:
        return _ingest_once(engine, txs)
    except IntegrityError:
        log.info("webhook: concurrent duplicate, retrying")
        return _ingest_once(engine, txs)


# -- watchlist -> Helius webhook sync -----------------------------------------------

def _sync_inputs(engine, settings) -> tuple[list[str], str | None]:
    with Session(engine) as session:
        addresses = list(session.scalars(select(Watchlist.address).order_by(Watchlist.address)))
        webhook_id = settings.HELIUS_WEBHOOK_ID or None
        if not webhook_id:
            kv = session.get(Kv, KV_WEBHOOK_ID)
            webhook_id = kv.value if kv else None
    return addresses, webhook_id


def _store_webhook_id(engine, webhook_id: str) -> None:
    with Session(engine) as session:
        session.merge(Kv(key=KV_WEBHOOK_ID, value=webhook_id))
        session.commit()


async def sync_webhook(app) -> str:
    """Push the watchlist to the Helius webhook. Returns 'ok' | 'skipped' | 'error'.

    Never raises: the watchlist change is already committed and must not fail.
    """
    settings = app.state.settings
    if not settings.PUBLIC_WEBHOOK_URL:
        return "skipped"
    if not settings.HELIUS_API_KEY or not settings.HELIUS_WEBHOOK_SECRET:
        log.warning("webhook sync: HELIUS_API_KEY or HELIUS_WEBHOOK_SECRET not configured")
        return "error"
    url = settings.PUBLIC_WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
    async with app.state.sync_lock:  # serialise: two concurrent creates = two webhooks
        try:
            addresses, webhook_id = await run_in_threadpool(_sync_inputs, app.state.engine, settings)
            async with HeliusClient(
                settings.HELIUS_API_KEY, settings.RATE_LIMIT_RPS, max_retries=SYNC_MAX_RETRIES
            ) as client:
                new_id = await client.upsert_webhook(
                    addresses, url, settings.HELIUS_WEBHOOK_SECRET, webhook_id)
            if not settings.HELIUS_WEBHOOK_ID and new_id != webhook_id:
                await run_in_threadpool(_store_webhook_id, app.state.engine, new_id)
        except Exception as e:
            log.warning("webhook sync failed: %s", type(e).__name__)
            return "error"
    return "ok"
