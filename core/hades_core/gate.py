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
    # Concentration per group (group_id, else position_id, else market_id) instead of
    # per closed trade. Polymarket: "no single event > 50%".
    top_share_by_group: bool = False


DEFAULT_GATE = GateConfig()
# Owner decision Q1: replaces the default gate for the Polymarket head only.
POLYMARKET_GATE = GateConfig(56, 20, Decimal("0.25"), Decimal("0.50"), top_share_by_group=True)


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
    """Full consistency gate.

    - net_pnl passes only if BOTH the equity-based net (incl. fees and capped
      unrealized) and the realized net of closed trades are > 0.
    - top_share = largest positive closed-trade (or group) pnl / realized net; it is
      False ("n/a") when realized net <= 0, so an open winner cannot dilute it.
    """
    require_utc("follow_started_at", follow_started_at)
    require_utc("now", now)
    curve = _known(result, now)
    closed = [c for c in result.closed_trades if c.closed_at <= now]

    elapsed = now - follow_started_at
    net = curve[-1][1] - curve[0][1] if curve else ZERO
    realized = sum((c.pnl_usd for c in closed), ZERO)
    dd = _max_drawdown([eq for _, eq in curve])
    if cfg.top_share_by_group:
        groups: dict[str, Decimal] = {}
        for c in closed:
            g = c.group_id or c.position_id or c.market_id
            groups[g] = groups.get(g, ZERO) + c.pnl_usd
        top = max(groups.values(), default=ZERO)
    else:
        top = max((c.pnl_usd for c in closed), default=ZERO)

    checks = {
        "days": elapsed >= timedelta(days=cfg.min_days),
        "closed_trades": len(closed) >= cfg.min_closed,
        "net_pnl": net > ZERO and realized > ZERO,
        "max_dd": dd < cfg.max_dd,
    }
    values = {
        "days": str(elapsed.days),
        "closed_trades": str(len(closed)),
        "net_pnl": str(net),
        "realized_net_pnl": str(realized),
        "max_dd": str(dd),
    }
    if realized > ZERO:
        share = max(top, ZERO) / realized
        checks["top_share"] = share <= cfg.max_top_share
        values["top_share"] = str(share)
    else:
        # Not evaluable without realized profit; the gate already fails on net_pnl.
        checks["top_share"] = False
        values["top_share"] = "n/a"
    return GateResult(all(checks.values()), checks, values)


def evaluate_rolling(result: PaperResult, now: datetime, cfg: GateConfig,
                     days: int = 28) -> GateResult:
    """Net PnL > 0 and max DD < cfg.max_dd over the window [now - days, now].

    Both equity points and closed trades are in the window when start <= t <= now.
    The window is seeded with the last equity strictly before its start, so a loss
    that straddles the window start is not hidden.
    """
    require_utc("now", now)
    start = now - timedelta(days=days)
    curve = _known(result, now)
    before = [eq for t, eq in curve if t < start]
    inside = [eq for t, eq in curve if start <= t]
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
