"""Per-wallet Hyperliquid stats over a trailing window (spec 8.2).

Definitions (all outputs are plain floats/ints/None so they serialize as JSON):

- ``net_pnl_usd``: realized closedPnl - fees + funding (see :mod:`hades_hl.pnl`).
- margin: replaying the fills, ``sum_coin |position| * last px / leverage``; its peak
  over the window is the capital at risk. Leverage per coin comes from
  clearinghouseState; a coin without one uses the median known leverage, else 1
  (so without clearinghouseState ROI is ROI on peak notional, the conservative case).
- ``roi_on_margin``: net / peak margin (None when there was no exposure).
- ``daily_sharpe``: mean / sample stdev of daily PnL over every day of the window
  (flat days count as 0), not annualized; None with < 2 days or zero variance.
- ``max_dd``: max fractional drawdown of equity = peak margin + cumulative PnL,
  stepped per fill and funding payment; 0 when nothing happened.
- ``trades``: perp fills; ``n``: closed positions; ``winrate``: wins / n (a
  liquidated position is always a loss), None when n = 0.
- ``avg_leverage``: fill-notional-weighted leverage over coins with a known
  leverage, None if none is known.
- ``median_fills_per_day``: median fill count over active days; ``median_hold_s``:
  median hold of closed positions whose open we saw; ``maker_share``: fraction of
  fills with ``crossed == False``.
- Days are 24 h buckets anchored at the window start.

Partial history (ruling P17): with ``history_partial=True`` (the fills read hit the
10k-fill cap) the window starts at ``oldest_fill_time`` (the backfill coverage start,
ruling P27; else the oldest of the given fills; never before the nominal window start)
and ``window_days`` reports the covered span ``(now - that start)`` in days.
``sz_decimals`` also sets the startPosition chain tolerance (ruling P28).

- ``chain_gaps``: startPosition chain breaks (missing fills) in the window, see
  :func:`hades_hl.normalize.normalize_fills`.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal

from hades_core.events import require_utc

from hades_hl.normalize import is_spot, leverage_by_coin, normalize_fills
from hades_hl.pnl import funding_usdc, wallet_pnl

DAY_MS = 86_400_000
ONE = Decimal(1)


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def wallet_stats(
    addr: str,
    fills: Iterable[Mapping],
    funding: Iterable[Mapping],
    now: datetime,
    window_days: float = 30,
    *,
    history_partial: bool = False,
    clearinghouse: Mapping | None = None,
    oldest_fill_time: datetime | None = None,
    sz_decimals: Mapping[str, int] | None = None,
) -> dict:
    require_utc("now", now)
    fills = list(fills)
    now_ms = int(now.timestamp() * 1000)
    start_ms = now_ms - int(window_days * DAY_MS)
    in_win = [f for f in fills if start_ms <= int(f["time"]) <= now_ms]
    span_days: float = window_days
    if history_partial:
        if oldest_fill_time is not None:
            require_utc("oldest_fill_time", oldest_fill_time)
            oldest = int(oldest_fill_time.timestamp() * 1000)
        elif fills:
            oldest = min(int(f["time"]) for f in fills)
        else:
            oldest = now_ms
        start_ms = min(max(start_ms, oldest), now_ms)
        in_win = [f for f in fills if start_ms <= int(f["time"]) <= now_ms]
        span_days = (now_ms - start_ms) / DAY_MS
    perp = [f for f in in_win if not is_spot(f)]
    fund = [r for r in funding if start_ms <= int(r["time"]) <= now_ms]

    pnl = wallet_pnl(addr, perp, fund, sz_decimals=sz_decimals)
    legs = normalize_fills(addr, perp, sz_decimals=sz_decimals).legs

    # -- margin / leverage ------------------------------------------------------
    lev = leverage_by_coin(clearinghouse)
    fallback = Decimal(str(statistics.median(lev.values()))) if lev else ONE
    pos: dict[str, Decimal] = {}
    px: dict[str, Decimal] = {}
    peak_margin = Decimal(0)
    lev_num = lev_den = Decimal(0)

    def margin() -> Decimal:
        return sum((abs(pos[c]) * px[c] / lev.get(c, fallback) for c in pos), Decimal(0))

    for lg in legs:
        if lg.coin not in pos:
            pos[lg.coin], px[lg.coin] = lg.start, lg.price
            peak_margin = max(peak_margin, margin())
        pos[lg.coin], px[lg.coin] = lg.end, lg.price
        peak_margin = max(peak_margin, margin())
        if lg.coin in lev:
            notional = lg.size * lg.price
            lev_num += lev[lg.coin] * notional
            lev_den += notional

    # -- equity curve, daily pnl, drawdown -----------------------------------------
    steps: list[tuple[int, int, Decimal]] = [
        (lg.time_ms, 0, lg.closed_pnl - lg.fee) for lg in legs
    ] + [(int(r["time"]), 1, funding_usdc(r)) for r in fund]
    steps.sort(key=lambda s: (s[0], s[1]))
    n_days = max(1, math.ceil(span_days - 1e-9))

    def day(t: int) -> int:
        return min(max((t - start_ms) // DAY_MS, 0), n_days - 1)

    daily = [0.0] * n_days
    base = float(peak_margin)
    eq = peak = base
    max_dd = 0.0
    for t, _, delta in steps:
        daily[day(t)] += float(delta)
        eq += float(delta)
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
    sharpe = None
    if n_days >= 2:
        sd = statistics.stdev(daily)
        if sd > 0:
            sharpe = statistics.fmean(daily) / sd

    # -- activity ------------------------------------------------------------------
    per_day: dict[int, int] = {}
    for f in perp:
        k = day(int(f["time"]))
        per_day[k] = per_day.get(k, 0) + 1
    closed = pnl.closed_positions
    holds = [c.hold_s for c in closed if c.hold_s is not None]
    n = len(closed)
    net = float(pnl.net_usd)

    return {
        "net_pnl_usd": net,
        "roi_on_margin": net / float(peak_margin) if peak_margin > 0 else None,
        "daily_sharpe": sharpe,
        "max_dd": max_dd,
        "trades": len(perp),
        "winrate": sum(1 for c in closed if c.is_win) / n if n else None,
        "n": n,
        "avg_leverage": float(lev_num / lev_den) if lev_den > 0 else None,
        "median_fills_per_day": _median([float(v) for v in per_day.values()]),
        "median_hold_s": _median(holds),
        "maker_share": (sum(1 for f in perp if f.get("crossed") is False) / len(perp)
                        if perp else None),
        "history_partial": bool(history_partial),
        "window_days": span_days,
        "realized_usd": float(pnl.realized_usd),
        "fees_usd": float(pnl.fees_usd),
        "funding_usd": float(pnl.funding_usd),
        "liquidations": pnl.liquidations,
        "peak_margin_usd": float(peak_margin),
        "closedpnl_mismatch": pnl.closedpnl_mismatch,
        "closedpnl_mismatch_pct": (float(pnl.mismatch_pct)
                                   if pnl.mismatch_pct is not None else None),
        "chain_gaps": pnl.chain_gaps,
    }
