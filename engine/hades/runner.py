"""Runs each watcher on its own interval. A failing watcher never stops the others (spec 5)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from hades.models import ListingEvent, Signal
from hades.store import Store
from hades.watchers.base import Watcher

log = logging.getLogger(__name__)


class SignalSink(Protocol):
    async def handle(self, sig: Signal) -> None: ...


class Runner:
    def __init__(
        self,
        store: Store,
        http: Any,
        watchers: list[Watcher],
        sink: SignalSink,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        on_listing: Callable[[ListingEvent], Awaitable[None]] | None = None,
        stagger_s: float = 5.0,
    ) -> None:
        self.store = store
        self.http = http
        self.watchers = watchers
        self.sink = sink
        self.clock = clock
        self.sleep = sleep
        self.on_listing = on_listing
        self.stagger_s = stagger_s

    async def run_once(self, w: Watcher) -> bool:
        now = self.clock()
        try:
            state = self.store.get_state(w.name)
            raw = await w.fetch(self.http, state)
            result = w.diff(state, w.parse(raw), now)
        except Exception as e:  # noqa: BLE001 - isolation is the point
            log.warning("watcher %s failed: %s: %s", w.name, type(e).__name__, e)
            self.store.record_error(w.name, f"{type(e).__name__}: {e}", now)
            return False
        for sig in result.signals:
            try:
                await self.sink.handle(sig)
            except Exception:  # noqa: BLE001
                log.exception("sink failed for %s", sig.dedupe_key)
        for ev in result.listings:
            if self.store.insert_listing(ev) and self.on_listing is not None:
                try:
                    await self.on_listing(ev)
                except Exception:  # noqa: BLE001
                    log.exception("on_listing failed for %s %s", ev.exchange, ev.symbol)
        for d in result.domains:
            self.store.add_domain(d, "auto")
        if result.new_state is not None:
            self.store.save_state(w.name, result.new_state)
        self.store.record_ok(w.name, now)
        return True

    async def _loop(self, w: Watcher, delay: float, stop: asyncio.Event) -> None:
        if delay:
            await self.sleep(delay)
        while not stop.is_set():
            try:
                await self.run_once(w)
            except Exception:  # noqa: BLE001 - e.g. sqlite "database is locked" after the fetch
                log.exception("watcher %s: unexpected error outside fetch/diff", w.name)
            await self.sleep(w.interval_s)

    async def run_forever(self, stop: asyncio.Event) -> None:
        tasks = [asyncio.create_task(self._loop(w, i * self.stagger_s, stop)) for i, w in enumerate(self.watchers)]
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
