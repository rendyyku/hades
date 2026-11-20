from datetime import datetime, timedelta, timezone
from decimal import Decimal

from hades_core.events import TradeEvent
from hades_core.paper import (
    OpenPosition,
    PaperConfig,
    PaperResult,
    simulate,
)

T0 = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
FOLLOW = T0


class FakeModel:
    """Prices by source_ref; None (or absent) means missed. Fee is a flat rate."""

    def __init__(self, prices: dict[str, Decimal | None], fee_rate=Decimal("0"), max_leverage=Decimal("50")):
        self.prices = prices
        self.fee_rate = fee_rate
        self.max_leverage = max_leverage

    def fill_price(self, ev: TradeEvent) -> Decimal | None:
        return self.prices.get(ev.source_ref)

    def fee(self, notional: Decimal) -> Decimal:
        return notional * self.fee_rate


def ev(ref, side, minutes, *, size="1", price="100", direction="long", pid="p1",
       market="BTC", leverage=None, leader_minutes=None):
    lt = T0 + timedelta(minutes=minutes if leader_minutes is None else leader_minutes)
    return TradeEvent(
        head="hyperliquid",
        wallet="0xabc",
        market_id=market,
        side=side,
        direction=direction,
        size=Decimal(size),
        price=Decimal(price),
        notional_usd=Decimal(size) * Decimal(price),
        fee_usd=Decimal("0"),
        leader_time=lt,
        observed_time=T0 + timedelta(minutes=minutes, seconds=1),
        source_ref=ref,
        leverage=None if leverage is None else Decimal(leverage),
        position_id=pid,
    )


CFG = PaperConfig()


def final_equity(res: PaperResult) -> Decimal:
    return res.equity_curve[-1][1]


def test_paper_config_defaults():
    assert CFG.bankroll == Decimal("10000")
    assert CFG.risk_fraction == Decimal("0.05")
    assert CFG.max_leverage == Decimal("10")


def test_uses_model_price_not_leader_price():
    events = [
        ev("a", "open", 1, price="100"),
        ev("b", "close", 2, price="200"),
    ]
    model = FakeModel({"a": Decimal("125"), "b": Decimal("150")})
    res = simulate(events, model, CFG, FOLLOW)
    assert [f.price for f in res.fills] == [Decimal("125"), Decimal("150")]
    # 1x: margin 500 -> qty 4; pnl = 4 * 25 = 100 (leader prices would give 500)
    assert res.closed_trades[0].pnl_usd == Decimal("100")
    assert final_equity(res) == Decimal("10100")


def test_ignores_positions_opened_before_follow():
    events = [
        # leader opened p_old before we followed; its later events must be ignored
        ev("old-open", "open", -60, pid="p_old", leader_minutes=-60),
        ev("old-reduce", "reduce", 5, pid="p_old", size="0.5"),
        ev("old-close", "close", 10, pid="p_old"),
        ev("new-open", "open", 3, pid="p_new"),
        ev("new-close", "close", 12, pid="p_new"),
    ]
    model = FakeModel({r: Decimal("100") for r in
                       ["old-open", "old-reduce", "old-close", "new-open", "new-close"]})
    res = simulate(events, model, CFG, FOLLOW)
    refs = [f.ev_ref for f in res.fills]
    assert refs == ["new-open", "new-close"]
    assert [c.position_id for c in res.closed_trades] == ["p_new"]
    assert res.open_positions == {}


def test_leader_time_before_follow_ignored_even_if_observed_after():
    # observed after follow start but the leader actually opened before it
    events = [
        ev("o", "open", 2, pid="p", leader_minutes=-1),
        ev("c", "close", 5, pid="p"),
    ]
    res = simulate(events, FakeModel({"o": Decimal("100"), "c": Decimal("100")}), CFG, FOLLOW)
    assert res.fills == []
    assert res.closed_trades == []


def test_missed_open_ignores_rest_of_position():
    events = [
        ev("o", "open", 1),
        ev("i", "increase", 2, size="1"),
        ev("c", "close", 3),
    ]
    model = FakeModel({"o": None, "i": Decimal("100"), "c": Decimal("120")})
    res = simulate(events, model, CFG, FOLLOW)
    assert len(res.fills) == 1
    assert res.fills[0].missed is True
    assert res.fills[0].ev_ref == "o"
    assert res.open_positions == {}
    assert res.closed_trades == []
    assert final_equity(res) == CFG.bankroll


