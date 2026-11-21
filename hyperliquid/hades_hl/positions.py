"""Live open positions of a wallet (Task 14, ruling P64).

Source: Hyperliquid's public ``clearinghouseState`` for the address, through the
shared :class:`~hades_hl.client.HLClient` (so the call is charged to the same rate
weight budget as everything else; weight 2). Read-only: no keys, no signing.

- :func:`positions_from_state` maps one answer to :class:`WalletPositions`. Any
  unexpected shape raises ``ValueError``: a partial answer is never served as the
  wallet's positions.
- :class:`PositionsCache` keeps one answer per address for ``POSITIONS_TTL_S`` (5 s),
  shares one upstream call between concurrent requests, and remembers a failure for
  the same 5 s, so a broken upstream is asked once per window, not once per poll.

Scope: the main perp dex. Positions on HIP-3 dexes (``dex:COIN``) are a separate
``clearinghouseState`` per dex and are not listed (``scope`` says so).

HL reports no mark price per position: ``mark_px`` is ``positionValue / |szi|``
(8 significant digits), which is the mark HL valued the position at.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Context, Decimal
from typing import Any, Literal

from pydantic import BaseModel

from hades_hl.client import check_clearinghouse_state

POSITIONS_TTL_S = 5.0
UPSTREAM_TIMEOUT_S = 8.0  # the whole upstream call, retries included
MAX_ENTRIES = 512
SCOPE = "main perp dex"
_MARK_CTX = Context(prec=8)
ZERO = Decimal(0)


class Leverage(BaseModel):
    type: str  # "cross" | "isolated"
    value: Decimal | None


class Position(BaseModel):
    coin: str
    side: Literal["long", "short"]
    size: Decimal  # absolute, in coin units
    entry_px: Decimal | None
    mark_px: Decimal | None
    position_value: Decimal
    unrealized_pnl: Decimal
    roe_pct: Decimal | None  # return on the position's margin, percent
    leverage: Leverage
    liquidation_px: Decimal | None
    margin_used: Decimal


class WalletPositions(BaseModel):
    address: str
    account_value: Decimal
    total_unrealized_pnl: Decimal
    positions: list[Position]
    as_of: datetime  # HL's state time (our fetch time if absent)
    scope: str = SCOPE


class PositionsUnavailable(Exception):
    """The upstream call failed or answered an unusable body (details are dropped)."""


def _dec(v: Any) -> Decimal:
    if v is None or isinstance(v, bool):
        raise ValueError("missing number")
    d = Decimal(str(v))  # InvalidOperation is an ArithmeticError: mapped by the caller
    if not d.is_finite():
        raise ValueError("not a finite number")
    return d


def _opt(v: Any) -> Decimal | None:
    return None if v is None else _dec(v)


def _position(ap: Any) -> Position | None:
    pos = ap.get("position") if isinstance(ap, dict) else None
    if not isinstance(pos, dict) or not isinstance(pos.get("coin"), str):
        raise ValueError("position entry without a coin")
    szi = _dec(pos.get("szi"))
    if szi == 0:
        return None
    size = abs(szi)
    value = _dec(pos.get("positionValue"))
    lev = pos.get("leverage")
    if not isinstance(lev, dict) or not isinstance(lev.get("type"), str):
        raise ValueError("position entry without leverage")
    roe = _opt(pos.get("returnOnEquity"))
    return Position(
        coin=pos["coin"], side="long" if szi > 0 else "short", size=size,
        entry_px=_opt(pos.get("entryPx")), mark_px=_MARK_CTX.divide(value, size),
        position_value=value, unrealized_pnl=_dec(pos.get("unrealizedPnl")),
        roe_pct=None if roe is None else roe * 100,
        leverage=Leverage(type=lev["type"], value=_opt(lev.get("value"))),
        liquidation_px=_opt(pos.get("liquidationPx")), margin_used=_dec(pos.get("marginUsed")))


def positions_from_state(addr: str, state: Any, fetched_at: datetime) -> WalletPositions:
    """Map a ``clearinghouseState`` answer. Raises ValueError on any unexpected shape."""
    try:
        state = check_clearinghouse_state(state)
        rows = [p for p in (_position(ap) for ap in state["assetPositions"]) if p is not None]
        t = state.get("time")
        as_of = (datetime.fromtimestamp(t / 1000, tz=timezone.utc)
                 if isinstance(t, int) and not isinstance(t, bool) else fetched_at)
        rows.sort(key=lambda p: (-p.position_value, p.coin))
        return WalletPositions(
            address=addr, account_value=_dec(state["marginSummary"].get("accountValue")),
            total_unrealized_pnl=sum((p.unrealized_pnl for p in rows), ZERO),
            positions=rows, as_of=as_of)
    except ValueError:
        raise
    except Exception as e:  # HLError, InvalidOperation, OverflowError, ...
        raise ValueError(f"unusable clearinghouseState ({type(e).__name__})") from None


class PositionsCache:
    """Per-key TTL cache with single-flight loading and negative caching."""

    def __init__(self, ttl_s: float = POSITIONS_TTL_S, *,
                 clock: Callable[[], float] = time.monotonic,
                 max_entries: int = MAX_ENTRIES) -> None:
        self.ttl_s = ttl_s
        self.clock = clock
        self.max_entries = max_entries
        # key -> (loaded at, ok, value)
        self._entries: dict[str, tuple[float, bool, Any]] = {}
        self._inflight: dict[str, asyncio.Future] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _fresh(self, key: str) -> tuple[bool, Any] | None:
        hit = self._entries.get(key)
        if hit is None or self.clock() - hit[0] >= self.ttl_s:
            return None
        return hit[1], hit[2]

    def _store(self, key: str, ok: bool, value: Any) -> None:
        now = self.clock()
        self._entries[key] = (now, ok, value)
        if len(self._entries) > self.max_entries:
            for k in [k for k, v in self._entries.items() if now - v[0] >= self.ttl_s]:
                del self._entries[k]
            while len(self._entries) > self.max_entries:  # still over: drop the oldest
                del self._entries[min(self._entries, key=lambda k: self._entries[k][0])]

    async def _load(self, key: str, fetch: Callable[[], Awaitable[Any]]) -> tuple[bool, Any]:
        try:
            res = (True, await fetch())
        except Exception:  # never propagated: the reason must not reach a response
            res = (False, None)
        self._store(key, *res)
        return res

    def _forget(self, key: str, task: asyncio.Future) -> None:
        if self._inflight.get(key) is task:
            del self._inflight[key]

    async def get(self, key: str, fetch: Callable[[], Awaitable[Any]]) -> Any:
        """The cached value of ``key``, else ``await fetch()`` (one call shared by
        concurrent callers). Raises PositionsUnavailable when it failed."""
        res = self._fresh(key)
        if res is None:
            task = self._inflight.get(key)
            if task is None or task.done():
                task = asyncio.ensure_future(self._load(key, fetch))
                self._inflight[key] = task
                task.add_done_callback(lambda t: self._forget(key, t))
            res = await asyncio.shield(task)  # a cancelled caller does not cancel the load
        ok, value = res
        if not ok:
            raise PositionsUnavailable(key)
        return value


__all__ = ["POSITIONS_TTL_S", "SCOPE", "UPSTREAM_TIMEOUT_S", "Leverage", "Position",
           "PositionsCache", "PositionsUnavailable", "WalletPositions", "positions_from_state"]
