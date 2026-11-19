"""Per-wallet stats over a rolling window, bot filter and leaderboard score.

Bot: ``trades_per_day > 150`` OR ``median_hold_sec < 30``. Only non-bot
wallets with ``tokens_closed >= 5`` are scored:

    score = 40·pct(total_pnl_sol) + 25·win_rate + 20·pct(median_roi) + 15·consistency

where ``pct`` is the percentile rank (0..1) among eligible wallets.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from hades.db import Wallet, WalletStats
from hades.pnl import compute_positions, unrealized_pnl

BOT_TRADES_PER_DAY = 150
BOT_MIN_MEDIAN_HOLD_SEC = 30
MIN_TOKENS_CLOSED = 5
CONSISTENCY_WEEKS = 4


@dataclass
class WalletStatsData:
    address: str
    window_days: int
    realized_pnl_sol: float
    unrealized_pnl_sol: float
    total_pnl_sol: float
    tokens_traded: int
    tokens_closed: int
    win_rate: float
    median_hold_sec: float | None
    trades_per_day: float
    is_bot: bool
    median_roi: float | None
    consistency: float
    score: float | None
    computed_at: datetime


def _consistency(positions: Iterable[Any], now: datetime) -> float:
    weekly = [0.0] * CONSISTENCY_WEEKS
    for pos in positions:
        t = pos.last_sell_time
        if t is None:
            continue
        for k in range(CONSISTENCY_WEEKS):
            if now - timedelta(days=7 * (k + 1)) <= t < now - timedelta(days=7 * k):
                weekly[k] += pos.realized_pnl_sol
                break
    return sum(1 for v in weekly if v > 0) / CONSISTENCY_WEEKS


def wallet_stats(
    address: str,
    swaps: Iterable[Any],
    prices: dict[str, float],
    now: datetime,
    window_days: int = 30,
) -> WalletStatsData:
    """Stats for one wallet from the swaps with ``now - window_days <= block_time <= now``.

    Raises ``ValueError`` (from ``compute_positions``) on an unknown swap side.
    """
    start = now - timedelta(days=window_days)
    windowed = [s for s in swaps if start <= s.block_time <= now]
    positions = compute_positions(windowed)
    pos_list = list(positions.values())
    closed = [p for p in pos_list if p.closed]

    realized = sum(p.realized_pnl_sol for p in pos_list)
    unrealized = sum(unrealized_pnl(p, prices.get(p.mint)) for p in pos_list)
    wins = sum(1 for p in closed if p.realized_pnl_sol > 0)
    win_rate = wins / len(closed) if closed else 0.0

    holds = [
        (p.last_sell_time - p.first_buy_time).total_seconds()
        for p in closed
        if p.first_buy_time is not None and p.last_sell_time is not None
    ]
    median_hold = float(median(holds)) if holds else None
    rois = [p.realized_pnl_sol / p.cost_sol for p in closed if p.cost_sol > 0]
    median_roi = float(median(rois)) if rois else None

    trades_per_day = len(windowed) / window_days
    is_bot = trades_per_day > BOT_TRADES_PER_DAY or (
        median_hold is not None and median_hold < BOT_MIN_MEDIAN_HOLD_SEC
    )

    return WalletStatsData(
        address=address,
        window_days=window_days,
        realized_pnl_sol=realized,
        unrealized_pnl_sol=unrealized,
        total_pnl_sol=realized + unrealized,
        tokens_traded=len(pos_list),
        tokens_closed=len(closed),
        win_rate=win_rate,
        median_hold_sec=median_hold,
        trades_per_day=trades_per_day,
        is_bot=is_bot,
        median_roi=median_roi,
        consistency=_consistency(pos_list, now),
        score=None,
        computed_at=now,
    )


def is_eligible(st: WalletStatsData) -> bool:
    return not st.is_bot and st.tokens_closed >= MIN_TOKENS_CLOSED


def _pct_ranks(values: list[float]) -> list[float]:
    """Percentile rank of each value: (#less + 0.5·#equal-others) / (n-1); 1.0 if n == 1."""
    n = len(values)
    if n == 1:
        return [1.0]
    out = []
    for x in values:
        less = sum(1 for v in values if v < x)
        equal_others = sum(1 for v in values if v == x) - 1
        out.append((less + 0.5 * equal_others) / (n - 1))
    return out


def score_wallets(stats: list[WalletStatsData]) -> list[WalletStatsData]:
    """Return copies with ``score`` set for eligible wallets and None for the rest."""
    eligible = [s for s in stats if is_eligible(s)]
    scores: dict[int, float] = {}
    if eligible:
        pnl_pct = _pct_ranks([s.total_pnl_sol for s in eligible])
        roi_key: Callable[[WalletStatsData], float] = (
            lambda s: s.median_roi if s.median_roi is not None else float("-inf")
        )
        roi_pct = _pct_ranks([roi_key(s) for s in eligible])
        for s, pp, rp in zip(eligible, pnl_pct, roi_pct):
            raw = 40 * pp + 25 * s.win_rate + 20 * rp + 15 * s.consistency
            scores[id(s)] = min(100.0, max(0.0, round(raw, 2)))
    return [replace(s, score=scores.get(id(s))) for s in stats]


def persist_stats(session: Session, stats: Iterable[WalletStatsData]) -> None:
    """Upsert ``WalletStats`` rows and ``Wallet.is_bot``. Does not commit."""
    for st in stats:
        row = session.get(WalletStats, st.address)
        if row is None:
            row = WalletStats(address=st.address)
            session.add(row)
        row.window_days = st.window_days
        row.realized_pnl_sol = st.realized_pnl_sol
        row.unrealized_pnl_sol = st.unrealized_pnl_sol
        row.total_pnl_sol = st.total_pnl_sol
        row.tokens_traded = st.tokens_traded
        row.tokens_closed = st.tokens_closed
        row.win_rate = st.win_rate
        row.median_hold_sec = st.median_hold_sec
        row.trades_per_day = st.trades_per_day
        row.score = st.score
        row.computed_at = st.computed_at

        wallet = session.get(Wallet, st.address)
        if wallet is None:
            wallet = Wallet(address=st.address)
            session.add(wallet)
        wallet.is_bot = st.is_bot
