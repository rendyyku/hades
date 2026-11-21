"""Async client for the public Hyperliquid info API (no keys, no signing).

Rate limits (per IP), verified 2026-11-20 against
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits :

- "REST requests share an aggregated weight limit of 1200 per minute."
- info weight 2: l2Book, allMids, clearinghouseState, orderStatus,
  spotClearinghouseState, exchangeStatus.
- info weight 60: userRole.
- "All other documented info requests have weight 20."
- "additional rate limit weight per 20 items returned in the response" for
  recentTrades, historicalOrders, userFills, userFillsByTime, fundingHistory,
  userFunding, nonUserFundingUpdates, twapHistory, userTwapSliceFills,
  userTwapSliceFillsByTime, delegatorHistory, delegatorRewards, validatorStats.
- candleSnapshot: additional weight per 60 items returned.

The per-item weight is only known once the response arrives, so it is charged to the
bucket afterwards (the bucket may go into debt and the next request waits it off).

Paging caps (spec 8.2 and observed live): userFillsByTime returns at most 2000 rows per
call; when more fills match it returns the OLDEST 2000 of ``[startTime, endTime]``,
ascending (live probe 2026-11-20, both bounds inclusive). The docs say only the 10,000
most recent fills are reachable. :meth:`HLClient.user_fills_backward` therefore walks
time windows newest-first (ruling P27). userFunding returns at most 500 rows per call,
oldest first.

The leaderboard is NOT part of the documented API: it is the static JSON the HL web
app loads (``stats-data.hyperliquid.xyz/Mainnet/leaderboard``, ~38 MB, ~46k rows on
2026-11-20). It is a different host, so it is not charged to the info weight budget.
If it fails, ``leaderboard()`` raises ``HLUnavailable`` and discovery falls back to
wallets seen in large trades on the ``trades`` WS (spec 8.2).
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
# Unofficial too: the vault list the HL app's Vaults page loads (~14 MB, ~9.5k vaults on
# 2026-11-20). Used only to exclude vaults from discovery candidates.
DEFAULT_VAULTS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
RATE_LIMITS_DOC = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits"
)

FILLS_PAGE_LIMIT = 2000
FILLS_HISTORY_CAP = 10_000
# user_fills_backward: first window width, and a hard stop against a misbehaving API
BACKWARD_WINDOW_MS = 3_600_000
MAX_BACKWARD_REQUESTS = 500
FUNDING_PAGE_LIMIT = 500
MAX_FUNDING_PAGES = 200  # 100k funding rows: a hard stop against a misbehaving API
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_S = 120.0
BUCKET_EPS = 1e-6  # weight units
MIN_BUCKET_SLEEP_S = 1e-3

# -- weights (source: RATE_LIMITS_DOC) ---------------------------------------------
WEIGHT_PER_MIN = 1200
INFO_WEIGHT_DEFAULT = 20
INFO_WEIGHT: dict[str, int] = {
    "l2Book": 2,
    "allMids": 2,
    "clearinghouseState": 2,
    "orderStatus": 2,
    "spotClearinghouseState": 2,
    "exchangeStatus": 2,
    "userRole": 60,
}
# request type -> number of returned items that cost +1 weight
ITEMS_PER_EXTRA_WEIGHT: dict[str, int] = {
    **dict.fromkeys(
        (
            "recentTrades", "historicalOrders", "userFills", "userFillsByTime",
            "fundingHistory", "userFunding", "nonUserFundingUpdates", "twapHistory",
            "userTwapSliceFills", "userTwapSliceFillsByTime", "delegatorHistory",
            "delegatorRewards", "validatorStats",
        ),
        20,
    ),
    "candleSnapshot": 60,
}


def info_weight(request_type: str) -> int:
    """Base IP weight of one ``info`` request of ``request_type``."""
    return INFO_WEIGHT.get(request_type, INFO_WEIGHT_DEFAULT)


def item_weight(request_type: str, n_items: int) -> int:
    """Extra weight charged for ``n_items`` returned by ``request_type``."""
    per = ITEMS_PER_EXTRA_WEIGHT.get(request_type)
    return n_items // per if per else 0


def check_clearinghouse_state(state: Any) -> dict:
    """``state`` if it is a complete ``clearinghouseState`` answer: an object with a
    ``marginSummary`` object and an ``assetPositions`` list (a flat account has an
    empty list, never a missing one). Anything else (``null``, ``{}``, an error
    object, a truncated body) raises HLError: an unknown state must never read as
    "no positions"."""
    if not isinstance(state, dict):
        raise HLError(f"info clearinghouseState: expected an object, got {type(state).__name__}")
    summary, positions = state.get("marginSummary"), state.get("assetPositions")
    if not isinstance(summary, dict) or not isinstance(positions, list):
        raise HLError("info clearinghouseState: no marginSummary / assetPositions")
    return state


SleepFn = Callable[[float], Awaitable[Any]]
PageFn = Callable[[list[dict]], Any]
ClockFn = Callable[[], float]


class HLError(Exception):
    """An HL request failed (persistent 5xx, non-retryable 4xx, network, bad JSON)."""

    def __init__(self, *args: object, status: int | None = None) -> None:
        super().__init__(*args)
        self.status = status


class HLRateLimited(HLError):
    """Still HTTP 429 after MAX_ATTEMPTS attempts."""


class HLUnavailable(HLError):
    """An unofficial source (the leaderboard feed) is down or changed shape."""


@dataclass
class HLPage:
    """Result of a paged fills read.

    ``truncated``: the history is known to be incomplete. Either the 10,000-fill
    history cap was reached (fills older than the first one returned may exist but are
    unreachable through the info API), or a full 2000-row page was entirely one
    millisecond and paging had to skip past that millisecond (more fills of that ms
    may exist). Scoring treats both as partial history (ruling P17).
    """

    fills: list[dict] = field(default_factory=list)
    truncated: bool = False
    # user_fills_backward only: ``capped`` = stopped at ``cap`` (older fills exist but
    # were not fetched); ``cov_start`` = start (ms) of the contiguous range covered:
    # ``start_ms`` normally, the oldest kept fill's time when capped.
    capped: bool = False
    cov_start: int | None = None


@dataclass
class FundingPage:
    """Result of a paged funding read. ``truncated``: paging stopped at
    MAX_FUNDING_PAGES while pages were still full, so later rows may be missing."""

    rows: list[dict] = field(default_factory=list)
    truncated: bool = False


class WeightBucket:
    """Async token bucket over request *weight*: ``per_min`` weight per minute,
    burst capacity ``per_min``. ``charge`` debits post-hoc weight (may go negative)."""

    def __init__(
        self, per_min: float, *, sleep: SleepFn = asyncio.sleep, clock: ClockFn = time.monotonic
    ) -> None:
        if per_min <= 0:
            raise ValueError("per_min must be > 0")
        self.capacity = float(per_min)
        self.rate = float(per_min) / 60.0  # weight per second
        self.tokens = float(per_min)
        self._clock = clock
        self._sleep = sleep
        self.updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    async def acquire(self, weight: float) -> None:
        weight = min(float(weight), self.capacity)
        async with self._lock:
            while True:
                self._refill()
                # tolerance: float rounding can leave tokens a hair below ``weight`` after
                # a refill; the matching tiny sleep is lost in a large clock value and the
                # loop would spin forever
                if self.tokens >= weight - BUCKET_EPS:
                    self.tokens -= weight
                    return
                await self._sleep(max((weight - self.tokens) / self.rate, MIN_BUCKET_SLEEP_S))

    def charge(self, weight: float) -> None:
        self._refill()
        self.tokens -= float(weight)


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        secs = float(raw)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        secs = (when - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(secs, MAX_RETRY_AFTER_S))


class HLClient:
    def __init__(
        self,
        *,
        info_url: str = DEFAULT_INFO_URL,
        leaderboard_url: str = DEFAULT_LEADERBOARD_URL,
        vaults_url: str = DEFAULT_VAULTS_URL,
        weight_per_min: int = WEIGHT_PER_MIN,
        max_attempts: int = MAX_ATTEMPTS,
        base_delay: float = 1.0,
        timeout: float = 30.0,
        leaderboard_timeout: float = 120.0,
        sleep: SleepFn = asyncio.sleep,
        clock: ClockFn = time.monotonic,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.info_url = info_url
        self.leaderboard_url = leaderboard_url
        self.vaults_url = vaults_url
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.leaderboard_timeout = leaderboard_timeout
        self._sleep = sleep
        self._bucket = WeightBucket(weight_per_min, sleep=sleep, clock=clock)
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http is None

    @classmethod
    def from_settings(cls, settings: Any, **kw: Any) -> "HLClient":
        return cls(
            info_url=settings.INFO_URL,
            leaderboard_url=settings.LEADERBOARD_URL,
            vaults_url=getattr(settings, "VAULTS_URL", DEFAULT_VAULTS_URL),
            weight_per_min=settings.RATE_WEIGHT_PER_MIN,
            **kw,
        )

    def __repr__(self) -> str:
        return f"HLClient(info_url={self.info_url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "HLClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- transport ----------------------------------------------------------------

    async def _send(
        self, method: str, url: str, *, json: Any = None, weight: float = 0,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Send with weight limiting; retry 429 (honouring Retry-After), 5xx and
        network errors with exponential backoff, at most ``max_attempts`` attempts."""
        what = json.get("type") if isinstance(json, dict) else url
        last_status: int | None = None
        for attempt in range(1, self.max_attempts + 1):
            if weight:
                await self._bucket.acquire(weight)
            kwargs: dict[str, Any] = {"json": json} if json is not None else {}
            if timeout is not None:
                kwargs["timeout"] = timeout
            delay: float | None = None
            try:
                resp = await self._http.request(method, url, **kwargs)
            except httpx.TransportError as e:
                last_status = None
                if attempt == self.max_attempts:
                    raise HLError(f"{method} {what}: {type(e).__name__}") from None
            else:
                status = resp.status_code
                if status < 400:
                    return resp
                if status != 429 and status < 500:
                    raise HLError(f"{method} {what} -> HTTP {status}", status=status)
                last_status = status
                if status == 429:
                    delay = _retry_after(resp)
                if attempt == self.max_attempts:
                    break
            if delay is None:
                delay = self.base_delay * 2 ** (attempt - 1)
            log.warning("hl %s %s -> %s, attempt %d/%d, retry in %.1fs",
                        method, what, last_status or "network error", attempt,
                        self.max_attempts, delay)
            await self._sleep(delay)
        if last_status == 429:
            raise HLRateLimited(
                f"{method} {what}: still HTTP 429 after {self.max_attempts} attempts", status=429
            )
        raise HLError(
            f"{method} {what}: HTTP {last_status} after {self.max_attempts} attempts",
            status=last_status,
        )

    async def _info(self, body: dict) -> Any:
        rtype = body["type"]
        resp = await self._send("POST", self.info_url, json=body, weight=info_weight(rtype))
        try:
            data = resp.json()
        except ValueError:
            raise HLError(f"info {rtype}: response is not JSON") from None
        if isinstance(data, list):
            extra = item_weight(rtype, len(data))
            if extra:
                self._bucket.charge(extra)
        return data

    # -- endpoints ------------------------------------------------------------------

    async def user_fills_by_time(
        self, addr: str, start_ms: int, end_ms: int | None = None, *,
        on_page: PageFn | None = None,
    ) -> HLPage:
        """All reachable fills of ``addr`` from ``start_ms`` (inclusive), oldest first.

        Pages forward while a page is full (2000 rows). Each next page restarts at the
        page's last ``time`` *inclusive* and de-duplicates by ``tid``, because fills of
        one order can share a millisecond and a page boundary can fall inside it (a
        plain ``last time + 1`` restart would silently drop those). If a full page is
        entirely one millisecond the restart advances by 1 ms to guarantee progress.
        Stops at the 10,000-fill history cap with ``truncated=True``.

        ``on_page(new_fills)`` is called after each page with the fills it added, so a
        caller can persist progress before a later page fails (e.g. HLRateLimited).
        """
        out: list[dict] = []
        seen: set[int] = set()
        cursor = int(start_ms)
        skipped_ms = False
        while True:
            body: dict[str, Any] = {"type": "userFillsByTime", "user": addr, "startTime": cursor}
            if end_ms is not None:
                body["endTime"] = int(end_ms)
            page = await self._info(body)
            if not isinstance(page, list):
                raise HLError("info userFillsByTime: expected a list")
            new: list[dict] = []
            capped = False
            for f in page:
                tid = f.get("tid")
                if tid is None:
                    raise HLError(f"info userFillsByTime: fill without tid at time {f.get('time')}")
                if tid in seen:
                    continue
                seen.add(tid)
                new.append(f)
                if len(out) + len(new) >= FILLS_HISTORY_CAP:
                    capped = True
                    break
            out.extend(new)
            if on_page is not None and new:
                on_page(new)
            if capped:
                return HLPage(out, truncated=True)
            if len(page) < FILLS_PAGE_LIMIT:
                return HLPage(out, truncated=skipped_ms)
            last = max(int(f["time"]) for f in page)
            if last <= cursor:
                # a full page inside one millisecond: more fills of this ms may exist
                # but cannot be paged to; skip ahead and flag the history as partial
                log.warning("hl userFillsByTime %s: full page in the same millisecond %d, "
                            "skipping 1 ms; history marked truncated", addr, last)
                skipped_ms = True
                cursor = last + 1
            else:
                cursor = last

    async def _fills_window(self, addr: str, lo: int, hi: int) -> list[dict]:
        page = await self._info(
            {"type": "userFillsByTime", "user": addr, "startTime": lo, "endTime": hi})
        if not isinstance(page, list):
            raise HLError("info userFillsByTime: expected a list")
        for f in page:
            if f.get("tid") is None:
                raise HLError(f"info userFillsByTime: fill without tid at time {f.get('time')}")
        return page

    async def user_fills_backward(
        self, addr: str, start_ms: int, end_ms: int | None = None,
        cap: int = FILLS_HISTORY_CAP, *, on_page: PageFn | None = None,
        window_ms: int = BACKWARD_WINDOW_MS, max_requests: int | None = None,
    ) -> HLPage:
        """Fills of ``addr`` in ``[start_ms, end_ms]`` (default end: now), fetched
        NEWEST FIRST so a ``cap`` keeps the most recent fills (ruling P27).

        HL's ``userFillsByTime`` returns the *oldest* <= 2000 fills of the requested
        window, ascending (live probe 2026-11-20), so a single ``[start, cursor]``
        window cannot page backward. Instead this walks adjacent, disjoint windows
        ``[hi - w + 1, hi]`` from ``end_ms`` down to ``start_ms``:

        - a page below 2000 rows is the window's complete content: keep it, move
          ``hi`` below the window, and widen ``w`` (x2) when the page was small;
        - a full page means the window may hold more: shrink ``w`` (to 3/4 of the
          page's own span, at most half) and retry the same ``hi``; a full page of a
          1 ms window cannot be split further: keep it and flag ``truncated``.

        Empty windows widen geometrically, so sparse histories cost O(log range)
        requests. Stops at ``start_ms``, at ``cap`` unique fills (``capped=True``,
        ``truncated=True``; within the last window the newest fills are kept) or when
        a non-empty page brings no new fill (the API ignoring the window). Inclusive
        bounds; de-duplicated by ``tid``. ``on_page(new)`` after every kept page, newest
        window first.

        Early exits (``max_requests``, default MAX_BACKWARD_REQUESTS, spent; or a stalled
        window) leave ``[start_ms, hi]`` unfetched: ``cov_start = hi + 1`` (only what
        was fetched is covered; empty when that is above ``end_ms``) and
        ``truncated=True`` (history partial), ``capped=False``.
        """
        start_ms = int(start_ms)
        hi = int(end_ms) if end_ms is not None else int(time.time() * 1000)
        w = max(1, int(window_ms))
        budget = MAX_BACKWARD_REQUESTS if max_requests is None else int(max_requests)
        out: list[dict] = []
        seen: set[int] = set()
        capped = skipped_ms = early = False
        n_req = 0
        while hi >= start_ms and not capped:
            if n_req >= budget:
                log.warning("hl userFillsByTime %s: stopped after %d requests; coverage only "
                            "down to %d; history marked truncated", addr, n_req, hi + 1)
                early = True
                break
            n_req += 1
            lo = max(start_ms, hi - w + 1)
            page = await self._fills_window(addr, lo, hi)
            if len(page) >= FILLS_PAGE_LIMIT:
                if hi > lo:
                    times = [int(f["time"]) for f in page]
                    span = max(times) - min(times) + 1
                    w = max(1, min((hi - lo + 1) // 2, span * 3 // 4))
                    continue
                log.warning("hl userFillsByTime %s: full page in the single millisecond %d; "
                            "more fills of that ms may exist; history marked truncated",
                            addr, hi)
                skipped_ms = True
            new = [f for f in page if f["tid"] not in seen]
            if page and not new:
                log.warning("hl userFillsByTime %s: window [%d, %d] brought no new fills; "
                            "stopping; coverage only down to %d; history marked truncated",
                            addr, lo, hi, hi + 1)
                early = True
                break
            room = cap - len(out)
            if len(new) > room or (len(new) == room and lo > start_ms):
                new.sort(key=lambda f: int(f["time"]), reverse=True)
                new = new[:room]
                capped = True
            seen.update(f["tid"] for f in new)
            out.extend(new)
            if on_page is not None and new:
                on_page(new)
            if len(page) < FILLS_PAGE_LIMIT // 2:
                w *= 2
            hi = lo - 1
        if capped:
            cov_start = min(int(f["time"]) for f in out) if out else hi + 1
        elif early:
            cov_start = hi + 1
        else:
            cov_start = start_ms
        return HLPage(out, truncated=capped or skipped_ms or early, capped=capped,
                      cov_start=cov_start)

    async def user_fills(self, addr: str) -> list[dict]:
        """The latest <= 2000 fills of ``addr`` (``userFills``; one request). Used as the
        cheap discovery activity probe (ruling P30)."""
        page = await self._info({"type": "userFills", "user": addr})
        if not isinstance(page, list):
            raise HLError("info userFills: expected a list")
        return page

    async def user_funding(
        self, addr: str, start_ms: int, end_ms: int | None = None, *,
        on_page: PageFn | None = None,
    ) -> FundingPage:
        """Funding payments of ``addr`` from ``start_ms`` (inclusive), oldest first.

        Pages while a page is full (500 rows), restarting inclusive at the last time
        and de-duplicating by (time, coin) like :meth:`user_fills_by_time`. Stops after
        MAX_FUNDING_PAGES pages with ``truncated=True`` (and a warning).
        ``on_page(new_rows)`` as in :meth:`user_fills_by_time`.
        """
        out: list[dict] = []
        seen: set[tuple[int, str]] = set()
        cursor = int(start_ms)
        for _ in range(MAX_FUNDING_PAGES):
            body: dict[str, Any] = {"type": "userFunding", "user": addr, "startTime": cursor}
            if end_ms is not None:
                body["endTime"] = int(end_ms)
            page = await self._info(body)
            if not isinstance(page, list):
                raise HLError("info userFunding: expected a list")
            new: list[dict] = []
            for r in page:
                key = (int(r["time"]), (r.get("delta") or {}).get("coin", ""))
                if key in seen:
                    continue
                seen.add(key)
                new.append(r)
            out.extend(new)
            if on_page is not None and new:
                on_page(new)
            if len(page) < FUNDING_PAGE_LIMIT:
                return FundingPage(out, truncated=False)
            last = max(int(r["time"]) for r in page)
            cursor = last + 1 if last <= cursor else last
        log.warning("hl userFunding %s: stopped at MAX_FUNDING_PAGES=%d with full pages; "
                    "funding marked truncated", addr, MAX_FUNDING_PAGES)
        return FundingPage(out, truncated=True)

    async def funding_history(self, coin: str, start_ms: int,
                              end_ms: int | None = None) -> FundingPage:
        """Hourly funding rates of ``coin`` (``fundingHistory``: ``coin``, ``fundingRate``,
        ``premium``, ``time``) from ``start_ms`` (inclusive), oldest first. Pages like
        :meth:`user_funding` (500 rows per page, de-duplicated by ``time``)."""
        out: list[dict] = []
        seen: set[int] = set()
        cursor = int(start_ms)
        for _ in range(MAX_FUNDING_PAGES):
            body: dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": cursor}
            if end_ms is not None:
                body["endTime"] = int(end_ms)
            page = await self._info(body)
            if not isinstance(page, list):
                raise HLError("info fundingHistory: expected a list")
            for r in page:
                t = int(r["time"])
                if t not in seen:
                    seen.add(t)
                    out.append(r)
            if len(page) < FUNDING_PAGE_LIMIT:
                return FundingPage(out, truncated=False)
            last = max(int(r["time"]) for r in page)
            cursor = last + 1 if last <= cursor else last
        log.warning("hl fundingHistory %s: stopped at MAX_FUNDING_PAGES=%d with full pages",
                    coin, MAX_FUNDING_PAGES)
        return FundingPage(out, truncated=True)

    async def clearinghouse_state(self, addr: str, dex: str | None = None) -> dict:
        """``clearinghouseState`` of ``addr``: the main perp dex, or the HIP-3 dex
        ``dex`` (the default answer lists main-dex positions only; verified live
        2026-11-22). Raises HLError unless the answer has the documented shape
        (:func:`check_clearinghouse_state`): callers close paper positions on it."""
        body: dict[str, Any] = {"type": "clearinghouseState", "user": addr}
        if dex:
            body["dex"] = dex
        return check_clearinghouse_state(await self._info(body))

    async def all_mids(self) -> dict[str, Decimal]:
        raw = await self._info({"type": "allMids"})
        out: dict[str, Decimal] = {}
        for coin, px in (raw or {}).items():
            try:
                out[coin] = Decimal(px)
            except (InvalidOperation, TypeError):
                log.warning("hl allMids: unparseable mid for %s", coin)
        return out

    async def meta(self) -> dict:
        data = await self._info({"type": "meta"})
        if not isinstance(data, dict):
            raise HLError(f"info meta: expected an object, got {type(data).__name__}")
        return data

    async def candle_snapshot(self, coin: str, interval: str, start_ms: int,
                              end_ms: int) -> list[dict]:
        """Candles of ``coin`` in ``[start_ms, end_ms]`` (``t``/``T`` open/close ms,
        ``o``/``h``/``l``/``c`` prices as strings, ``n`` trades, ``v`` volume).
        HL serves only the most recent 5000 candles per interval (live probe
        2026-11-21: 1m reaches ~3.5 days back, 5m ~17 d, 15m ~52 d, 1h ~208 d)."""
        data = await self._info({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval, "startTime": int(start_ms),
            "endTime": int(end_ms)}})
        if not isinstance(data, list):
            raise HLError("info candleSnapshot: expected a list")
        return data

    async def user_fills_range(self, addr: str, start_ms: int, end_ms: int) -> list[dict]:
        """One ``userFillsByTime`` request over ``[start_ms, end_ms]``: the OLDEST <= 2000
        fills of that range, ascending (see the module doc)."""
        return await self._fills_window(addr, int(start_ms), int(end_ms))

    async def leaderboard(self) -> list[dict]:
        """Rows of the HL app's leaderboard feed (unofficial; see module doc).

        Raises HLUnavailable on any failure (HTTP error, network, shape change).
        Use the rows only as discovery candidates; never trust their stats.
        """
        try:
            resp = await self._send(
                "GET", self.leaderboard_url, timeout=self.leaderboard_timeout
            )
            data = resp.json()
        except (HLError, ValueError) as e:
            raise HLUnavailable(f"leaderboard unavailable: {e}") from None
        rows = data.get("leaderboardRows") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise HLUnavailable("leaderboard: unexpected shape (no leaderboardRows list)")
        return [r for r in rows if isinstance(r, dict) and r.get("ethAddress")]

    async def vault_addresses(self) -> set[str]:
        """Lowercase addresses of every HL vault, from the app's vault list (unofficial).

        Raises HLUnavailable on any failure (HTTP error, network, shape change).
        """
        try:
            resp = await self._send("GET", self.vaults_url, timeout=self.leaderboard_timeout)
            data = resp.json()
        except (HLError, ValueError) as e:
            raise HLUnavailable(f"vault list unavailable: {e}") from None
        if not isinstance(data, list):
            raise HLUnavailable("vault list: unexpected shape (not a list)")
        out: set[str] = set()
        for v in data:
            addr = ((v or {}).get("summary") or {}).get("vaultAddress") if isinstance(v, dict) else None
            if addr:
                out.add(str(addr).lower())
        return out
