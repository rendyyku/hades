"""Bot/MM/vault rule and percentile score for Hyperliquid wallets.

Bot rule (global, verbatim): vault, OR median fills/day > 300, OR median hold < 60 s,
OR maker share > 70% (maker share = fraction of fills with ``crossed == False``).
A metric that is None (no data) never triggers the rule.

Score: among eligible wallets only, mid-rank percentiles (in (0, 1); ties share the
average; None ranks lowest) of net PnL (40%), daily Sharpe (25%), ROI on margin
(20%) and max drawdown (15%, inverted: lower is better), scaled to 0..100.

Unscored wallets get ``score=None`` and a ``score_reason``, checked in this order:
``'bot'``; ``'history too short (hyperactive)'`` (partial history covering < 7 days,
ruling P17); ``'n<30'`` (fewer than 30 closed positions).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

BOT_MAX_FILLS_PER_DAY = 300
BOT_MIN_HOLD_S = 60
BOT_MAX_MAKER_SHARE = 0.70
MIN_CLOSED_POSITIONS = 30
MIN_PARTIAL_SPAN_DAYS = 7

REASON_BOT = "bot"
REASON_FEW = "n<30"
REASON_SHORT_HISTORY = "history too short (hyperactive)"

# metric -> (weight, higher_is_better)
WEIGHTS: dict[str, tuple[float, bool]] = {
    "net_pnl_usd": (0.40, True),
    "daily_sharpe": (0.25, True),
    "roi_on_margin": (0.20, True),
    "max_dd": (0.15, False),
}


def is_bot(stats: Mapping, is_vault: bool) -> bool:
    if is_vault:
        return True
    fpd = stats.get("median_fills_per_day")
    hold = stats.get("median_hold_s")
    maker = stats.get("maker_share")
    return bool(
        (fpd is not None and fpd > BOT_MAX_FILLS_PER_DAY)
        or (hold is not None and hold < BOT_MIN_HOLD_S)
        or (maker is not None and maker > BOT_MAX_MAKER_SHARE)
    )


def unscored_reason(stats: Mapping, is_vault: bool = False) -> str | None:
    """Why this wallet cannot be scored, or None if it is eligible."""
    if is_bot(stats, is_vault):
        return REASON_BOT
    if stats.get("history_partial") and (stats.get("window_days") or 0) < MIN_PARTIAL_SPAN_DAYS:
        return REASON_SHORT_HISTORY
    if (stats.get("n") or 0) < MIN_CLOSED_POSITIONS:
        return REASON_FEW
    return None


@dataclass(frozen=True)
class Scored:
    score: float | None
    score_reason: str | None


def _rows(stats_rows: Mapping[str, Mapping] | Iterable[Mapping]) -> list[tuple[str, Mapping]]:
    if isinstance(stats_rows, Mapping):
        return [(str(a), r) for a, r in stats_rows.items()]
    return [(str(r["address"]), r) for r in stats_rows]


def _percentiles(values: list[float | None]) -> list[float]:
    """Mid-rank percentile of each value; None ranks below every number."""
    n = len(values)
    keyed = [(-float("inf") if v is None else float(v)) for v in values]
    order = sorted(range(n), key=keyed.__getitem__)
    out = [0.0] * n
    i = 0
    while i < n:  # O(n log n): one pass over runs of equal values
        j = i
        while j < n and keyed[order[j]] == keyed[order[i]]:
            j += 1
        p = (i + 0.5 * (j - i)) / n  # i values below, j - i ties
        for k in order[i:j]:
            out[k] = p
        i = j
    return out


def score_detailed(stats_rows: Mapping[str, Mapping] | Iterable[Mapping]) -> dict[str, Scored]:
    """Score every wallet. ``stats_rows``: ``{addr: stats}`` or dicts with ``address``.

    A row may carry ``is_vault`` (default False).
    """
    rows = _rows(stats_rows)
    out: dict[str, Scored] = {}
    eligible: list[tuple[str, Mapping]] = []
    for addr, st in rows:
        reason = unscored_reason(st, bool(st.get("is_vault", False)))
        if reason is None:
            eligible.append((addr, st))
        else:
            out[addr] = Scored(None, reason)
    if eligible:
        total = [0.0] * len(eligible)
        for metric, (w, higher) in WEIGHTS.items():
            vals = []
            for _, st in eligible:
                v = st.get(metric)
                vals.append(None if v is None else (float(v) if higher else -float(v)))
            for i, p in enumerate(_percentiles(vals)):
                total[i] += w * p
        for (addr, _), s in zip(eligible, total):
            out[addr] = Scored(round(100.0 * s, 6), None)
    return out


def score(stats_rows: Mapping[str, Mapping] | Iterable[Mapping]) -> dict[str, float | None]:
    """``{addr: score in 0..100 or None}``; see :func:`score_detailed` for reasons."""
    return {a: s.score for a, s in score_detailed(stats_rows).items()}