def test_missed_close_keeps_position_open():
    events = [
        ev("o", "open", 1),
        ev("c", "close", 2),
        ev("x", "open", 5, pid="other", market="ETH"),  # later activity drives marking
    ]
    model = FakeModel({"o": Decimal("100"), "c": None, "x": Decimal("10")})
    marks = {"BTC": Decimal("90"), "ETH": Decimal("10")}
    res = simulate(events, model, CFG, FOLLOW, mark=lambda m, t: marks[m])
    missed = [f for f in res.fills if f.missed]
    assert [f.ev_ref for f in missed] == ["c"]
    assert "p1" in res.open_positions
    pos = res.open_positions["p1"]
    assert isinstance(pos, OpenPosition)
    assert pos.qty == Decimal("5")  # 500 / 100
    assert res.closed_trades == []
    # never retro-filled: equity reflects the mark (90), not a fill
    assert final_equity(res) == Decimal("10000") - Decimal("50")


def test_missed_close_then_resolve_closes_position():
    events = [
        ev("o", "open", 1, direction="yes", market="tok", price="0.4"),
        ev("c", "close", 2, direction="yes", market="tok", price="0.6"),
        ev("r", "resolve", 3, direction="yes", market="tok", price="1"),
    ]
    model = FakeModel({"o": Decimal("0.5"), "c": None, "r": Decimal("1")}, max_leverage=Decimal("1"))
    res = simulate(events, model, CFG, FOLLOW)
    assert res.open_positions == {}
    # 500 / 0.5 = 1000 shares, paid out at 1 -> +500
    assert res.closed_trades[0].pnl_usd == Decimal("500")


def test_sizing_5pct_equity_with_leverage_cap():
    events = [ev("o", "open", 1, leverage="25", price="50")]
    res = simulate(events, FakeModel({"o": Decimal("50")}), CFG, FOLLOW)
    pos = res.open_positions["p1"]
    assert pos.leverage == Decimal("10")
    assert pos.margin == Decimal("500")
    assert pos.qty * pos.entry_price == Decimal("5000")
    assert pos.qty == Decimal("100")
    assert pos.leader_size == Decimal("1")


def test_model_max_leverage_also_caps():
    events = [ev("o", "open", 1, leverage="5")]
    res = simulate(events, FakeModel({"o": Decimal("100")}, max_leverage=Decimal("1")), CFG, FOLLOW)
    assert res.open_positions["p1"].leverage == Decimal("1")


def test_sizing_uses_current_equity():
    events = [
        ev("o1", "open", 1, pid="a"),
        ev("c1", "close", 2, pid="a"),
        ev("o2", "open", 3, pid="b"),
    ]
    model = FakeModel({"o1": Decimal("100"), "c1": Decimal("200"), "o2": Decimal("100")})
    res = simulate(events, model, CFG, FOLLOW)
    # first trade: margin 500, qty 5, +500 -> equity 10500 -> next margin 525
    assert res.open_positions["b"].margin == Decimal("525")


def test_increase_reduce_scale_proportionally():
    events = [
        ev("o", "open", 1, size="2"),
        ev("i", "increase", 2, size="1"),   # leader 2 -> 3 : +50%
        ev("r", "reduce", 3, size="1.5"),   # leader 3 -> 1.5 : -50%
    ]
    model = FakeModel({"o": Decimal("100"), "i": Decimal("100"), "r": Decimal("120")})
    res = simulate(events, model, CFG, FOLLOW)
    sizes = [f.size for f in res.fills]
    assert sizes == [Decimal("5"), Decimal("2.5"), Decimal("3.75")]
    pos = res.open_positions["p1"]
    assert pos.qty == Decimal("3.75")
    assert pos.leader_size == Decimal("1.5")
    assert pos.margin == Decimal("375")
    # reduce realized 3.75 * 20 = 75
    assert res.fills[2].realized_pnl == Decimal("75")


