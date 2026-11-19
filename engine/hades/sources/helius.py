"""Async Helius client: Enhanced Transactions API + RPC, rate-limited with retries.

The API key is only ever placed in request query params; it is never logged,
included in exception messages, or shown in repr().
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.helius.xyz"
DEFAULT_RPC_URL = "https://mainnet.helius-rpc.com/"
PAGE_LIMIT = 100
SIG_PAGE_LIMIT = 1000
PARSE_CHUNK = 100

SleepFn = Callable[[float], Awaitable[Any]]

_KEY_RE = re.compile(r"(api-key=)[^&\s\"']+")


class _RedactApiKey(logging.Filter):
    """httpx logs full request URLs at INFO; strip the api-key value."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "api-key=" in msg:
            record.msg = _KEY_RE.sub(r"\1***", msg)
            record.args = ()
        return True


_REDACTOR = _RedactApiKey()


def _install_redactor() -> None:
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        if _REDACTOR not in lg.filters:
            lg.addFilter(_REDACTOR)


class HeliusError(Exception):
    """Helius request failed (persistent 5xx, unexpected 4xx, RPC error, network).

    For paginated calls, `partial` holds the items fetched before the failure
    and `cursor` the `before` signature to resume from (None = from newest).
    """

    def __init__(self, *args: object, partial: list[dict] | None = None, cursor: str | None = None) -> None:
        super().__init__(*args)
        self.partial: list[dict] = partial if partial is not None else []
        self.cursor: str | None = cursor


class HeliusRateLimited(HeliusError):
    """Still HTTP 429 after all retries were used."""


class _ContinueSearch(HeliusError):
    """4xx from the Enhanced API telling us to keep paging from `cursor`."""


# e.g. "... To continue search, query the API again with the `before` parameter set to <sig>."
_CONTINUE_RE = re.compile(r"continue search.*?parameter set to\s+`?([A-Za-z0-9]+)", re.I | re.S)


class TokenBucket:
    """Async token bucket: `rate` tokens/sec, burst capacity `rate`."""

    def __init__(self, rate: float, sleep: SleepFn = asyncio.sleep) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate = float(rate)
        self.capacity = float(rate)
        self.tokens = float(rate)
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


