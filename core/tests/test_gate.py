from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hades_core.gate import (
    POLYMARKET_GATE,
    GateConfig,
    GateResult,
    evaluate_gate,
    evaluate_rolling,
)
from hades_core.paper import ClosedTrade, PaperResult

FOLLOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
CFG = GateConfig()


def trades(n, pnl="10", start=FOLLOW):
    return [
        ClosedTrade(f"p{i}", "BTC", Decimal(pnl), start + timedelta(hours=i), start + timedelta(hours=i + 1))
        for i in range(n)
    ]


def result(curve, closed):
    return PaperResult(fills=[], equity_curve=curve, open_positions={}, closed_trades=closed)


def healthy(n_trades=30):
    # 30 trades of +10 -> net +300, no drawdown
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10300"))]
    return result(curve, trades(n_trades))


def test_defaults():
    assert CFG.min_days == 28
    assert CFG.min_closed == 30
    assert CFG.max_dd == Decimal("0.20")
    assert CFG.max_top_share == Decimal("0.50")


def test_polymarket_gate_values():
    assert POLYMARKET_GATE == GateConfig(56, 20, Decimal("0.25"), Decimal("0.50"),
                                         top_share_by_group=True)
    assert CFG.top_share_by_group is False


def test_healthy_passes():
    g = evaluate_gate(healthy(), FOLLOW, FOLLOW + timedelta(days=28), CFG)
    assert isinstance(g, GateResult)
    assert g.passed, g.checks
    assert all(g.checks.values())


def test_days_boundary_27_fails_28_passes():
    r = healthy()
    g27 = evaluate_gate(r, FOLLOW, FOLLOW + timedelta(days=27, hours=23, minutes=59), CFG)
    g28 = evaluate_gate(r, FOLLOW, FOLLOW + timedelta(days=28), CFG)
    assert g27.checks["days"] is False and not g27.passed
    assert g28.checks["days"] is True and g28.passed


def test_closed_boundary_29_fails_30_passes():
    now = FOLLOW + timedelta(days=30)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10300"))]
    g29 = evaluate_gate(result(curve, trades(29)), FOLLOW, now, CFG)
    g30 = evaluate_gate(result(curve, trades(30)), FOLLOW, now, CFG)
    assert g29.checks["closed_trades"] is False and not g29.passed
    assert g30.checks["closed_trades"] is True and g30.passed


def test_net_must_be_positive():
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10000"))]
    g = evaluate_gate(result(curve, trades(30, "0")), FOLLOW, FOLLOW + timedelta(days=30), CFG)
    assert g.checks["net_pnl"] is False
    assert not g.passed


def test_dd_exactly_20pct_fails():
    now = FOLLOW + timedelta(days=30)
    d = FOLLOW + timedelta(days=1)
    curve = [(FOLLOW, Decimal("10000")), (d, Decimal("8000")), (d + timedelta(days=1), Decimal("10300"))]
    g = evaluate_gate(result(curve, trades(30)), FOLLOW, now, CFG)
    assert g.checks["max_dd"] is False
    assert g.values["max_dd"] == "0.2"
    assert not g.passed
    curve[1] = (d, Decimal("8001"))
    g2 = evaluate_gate(result(curve, trades(30)), FOLLOW, now, CFG)
    assert g2.checks["max_dd"] is True and g2.passed


def test_dd_measured_from_running_peak():
    now = FOLLOW + timedelta(days=30)
    d = FOLLOW + timedelta(days=1)
    curve = [(FOLLOW, Decimal("10000")), (d, Decimal("20000")),
             (d + timedelta(days=1), Decimal("15000")), (d + timedelta(days=2), Decimal("21000"))]
    g = evaluate_gate(result(curve, trades(30, "366.67")), FOLLOW, now, CFG)
    assert g.values["max_dd"] == "0.25"
    assert g.checks["max_dd"] is False


def test_top_share_exactly_50pct_passes():
    now = FOLLOW + timedelta(days=30)
    # realized net 200; biggest trade 100 -> share exactly 0.5
    closed = (trades(28, "0") + [ClosedTrade("other", "BTC", Decimal("100"), FOLLOW, FOLLOW + timedelta(hours=1))]
              + [ClosedTrade("big", "BTC", Decimal("100"), FOLLOW, FOLLOW + timedelta(hours=1))])
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10200"))]
    g = evaluate_gate(result(curve, closed), FOLLOW, now, CFG)
    assert g.checks["top_share"] is True and g.passed
    closed[-1] = ClosedTrade("big", "BTC", Decimal("100.01"), FOLLOW, FOLLOW + timedelta(hours=1))
    g2 = evaluate_gate(result(curve, closed), FOLLOW, now, CFG)
    assert g2.checks["top_share"] is False and not g2.passed


def test_net_counts_equity_after_now_excluded():
    # points after `now` are not known yet
    now = FOLLOW + timedelta(days=28)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10300")),
             (now + timedelta(days=1), Decimal("9000"))]
    g = evaluate_gate(result(curve, trades(30)), FOLLOW, now, CFG)
    assert g.passed
    assert g.values["net_pnl"] == "300"


def test_rolling_uses_only_last_28_days():
    now = FOLLOW + timedelta(days=100)
    curve = [
        (FOLLOW, Decimal("10000")),
        (FOLLOW + timedelta(days=10), Decimal("5000")),   # old 50% drawdown, outside window
        (FOLLOW + timedelta(days=50), Decimal("12000")),
        (now - timedelta(days=10), Decimal("12500")),
    ]
    g = evaluate_rolling(result(curve, trades(5)), now, CFG)
    assert g.passed
    assert set(g.checks) == {"net_pnl", "max_dd"}
    assert g.values["net_pnl"] == "500"


