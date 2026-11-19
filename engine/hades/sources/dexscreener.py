"""Async DexScreener client (public API, no key) + runner filter.

Any field of a DexScreener pair may be missing or null; helpers here coerce
missing/invalid numbers to 0 (or None) and never raise on malformed pairs.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import httpx

from hades.sources.helius import TokenBucket

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.dexscreener.com"
DEFAULT_RPS = 4
MINTS_PER_REQUEST = 30
DAY_MS = 86_400_000

RUNNER_MIN_LIQ_USD = 30_000
RUNNER_MIN_H24_PCT = 300
RUNNER_MIN_FDV = 1_000_000
RUNNER_MAX_AGE_DAYS = 7

SleepFn = Callable[[float], Awaitable[Any]]


class DexScreenerError(Exception):
    """DexScreener request failed (persistent 429/5xx, unexpected 4xx, network)."""


# -- pair helpers ------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _sub(pair: Any, key: str) -> dict:
    v = pair.get(key) if isinstance(pair, dict) else None
    return v if isinstance(v, dict) else {}


def liquidity_usd(pair: dict) -> float:
    return _num(_sub(pair, "liquidity").get("usd")) or 0.0


def base_address(pair: dict) -> str | None:
    addr = _sub(pair, "baseToken").get("address")
    return addr if isinstance(addr, str) else None


def best_pair(pairs: Iterable[dict]) -> dict | None:
    """Pair with the largest liquidity.usd (first wins on ties), or None if empty."""
    best: dict | None = None
    best_liq = -1.0
    for p in pairs:
        if not isinstance(p, dict):
            continue
        liq = liquidity_usd(p)
        if liq > best_liq:
            best, best_liq = p, liq
    return best


def is_runner(pair: dict, now_ms: int) -> bool:
    """liquidity.usd >= 30k AND (h24 >= 300% OR (fdv >= 1M AND age <= 7 days))."""
    if not isinstance(pair, dict):
        return False
    if liquidity_usd(pair) < RUNNER_MIN_LIQ_USD:
        return False
    h24 = _num(_sub(pair, "priceChange").get("h24")) or 0.0
    if h24 >= RUNNER_MIN_H24_PCT:
        return True
    fdv = _num(pair.get("fdv")) or 0.0
    created = _num(pair.get("pairCreatedAt"))
    if created is None or fdv < RUNNER_MIN_FDV:
        return False
    age_days = (now_ms - created) / DAY_MS
    return age_days <= RUNNER_MAX_AGE_DAYS


# -- client ------------------------------------------------------------------


class DexScreener:
    def __init__(
        self,
        rps: float = DEFAULT_RPS,
        max_retries: int = 3,
        *,
        base_url: str = DEFAULT_BASE_URL,
        base_delay: float = 0.5,
        sleep: SleepFn = asyncio.sleep,
        timeout: float = 20.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.rps = rps
        self.max_retries = max_retries
        self.base_url = base_url.rstrip("/")
        self.base_delay = base_delay
        self._sleep = sleep
        self._bucket = TokenBucket(rps, sleep)
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http is None

    def __repr__(self) -> str:
        return f"DexScreener(base_url={self.base_url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "DexScreener":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str) -> Any:
        """GET with rate limiting; retry 429/5xx/network errors with backoff."""
        url = f"{self.base_url}{path}"
        last: str = "?"
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.base_delay * 2 ** (attempt - 1)
                log.warning("dexscreener GET %s -> %s, retry %d/%d in %.2fs",
                            path, last, attempt, self.max_retries, delay)
                await self._sleep(delay)
            await self._bucket.acquire()
            try:
                resp = await self._http.get(url)
            except httpx.TransportError as e:
                last = type(e).__name__
                continue
            status = resp.status_code
            if status == 429 or status >= 500:
                last = f"HTTP {status}"
                continue
            if status >= 400:
                raise DexScreenerError(f"GET {path} -> HTTP {status}")
            try:
                return resp.json()
            except ValueError:
                raise DexScreenerError(f"GET {path}: invalid JSON") from None
        raise DexScreenerError(f"GET {path}: {last} after {self.max_retries} retries")

    async def token_pairs(self, mints: list[str]) -> list[dict]:
        """All Solana pairs for the given mints (30 mints per request, concatenated)."""
        out: list[dict] = []
        for i in range(0, len(mints), MINTS_PER_REQUEST):
            chunk = mints[i : i + MINTS_PER_REQUEST]
            data = await self._get(f"/tokens/v1/solana/{','.join(chunk)}")
            if isinstance(data, list):
                out.extend(p for p in data if isinstance(p, dict))
        return out

    async def boosted_tokens(self) -> list[str]:
        """De-duplicated Solana token addresses from top boosts, then latest profiles."""
        seen: dict[str, None] = {}
        for path in ("/token-boosts/top/v1", "/token-profiles/latest/v1"):
            data = await self._get(path)
            if not isinstance(data, list):
                continue
            for item in data:
                if not isinstance(item, dict) or item.get("chainId") != "solana":
                    continue
                addr = item.get("tokenAddress")
                if isinstance(addr, str) and addr:
                    seen.setdefault(addr, None)
        return list(seen)

    async def price_native(self, mint: str) -> float | None:
        """priceNative of the most liquid pair where `mint` is the base token."""
        pairs = [p for p in await self.token_pairs([mint]) if base_address(p) == mint]
        best = best_pair(pairs)
        if best is None:
            return None
        return _num(best.get("priceNative"))
