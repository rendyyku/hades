from decimal import Decimal

from hades_hl.pnl import PnlSummary, wallet_pnl
from hlfills import ADDR, T0, funding, mk

D = Decimal


def test_pnl_is_closedpnl_minus_fees_plus_funding():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, fee="0.5"),
        mk("BTC", "A", "1", "110", "1", T0 + 1000, closed_pnl="10", fee="0.5"),
        mk("ETH", "A", "2", "50", "0", T0 + 2000, fee="0.2"),
        mk("ETH", "B", "2", "55", "-2", T0 + 3000, closed_pnl="-10", fee="0.2"),
    ]
    fund = [funding("BTC", "-0.3", T0 + 500), funding("ETH", "0.1", T0 + 2500)]
    p = wallet_pnl(fills, fund)
    assert isinstance(p, PnlSummary)
    assert p.realized_usd == D("0")
    assert p.fees_usd == D("1.4")
    assert p.funding_usd == D("-0.2")
    assert p.net_usd == p.realized_usd - p.fees_usd + p.funding_usd == D("-1.6")
    assert p.liquidations == 0
    assert len(p.closed_positions) == 2
    btc, eth = p.closed_positions
    assert (btc.coin, btc.direction, btc.position_id) == ("BTC", "long", "BTC:0")
    assert btc.realized_usd == D("10") and btc.fees_usd == D("1.0")
    assert btc.funding_usd == D("-0.3")
    assert btc.net_usd == D("8.7")
    assert btc.hold_s == 1.0
    assert eth.net_usd == D("-10") - D("0.4") + D("0.1")
    assert p.closedpnl_mismatch is False
    assert p.compared_fills == 2


def test_rebate_fee_negative_reduces_fees():
    fills = [mk("BTC", "B", "1", "100", "0", T0, fee="-0.02", crossed=False)]
    assert wallet_pnl(fills, []).fees_usd == D("-0.02")


def test_open_position_is_not_closed():
    fills = [mk("BTC", "B", "1", "100", "0", T0), mk("BTC", "A", "0.5", "100", "1", T0 + 1)]
    assert wallet_pnl(fills, []).closed_positions == []


def test_liquidation_counted_as_loss():
    liq = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}
    fills = [
        mk("BTC", "B", "1", "100", "0", T0),
        mk("BTC", "A", "1", "90", "1", T0 + 1, closed_pnl="-10", liquidation=liq),
    ]
    p = wallet_pnl(fills, [], addr=ADDR)
    assert p.liquidations == 1
    (cp,) = p.closed_positions
    assert cp.liquidated is True
    assert cp.is_win is False


def test_liquidation_of_other_user_not_counted():
    # we were the liquidator (backstop) side: not our liquidation
    liq = {"liquidatedUser": "0x" + "cd" * 20, "markPx": "90", "method": "backstop"}
    fills = [mk("BTC", "B", "1", "90", "0", T0, liquidation=liq)]
    assert wallet_pnl(fills, [], addr=ADDR).liquidations == 0


def test_liquidated_episode_counted_once():
    liq = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}
    fills = [
        mk("BTC", "B", "2", "100", "0", T0),
        mk("BTC", "A", "1", "90", "2", T0 + 1, closed_pnl="-10", liquidation=liq),
        mk("BTC", "A", "1", "90", "1", T0 + 1, closed_pnl="-10", liquidation=liq),
    ]
    p = wallet_pnl(fills, [], addr=ADDR)
    assert p.liquidations == 1
    assert len(p.closed_positions) == 1


def test_flip_closes_one_position_and_opens_another():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, fee="0"),
        mk("BTC", "A", "3", "110", "1", T0 + 2000, closed_pnl="10", fee="0.3"),
        mk("BTC", "B", "2", "100", "-2", T0 + 5000, closed_pnl="20", fee="0"),
    ]
    p = wallet_pnl(fills, [])
    assert [(c.position_id, c.direction) for c in p.closed_positions] == [
        ("BTC:0", "long"), ("BTC:1", "short")]
    assert p.closed_positions[0].fees_usd == D("0.1")
    assert p.closed_positions[1].fees_usd == D("0.2")
    assert p.closed_positions[1].hold_s == 3.0
    assert p.closedpnl_mismatch is False


def test_reconstruction_mismatch_is_flagged():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0),
        mk("BTC", "A", "1", "110", "1", T0 + 1, closed_pnl="25"),  # we compute 10
    ]
    p = wallet_pnl(fills, [])
    assert p.reconstructed_usd == D("10")
    assert p.closedpnl_mismatch is True
    assert p.mismatch_pct > 1


def test_position_opened_before_history_is_partial_and_not_compared():
    fills = [mk("BTC", "A", "1", "110", "1", T0, closed_pnl="10")]
    p = wallet_pnl(fills, [])
    (cp,) = p.closed_positions
    assert cp.partial is True and cp.open_time is None and cp.hold_s is None
    assert p.compared_fills == 0
    assert p.mismatch_pct is None and p.closedpnl_mismatch is False


def test_spot_excluded():
    fills = [mk("@1", "B", "1", "10", "0", T0, dir="Buy", fee="5", closed_pnl="3")]
    p = wallet_pnl(fills, [])
    assert p.fees_usd == 0 and p.realized_usd == 0


def test_real_fixture_crosscheck_within_1pct(fixture):
    fills = fixture("user_fills_by_time.json")
    fund = fixture("user_funding.json")
    meta = fixture("fixture_meta.json")
    p = wallet_pnl(fills, fund, addr=meta["address"])
    assert p.realized_usd == sum(D(f["closedPnl"]) for f in fills)
    assert p.fees_usd == sum(D(f["fee"]) for f in fills)
    assert p.funding_usd == sum(D(r["delta"]["usdc"]) for r in fund)
    assert p.net_usd == p.realized_usd - p.fees_usd + p.funding_usd
    assert p.compared_fills > 300
    assert p.mismatch_pct is not None and p.mismatch_pct < 1
    assert p.closedpnl_mismatch is False
    assert p.liquidations == 0
    assert len(p.closed_positions) > 0