def test_rolling_fails_on_net_le_zero():
    now = FOLLOW + timedelta(days=100)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=50), Decimal("12000")),
             (now - timedelta(days=5), Decimal("12000"))]
    g = evaluate_rolling(result(curve, []), now, CFG)
    assert g.checks["net_pnl"] is False and not g.passed


def test_rolling_fails_on_dd_at_limit():
    now = FOLLOW + timedelta(days=100)
    curve = [(FOLLOW, Decimal("10000")), (now - timedelta(days=20), Decimal("8000")),
             (now - timedelta(days=1), Decimal("10500"))]
    g = evaluate_rolling(result(curve, []), now, CFG)
    assert g.checks["net_pnl"] is True
    assert g.checks["max_dd"] is False and not g.passed


# --- fix round 1 -----------------------------------------------------------------


def test_unrealized_winner_cannot_hide_concentrated_realized_profit():
    # reviewer probe: 29 trades at -1, one at +40 (realized +11) plus +489 unrealized
    now = FOLLOW + timedelta(days=30)
    closed = trades(29, "-1") + [ClosedTrade("big", "BTC", Decimal("40"), FOLLOW, FOLLOW + timedelta(hours=1))]
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10500"))]
    g = evaluate_gate(result(curve, closed), FOLLOW, now, CFG)
    assert g.values["net_pnl"] == "500"
    assert g.values["realized_net_pnl"] == "11"
    assert g.checks["net_pnl"] is True
    assert g.checks["top_share"] is False and not g.passed


def test_net_check_requires_realized_profit():
    # equity up thanks to an open winner, but closed trades lost money
    now = FOLLOW + timedelta(days=30)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10500"))]
    g = evaluate_gate(result(curve, trades(30, "-1")), FOLLOW, now, CFG)
    assert g.values["realized_net_pnl"] == "-30"
    assert g.checks["net_pnl"] is False
    assert g.checks["top_share"] is False and g.values["top_share"] == "n/a"
    assert not g.passed


def test_net_check_requires_equity_profit():
    # realized profit, but an open loser drags equity below the start
    now = FOLLOW + timedelta(days=30)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("9900"))]
    g = evaluate_gate(result(curve, trades(30)), FOLLOW, now, CFG)
    assert g.checks["net_pnl"] is False and not g.passed


def _grouped_closed():
    # realized net 100: two +30 trades in group "ev1" (60%), each alone 30%
    out = trades(28, "0")
    out += [ClosedTrade("a", "tokA", Decimal("30"), FOLLOW, FOLLOW + timedelta(hours=1), group_id="ev1"),
            ClosedTrade("b", "tokB", Decimal("30"), FOLLOW, FOLLOW + timedelta(hours=2), group_id="ev1"),
            ClosedTrade("c", "tokC", Decimal("40"), FOLLOW, FOLLOW + timedelta(hours=3), group_id="ev2")]
    return out


def test_top_share_by_group_fails_when_group_exceeds_limit():
    now = FOLLOW + timedelta(days=60)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10100"))]
    cfg = GateConfig(min_days=28, min_closed=30, top_share_by_group=True)
    g = evaluate_gate(result(curve, _grouped_closed()), FOLLOW, now, cfg)
    assert g.values["top_share"] == "0.6"
    assert g.checks["top_share"] is False and not g.passed


def test_top_share_per_trade_passes_same_trades():
    now = FOLLOW + timedelta(days=60)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10100"))]
    g = evaluate_gate(result(curve, _grouped_closed()), FOLLOW, now, CFG)
    assert g.values["top_share"] == "0.4"
    assert g.checks["top_share"] is True and g.passed


def test_top_share_group_falls_back_to_position_then_market():
    now = FOLLOW + timedelta(days=60)
    curve = [(FOLLOW, Decimal("10000")), (FOLLOW + timedelta(days=1), Decimal("10100"))]
    cfg = GateConfig(min_days=28, min_closed=3, top_share_by_group=True)
    closed = [ClosedTrade("p", "m1", Decimal("30"), FOLLOW, FOLLOW + timedelta(hours=1)),
              ClosedTrade("p", "m2", Decimal("30"), FOLLOW, FOLLOW + timedelta(hours=2)),  # same pid
              ClosedTrade("q", "m1", Decimal("40"), FOLLOW, FOLLOW + timedelta(hours=3))]
    g = evaluate_gate(result(curve, closed), FOLLOW, now, cfg)
    assert g.values["top_share"] == "0.6"


def test_rolling_point_exactly_at_window_start_is_inside():
    now = FOLLOW + timedelta(days=100)
    start = now - timedelta(days=28)
    curve = [(FOLLOW, Decimal("10000")), (start, Decimal("8000")),
             (now - timedelta(days=1), Decimal("10500"))]
    closed = [ClosedTrade("s", "BTC", Decimal("1"), start - timedelta(hours=1), start)]
    g = evaluate_rolling(result(curve, closed), now, CFG)
    # the 10000 -> 8000 drop lands at t == start, so it belongs to the window
    assert g.values["max_dd"] == "0.2"
    assert g.checks["max_dd"] is False
    assert g.values["closed_trades"] == "1"
