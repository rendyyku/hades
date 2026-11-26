"""Shared async HTTP client: per-host token bucket, retry with backoff, JSON-or-fail.

Every source goes through here, so rate limits and the User-Agent are applied once.
Raises SourceError for anything a watcher should treat as "this source failed".
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "hades-signals/2.0 (+github.com/rendyyku/hades)"
DEFAULT_RPS = 2.0
MAX_DELAY_S = 60.0

SleepFn = Callable[[float], Awaitable[Any]]


class SourceError(Exception):
    """A request failed for good (4xx, exhausted retries, network, or a non-JSON body)."""

    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


class TokenBucket:
    """Async token bucket: `rate` tokens/sec, burst capacity max(1, rate)."""

    def __init__(self, rate: float, sleep: SleepFn = asyncio.sleep) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = max(1.0, float(rate))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._sleep = sleep
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await self._sleep((1 - self.tokens) / self.rate)


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return max(0.0, float(resp.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


class Http:
    def __init__(self, client: httpx.AsyncClient, rates: dict[str, float], sleep: SleepFn = asyncio.sleep) -> None:
        self.client = client
        self.rates = dict(rates)
        self._sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}

    def _bucket(self, url: str) -> TokenBucket:
        host = urlsplit(url).hostname or ""
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(self.rates.get(host, DEFAULT_RPS), self._sleep)
        return self._buckets[host]

    async def _request(self, method: str, url: str, *, max_retries: int, **kw: Any) -> httpx.Response:
        headers = {"User-Agent": USER_AGENT, **(kw.pop("headers", None) or {})}
        for attempt in range(max_retries + 1):
            await self._bucket(url).acquire()
            try:
                resp = await self.client.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as e:
                err: str = f"network: {type(e).__name__}"
                wait = None
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code != 429 and resp.status_code < 500:
                    raise SourceError(f"HTTP {resp.status_code} for {urlsplit(url).hostname}", resp.status_code)
                err = f"HTTP {resp.status_code}"
                wait = _retry_after(resp)
            if attempt == max_retries:
                raise SourceError(f"{err} for {urlsplit(url).hostname} after {attempt + 1} tries")
            delay = wait if wait is not None else min(MAX_DELAY_S, 2**attempt) * random.uniform(0.5, 1.5)
            log.debug("retry %s %s in %.1fs (%s)", method, url, delay, err)
            await self._sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise SourceError(f"non-JSON body from {resp.request.url.host}") from e

    async def get_json(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                       max_retries: int = 4, timeout: float = 20.0) -> Any:
        resp = await self._request("GET", url, params=params, headers=headers, max_retries=max_retries,
                                   timeout=timeout)
        return self._json(resp)

    async def get_text(self, url: str, *, params: dict | None = None, headers: dict | None = None,
                       max_retries: int = 4, timeout: float = 20.0) -> str:
        resp = await self._request("GET", url, params=params, headers=headers, max_retries=max_retries,
                                   timeout=timeout)
        return resp.text

    async def post_json(self, url: str, *, json: Any, headers: dict | None = None,
                        max_retries: int = 2, timeout: float = 20.0) -> Any:
        resp = await self._request("POST", url, json=json, headers=headers, max_retries=max_retries,
                                   timeout=timeout)
        return self._json(resp)
