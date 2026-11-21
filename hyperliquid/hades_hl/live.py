"""Live layer of the Hyperliquid head (Task 8): price recorder, fill watcher, and the
execution model the core paper simulator prices copies with.

No hindsight (spec section 7, 12a):

- ``observed_time`` is OUR clock when a fill reached us: ``now()`` when a WS message or
  a poll response arrives; for fills recovered by REST after a (re)connect or a gap,
  the time that recovery response arrived. Never the leader's fill time.
  ``delay_ms = observed_time - fill.time`` is stored per event.
- The WS ``userFills`` subscription first sends a snapshot (``isSnapshot: true``) of
  historical fills. Those are stored (``fills`` dedupes by ``(address, tid)``) but are
  NEVER emitted as live paper events. Fills the snapshot covers that happened while we
  were disconnected are re-fetched by REST recovery and emitted with the recovery time.
- Only fills at or after ``max(watcher start, follow start)`` become paper events.
- Our copy fills at the recorded mid ``price_at(coin, observed_time + 1 s)``: the first
  mid we recorded in ``[observed + 1 s, observed + 30 s]`` (ruling P45); none means
  missed.
- A failed REST recovery sets a per-address ``recovery_floor_ms`` (the pre-gap
  ``last_seen``); ``last_seen`` stays there until a recovery succeeds. Recovery is
  retried on a timer (RECOVERY_RETRY_S, doubling up to RECOVERY_RETRY_MAX_S) and on
  every reconnect; the successful retry emits every not-yet-emitted fill since the floor
  (source_ref dedupe) with the retry response time as ``observed_time`` (P44).
- While a recovery is pending (floor set), live fills (WS or poll) of that address are
  stored but HELD, not emitted (P46): an emitted close could otherwise precede the
  open the recovery returns later. The successful recovery emits the gap fills and the
  held fills as ONE chain-ordered batch (seq in that order), every event with the
  recovery response time as ``observed_time``; then the floor clears and ``last_seen``
  advances. Held fills are not emitted if the watcher stops first.
- Recovery max age (P47): a recovered (or held-and-released) fill whose leader time is
  more than RECOVERY_MAX_AGE_S (10 min) before the recovery time is emitted as MISSED:
  stored already settled (``missed=True``), never priced. Copying a days-old open at
  today's price would be fiction.

Limits (verified 2026-11-21 against
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits):
"Maximum of 10 unique users across user-specific websocket subscriptions" (per IP), so
at most WS_MAX_USER_SUBS followed wallets ride the WS; the rest are polled with
``userFillsByTime`` (weight 20 + 1 per 20 fills) through the client's shared token
bucket, at most every POLL_INTERVAL_S and never faster than POLL_WEIGHT_SHARE of the
1200/min budget allows. Heartbeat: the server closes a connection it has not sent to
for 60 s; we send ``{"method": "ping"}`` every PING_INTERVAL_S
(https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/timeouts-and-heartbeats).

Taker fee: the base-tier perps taker rate is 0.045% (maker 0.015%), verified 2026-11-21
at https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees ("Perps fee tiers",
tier 0, base rate).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from websockets.asyncio.client import connect

from hades_core.events import TradeEvent
from hades_core.paper import OpenPosition
from hades_hl import pipeline
from hades_hl.client import INFO_WEIGHT_DEFAULT, WEIGHT_PER_MIN, HLError, info_weight, item_weight
from hades_hl.db import Fill, Kv, PaperEvent, Price, _insert_ignore, insert_fills, utcnow
from hades_hl.normalize import fill_legs, is_spot, leverage_by_coin, ms_to_dt
from hades_hl.normalize import HEAD

log = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://api.hyperliquid.xyz/ws"
# ``allMids`` is the ruling's feed, but HL marks it deprecated and (live probe
# 2026-11-21) pushes it only every ~12 s; ``fastAssetCtxs`` (the documented successor)
# pushed ~1 message/s with ``midPx`` diffs in the same probe.
PRICE_FEEDS = ("allMids", "fastAssetCtxs")
# "Maximum of 10 unique users across user-specific websocket subscriptions" (per IP):
# https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
WS_MAX_USER_SUBS = 10
POLL_INTERVAL_S = 5.0
POLL_WEIGHT_SHARE = 0.5  # polling may use at most half of the per-IP REST weight budget
PING_INTERVAL_S = 50.0  # server closes after 60 s without a message
RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 60.0
LEVERAGE_TTL_S = 60.0
RECOVERY_RETRY_S = 10.0  # failed recovery: retry after 10 s, doubling ...
RECOVERY_RETRY_MAX_S = 60.0  # ... up to 60 s
FEED_FALLBACK_AFTER = 3  # P41: consecutive failed price-feed connections before fallback
FEED_PRIMARY_RETRY_S = 3600.0  # ... and the primary is retried after an hour
RECOVERY_MAX_AGE_S = 600.0  # P47: older recovered fills are emitted as missed
OTHER_COINS_INTERVAL_S = 5.0  # P53: coins outside the watch set, at most every 5 s

# -- execution model constants ---------------------------------------------------------
PRICE_WINDOW_S = 30  # spec 12a: no live price within 30 s -> missed
FILL_DELAY = timedelta(seconds=1)  # spec 8.2: mid at observed_time + 1 s
# ruling P45: the window is [observed + 1 s, observed + 30 s], i.e. 29 s after the
# observed + 1 s anchor (the spec's literal "within 30 s" of observation)
MODEL_WINDOW_S = PRICE_WINDOW_S - int(FILL_DELAY.total_seconds())
HALF_SPREAD = Decimal("0.0002")  # 0.02%
SLIPPAGE_COEF = Decimal("0.0005")  # slippage = 0.0005 * sqrt(notional / 100k)
SLIPPAGE_REF_USD = Decimal("100000")
TAKER_FEE = Decimal("0.00045")  # 0.045%, base tier (see module doc)
MAX_LEVERAGE = Decimal("10")
DEFAULT_STAKE_USD = Decimal("500")  # PaperConfig default: 5% of a 10,000 bankroll

KV_SEQ = "hl:seq:{}"  # per-address monotonic event counter (ruling P26)
KV_LAST_SEEN = "hl:last_seen:{}"  # M6: per-address watcher cursor (epoch ms)
KV_FLOOR = "hl:floor:{}"  # M6: per-address pending recovery floor (epoch ms)
KV_FUNDING = "hl:funding:{}:{}"  # coin, UTC day start ms -> hourly funding rates
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
FUNDING_FINAL_AFTER_MS = 10 * 60_000  # a day's cache is final 10 min after it ends
FUNDING_REFRESH_MS = HOUR_MS  # an unfinished day is re-fetched after an hour

Now = Callable[[], datetime]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# -- TradeEvent JSON -------------------------------------------------------------------

def event_to_json(ev: TradeEvent) -> dict:
    out: dict[str, Any] = {}
    for k, v in asdict(ev).items():
        if isinstance(v, Decimal):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


_DEC_FIELDS = ("size", "price", "notional_usd", "fee_usd", "leverage")
_DT_FIELDS = ("leader_time", "observed_time")
_EV_FIELDS = set(TradeEvent.__dataclass_fields__)


def event_from_json(d: Mapping) -> TradeEvent:
    """A TradeEvent from :func:`event_to_json` output (extra keys such as ``via`` are
    ignored)."""
    kw: dict[str, Any] = {k: v for k, v in d.items() if k in _EV_FIELDS}
    for k in _DEC_FIELDS:
        if kw.get(k) is not None:
            kw[k] = Decimal(kw[k])
    for k in _DT_FIELDS:
        kw[k] = datetime.fromisoformat(kw[k]).astimezone(timezone.utc)
    return TradeEvent(**kw)


# -- PriceRecorder ---------------------------------------------------------------------

class PriceRecorder:
    """Records HL mids from the WS ``allMids`` feed (or ``fastAssetCtxs`` with
    ``feed="fastAssetCtxs"``) into ``prices``.

    At most one row per coin per second (``min_interval_s``); an unchanged mid is only
    rewritten every ``refresh_s`` (< PRICE_WINDOW_S, so every 30 s window still has a
    row: the mid of a row written later with the same value is the mid at the earlier
    time). Spot mids (``@N``, ``A/B``) are skipped; ``coins()`` (optional) is the watch
    set: coins outside it are skipped, or, with ``other_interval_s`` (ruling P53),
    recorded at most every ``other_interval_s`` (so a leader's first trade in a new
    coin still finds a price in its 30 s window); ``#N`` outcome tokens are skipped
    too. Row times are OUR receipt time (epoch ms).
    """

    def __init__(self, engine: Engine, ws_url: str = DEFAULT_WS_URL, *,
                 now: Now = utcnow, min_interval_s: float = 1.0, refresh_s: float = 10.0,
                 coins: Callable[[], set[str] | None] | None = None,
                 reconnect_base_s: float = RECONNECT_BASE_S,
                 reconnect_max_s: float = RECONNECT_MAX_S,
                 ping_interval_s: float = PING_INTERVAL_S,
                 feed: str = "allMids", fallback_feed: str | None = None,
                 fallback_after: int = FEED_FALLBACK_AFTER,
                 primary_retry_s: float = FEED_PRIMARY_RETRY_S,
                 other_interval_s: float | None = None) -> None:
        if feed not in PRICE_FEEDS:
            raise ValueError(f"feed must be one of {PRICE_FEEDS}")
        if fallback_feed is not None and fallback_feed not in PRICE_FEEDS:
            raise ValueError(f"fallback_feed must be one of {PRICE_FEEDS}")
        self.engine = engine
        self.ws_url = ws_url
        self.feed = feed
        # P41: the primary feed; after ``fallback_after`` consecutive failed connections
        # (errors before any usable message) switch to ``fallback_feed``, and try the
        # primary again after ``primary_retry_s``
        self.primary_feed = feed
        self.fallback_feed = fallback_feed
        self.fallback_after = fallback_after
        self.primary_retry_s = primary_retry_s
        self._fell_back_at: float | None = None
        self._mids: dict[str, str] = {}  # fastAssetCtxs: latest midPx per coin
        self._now = now
        self.min_interval_ms = int(min_interval_s * 1000)
        self.other_min_interval_ms = (None if other_interval_s is None
                                      else int(other_interval_s * 1000))
        self.refresh_ms = int(refresh_s * 1000)
        self._coins = coins
        self._last: dict[str, tuple[int, Decimal, int]] = {}  # coin -> (second, mid, ms)
        self.rows_written = 0
        self.messages = 0
        self.reconnect_base_s = reconnect_base_s
        self.reconnect_max_s = reconnect_max_s
        self.ping_interval_s = ping_interval_s

    def record(self, mids: Mapping[str, Any], at: datetime | None = None) -> int:
        """Store the throttled subset of ``mids`` at ``at`` (default now). Returns rows
        written."""
        at_ms = _ms(at if at is not None else self._now())
        sec = at_ms // 1000
        allowed = self._coins() if self._coins is not None else None
        rows = []
        for coin, raw in mids.items():
            coin = str(coin)
            if coin.startswith(("@", "#")) or "/" in coin:  # spot, outcome tokens
                continue
            interval = self.min_interval_ms
            if allowed is not None and coin not in allowed:
                if self.other_min_interval_ms is None:
                    continue
                interval = max(interval, self.other_min_interval_ms)
            try:
                mid = Decimal(str(raw))
            except (InvalidOperation, TypeError):
                continue
            last = self._last.get(coin)
            if last is not None:
                last_sec, last_mid, last_ms = last
                if (sec - last_sec) * 1000 < interval:
                    continue
                if mid == last_mid and at_ms - last_ms < self.refresh_ms:
                    continue
            rows.append({"coin": coin, "time": at_ms, "mid": str(mid)})
            self._last[coin] = (sec, mid, at_ms)
        if rows:
            with Session(self.engine) as s:
                _insert_ignore(s, Price.__table__, rows)
                s.commit()
            self.rows_written += len(rows)
        return len(rows)

    def price_at(self, coin: str, t: datetime, within_s: float = PRICE_WINDOW_S) -> Decimal | None:
        """The first recorded mid of ``coin`` with ``t <= time <= t + within_s``; None if
        none (never a mid recorded before ``t``)."""
        lo = _ms(t)
        hi = lo + int(within_s * 1000)
        with Session(self.engine) as s:
            return s.scalar(select(Price.mid).where(
                Price.coin == coin, Price.time >= lo, Price.time <= hi)
                .order_by(Price.time).limit(1))

    async def run(self, stop: asyncio.Event) -> None:
        """Record until ``stop`` is set; reconnects with exponential backoff."""
        task = asyncio.create_task(self._loop())
        try:
            await stop.wait()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _choose_feed(self, failures: int, now: float) -> bool:
        """P41 feed switching, before each (re)connect. True when the feed changed."""
        if self.fallback_feed is None or self.fallback_feed == self.primary_feed:
            return False
        if self.feed == self.primary_feed and failures >= self.fallback_after:
            log.warning("hl %s feed failed %d time(s) in a row; falling back to %s",
                        self.primary_feed, failures, self.fallback_feed)
            self.feed = self.fallback_feed
            self._fell_back_at = now
            return True
        if (self.feed == self.fallback_feed and self._fell_back_at is not None
                and now - self._fell_back_at >= self.primary_retry_s):
            log.info("hl price feed: retrying the primary %s", self.primary_feed)
            self.feed = self.primary_feed
            self._fell_back_at = None
            return True
        return False

    async def _loop(self) -> None:
        attempt = 0
        failures = 0  # consecutive connections that ended in an error, no usable message
        loop = asyncio.get_running_loop()
        while True:
            if self._choose_feed(failures, loop.time()):
                failures = 0
            try:
                async with connect(self.ws_url, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"method": "subscribe",
                                              "subscription": {"type": self.feed}}))
                    self._mids.clear()  # fastAssetCtxs restarts with a snapshot
                    hb = asyncio.create_task(
                        _heartbeat(ws, self.ping_interval_s, f"hl {self.feed}"))
                    try:
                        async for raw in ws:
                            at = self._now()
                            msg = json.loads(raw)
                            ch = msg.get("channel")
                            if ch == "allMids":
                                mids = (msg.get("data") or {}).get("mids") or {}
                            elif ch == "fastAssetCtxs":
                                mids = self._apply_ctxs(msg.get("data"))
                            else:
                                continue
                            attempt = 0
                            failures = 0
                            self.messages += 1
                            self.record(mids, at)
                    finally:
                        await _stop_task(hb)
                log.warning("hl %s WS closed; reconnecting", self.feed)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network, protocol, DB: keep recording
                failures += 1
                log.warning("hl %s WS error: %s: %s", self.feed, type(e).__name__, e)
            await asyncio.sleep(min(self.reconnect_max_s, self.reconnect_base_s * 2 ** attempt))
            attempt += 1


    def _apply_ctxs(self, data: Any) -> dict[str, str]:
        """Merge one ``fastAssetCtxs`` message (base64 raw-DEFLATE JSON ``{coin:
        {markPx?, midPx?}}``; a snapshot, then only changed fields; ``midPx: null`` = no
        mid) into the latest mids, and return them all."""
        ctxs = json.loads(zlib.decompress(base64.b64decode(data), wbits=-15))
        for coin, ctx in (ctxs or {}).items():
            if not isinstance(ctx, dict) or "midPx" not in ctx:
                continue
            if ctx["midPx"] is None:
                self._mids.pop(coin, None)
            else:
                self._mids[coin] = str(ctx["midPx"])
        return self._mids


async def _heartbeat(ws: Any, interval: float, name: str = "hl") -> None:
    """Ping every ``interval``; a failed ping closes ``ws`` so its reader ends and the
    owner reconnects (a silent half-open socket would otherwise hang the reader)."""
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send(json.dumps({"method": "ping"}))
        except Exception as e:
            log.warning("%s WS ping failed (%s); closing to reconnect", name, type(e).__name__)
            try:
                await ws.close()
            except Exception:
                pass
            return


async def _stop_task(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# -- FillWatcher -----------------------------------------------------------------------

OnFill = Callable[[TradeEvent], Any]


class FillWatcher:
    """Watches followed wallets' fills: WS ``userFills`` for the first
    ``ws_max_user_subs`` follows (by follow start, then address), REST polling for the
    rest. Every new fill is stored; each new leg at or after the wallet's live start
    becomes a TradeEvent (``seq`` from kv ``hl:seq:{addr}``), is persisted in
    ``paper_events`` (idempotent on source_ref) and handed to ``on_fill``.

    ``follows``: ``{address: follow started_at or None}`` (None = the watcher start).
    """

    def __init__(self, engine: Engine, client: Any, follows: Mapping[str, datetime | None],
                 *, ws_url: str = DEFAULT_WS_URL, on_fill: OnFill | None = None,
                 now: Now = utcnow, start_time: datetime | None = None,
                 ws_max_user_subs: int = WS_MAX_USER_SUBS,
                 poll_interval_s: float = POLL_INTERVAL_S,
                 weight_per_min: int = WEIGHT_PER_MIN,
                 poll_weight_share: float = POLL_WEIGHT_SHARE,
                 reconnect_base_s: float = RECONNECT_BASE_S,
                 reconnect_max_s: float = RECONNECT_MAX_S,
                 ping_interval_s: float = PING_INTERVAL_S,
                 fetch_leverage: bool = True,
                 sz_decimals: Mapping[str, int] | None = None,
                 recovery_retry_s: float = RECOVERY_RETRY_S,
                 recovery_retry_max_s: float = RECOVERY_RETRY_MAX_S,
                 extra_weight_per_min: float = 0.0,
                 recovery_max_age_s: float = RECOVERY_MAX_AGE_S) -> None:
        self.engine = engine
        self.client = client
        self.ws_url = ws_url
        self.on_fill = on_fill
        self._now = now
        self.start_time = start_time or now()
        self.follow_start: dict[str, datetime] = {
            a.lower(): (t or self.start_time) for a, t in follows.items()}
        order = sorted(self.follow_start, key=lambda a: (self.follow_start[a], a))
        self.ws_addresses = order[:ws_max_user_subs]
        self.poll_addresses = order[ws_max_user_subs:]
        self.live_from_ms = {a: _ms(max(self.start_time, t))
                             for a, t in self.follow_start.items()}
        # newest non-snapshot fill time seen per address: REST recovery / polling restart
        # here (inclusive; duplicates are dropped by source_ref)
        self.last_seen_ms = dict(self.live_from_ms)
        # set by a failed recovery: the pre-gap last_seen; last_seen stays here until a
        # recovery succeeds (fills after it may be missing)
        self.recovery_floor_ms: dict[str, int] = {}
        self._newest_ms: dict[str, int] = {}  # newest committed fill time (any path)
        # P46: live fills (ws/poll) stored but held while a recovery is pending,
        # addr -> {tid: (fill, via)}; released by the next successful recovery. Lost
        # (not emitted) if the watcher stops first.
        self._held: dict[str, dict[int, tuple[dict, str]]] = {}
        self.recovery_retry_s = recovery_retry_s
        self.recovery_retry_max_s = recovery_retry_max_s
        self.recovery_max_age_s = recovery_max_age_s
        self.stale_missed = 0  # P47: recovered fills emitted as missed
        self._retry_delay: dict[str, float] = {}
        self._retry_due: dict[str, float] = {}  # loop.time() of the next retry
        self.poll_interval_s = poll_interval_s
        self.weight_per_min = weight_per_min
        self.poll_weight_share = poll_weight_share
        self.reconnect_base_s = reconnect_base_s
        self.reconnect_max_s = reconnect_max_s
        self.ping_interval_s = ping_interval_s
        self.fetch_leverage = fetch_leverage
        self.sz_decimals = dict(sz_decimals) if sz_decimals is not None else None
        self._leverage: dict[str, tuple[float, dict[str, Decimal]]] = {}
        self.connects: list[datetime] = []  # WS (re)connect times
        self.events: list[TradeEvent] = []  # emitted in this run (debug / smoke)
        # M7: other REST weight per minute spent by the process (e.g. the runtime's
        # reconcile clearinghouseState calls), reserved out of the polling share
        self.extra_weight_per_min = extra_weight_per_min
        self._item_weight: dict[str, int] = {}  # last poll's per-item weight per address
        # one recovery (fetch + ingest) per address at a time; ``since`` is read under it
        self._recover_locks: dict[str, asyncio.Lock] = {}
        self._persisted: dict[str, tuple[int | None, int | None]] = {}
        self._restore_cursors()

    # -- persisted cursors (M6) --------------------------------------------------------

    def _restore_cursors(self) -> None:
        """Resume from the persisted per-address cursor (kv ``hl:last_seen:{addr}`` /
        ``hl:floor:{addr}``) so fills made while the process was down are recovered
        and emitted (observed_time = the recovery response time). Never before the
        follow start. A pending floor is restored for WS addresses (their recovery
        retries it); a polled address simply polls from it."""
        with Session(self.engine) as s:
            for a, fs in self.follow_start.items():
                fs_ms = _ms(fs)
                seen = pipeline.kv_get_json(s, KV_LAST_SEEN.format(a))
                floor = pipeline.kv_get_json(s, KV_FLOOR.format(a))
                seen = seen if isinstance(seen, int) else None
                floor = floor if isinstance(floor, int) and floor >= fs_ms else None
                cursor = floor if floor is not None else seen
                if cursor is None:
                    # first watch of this follow: persist the live start, so a restart
                    # recovers fills made while down even if none was seen before
                    self._persist_cursor(a)
                    continue
                start = max(fs_ms, cursor)
                self.live_from_ms[a] = start
                self.last_seen_ms[a] = start
                if floor is not None and a in self.ws_addresses:
                    self.recovery_floor_ms[a] = start
                    self._retry_due[a] = 0.0  # due at once
                self._persisted[a] = (seen, floor)

    def _persist_cursor(self, addr: str) -> None:
        cur = (self.last_seen_ms.get(addr), self.recovery_floor_ms.get(addr))
        if self._persisted.get(addr) == cur:
            return
        try:
            with Session(self.engine) as s:
                pipeline.kv_set_json(s, KV_LAST_SEEN.format(addr), cur[0])
                if cur[1] is None:
                    row = s.get(Kv, KV_FLOOR.format(addr))
                    if row is not None:
                        s.delete(row)
                else:
                    pipeline.kv_set_json(s, KV_FLOOR.format(addr), cur[1])
                s.commit()
        except Exception:  # I4: a DB error must not kill the watcher; retried next change
            log.exception("hl watcher %s: persisting the cursor failed", addr)
            return
        self._persisted[addr] = cur

    # -- ingest ----------------------------------------------------------------------

    async def ingest(self, addr: str, fills: Iterable[Mapping], *, observed: datetime,
                     via: str, snapshot: bool = False) -> list[TradeEvent]:
        """Store ``fills`` of ``addr``; unless ``snapshot``, emit the new legs at or after
        the live start with ``observed_time = observed``. ``via``: ws | poll | recovery."""
        addr = addr.lower()
        fills = [dict(f) for f in fills]
        release = via == "recovery"  # a successful recovery releases the held fills
        if not fills and not (release and addr in self.recovery_floor_ms):
            return []
        newest = max((int(f["time"]) for f in fills), default=0)
        live_from = self.live_from_ms.get(addr, _ms(self.start_time))
        cand = [] if snapshot else [
            f for f in fills if int(f["time"]) >= live_from and not is_spot(f)]
        if cand and not release and addr in self.recovery_floor_ms:
            # P46: a recovery is pending -> store but hold; emitted with the gap fills.
            # Buffered before any await, so the release cannot miss it.
            held = self._held.setdefault(addr, {})
            for f in cand:
                held.setdefault(int(f["tid"]), (f, via))
            cand = []
        vias: dict[int, str] = {int(f["tid"]): via for f in cand}
        need_lev = bool(cand) or (release and bool(self._held.get(addr)))
        # leverage BEFORE any DB session: no session is held across the network call
        lev = await self._leverage_for(addr) if need_lev and self.fetch_leverage else {}
        if release:  # no await from here to the floor clear: nothing can slip between
            for tid, (f, v) in self._held.get(addr, {}).items():
                if tid not in vias:
                    cand.append(f)
                    vias[tid] = f"{v}:held"
                    if tid not in {int(x["tid"]) for x in fills}:
                        fills.append(f)  # re-store (insert dedupes) for the chain context
        # P47: a recovered fill older than the max age is emitted as missed
        stale_ms = _ms(observed) - int(self.recovery_max_age_s * 1000) if release else None
        out = self._store_and_emit(addr, fills, cand, lev, observed, vias, stale_ms)
        # only now is everything up to ``newest`` committed
        if release:
            self._held.pop(addr, None)
            if self.recovery_floor_ms.pop(addr, None) is not None:
                log.info("hl watcher %s: recovery succeeded; floor cleared, %d event(s) "
                         "released", addr, len(out))
            self._retry_delay.pop(addr, None)
            self._retry_due.pop(addr, None)
        if not snapshot:
            self._advance(addr, newest)
        for ev in out:
            self.events.append(ev)
            if self.on_fill is not None:
                try:
                    res = self.on_fill(ev)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:  # a consumer bug must not stop the watcher
                    log.exception("hl watcher: on_fill failed for %s", ev.source_ref)
        return out

    def _advance(self, addr: str, newest: int) -> None:
        if newest > self._newest_ms.get(addr, 0):
            self._newest_ms[addr] = newest
        if addr not in self.recovery_floor_ms:  # else held at the floor until recovery
            # after a release this jumps past the held fills too (all committed)
            newest = self._newest_ms.get(addr, 0)
            if newest > self.last_seen_ms.get(addr, 0):
                self.last_seen_ms[addr] = newest
        self._persist_cursor(addr)

    def _store_and_emit(self, addr: str, fills: list[dict], cand: list[dict],
                        lev: Mapping[str, Decimal], observed: datetime,
                        vias: Mapping[int, str],
                        stale_before_ms: int | None = None) -> list[TradeEvent]:
        """Synchronous (no await): store fills, persist the new legs' events (one
        chain-ordered batch, all ``observed``), commit. ``vias``: tid -> via. Legs with
        a leader time before ``stale_before_ms`` are stored settled as missed (P47)."""
        with Session(self.engine) as s:
            insert_fills(s, addr, fills)
            s.commit()
            if not cand:
                return []
            legs = self._new_legs(s, addr, cand)
            if not legs:
                return []
            key = KV_SEQ.format(addr)
            seq = int(pipeline.kv_get_json(s, key) or 0)
            out: list[TradeEvent] = []
            for lg in legs:
                seq += 1
                ev = TradeEvent(
                    head=HEAD, wallet=addr, market_id=lg.coin, side=lg.side,  # type: ignore[arg-type]
                    direction=lg.direction,  # type: ignore[arg-type]
                    size=lg.size, price=lg.price, notional_usd=lg.size * lg.price,
                    fee_usd=lg.fee, leader_time=ms_to_dt(lg.time_ms), observed_time=observed,
                    source_ref=lg.source_ref, leverage=lev.get(lg.coin),
                    position_id=lg.position_id, group_id=None, seq=seq)
                delay = _ms(observed) - lg.time_ms
                stale = stale_before_ms is not None and lg.time_ms < stale_before_ms
                if stale:
                    self.stale_missed += 1
                    log.info("hl watcher %s: recovered fill %s is %.0f min old; missed",
                             addr, lg.source_ref, delay / 60_000)
                s.add(PaperEvent(source_ref=ev.source_ref, address=addr,
                                 json={**event_to_json(ev),
                                       "via": vias.get(int(lg.fill["tid"]), ""),
                                       # the leader's signed position around this leg
                                       # (alerts: size change vs previous size)
                                       "leader_start": str(lg.start),
                                       "leader_end": str(lg.end),
                                       # P49: the leader's realized PnL on this leg
                                       "leader_closed_pnl": str(lg.closed_pnl),
                                       **({"stale_recovery": True} if stale else {})},
                                 observed_time=observed, delay_ms=delay, seq=seq,
                                 missed=True if stale else None,
                                 settled_at=observed if stale else None))
                out.append(ev)
            pipeline.kv_set_json(s, key, seq)
            s.commit()
        return out

    def _new_legs(self, s: Session, addr: str, cand: list[dict]):
        """Chain-ordered legs of ``cand`` not yet emitted. Legs are normalized over the
        stored fills of the same coins since the follow start, so position ids continue
        episodes across batches (ids depend only on that fixed window start)."""
        coins = sorted({f["coin"] for f in cand})
        ctx_start = _ms(self.follow_start.get(addr, self.start_time))
        rows = s.scalars(select(Fill).where(Fill.address == addr, Fill.coin.in_(coins),
                                            Fill.time >= ctx_start)
                         .order_by(Fill.time, Fill.tid))
        ctx = [pipeline.fill_dict(r) for r in rows]
        tids = {int(f["tid"]) for f in cand}
        legs = [lg for lg in fill_legs(addr, ctx, sz_decimals=self.sz_decimals)
                if int(lg.fill["tid"]) in tids]
        if not legs:
            return []
        refs = [lg.source_ref for lg in legs]
        done = set(s.scalars(select(PaperEvent.source_ref)
                             .where(PaperEvent.source_ref.in_(refs))))
        return [lg for lg in legs if lg.source_ref not in done]

    async def _leverage_for(self, addr: str) -> dict[str, Decimal]:
        loop = asyncio.get_running_loop()
        hit = self._leverage.get(addr)
        if hit is not None and loop.time() - hit[0] < LEVERAGE_TTL_S:
            return hit[1]
        try:
            lev = leverage_by_coin(await self.client.clearinghouse_state(addr))
        except Exception as e:  # HL down or a malformed answer: leverage unknown (None)
            log.warning("hl watcher %s: clearinghouseState unusable (%s: %s); leverage unknown",
                        addr, type(e).__name__, e)
            lev = {}
        self._leverage[addr] = (loop.time(), lev)
        return lev

    # -- run -------------------------------------------------------------------------

    def poll_interval(self) -> float:
        """Seconds between poll cycles: POLL_INTERVAL_S, or slower when the polled
        wallets would use more than ``poll_weight_share`` of the weight budget."""
        if not self.poll_addresses:
            return self.poll_interval_s
        # M7: base weight plus the last-seen per-item weight of every polled address ...
        per_cycle = sum(INFO_WEIGHT_DEFAULT + self._item_weight.get(a, 0)
                        for a in self.poll_addresses)
        # ... and the weight spent outside polling: leverage lookups (clearinghouseState,
        # at most once per LEVERAGE_TTL_S per follow) plus the caller's extra
        other = self.extra_weight_per_min
        if self.fetch_leverage:
            other += (len(self.follow_start) * info_weight("clearinghouseState")
                      * 60.0 / LEVERAGE_TTL_S)
        budget = max(self.weight_per_min * self.poll_weight_share - other, 1.0)
        return max(self.poll_interval_s, per_cycle * 60.0 / budget)

    def _note_poll(self, addr: str, n_fills: int) -> None:
        self._item_weight[addr] = item_weight("userFillsByTime", n_fills)

    async def run(self, stop: asyncio.Event) -> None:
        if self.sz_decimals is None:
            try:
                with Session(self.engine) as s:
                    self.sz_decimals = await pipeline.load_sz_decimals(s, self.client)
            except Exception as e:  # chain tolerance falls back to the relative default
                log.warning("hl watcher: szDecimals unavailable (%s)", type(e).__name__)
                self.sz_decimals = {}
        tasks = []
        if self.ws_addresses:
            tasks.append(asyncio.create_task(self._ws_loop()))
            tasks.append(asyncio.create_task(self._retry_loop()))
        if self.poll_addresses:
            tasks.append(asyncio.create_task(self._poll_loop()))
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _recover(self, addrs: Iterable[str], *, retry: bool = False) -> None:
        """REST backfill since the last seen fill (the floor after a failed recovery);
        observed = when the response arrived (the recovery time: never before the fill,
        never the leader time). A failure keeps the floor and schedules a retry.

        One recovery per address at a time (a per-address lock around fetch + ingest);
        ``since`` is read under the lock, so a late failure can never re-set a stale
        floor. ``retry=True`` (the retry timer) skips an address whose floor another
        recovery has cleared meanwhile."""
        loop = asyncio.get_running_loop()
        for a in addrs:
            lock = self._recover_locks.setdefault(a, asyncio.Lock())
            async with lock:
                if retry and a not in self.recovery_floor_ms:
                    self._retry_due.pop(a, None)
                    self._retry_delay.pop(a, None)
                    continue
                since = self.recovery_floor_ms.get(a, self.last_seen_ms[a])
                try:
                    page = await self.client.user_fills_by_time(a, since)
                    await self.ingest(a, page.fills, observed=self._now(), via="recovery")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.recovery_floor_ms.setdefault(a, since)
                    self._persist_cursor(a)
                    prev = self._retry_delay.get(a)
                    delay = self.recovery_retry_s if prev is None else \
                        min(self.recovery_retry_max_s, prev * 2)
                    self._retry_delay[a] = delay
                    self._retry_due[a] = loop.time() + delay
                    log.warning("hl watcher %s: recovery since %d failed (%s: %s); "
                                "retry in %.0f s", a, since, type(e).__name__, e, delay)
                    continue
                # success: ingest released the held fills, cleared the floor, advanced

    async def _retry_loop(self) -> None:
        """Re-run failed recoveries when due (without waiting for a reconnect)."""
        loop = asyncio.get_running_loop()
        tick = min(1.0, self.recovery_retry_s / 2)
        while True:
            await asyncio.sleep(tick)
            now = loop.time()
            due = [a for a, t in self._retry_due.items() if t <= now]
            if due:
                await self._recover(due, retry=True)

    async def _ws_loop(self) -> None:
        attempt = 0
        subs = set(self.ws_addresses)
        while True:
            try:
                async with connect(self.ws_url, ping_interval=None, max_size=None) as ws:
                    for a in self.ws_addresses:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {
                            "type": "userFills", "user": a}}))
                    self.connects.append(self._now())
                    hb = asyncio.create_task(
                        _heartbeat(ws, self.ping_interval_s, "hl userFills"))
                    try:
                        # messages queue meanwhile; fills of the gap come back here
                        await self._recover(self.ws_addresses)
                        attempt = 0
                        async for raw in ws:
                            received = self._now()
                            await self._on_ws(raw, received, subs)
                    finally:
                        await _stop_task(hb)
                log.warning("hl userFills WS closed; reconnecting")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("hl userFills WS error: %s: %s", type(e).__name__, e)
            await asyncio.sleep(min(self.reconnect_max_s, self.reconnect_base_s * 2 ** attempt))
            attempt += 1

    async def _on_ws(self, raw: str | bytes, received: datetime, subs: set[str]) -> None:
        msg = json.loads(raw)
        ch = msg.get("channel")
        if ch == "error":
            log.warning("hl WS error message: %s", msg.get("data"))
            return
        if ch != "userFills":
            return
        data = msg.get("data") or {}
        user = str(data.get("user") or "").lower()
        if user not in subs:
            return
        await self.ingest(user, data.get("fills") or [], observed=received, via="ws",
                          snapshot=bool(data.get("isSnapshot")))

    async def _poll_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            t0 = loop.time()
            for a in self.poll_addresses:
                try:
                    page = await self.client.user_fills_by_time(a, self.last_seen_ms[a])
                    self._note_poll(a, len(page.fills))
                    await self.ingest(a, page.fills, observed=self._now(), via="poll")
                except asyncio.CancelledError:
                    raise
                except HLError as e:
                    log.warning("hl watcher %s: poll failed (%s)", a, type(e).__name__)
                except Exception:  # malformed answer, DB error: log, keep polling
                    log.exception("hl watcher %s: poll iteration failed", a)
            await asyncio.sleep(max(0.0, self.poll_interval() - (loop.time() - t0)))


# -- funding ---------------------------------------------------------------------------

class FundingRates:
    """Hourly funding rates per coin (``fundingHistory``), cached in kv per UTC day
    (``hl:funding:{coin}:{day_ms}`` = ``{"fetched_at", "rates": {hour_ms: rate}}``).
    :meth:`ensure` fetches (async); :meth:`rates` only reads the cache (sync, for the
    simulator's ``holding_cost``)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._mem: dict[tuple[str, int], dict] = {}
        self.missing_hours = 0

    def _entry(self, s: Session, coin: str, day: int) -> dict | None:
        hit = self._mem.get((coin, day))
        if hit is None:
            raw = pipeline.kv_get_json(s, KV_FUNDING.format(coin, day))
            if isinstance(raw, dict) and isinstance(raw.get("rates"), dict):
                hit = raw
                self._mem[(coin, day)] = hit
        return hit

    @staticmethod
    def _days(start_ms: int, end_ms: int) -> list[int]:
        return list(range(start_ms // DAY_MS * DAY_MS, end_ms + 1, DAY_MS)) if end_ms >= start_ms \
            else []

    async def ensure(self, client: Any, coin: str, start: datetime, end: datetime,
                     now: datetime | None = None) -> None:
        now_ms = _ms(now or utcnow())
        s_ms, e_ms = _ms(start), min(_ms(end), now_ms)
        with Session(self.engine) as s:
            stale = []
            for day in self._days(s_ms, e_ms):
                c = self._entry(s, coin, day)
                fetched = c.get("fetched_at", 0) if c else None
                final = fetched is not None and fetched >= day + DAY_MS + FUNDING_FINAL_AFTER_MS
                if c is None or (not final and now_ms - fetched >= FUNDING_REFRESH_MS):
                    stale.append(day)
            if not stale:
                return
            lo, hi = stale[0], min(stale[-1] + DAY_MS - 1, now_ms)
            page = await client.funding_history(coin, lo, hi)
            by_day: dict[int, dict[str, str]] = {d: {} for d in stale}
            for r in page.rows:
                h = int(r["time"]) // HOUR_MS * HOUR_MS
                d = h // DAY_MS * DAY_MS
                if d in by_day:
                    by_day[d][str(h)] = str(r["fundingRate"])
            for d, rates in by_day.items():
                entry = {"fetched_at": now_ms, "rates": rates}
                pipeline.kv_set_json(s, KV_FUNDING.format(coin, d), entry)
                self._mem[(coin, d)] = entry
            s.commit()

    def rates(self, coin: str, start: datetime, end: datetime) -> list[Decimal]:
        """Cached hourly rates of the funding hours ``h`` with ``start <= h < end``,
        oldest first. Hours not cached are skipped (counted in ``missing_hours``)."""
        s_ms, e_ms = _ms(start), _ms(end)
        first = -(-s_ms // HOUR_MS) * HOUR_MS
        out: list[Decimal] = []
        missing = 0
        with Session(self.engine) as s:
            for h in range(first, e_ms, HOUR_MS):
                c = self._entry(s, coin, h // DAY_MS * DAY_MS)
                r = c["rates"].get(str(h)) if c else None
                if r is None:
                    missing += 1
                    continue
                out.append(Decimal(r))
        if missing:
            self.missing_hours += missing
            log.warning("hl funding %s: %d hour(s) in [%s, %s) not cached; counted as 0",
                        coin, missing, start.isoformat(), end.isoformat())
        return out


# -- execution model -------------------------------------------------------------------

def is_buy(ev: TradeEvent) -> bool:
    """Our copy buys when it opens/increases a long or reduces/closes a short."""
    return (ev.direction == "short") != (ev.side in ("open", "increase"))


class HLExecutionModel:
    """core ``ExecutionModel`` for Hyperliquid perps (spec 8.2, 12a).

    - ``fill_price``: the recorded mid at ``observed_time + 1 s`` (first mid in
      ``[observed + 1 s, observed + 30 s]``, ruling P45; else None = missed), moved AGAINST us by half-spread 0.02% plus slippage
      ``0.0005 * sqrt(notional / 100_000)``. ``notional`` is OUR copy's notional: the
      fixed ``copy_notional_usd`` when given, else ``stake_usd x min(leverage, 10)``
      (the simulator sizes after pricing, so the model can only estimate it).
    - ``fee``: base taker fee 0.045% of notional.
    - ``holding_cost``: funding while held: ``notional x sum(hourly rates in [start,
      end))``; a long pays positive rates, a short receives them (needs
      ``FundingRates.ensure`` beforehand; uncached hours count 0 and are logged).
      Approximation: the notional is the ENTRY notional (``qty x entry_price``), not
      the hourly mark notional, so funding drifts from HL's as the price moves.
    """

    def __init__(self, prices: Any, funding: FundingRates | None = None, *,
                 copy_notional_usd: Decimal | None = None,
                 stake_usd: Decimal = DEFAULT_STAKE_USD,
                 taker_fee: Decimal = TAKER_FEE, max_leverage: Decimal = MAX_LEVERAGE) -> None:
        self.prices = prices
        self.funding = funding
        self.copy_notional_usd = copy_notional_usd
        self.stake_usd = stake_usd
        self.taker_fee = taker_fee
        self.max_leverage = max_leverage

    def notional(self, ev: TradeEvent) -> Decimal:
        if self.copy_notional_usd is not None:
            return self.copy_notional_usd
        lev = min(ev.leverage or Decimal(1), self.max_leverage)
        return self.stake_usd * lev

    def adjustment(self, notional: Decimal) -> Decimal:
        return HALF_SPREAD + SLIPPAGE_COEF * (max(notional, Decimal(0)) / SLIPPAGE_REF_USD).sqrt()

    def fill_price(self, ev: TradeEvent) -> Decimal | None:
        mid = self.prices.price_at(ev.market_id, ev.observed_time + FILL_DELAY, MODEL_WINDOW_S)
        if mid is None:
            return None
        adj = self.adjustment(self.notional(ev))
        return mid * (1 + adj) if is_buy(ev) else mid * (1 - adj)

    def fee(self, notional: Decimal) -> Decimal:
        return notional * self.taker_fee

    def holding_cost(self, pos: OpenPosition, start: datetime, end: datetime) -> Decimal:
        if self.funding is None or end <= start:
            return Decimal(0)
        total = sum(self.funding.rates(pos.market_id, start, end), Decimal(0))
        cost = pos.qty * pos.entry_price * total
        return -cost if pos.direction == "short" else cost
