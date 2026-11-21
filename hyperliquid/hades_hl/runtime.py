"""Background runtime of ``hades-hl serve`` (Task 9): the scheduler that keeps the
paper copies live.

- daily: rescore (stored fills) then the auto-follow sync, and price pruning (P42:
  prices older than ``PRICE_KEEP_DAYS`` = 14 days). The last run time is persisted
  (kv ``hl:daily_at``), so a restart does not redo it.
- every ``paper_interval_s`` (60 s): :func:`paper_runtime.run_paper_live` for each
  follow that needs it (new settled events, never run, or last run over an hour ago).
- every ``reconcile_tick_s``: the P11 reconcile, at most once per 5 min per follow with
  open paper positions (skipped while the watcher recovers that wallet).
- the price recorder: ``fastAssetCtxs`` with ``allMids`` fallback (P41), recording
  only the watch set (P42): coins traded by follows in the last 30 days, coins of open
  paper positions, plus BTC/ETH; refreshed every ``watch_refresh_s``.
- the fill watcher over the active follows; restarted when the follow set changes
  (checked every ``watch_refresh_s`` and after a follow sync). The watcher's persisted
  cursors make the restart lossless. New fills alert at once (NOTIF/COPY wallets).

Task 10 mounts the API in the same process: ``await rt.start()`` in the app lifespan,
``await rt.shutdown()`` on exit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from hades_core.events import TradeEvent
from hades_hl import alerts, follows, pipeline
from hades_hl import paper_runtime as pr
from hades_hl.client import WEIGHT_PER_MIN, info_weight
from hades_hl.db import Fill, Price, utcnow
from hades_hl.live import DEFAULT_WS_URL, FillWatcher, FundingRates, PriceRecorder

log = logging.getLogger(__name__)

ALWAYS_COINS = frozenset({"BTC", "ETH"})
WATCH_DAYS = 30
PRICE_KEEP_DAYS = 14
KV_DAILY = "hl:daily_at"
DAILY_INTERVAL_S = 86_400.0
DAILY_CHECK_S = 600.0
PAPER_INTERVAL_S = 60.0
RECONCILE_TICK_S = 30.0
WATCH_REFRESH_S = 60.0
PRIMARY_FEED = "fastAssetCtxs"  # P41
FALLBACK_FEED = "allMids"

Now = Callable[[], datetime]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _perp(coin: str) -> bool:
    return not (coin.startswith(("@", "#")) or "/" in coin)


def watch_coins(session: Session, now: datetime) -> set[str]:
    """P42 watch set: BTC/ETH, coins the active follows traded in the last 30 days,
    and the coins of their open paper positions (perps only)."""
    coins = set(ALWAYS_COINS)
    addrs = [f.address for f in follows.active_follows(session)]
    if not addrs:
        return coins
    lo = _ms(now - timedelta(days=WATCH_DAYS))
    coins.update(session.scalars(select(Fill.coin).where(
        Fill.address.in_(addrs), Fill.time >= lo).distinct()))
    for a in addrs:
        for p in (pr.snapshot(session, a) or {}).get("open_positions") or []:
            coins.add(str(p.get("market_id")))
    return {c for c in coins if c and _perp(c)}


def prune_prices(session: Session, now: datetime, keep_days: int = PRICE_KEEP_DAYS) -> int:
    res = session.execute(delete(Price).where(
        Price.time < _ms(now - timedelta(days=keep_days))))
    return int(res.rowcount or 0)


WatcherFactory = Callable[[Mapping[str, datetime], Callable[[TradeEvent], Any]], Any]
RecorderFactory = Callable[[Callable[[], set[str]]], Any]


class HLRuntime:
    def __init__(self, engine: Engine, client: Any, *, ws_url: str = DEFAULT_WS_URL,
                 now: Now = utcnow, weight_per_min: int = WEIGHT_PER_MIN,
                 max_follows: int = follows.MAX_FOLLOWS,
                 auto_follow_n: int = follows.AUTO_FOLLOW_N,
                 paper_interval_s: float = PAPER_INTERVAL_S,
                 reconcile_tick_s: float = RECONCILE_TICK_S,
                 watch_refresh_s: float = WATCH_REFRESH_S,
                 daily_interval_s: float = DAILY_INTERVAL_S,
                 daily_check_s: float = DAILY_CHECK_S, run_daily: bool = True,
                 watcher_factory: WatcherFactory | None = None,
                 recorder_factory: RecorderFactory | None = None) -> None:
        self.engine = engine
        self.client = client
        self.ws_url = ws_url
        self._now = now
        self.weight_per_min = weight_per_min
        self.max_follows = max_follows
        self.auto_follow_n = auto_follow_n
        self.paper_interval_s = paper_interval_s
        self.reconcile_tick_s = reconcile_tick_s
        self.watch_refresh_s = watch_refresh_s
        self.daily_interval_ms = int(daily_interval_s * 1000)
        self.daily_check_s = daily_check_s
        self.run_daily = run_daily
        self.watcher_factory = watcher_factory or self._default_watcher
        self.recorder_factory = recorder_factory or self._default_recorder
        self.funding = FundingRates(engine)
        self.reconciler = pr.Reconciler()
        self.watch_set: set[str] = set(ALWAYS_COINS)
        self.dirty: set[str] = set()  # follows with events since their last paper run
        self.watcher: Any = None
        self.recorder: Any = None
        self._watcher_follows: dict[str, datetime] | None = None
        self._watcher_stop: asyncio.Event | None = None
        self._watcher_task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        # counters (smoke / health)
        self.fills_observed = 0
        self.paper_runs = 0
        self.reconcile_events = 0
        self.alerts_emitted = 0
        self.watcher_starts = 0

    # -- factories -------------------------------------------------------------------

    def _reconcile_weight_per_min(self, n_follows: int) -> float:
        # one clearinghouseState per follow per 5 min at most
        return n_follows * info_weight("clearinghouseState") * 60.0 / pr.RECONCILE_INTERVAL_S

    def _default_watcher(self, follows_: Mapping[str, datetime], on_fill) -> FillWatcher:
        return FillWatcher(self.engine, self.client, follows_, ws_url=self.ws_url,
                           on_fill=on_fill, now=self._now, weight_per_min=self.weight_per_min,
                           extra_weight_per_min=self._reconcile_weight_per_min(len(follows_)))

    def _default_recorder(self, coins: Callable[[], set[str]]) -> PriceRecorder:
        return PriceRecorder(self.engine, self.ws_url, now=self._now, coins=coins,
                             feed=PRIMARY_FEED, fallback_feed=FALLBACK_FEED)

    # -- jobs ------------------------------------------------------------------------

    def on_fill(self, ev: TradeEvent) -> None:
        """Watcher hook: alert at once (NOTIF/COPY), mark the follow for a paper run."""
        self.fills_observed += 1
        self.dirty.add(ev.wallet)
        self._alert(ev.wallet, [ev])

    def _alert(self, addr: str, evs: list[TradeEvent]) -> None:
        try:
            with Session(self.engine) as s:
                self.alerts_emitted += len(alerts.emit_alerts(s, addr, evs, None,
                                                              now=self._now()))
                s.commit()
        except Exception:
            log.exception("hl runtime: alerts for %s failed", addr)

    def refresh_watch_set(self) -> set[str]:
        with Session(self.engine) as s:
            self.watch_set = watch_coins(s, self._now())
        return self.watch_set

    async def daily(self) -> bool:
        """Rescore, then the auto-follow sync, then price pruning; once per day."""
        now = self._now()
        with Session(self.engine) as s:
            last = pipeline.kv_get_json(s, KV_DAILY)
        if isinstance(last, int) and _ms(now) - last < self.daily_interval_ms:
            return False
        try:
            with Session(self.engine) as s:
                szd = await pipeline.load_sz_decimals(s, self.client, now)
                s.commit()
        except Exception as e:
            log.warning("hl daily: szDecimals unavailable (%s); rescore uses the cache",
                        type(e).__name__)
            szd = None
        try:
            await asyncio.to_thread(self._rescore, now, szd)
        except Exception:
            log.exception("hl daily: rescore failed; follow sync uses the previous scores")
        with Session(self.engine) as s:
            diff = follows.sync_auto_follows(s, now, n=self.auto_follow_n, cap=self.max_follows)
            pruned = prune_prices(s, now)
            pipeline.kv_set_json(s, KV_DAILY, _ms(now))
            s.commit()
        log.info("hl daily: follows +%d -%d (kept open %d, over cap %d); pruned %d price rows",
                 len(diff.added), len(diff.removed), len(diff.kept_open),
                 len(diff.skipped_cap), pruned)
        if diff.added or diff.removed:
            self.refresh_watch_set()
            await self.sync_watcher()
        return True

    def _rescore(self, now: datetime, szd: dict | None) -> None:
        with Session(self.engine) as s:
            pipeline.rescore(s, now, sz_decimals=szd)
            s.commit()

    async def paper_tick(self) -> list[str]:
        now = self._now()
        with Session(self.engine) as s:
            addrs = [f.address for f in follows.active_follows(s)]
        ran: list[str] = []
        for a in addrs:
            try:
                with Session(self.engine) as s:
                    if not pr.needs_run(s, a, now):
                        continue
                    await pr.run_paper_live(s, self.client, a, now, funding=self.funding)
                    s.commit()
            except Exception:
                log.exception("hl paper run %s failed", a)
                continue
            ran.append(a)
            self.paper_runs += 1
            self.dirty.discard(a)
        return ran

    def _pending(self, addr: str) -> bool:
        w = self.watcher
        return bool(w is not None and addr in (getattr(w, "recovery_floor_ms", None) or {}))

    async def reconcile_tick(self) -> list[TradeEvent]:
        now = self._now()
        with Session(self.engine) as s:
            addrs = [f.address for f in follows.active_follows(s)]
        out: list[TradeEvent] = []
        for a in addrs:
            try:
                with Session(self.engine) as s:
                    evs = await self.reconciler.maybe(s, self.client, a, now,
                                                      pending=self._pending)
                    s.commit()  # no await since the seq was taken
            except Exception:
                log.exception("hl reconcile %s failed", a)
                continue
            if evs:
                self.reconcile_events += len(evs)
                self.dirty.add(a)
                self._alert(a, evs)
                out.extend(evs)
        return out

    # -- watcher ---------------------------------------------------------------------

    async def sync_watcher(self) -> None:
        """(Re)start the fill watcher when the active follow set changed."""
        with Session(self.engine) as s:
            want = {f.address: f.started_at for f in follows.active_follows(s)}
        if self.watcher is not None and want == self._watcher_follows:
            return
        if self.watcher is None and not want:
            return
        await self.stop_watcher()
        if not want:
            return
        self._watcher_follows = want
        self._watcher_stop = asyncio.Event()
        self.watcher = self.watcher_factory(want, self.on_fill)
        self._watcher_task = asyncio.create_task(self.watcher.run(self._watcher_stop))
        self.watcher_starts += 1
        log.info("hl runtime: fill watcher started for %d follow(s)", len(want))

    async def stop_watcher(self) -> None:
        if self._watcher_task is not None and self._watcher_stop is not None:
            self._watcher_stop.set()
            await asyncio.gather(self._watcher_task, return_exceptions=True)
        self.watcher = None
        self._watcher_task = None
        self._watcher_stop = None
        self._watcher_follows = None

    async def _refresh(self) -> None:
        self.refresh_watch_set()
        await self.sync_watcher()

    # -- run -------------------------------------------------------------------------

    async def _every(self, interval: float, fn: Callable[[], Awaitable[Any]],
                     name: str) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("hl runtime: %s failed", name)
            await asyncio.sleep(interval)

    async def run(self, stop: asyncio.Event) -> None:
        self.refresh_watch_set()
        if self.run_daily:
            try:
                await self.daily()
            except Exception:
                log.exception("hl runtime: daily jobs failed")
        await self.sync_watcher()
        self.recorder = self.recorder_factory(lambda: self.watch_set)
        rec_stop = asyncio.Event()
        rec_task = asyncio.create_task(self.recorder.run(rec_stop))
        jobs = [
            asyncio.create_task(self._every(self.paper_interval_s, self.paper_tick, "paper")),
            asyncio.create_task(self._every(self.reconcile_tick_s, self.reconcile_tick,
                                            "reconcile")),
            asyncio.create_task(self._every(self.watch_refresh_s, self._refresh, "refresh")),
        ]
        if self.run_daily:
            jobs.append(asyncio.create_task(self._every(self.daily_check_s, self.daily,
                                                        "daily")))
        try:
            await stop.wait()
        finally:
            for t in jobs:
                t.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            rec_stop.set()
            await asyncio.gather(rec_task, return_exceptions=True)
            await self.stop_watcher()

    async def start(self) -> None:
        """Run in the background (for an app lifespan)."""
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.run(self._stop))

    async def shutdown(self) -> None:
        if self._stop is not None and self._task is not None:
            self._stop.set()
            await asyncio.gather(self._task, return_exceptions=True)


__all__ = ["ALWAYS_COINS", "HLRuntime", "PRICE_KEEP_DAYS", "prune_prices", "watch_coins"]
