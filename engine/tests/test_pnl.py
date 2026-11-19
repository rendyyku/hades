from datetime import datetime, timedelta, timezone

import pytest

from hades.db import Swap
from hades.ingest.parse import ParsedSwap
from hades.pnl import Position, compute_positions, unrealized_pnl

MINT = "Mint1111111111111111111111111111111111111111"
WALLET = "Wallet11111111111111111111111111111111111111"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def mk(i, side, tokens, sol, *, mint=MINT, minutes=None, slot=None):
    return ParsedSwap(
        signature=f"sig{i:04d}",
        wallet=WALLET,
        mint=mint,
        slot=1000 + i if slot is None else slot,
        block_time=T0 + timedelta(minutes=i if minutes is None else minutes),
        side=side,
        token_amount=tokens,
        sol_amount=sol,
        dex="pumpswap",
    )


def test_simple_round_trip():
    pos = compute_positions([mk(0, "buy", 100, 1.0), mk(1, "sell", 100, 3.0)])[MINT]
    assert isinstance(pos, Position)
    assert pos.realized_pnl_sol == pytest.approx(2.0)
    assert pos.cost_sol == pytest.approx(1.0)
    assert pos.proceeds_sol == pytest.approx(3.0)
    assert pos.remaining_tokens == 0
    assert pos.closed is True
    assert pos.first_buy_time == T0
    assert pos.last_sell_time == T0 + timedelta(minutes=1)


def test_avg_cost_partial():
    pos = compute_positions(
        [mk(0, "buy", 100, 1.0), mk(1, "buy", 100, 3.0), mk(2, "sell", 100, 4.0)]
    )[MINT]
    assert pos.realized_pnl_sol == pytest.approx(2.0)
    assert pos.remaining_tokens == pytest.approx(100)
    assert pos.remaining_cost_sol == pytest.approx(2.0)
    assert pos.max_holding == pytest.approx(200)
    assert pos.bought_tokens == pytest.approx(200)
    assert pos.sold_tokens == pytest.approx(100)
    assert pos.closed is False


def test_oversell_not_profit():
    pos = compute_positions([mk(0, "buy", 100, 1.0), mk(1, "sell", 150, 3.0)])[MINT]
    assert pos.proceeds_sol == pytest.approx(2.0)
    assert pos.realized_pnl_sol == pytest.approx(1.0)
    assert pos.untracked_sell_tokens == pytest.approx(50)
    assert pos.sold_tokens == pytest.approx(150)
    assert pos.remaining_tokens == 0
    assert pos.closed is True


def test_ignores_null_sol():
    base = [mk(0, "buy", 100, 1.0)]
    ref = compute_positions(base)[MINT]
    pos = compute_positions(base + [mk(1, "sell", 50, None), mk(2, "buy", 10, None)])[MINT]
    assert pos == ref


def test_only_null_sol_swaps_yield_no_position():
    assert compute_positions([mk(0, "buy", 100, None)]) == {}


def test_unrealized():
    pos = compute_positions(
        [mk(0, "buy", 100, 1.0), mk(1, "buy", 100, 3.0), mk(2, "sell", 100, 4.0)]
    )[MINT]
    assert pos.remaining_tokens == pytest.approx(100)
    assert pos.remaining_cost_sol == pytest.approx(2.0)
    assert unrealized_pnl(pos, 0.05) == pytest.approx(3.0)
    assert unrealized_pnl(pos, None) == 0.0


def test_unrealized_zero_when_closed():
    pos = compute_positions([mk(0, "buy", 100, 1.0), mk(1, "sell", 100, 3.0)])[MINT]
    assert unrealized_pnl(pos, 10.0) == 0.0


def test_sell_with_no_prior_buy():
    pos = compute_positions([mk(0, "sell", 100, 5.0)])[MINT]
    assert pos.realized_pnl_sol == 0.0
    assert pos.proceeds_sol == 0.0
    assert pos.untracked_sell_tokens == pytest.approx(100)
    assert pos.sold_tokens == pytest.approx(100)
    assert pos.remaining_tokens == 0
    assert pos.max_holding == 0
    assert pos.closed is False
    assert pos.first_buy_time is None
    assert pos.last_sell_time == T0


def test_dust_clamp():
    # 0.1 + 0.2 - 0.3 style float residue must not leave a dust position
    swaps = [
        mk(0, "buy", 0.1, 0.5),
        mk(1, "buy", 0.2, 0.5),
        mk(2, "sell", 0.3, 2.0),
    ]
    pos = compute_positions(swaps)[MINT]
    assert pos.remaining_tokens == 0
    assert pos.remaining_cost_sol == 0
    assert pos.realized_pnl_sol == pytest.approx(1.0)
    assert pos.closed is True


def test_closed_threshold_one_percent():
    near = compute_positions([mk(0, "buy", 100, 1.0), mk(1, "sell", 99, 2.0)])[MINT]
    assert near.closed is True
    far = compute_positions([mk(0, "buy", 100, 1.0), mk(1, "sell", 98.9, 2.0)])[MINT]
    assert far.closed is False


def test_sorted_by_time_slot_signature():
    # given out of order: the sell happens after the buy chronologically
    swaps = [mk(1, "sell", 100, 3.0), mk(0, "buy", 100, 1.0)]
    pos = compute_positions(swaps)[MINT]
    assert pos.realized_pnl_sol == pytest.approx(2.0)
    assert pos.untracked_sell_tokens == 0
    # same block_time: slot breaks the tie
    swaps = [mk(1, "sell", 100, 3.0, minutes=0, slot=6), mk(0, "buy", 100, 1.0, minutes=0, slot=5)]
    pos = compute_positions(swaps)[MINT]
    assert pos.realized_pnl_sol == pytest.approx(2.0)
    # same block_time and slot: signature breaks the tie (sig0000 < sig0001)
    swaps = [mk(1, "sell", 100, 3.0, minutes=0, slot=5), mk(0, "buy", 100, 1.0, minutes=0, slot=5)]
    pos = compute_positions(swaps)[MINT]
    assert pos.realized_pnl_sol == pytest.approx(2.0)


def test_multiple_mints_and_time_bounds():
    other = "Mint2222222222222222222222222222222222222222"
    swaps = [
        mk(0, "buy", 100, 1.0),
        mk(1, "buy", 50, 2.0, mint=other),
        mk(2, "buy", 100, 1.0),
        mk(3, "sell", 50, 1.0),
        mk(4, "sell", 50, 1.5),
    ]
    out = compute_positions(swaps)
    assert set(out) == {MINT, other}
    a = out[MINT]
    assert a.mint == MINT
    assert a.first_buy_time == T0
    assert a.last_sell_time == T0 + timedelta(minutes=4)
    assert a.realized_pnl_sol == pytest.approx(2.5 - 1.0)
    b = out[other]
    assert b.last_sell_time is None
    assert b.closed is False
    assert b.remaining_cost_sol == pytest.approx(2.0)


def test_accepts_orm_swap_rows():
    rows = [
        Swap(signature="s1", wallet=WALLET, mint=MINT, slot=1, block_time=T0,
             side="buy", token_amount=100.0, sol_amount=1.0, source="backfill", dex="raydium"),
        Swap(signature="s2", wallet=WALLET, mint=MINT, slot=2, block_time=T0 + timedelta(seconds=5),
             side="sell", token_amount=100.0, sol_amount=3.0, source="backfill", dex="raydium"),
    ]
    pos = compute_positions(rows)[MINT]
    assert pos.realized_pnl_sol == pytest.approx(2.0)
    assert pos.closed is True