class HeliusClient:
    def __init__(
        self,
        api_key: str,
        rps: float,
        max_retries: int = 5,
        *,
        api_base: str = DEFAULT_API_BASE,
        rpc_url: str = DEFAULT_RPC_URL,
        base_delay: float = 0.5,
        sleep: SleepFn = asyncio.sleep,
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        _install_redactor()
        self._api_key = api_key
        self.max_retries = max_retries
        self.api_base = api_base.rstrip("/")
        self.rpc_url = rpc_url
        self.base_delay = base_delay
        self._sleep = sleep
        self._bucket = TokenBucket(rps, sleep)
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owns_http = http is None
        self._rpc_id = 0

    def __repr__(self) -> str:
        return f"HeliusClient(api_base={self.api_base!r}, rpc_url={self.rpc_url!r})"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "HeliusClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- transport -----------------------------------------------------------

    async def _request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> Any:
        """Send with rate limiting; retry 429/5xx/network errors with backoff."""
        query = {"api-key": self._api_key, **(params or {})}
        safe_url = httpx.URL(url).copy_with(query=None)  # for logs/errors: no key
        last_status: int | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.base_delay * 2 ** (attempt - 1)
                log.warning(
                    "helius %s %s -> %s, retry %d/%d in %.2fs",
                    method, safe_url, last_status or "network error", attempt, self.max_retries, delay,
                )
                await self._sleep(delay)
            await self._bucket.acquire()
            try:
                resp = await self._http.request(method, url, params=query, json=json)
            except httpx.TransportError as e:
                last_status = None
                last_exc_name = type(e).__name__
                if attempt == self.max_retries:
                    raise HeliusError(f"{method} {safe_url}: {last_exc_name}") from None
                continue
            status = resp.status_code
            if status == 429 or status >= 500:
                last_status = status
                continue
            if status >= 400:
                m = _CONTINUE_RE.search(resp.text)
                if m:
                    raise _ContinueSearch(f"{method} {safe_url} -> HTTP {status} (continue)", cursor=m.group(1))
                raise HeliusError(f"{method} {safe_url} -> HTTP {status}")
            return resp.json()
        if last_status == 429:
            raise HeliusRateLimited(
                f"{method} {safe_url}: still HTTP 429 after {self.max_retries} retries"
            )
        raise HeliusError(f"{method} {safe_url}: HTTP {last_status} after {self.max_retries} retries")

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        data = await self._request("POST", self.rpc_url, json=body)
        if data.get("error"):
            err = data["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            raise HeliusError(f"RPC {method}: {msg}")
        return data.get("result")

    # -- Enhanced API --------------------------------------------------------

    async def address_transactions(
        self,
        address: str,
        *,
        tx_type: str | None = "SWAP",
        before: str | None = None,
        max_pages: int,
    ) -> list[dict]:
        """Parsed txs for an address, newest first, 100 per page, up to max_pages.

        A Helius "continue search with before=<sig>" 4xx is followed (it counts
        toward max_pages). On any other failure the raised HeliusError carries
        `partial` (items fetched so far) and `cursor` (resume with before=cursor).
        """
        url = f"{self.api_base}/v0/addresses/{address}/transactions"
        out: list[dict] = []
        cursor = before
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": PAGE_LIMIT}
            if tx_type:
                params["type"] = tx_type
            if cursor:
                params["before"] = cursor
            try:
                page = await self._request("GET", url, params=params)
            except _ContinueSearch as e:
                # Sparse type filter: no match in this search window; keep
                # paging from the cursor Helius gave us (counts as a page).
                cursor = e.cursor
                continue
            except HeliusError as e:
                e.partial, e.cursor = out, cursor
                raise
            if not page:
                break
            out.extend(page)
            cursor = page[-1].get("signature")
            if not cursor:
                break
        return out

    async def parse_transactions(self, signatures: list[str]) -> list[dict]:
        """POST /v0/transactions in chunks of 100 signatures."""
        url = f"{self.api_base}/v0/transactions"
        out: list[dict] = []
        for i in range(0, len(signatures), PARSE_CHUNK):
            chunk = signatures[i : i + PARSE_CHUNK]
            out.extend(await self._request("POST", url, json={"transactions": chunk}) or [])
        return out

    # -- RPC -----------------------------------------------------------------

    async def signatures_oldest(self, address: str, max_pages: int) -> list[str]:
        """Signatures for address, paging backward; returned oldest -> newest.

        If the address has more than max_pages*1000 signatures the result is
        truncated: it starts at the oldest signature that was reached, not the
        address's true first signature.
        """
        newest_first: list[str] = []
        before: str | None = None
        for _ in range(max_pages):
            opts: dict[str, Any] = {"limit": SIG_PAGE_LIMIT}
            if before:
                opts["before"] = before
            result = await self._rpc("getSignaturesForAddress", [address, opts]) or []
            newest_first.extend(r["signature"] for r in result)
            if len(result) < SIG_PAGE_LIMIT:
                break
            before = result[-1]["signature"]
        newest_first.reverse()
        return newest_first

    async def first_incoming_sol(
        self, address: str, max_pages: int = 5
    ) -> tuple[str, str, int] | None:
        """(funder, signature, slot) of the oldest native SOL transfer into address."""
        sigs = await self.signatures_oldest(address, max_pages=max_pages)
        if not sigs:
            return None
        txs = await self.parse_transactions(sigs[:100])
        order = {s: i for i, s in enumerate(sigs)}
        txs.sort(key=lambda t: (t.get("slot") or 0, order.get(t.get("signature", ""), 0)))
        for tx in txs:
            if tx.get("transactionError"):
                continue
            for nt in tx.get("nativeTransfers") or []:
                if nt.get("toUserAccount") == address and (nt.get("amount") or 0) > 0:
                    return nt.get("fromUserAccount"), tx["signature"], int(tx["slot"])
        return None
