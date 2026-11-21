"""Ladder transitions (PAPER -> NOTIF -> COPY) and the leader-vs-paper gap."""

from __future__ import annotations

from decimal import Decimal

from hades_core.contract import Ladder
from hades_core.gate import GateResult


def _rolling_ok(rolling: GateResult) -> bool:
    return bool(rolling.checks.get("net_pnl")) and bool(rolling.checks.get("max_dd"))


def next_ladder(current: Ladder, gate_full: GateResult, gate_rolling: GateResult, *,
                cooling_down: bool = False) -> Ladder:
    """Next ladder state. COPY is only ever entered by explicit owner action.

    No flapping (final review I1):

    - PAPER -> NOTIF needs the full gate AND a passing rolling gate: a wallet the
      demotion rule would send straight back is never promoted, so a demotion sticks
      until the rolling window itself recovers.
    - NOTIF / COPY -> PAPER when the rolling gate fails (net <= 0 or drawdown >= max).
    - ``cooling_down`` (hysteresis, decided by the head: a demotion happened recently)
      blocks a promotion only. It never delays a demotion and never demotes.
    """
    if current == "PAPER":
        if cooling_down:
            return "PAPER"
        return "NOTIF" if gate_full.passed and _rolling_ok(gate_rolling) else "PAPER"
    if current in ("NOTIF", "COPY"):
        # Demotion: rolling-window net <= 0 or rolling drawdown >= max_dd.
        return current if _rolling_ok(gate_rolling) else "PAPER"
    raise ValueError(f"unknown ladder state: {current!r}")


def gap(leader_pnl_usd: Decimal, paper_pnl_usd: Decimal) -> Decimal:
    """Leader PnL minus our paper PnL over the same window: the 'can we copy this' cost."""
    return leader_pnl_usd - paper_pnl_usd
