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
call, oldest first from ``startTime``, and only the 10,000 most recent fills are
reachable. userFunding returns at most 500 rows per call, oldest first.

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
RATE_LIMITS_DOC = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits"
)

FILLS_PAGE_LIMIT = 2000
FILLS_HISTORY_CAP = 10_000
FUNDING_PAGE_LIMIT = 500
MAX_FUNDING_PAGES = 200  # 100k funding rows: a hard stop against a misbehaving API
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_S = 120.0

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


SleepFn = Callable[[float], Awaitable[Any]]
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

    ``truncated``: the 10,000-fill history cap was reached, so fills older than the
    first one returned may exist but are unreachable through the info API.
    """

    fills: list[dict] = field(default_factory=list)
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
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                await self._sleep((weight - self.tokens) / self.rate)

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
        self, addr: str, start_ms: int, end_ms: int | None = None
    ) -> HLPage:
        """All reachable fills of ``addr`` from ``start_ms`` (inclusive), oldest first.

        Pages forward while a page is full (2000 rows). Each next page restarts at the
        page's last ``time`` *inclusive* and de-duplicates by ``tid``, because fills of
        one order can share a millisecond and a page boundary can fall inside it (a
        plain ``last time + 1`` restart would silently drop those). If a full page is
        entirely one millisecond the restart advances by 1 ms to guarantee progress.
        Stops at the 10,000-fill history cap with ``truncated=True``.
        """
        out: list[dict] = []
        seen: set[int] = set()
        cursor = int(start_ms)
        while True:
            body: dict[str, Any] = {"type": "userFillsByTime", "user": addr, "startTime": cursor}
            if end_ms is not None:
                body["endTime"] = int(end_ms)
            page = await self._info(body)
            if not isinstance(page, list):
                raise HLError("info userFillsByTime: expected a list")
            for f in page:
                tid = f.get("tid")
                if tid in seen:
                    continue
                seen.add(tid)
                out.append(f)
                if len(out) >= FILLS_HISTORY_CAP:
                    return HLPage(out, truncated=True)
            if len(page) < FILLS_PAGE_LIMIT:
                return HLPage(out, truncated=False)
            last = max(int(f["time"]) for f in page)
            cursor = last + 1 if last <= cursor else last

    async def user_funding(self, addr: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Funding payments of ``addr`` from ``start_ms`` (inclusive), oldest first.

        Pages while a page is full (500 rows), restarting inclusive at the last time
        and de-duplicating by (time, coin) like :meth:`user_fills_by_time`.
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
            for r in page:
                key = (int(r["time"]), (r.get("delta") or {}).get("coin", ""))
                if key in seen:
                    continue
                seen.add(key)
                out.append(r)
            if len(page) < FUNDING_PAGE_LIMIT:
                break
            last = max(int(r["time"]) for r in page)
            cursor = last + 1 if last <= cursor else last
        return out

    async def clearinghouse_state(self, addr: str) -> dict:
        return await self._info({"type": "clearinghouseState", "user": addr})

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
        return await self._info({"type": "meta"})

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