def test_short_profits_when_price_falls():
    events = [
        ev("o", "open", 1, direction="short", leverage="2"),
        ev("c", "close", 2, direction="short"),
    ]
    res = simulate(events, FakeModel({"o": Decimal("100"), "c": Decimal("90")}), CFG, FOLLOW)
    # margin 500, 2x -> qty 10; pnl 10 * 10 = 100
    assert res.closed_trades[0].pnl_usd == Decimal("100")


def test_no_side_profits_when_its_own_price_rises():
    events = [
        ev("o", "open", 1, direction="no", market="tokNO", price="0.3"),
        ev("c", "close", 2, direction="no", market="tokNO", price="0.6"),
    ]
    model = FakeModel({"o": Decimal("0.25"), "c": Decimal("0.5")}, max_leverage=Decimal("1"))
    res = simulate(events, model, CFG, FOLLOW)
    assert res.closed_trades[0].pnl_usd == Decimal("500")


def test_loss_capped_at_margin():
    events = [
        ev("o", "open", 1, leverage="10"),
        ev("c", "close", 2),
    ]
    model = FakeModel({"o": Decimal("100"), "c": Decimal("50")})
    res = simulate(events, model, CFG, FOLLOW)
    # qty 50; raw loss 2500, isolated margin 500 -> loss capped at 500
    assert res.closed_trades[0].pnl_usd == Decimal("-500")
    assert final_equity(res) == Decimal("9500")


def test_unrealized_loss_capped_and_liquidates_on_mark():
    events = [
        ev("o", "open", 1, leverage="10"),
        ev("x", "open", 5, pid="other", market="ETH"),
        ev("c", "close", 9),  # price recovered, but we were already liquidated
    ]
    model = FakeModel({"o": Decimal("100"), "x": Decimal("10"), "c": Decimal("150")})
    marks = {("BTC", 5): Decimal("80"), ("ETH", 5): Decimal("10")}

    def mark(m, t):
        return marks.get((m, t.minute))

    res = simulate(events, model, CFG, FOLLOW, mark=mark)
    assert [c.pnl_usd for c in res.closed_trades if c.position_id == "p1"] == [Decimal("-500")]
    assert "p1" not in res.open_positions
    assert not any(f.ev_ref == "c" and not f.missed for f in res.fills)


def test_fees_reduce_equity():
    events = [
        ev("o", "open", 1),
        ev("c", "close", 2),
    ]
    model = FakeModel({"o": Decimal("100"), "c": Decimal("100")}, fee_rate=Decimal("0.001"))
    res = simulate(events, model, CFG, FOLLOW)
    # notional 500 on each side -> 0.5 fee each
    assert [f.fee for f in res.fills] == [Decimal("0.5"), Decimal("0.5")]
    assert res.closed_trades[0].pnl_usd == Decimal("-1")
    assert final_equity(res) == Decimal("9999")


def test_equity_curve_starts_at_bankroll():
    res = simulate([], FakeModel({}), CFG, FOLLOW)
    assert res.equity_curve == [(FOLLOW, Decimal("10000"))]


def test_deterministic():
    events = [
        ev("o", "open", 1, size="2", leverage="3"),
        ev("i", "increase", 2, size="1"),
        ev("o2", "open", 2, pid="q", market="ETH", direction="short"),
        ev("r", "reduce", 3, size="1"),
        ev("c2", "close", 4, pid="q", market="ETH", direction="short"),
        ev("c", "close", 5),
    ]
    model = FakeModel({"o": Decimal("100"), "i": Decimal("101"), "o2": Decimal("10"),
                       "r": Decimal("103"), "c2": Decimal("9"), "c": Decimal("99")},
                      fee_rate=Decimal("0.00035"))
    a = simulate(events, model, CFG, FOLLOW)
    b = simulate(list(reversed(events)), model, CFG, FOLLOW)
    assert a == b


def test_sorted_by_observed_time_then_source_ref():
    # same observed_time: "a-open" sorts before "b-close"
    o = ev("a-open", "open", 1)
    c = ev("b-close", "close", 1)
    res = simulate([c, o], FakeModel({"a-open": Decimal("100"), "b-close": Decimal("110")}), CFG, FOLLOW)
    assert [f.ev_ref for f in res.fills] == ["a-open", "b-close"]
