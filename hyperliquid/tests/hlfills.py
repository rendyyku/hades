"""Synthetic HL fill / funding builders shared by the Task 5 tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

ADDR = "0x" + "ab" * 20
T0 = 1_790_000_000_000  # ms
DAY_MS = 86_400_000

_tid = [1000]


def mk(coin: str, side: str, sz, px, start, t: int, *, dir: str | None = None,
       closed_pnl="0.0", fee="0.1", crossed: bool = True, tid: int | None = None,
       liquidation: dict | None = None, hash: str | None = None) -> dict:
    """One fill. ``side`` is 'B' (buy) or 'A' (sell); ``start`` is startPosition."""
    if tid is None:
        _tid[0] += 1
        tid = _tid[0]
    s = Decimal(str(start))
    d = Decimal(str(sz)) * (1 if side == "B" else -1)
    if dir is None:
        e = s + d
        if s == 0:
            dir = "Open Long" if d > 0 else "Open Short"
        elif (s > 0) == (d > 0):
            dir = "Open Long" if s > 0 else "Open Short"
        elif e != 0 and (e > 0) != (s > 0):
            dir = "Long > Short" if s > 0 else "Short > Long"
        else:
            dir = "Close Long" if s > 0 else "Close Short"
    f = {"coin": coin, "px": str(px), "sz": str(sz), "side": side, "time": t,
         "startPosition": str(start), "dir": dir, "closedPnl": str(closed_pnl),
         "hash": hash or f"0xh{tid}", "oid": tid, "crossed": crossed, "fee": str(fee),
         "tid": tid, "feeToken": "USDC"}
    if liquidation is not None:
        f["liquidation"] = liquidation
    return f


def funding(coin: str, usdc, t: int, szi="1.0") -> dict:
    return {"time": t, "hash": "0x0", "delta": {"type": "funding", "coin": coin,
                                               "usdc": str(usdc), "szi": str(szi),
                                               "fundingRate": "0.0001", "nSamples": None}}


def round_trip(coin: str, t_open: int, hold_ms: int, pnl, *, sz="1", px="100",
               fee="0.1", crossed=True, long=True) -> list[dict]:
    """Open then fully close one position; the close carries ``closedPnl=pnl``."""
    o_side, c_side = ("B", "A") if long else ("A", "B")
    start_close = sz if long else f"-{sz}"
    move = Decimal(str(pnl)) / Decimal(str(sz))  # close price consistent with closedPnl
    close_px = Decimal(str(px)) + (move if long else -move)
    return [
        mk(coin, o_side, sz, px, "0", t_open, fee=fee, crossed=crossed),
        mk(coin, c_side, sz, close_px, start_close, t_open + hold_ms, closed_pnl=pnl, fee=fee,
           crossed=crossed),
    ]


def ms_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
