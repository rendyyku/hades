"""Average-cost PnL per (wallet, mint), in SOL.

Every number here is derived only from SOL/WSOL-quoted swaps
(``sol_amount is not None``); other swaps are ignored. Sells exceeding the
tracked holdings (tokens obtained via transfer/airdrop) are never counted as
profit: only the tracked share of the sell and of its proceeds is realized,
the excess goes to ``untracked_sell_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

DUST_TOKENS = 1e-9
CLOSED_FRACTION = 0.01


@dataclass
class Position:
    mint: str
    bought_tokens: float = 0.0
    sold_tokens: float = 0.0
    cost_sol: float = 0.0
    proceeds_sol: float = 0.0
    realized_pnl_sol: float = 0.0
    remaining_tokens: float = 0.0
    remaining_cost_sol: float = 0.0
    max_holding: float = 0.0
    untracked_sell_tokens: float = 0.0
    first_buy_time: datetime | None = None
    last_sell_time: datetime | None = None
    closed: bool = False


def _apply_buy(pos: Position, tokens: float, sol: float, when: datetime) -> None:
    pos.cost_sol += sol
    pos.bought_tokens += tokens
    pos.remaining_tokens += tokens
    pos.remaining_cost_sol += sol
    if pos.remaining_tokens > pos.max_holding:
        pos.max_holding = pos.remaining_tokens
    if pos.first_buy_time is None or when < pos.first_buy_time:
        pos.first_buy_time = when


def _apply_sell(pos: Position, tokens: float, sol: float, when: datetime) -> None:
    tracked = min(tokens, pos.remaining_tokens)
    if tracked < 0:
        tracked = 0.0
    attributed = sol * tracked / tokens if tokens > 0 else 0.0
    avg_cost = pos.remaining_cost_sol / pos.remaining_tokens if pos.remaining_tokens > 0 else 0.0
    cost_out = avg_cost * tracked

    pos.realized_pnl_sol += attributed - cost_out
    pos.remaining_cost_sol -= cost_out
    pos.remaining_tokens -= tracked
    pos.untracked_sell_tokens += tokens - tracked
    pos.sold_tokens += tokens
    pos.proceeds_sol += attributed
    if pos.last_sell_time is None or when > pos.last_sell_time:
        pos.last_sell_time = when

    if pos.remaining_tokens < DUST_TOKENS:
        pos.remaining_tokens = 0.0
        pos.remaining_cost_sol = 0.0


def compute_positions(swaps: Iterable[Any]) -> dict[str, Position]:
    """Average-cost positions keyed by mint, for the swaps of a single wallet.

    Accepts any objects with the ``Swap`` attributes (ORM ``Swap`` or
    ``ParsedSwap``). Swaps with ``sol_amount is None`` are ignored. Processing
    order is ``(block_time, slot, signature)``.
    """
    priced = [s for s in swaps if s.sol_amount is not None]
    priced.sort(key=lambda s: (s.block_time, s.slot, s.signature))

    positions: dict[str, Position] = {}
    for s in priced:
        pos = positions.get(s.mint)
        if pos is None:
            pos = positions[s.mint] = Position(mint=s.mint)
        if s.side == "buy":
            _apply_buy(pos, float(s.token_amount), float(s.sol_amount), s.block_time)
        elif s.side == "sell":
            _apply_sell(pos, float(s.token_amount), float(s.sol_amount), s.block_time)
        else:
            raise ValueError(f"unknown swap side {s.side!r} in {s.signature}")

    for pos in positions.values():
        pos.closed = pos.max_holding > 0 and pos.remaining_tokens <= CLOSED_FRACTION * pos.max_holding
    return positions


def unrealized_pnl(pos: Position, price_native: float | None) -> float:
    """Mark-to-market PnL of the remaining holdings at ``price_native`` SOL/token."""
    if price_native is None or pos.remaining_tokens == 0:
        return 0.0
    return pos.remaining_tokens * price_native - pos.remaining_cost_sol
