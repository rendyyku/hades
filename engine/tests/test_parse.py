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


# -- real Helius payloads (trimmed) ----------------------------------------
# Ground truth per controller evidence: when the wallet's WSOL account is
# closed in the tx, the truth is nativeBalanceChange+fee; when WSOL is kept in
# the account, the truth is the WSOL token net; PUMP_FUN has no WSOL at all.

REAL_WALLET = "8DC3hpFtU3mEU3FUCtDfU72SmxTbrw4w35fyMYP8qGxg"


@pytest.mark.parametrize(
    ("fixture", "side", "mint_prefix", "token_amount", "sol_amount"),
    [
        ("real_pumpamm_sell_unwrap", "sell", "6ehEcT", 813554.823263, 14.56233218),
        ("real_pumpamm_buy_wrap", "buy", "HKpDdv", 957127.35136, 1.00176),
        ("real_pumpamm_sell_wsol_kept", "sell", "BCdwQB", 12716.385409, 5.62462471),
        ("real_pumpfun_buy", "buy", "3631Hg", 6866791.900463, 1.757118567),
    ],
)
def test_parse_real_payloads(fixture, side, mint_prefix, token_amount, sol_amount):
    swaps = parse_swaps(load(fixture))
    assert len(swaps) == 1
    s = swaps[0]
    assert s.wallet == REAL_WALLET
    assert s.side == side
    assert s.mint.startswith(mint_prefix)
    assert s.token_amount == pytest.approx(token_amount, abs=1e-6)
    assert s.sol_amount == pytest.approx(sol_amount, abs=1e-6)


def test_real_fixtures_contain_no_api_key():
    for path in FIXTURES.glob("*.json"):
        assert "api-key" not in path.read_text(encoding="utf-8").lower(), path.name


def test_parse_usdc_quote_with_token_account_rent_is_not_sol_quoted():
    # USDC-funded buy that also creates the MEME token account: the wallet pays
    # ~0.00204 SOL rent, so the SOL net is not ~0, but the swap is USDC-quoted.
    rent = 2_039_280
    tx = load("helius_swap_usdc_quote")
    fee = int(tx["fee"])
    for acc in tx["accountData"]:
        if acc["account"] == WALLET:
            acc["nativeBalanceChange"] = -fee - rent
    tx["accountData"].append({"account": "NewMemeAta111111111111111111111111111111111", "nativeBalanceChange": rent})
    tx["nativeTransfers"].append(
        {"fromUserAccount": WALLET, "toUserAccount": "NewMemeAta111111111111111111111111111111111", "amount": rent}
    )
    swaps = parse_swaps(tx)
    assert len(swaps) == 1
    assert swaps[0].side == "buy"
    assert swaps[0].sol_amount is None


def test_parse_multi_token_legs_have_no_sol_amount():
    # Real-data case: one tx buys two different tokens for 1.5 SOL. The SOL
    # cannot be attributed per leg, so neither leg may feed SOL PnL.
    tx = copy.deepcopy(load("helius_swap_simple"))
    tx["tokenTransfers"].append(
        {"fromUserAccount": "PooLOther", "toUserAccount": WALLET, "tokenAmount": 42.0, "mint": "OtherMint111"}
    )
    swaps = parse_swaps(tx)
    assert {s.mint for s in swaps} == {MEME, "OtherMint111"}
    assert all(s.sol_amount is None for s in swaps)
