import copy
import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hades.ingest.parse import WSOL, ParsedSwap, parse_swaps

FIXTURES = Path(__file__).parent / "fixtures"
WALLET = "WaLLetBuyer1111111111111111111111111111111"
MEME = "MeMeMint1111111111111111111111111111111pump"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def test_wsol_constant():
    assert WSOL == "So11111111111111111111111111111111111111112"


def test_parse_simple_buy():
    swaps = parse_swaps(load("helius_swap_simple"))
    assert len(swaps) == 1
    s = swaps[0]
    assert s.side == "buy"
    assert s.wallet == WALLET
    assert s.mint == MEME
    assert s.signature == "sigSimpleBuy1"
    assert s.slot == 300000001
    assert s.token_amount == pytest.approx(1_000_000)
    # fee (5000 lamports) must be excluded from the SOL amount
    assert s.sol_amount == pytest.approx(1.5, abs=1e-12)
    assert s.dex == "RAYDIUM"
    assert s.block_time == datetime.fromtimestamp(1763500000, timezone.utc)
    assert s.block_time.tzinfo is not None


def test_parsed_swap_is_frozen():
    s = parse_swaps(load("helius_swap_simple"))[0]
    assert isinstance(s, ParsedSwap)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.side = "sell"  # type: ignore[misc]


def test_parse_multihop_swap():
    swaps = parse_swaps(load("helius_swap_multihop"))
    assert len(swaps) == 1
    s = swaps[0]
    assert s.mint == MEME
    assert s.side == "buy"
    assert s.token_amount == pytest.approx(2_500_000)
    assert s.sol_amount == pytest.approx(2.0, abs=1e-12)
    assert s.dex == "JUPITER"


def test_parse_usdc_quote():
    swaps = parse_swaps(load("helius_swap_usdc_quote"))
    assert len(swaps) == 1
    s = swaps[0]
    assert s.mint == MEME
    assert s.side == "buy"
    assert s.token_amount == pytest.approx(50_000)
    assert s.sol_amount is None


def test_parse_simple_sell():
    tx = copy.deepcopy(load("helius_swap_simple"))
    # reverse the flows: wallet sends MEME, receives 1.5 SOL (pays the fee)
    tt = tx["tokenTransfers"][0]
    tt["fromUserAccount"], tt["toUserAccount"] = tt["toUserAccount"], tt["fromUserAccount"]
    nt = tx["nativeTransfers"][0]
    nt["fromUserAccount"], nt["toUserAccount"] = nt["toUserAccount"], nt["fromUserAccount"]
    tx["accountData"][0]["nativeBalanceChange"] = 1_500_000_000 - 5000
    tx["accountData"][1]["nativeBalanceChange"] = -1_500_000_000
    swaps = parse_swaps(tx)
    assert len(swaps) == 1
    assert swaps[0].side == "sell"
    assert swaps[0].token_amount == pytest.approx(1_000_000)
    assert swaps[0].sol_amount == pytest.approx(1.5, abs=1e-12)


def test_failed_tx_yields_nothing():
    tx = load("helius_swap_simple")
    tx["transactionError"] = {"InstructionError": [2, {"Custom": 6001}]}
    assert parse_swaps(tx) == []
