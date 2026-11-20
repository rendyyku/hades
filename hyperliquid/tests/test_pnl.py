from decimal import Decimal

from hades_hl.pnl import PnlSummary, wallet_pnl
from hlfills import ADDR, T0, funding, mk

D = Decimal


def test_pnl_is_closedpnl_minus_fees_plus_funding():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, fee="0.5", tid=501),
        mk("BTC", "A", "1", "110", "1", T0 + 1000, closed_pnl="10", fee="0.5"),
        mk("ETH", "A", "2", "50", "0", T0 + 2000, fee="0.2"),
        mk("ETH", "B", "2", "55", "-2", T0 + 3000, closed_pnl="-10", fee="0.2"),
    ]
    fund = [funding("BTC", "-0.3", T0 + 500), funding("ETH", "0.1", T0 + 2500)]
    p = wallet_pnl(ADDR, fills, fund)
    assert isinstance(p, PnlSummary)
    assert p.realized_usd == D("0")
    assert p.fees_usd == D("1.4")
    assert p.funding_usd == D("-0.2")
    assert p.net_usd == p.realized_usd - p.fees_usd + p.funding_usd == D("-1.6")
    assert p.liquidations == 0
    assert len(p.closed_positions) == 2
    btc, eth = p.closed_positions
    assert (btc.coin, btc.direction, btc.position_id) == ("BTC", "long", "BTC:501")
    assert btc.realized_usd == D("10") and btc.fees_usd == D("1.0")
    assert btc.funding_usd == D("-0.3")
    assert btc.net_usd == D("8.7")
    assert btc.hold_s == 1.0
    assert eth.net_usd == D("-10") - D("0.4") + D("0.1")
    assert p.closedpnl_mismatch is False
    assert p.compared_fills == 2


def test_rebate_fee_negative_reduces_fees():
    fills = [mk("BTC", "B", "1", "100", "0", T0, fee="-0.02", crossed=False)]
    assert wallet_pnl(ADDR, fills, []).fees_usd == D("-0.02")


def test_open_position_is_not_closed():
    fills = [mk("BTC", "B", "1", "100", "0", T0), mk("BTC", "A", "0.5", "100", "1", T0 + 1)]
    assert wallet_pnl(ADDR, fills, []).closed_positions == []


def test_liquidation_counted_as_loss():
    liq = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}
    fills = [
        mk("BTC", "B", "1", "100", "0", T0),
        mk("BTC", "A", "1", "90", "1", T0 + 1, closed_pnl="-10", liquidation=liq),
    ]
    p = wallet_pnl(ADDR, fills, [])
    assert p.liquidations == 1
    (cp,) = p.closed_positions
    assert cp.liquidated is True
    assert cp.is_win is False


def test_liquidation_of_other_user_not_counted():
    # we were the liquidator (backstop) side: not our liquidation
    liq = {"liquidatedUser": "0x" + "cd" * 20, "markPx": "90", "method": "backstop"}
    fills = [mk("BTC", "B", "1", "90", "0", T0, liquidation=liq)]
    assert wallet_pnl(ADDR, fills, []).liquidations == 0


def test_liquidated_episode_counted_once():
    liq = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}
    fills = [
        mk("BTC", "B", "2", "100", "0", T0),
        mk("BTC", "A", "1", "90", "2", T0 + 1, closed_pnl="-10", liquidation=liq),
        mk("BTC", "A", "1", "90", "1", T0 + 1, closed_pnl="-10", liquidation=liq),
    ]
    p = wallet_pnl(ADDR, fills, [])
    assert p.liquidations == 1
    assert len(p.closed_positions) == 1


def test_flip_closes_one_position_and_opens_another():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, fee="0", tid=601),
        mk("BTC", "A", "3", "110", "1", T0 + 2000, closed_pnl="10", fee="0.3", tid=602),
        mk("BTC", "B", "2", "100", "-2", T0 + 5000, closed_pnl="20", fee="0"),
    ]
    p = wallet_pnl(ADDR, fills, [])
    assert [(c.position_id, c.direction) for c in p.closed_positions] == [
        ("BTC:601", "long"), ("BTC:602f", "short")]
    assert p.closed_positions[0].fees_usd == D("0.1")
    assert p.closed_positions[1].fees_usd == D("0.2")
    assert p.closed_positions[1].hold_s == 3.0
    assert p.closedpnl_mismatch is False


def test_reconstruction_mismatch_is_flagged():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0),
        mk("BTC", "A", "1", "110", "1", T0 + 1, closed_pnl="25"),  # we compute 10
    ]
    p = wallet_pnl(ADDR, fills, [])
    assert p.reconstructed_usd == D("10")
    assert p.closedpnl_mismatch is True
    assert p.mismatch_pct > 1


def test_position_opened_before_history_is_partial_and_not_compared():
    fills = [mk("BTC", "A", "1", "110", "1", T0, closed_pnl="10")]
    p = wallet_pnl(ADDR, fills, [])
    (cp,) = p.closed_positions
    assert cp.partial is True and cp.open_time is None and cp.hold_s is None
    assert p.compared_fills == 0
    assert p.mismatch_pct is None and p.closedpnl_mismatch is False


def test_spot_excluded():
    fills = [mk("@1", "B", "1", "10", "0", T0, dir="Buy", fee="5", closed_pnl="3")]
    p = wallet_pnl(ADDR, fills, [])
    assert p.fees_usd == 0 and p.realized_usd == 0


