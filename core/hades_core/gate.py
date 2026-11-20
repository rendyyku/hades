"""Consistency gate (spec section 7, 12b) and its rolling-window variant (ladder demotion)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from hades_core.events import require_utc
from hades_core.paper import PaperResult

ZERO = Decimal("0")


@dataclass(frozen=True)
class GateConfig:
    min_days: int = 28
    min_closed: int = 30
    max_dd: Decimal = Decimal("0.20")
    max_top_share: Decimal = Decimal("0.50")


DEFAULT_GATE = GateConfig()
# Owner decision Q1: replaces the default gate for the Polymarket head only.
POLYMARKET_GATE = GateConfig(56, 20, Decimal("0.25"), Decimal("0.50"))


@dataclass
class GateResult:
    passed: bool
    checks: dict[str, bool]
    values: dict[str, str]


def _max_drawdown(curve: list[Decimal]) -> Decimal:
    """Largest peak-to-trough fall as a fraction of the running peak."""
    peak: Decimal | None = None
    worst = ZERO
    for eq in curve:
        if peak is None or eq > peak:
            peak = eq
        if peak > ZERO:
            worst = max(worst, (peak - eq) / peak)
    return worst


def _known(result: PaperResult, now: datetime) -> list[tuple[datetime, Decimal]]:
    return [(t, eq) for t, eq in result.equity_curve if t <= now]


def evaluate_gate(result: PaperResult, follow_started_at: datetime, now: datetime,
                  cfg: GateConfig) -> GateResult:
    require_utc("follow_started_at", follow_started_at)
    require_utc("now", now)
    curve = _known(result, now)
    closed = [c for c in result.closed_trades if c.closed_at <= now]

    elapsed = now - follow_started_at
    net = curve[-1][1] - curve[0][1] if curve else ZERO
    dd = _max_drawdown([eq for _, eq in curve])
    top = max((c.pnl_usd for c in closed), default=ZERO)

    checks = {
        "days": elapsed >= timedelta(days=cfg.min_days),
        "closed_trades": len(closed) >= cfg.min_closed,
        "net_pnl": net > ZERO,
        "max_dd": dd < cfg.max_dd,
    }
    values = {
        "days": str(elapsed.days),
        "closed_trades": str(len(closed)),
        "net_pnl": str(net),
        "max_dd": str(dd),
    }
    if net > ZERO:
        share = max(top, ZERO) / net
        checks["top_share"] = share <= cfg.max_top_share
        values["top_share"] = str(share)
    else:
        # Not evaluable without net profit; the gate already fails on net_pnl.
        checks["top_share"] = False
        values["top_share"] = "n/a"
    return GateResult(all(checks.values()), checks, values)


def evaluate_rolling(result: PaperResult, now: datetime, cfg: GateConfig,
                     days: int = 28) -> GateResult:
    """Net PnL > 0 and max DD < cfg.max_dd over the window [now - days, now].

    The window opens at the last equity known at its start (or the first point inside
    it), so a loss that straddles the window start is not hidden.
    """
    require_utc("now", now)
    start = now - timedelta(days=days)
    curve = _known(result, now)
    before = [eq for t, eq in curve if t <= start]
    inside = [eq for t, eq in curve if t > start]
    window = ([before[-1]] if before else []) + inside
    closed = [c for c in result.closed_trades if start <= c.closed_at <= now]

    net = window[-1] - window[0] if window else ZERO
    dd = _max_drawdown(window)
    checks = {"net_pnl": net > ZERO, "max_dd": dd < cfg.max_dd}
    values = {
        "window_days": str(days),
        "net_pnl": str(net),
        "max_dd": str(dd),
        "closed_trades": str(len(closed)),
    }
    return GateResult(all(checks.values()), checks, values)