def test_real_fixture_crosscheck_within_1pct(fixture):
    fills = fixture("user_fills_by_time.json")
    fund = fixture("user_funding.json")
    meta = fixture("fixture_meta.json")
    p = wallet_pnl(meta["address"], fills, fund)
    assert p.realized_usd == sum(D(f["closedPnl"]) for f in fills)
    assert p.fees_usd == sum(D(f["fee"]) for f in fills)
    assert p.funding_usd == sum(D(r["delta"]["usdc"]) for r in fund)
    assert p.net_usd == p.realized_usd - p.fees_usd + p.funding_usd
    assert p.compared_fills > 300
    # without szDecimals our exact average entry differs from HL's truncated one
    # per fill (mostly PUMP at ~$0.0025): 1.09% per fill, 0.43% netted
    assert 1 < p.mismatch_pct < D("1.2")
    assert p.aggregate_mismatch_pct < D("0.5")
    assert p.chain_gaps == 0
    assert p.liquidations == 0
    assert len(p.closed_positions) > 0


def test_real_fixture_crosscheck_exact_with_sz_decimals(fixture):
    from hades_hl.normalize import sz_decimals_by_coin

    fills = fixture("user_fills_by_time.json")
    meta = fixture("fixture_meta.json")
    szd = sz_decimals_by_coin(fixture("meta.json"))
    assert szd["BTC"] == 5
    p = wallet_pnl(meta["address"], fills, fixture("user_funding.json"), sz_decimals=szd)
    assert p.compared_fills > 300
    assert p.mismatch_pct == 0 and p.aggregate_mismatch_pct == 0
    assert p.closedpnl_mismatch is False


def test_entry_truncated_to_price_decimals():
    # HL computes closedPnl from the entry truncated to 6 - szDecimals decimals
    fills = [mk("PUMP", "B", "100000", "0.002538", "0", T0),
             mk("PUMP", "B", "100000", "0.002541", "100000", T0 + 1),  # avg 0.0025395
             mk("PUMP", "A", "200000", "0.002545", "200000", T0 + 2, closed_pnl="1.2")]
    exact = wallet_pnl(ADDR, fills, [])
    assert exact.reconstructed_usd == D("1.1")
    trunc = wallet_pnl(ADDR, fills, [], sz_decimals={"PUMP": 0})  # entry -> 0.002539
    assert trunc.reconstructed_usd == D("1.2")
    assert trunc.mismatch_pct == 0


# -- fix round 1 -----------------------------------------------------------------

def test_addr_is_required_positional():
    import inspect

    p = inspect.signature(wallet_pnl).parameters["addr"]
    assert p.default is inspect.Parameter.empty
    assert p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert list(inspect.signature(wallet_pnl).parameters)[:3] == ["addr", "fills", "funding"]


def test_liquidator_side_fill_not_counted():
    other = {"liquidatedUser": "0x" + "cd" * 20, "markPx": "90", "method": "backstop"}
    fills = [mk("BTC", "B", "2", "90", "0", T0),
             mk("BTC", "A", "2", "90", "2", T0 + 1, liquidation=other)]
    p = wallet_pnl(ADDR, fills, [])
    assert p.liquidations == 0
    assert [c.liquidated for c in p.closed_positions] == [False]


def test_flip_liquidation_counts_once():
    liq = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}
    fills = [mk("BTC", "B", "1", "100", "0", T0),
             mk("BTC", "A", "3", "90", "1", T0 + 1, closed_pnl="-10", liquidation=liq),
             mk("BTC", "B", "2", "80", "-2", T0 + 2, closed_pnl="20")]
    p = wallet_pnl(ADDR, fills, [])
    assert p.liquidations == 1
    assert [c.liquidated for c in p.closed_positions] == [True, False]


def test_partial_liquidation_episode_is_liquidated_and_continues():
    liq = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}
    fills = [mk("BTC", "B", "3", "100", "0", T0),
             mk("BTC", "A", "1", "90", "3", T0 + 1, closed_pnl="-10", liquidation=liq),
             mk("BTC", "A", "2", "95", "2", T0 + 2, closed_pnl="-10")]
    p = wallet_pnl(ADDR, fills, [])
    (cp,) = p.closed_positions
    assert cp.liquidated is True and p.liquidations == 1
    assert cp.realized_usd == D("-20")


def test_builder_fee_in_fees():
    f = mk("BTC", "B", "1", "100", "0", T0, fee="0.1")
    f["builderFee"] = "0.05"
    g = mk("BTC", "A", "1", "100", "1", T0 + 1, fee="0.1")
    p = wallet_pnl(ADDR, [f, g], [])
    assert p.fees_usd == D("0.25")
    assert p.closed_positions[0].fees_usd == D("0.25")
    assert p.net_usd == D("-0.25")


def test_mismatch_is_per_fill_not_netted():
    # per-fill errors of +10 and -10 cancel in aggregate but not per fill
    fills = [mk("BTC", "B", "2", "100", "0", T0),
             mk("BTC", "A", "1", "110", "2", T0 + 1, closed_pnl="20"),  # ours 10
             mk("BTC", "A", "1", "110", "1", T0 + 2, closed_pnl="0")]   # ours 10
    p = wallet_pnl(ADDR, fills, [])
    assert p.aggregate_mismatch_pct == 0
    assert p.mismatch_pct == D(100)  # (10 + 10) / 20
    assert p.closedpnl_mismatch is True


def test_gap_makes_entry_unknown_and_is_counted():
    fills = [mk("BTC", "B", "1", "100", "0", T0),
             # a fill taking the position 1 -> 3 is missing
             mk("BTC", "A", "1", "110", "3", T0 + 5, closed_pnl="99")]
    p = wallet_pnl(ADDR, fills, [])
    assert p.chain_gaps == 1
    assert p.compared_fills == 0
