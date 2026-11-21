from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

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
       market="BTC", leverage=None, leader_minutes=None, group=None, **extra):
    lt = T0 + timedelta(minutes=minutes if leader_minutes is None else leader_minutes)
    if group is not None:
        extra["group_id"] = group
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
        **extra,
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
    # loss capped at the 500 margin: equity right after the liquidation step and at the end
    at_x = T0 + timedelta(minutes=5, seconds=1)
    assert dict(res.equity_curve)[at_x] == Decimal("9500")
    assert final_equity(res) == Decimal("9500")


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


# --- fix round 1 -----------------------------------------------------------------


def test_fallback_key_pre_follow_episode_does_not_block_later_positions():
    # no position_id: key falls back to wallet:market:direction
    events = [
        ev("old-open", "open", -60, pid=None),
        ev("old-close", "close", -30, pid=None),
        ev("new-open", "open", 5, pid=None),
        ev("new-close", "close", 10, pid=None),
    ]
    model = FakeModel({"old-open": Decimal("100"), "old-close": Decimal("100"),
                       "new-open": Decimal("100"), "new-close": Decimal("110")})
    res = simulate(events, model, CFG, FOLLOW)
    assert [f.ev_ref for f in res.fills] == ["new-open", "new-close"]
    assert [c.pnl_usd for c in res.closed_trades] == [Decimal("50")]  # qty 5 * +10


def test_fallback_key_pre_follow_reduce_to_zero_ends_episode():
    events = [
        ev("old-open", "open", -60, pid=None, size="2"),
        ev("old-reduce", "reduce", 2, pid=None, size="2"),  # leader size -> 0: episode over
        ev("new-open", "open", 5, pid=None),
    ]
    model = FakeModel({r: Decimal("100") for r in ["old-open", "old-reduce", "new-open"]})
    res = simulate(events, model, CFG, FOLLOW)
    assert [f.ev_ref for f in res.fills] == ["new-open"]
    assert "0xabc:BTC:long" in res.open_positions


def test_fallback_key_partial_reduce_keeps_ignoring():
    events = [
        ev("old-open", "open", -60, pid=None, size="2"),
        ev("old-reduce", "reduce", 2, pid=None, size="1"),  # leader still holds 1
        ev("old-incr", "increase", 5, pid=None, size="1"),
    ]
    model = FakeModel({r: Decimal("100") for r in ["old-open", "old-reduce", "old-incr"]})
    res = simulate(events, model, CFG, FOLLOW)
    assert res.fills == []
    assert res.open_positions == {}


def test_pre_follow_position_later_open_and_increase_labels_ignored():
    events = [
        ev("old-open", "open", -60, pid="p_old", leader_minutes=-60),
        ev("old-incr", "increase", 2, pid="p_old"),
        ev("old-open2", "open", 3, pid="p_old"),  # head re-labels an add as "open"
        ev("old-close", "close", 10, pid="p_old"),
    ]
    model = FakeModel({r: Decimal("100") for r in ["old-open", "old-incr", "old-open2", "old-close"]})
    res = simulate(events, model, CFG, FOLLOW)
    assert res.fills == []
    assert res.open_positions == {}
    assert res.closed_trades == []


def test_event_for_never_held_position_is_noop():
    events = [
        ev("o", "open", 1, leverage="10"),
        # stray events for a position we never held, same market
        ev("ghost-close", "close", 2, pid="ghost"),       # priced at 50: would liquidate p1
        ev("ghost-reduce", "reduce", 3, pid="ghost"),     # no price: would be a missed fill
    ]
    model = FakeModel({"o": Decimal("100"), "ghost-close": Decimal("50"), "ghost-reduce": None})
    res = simulate(events, model, CFG, FOLLOW)
    assert [f.ev_ref for f in res.fills] == ["o"]
    assert "p1" in res.open_positions
    assert res.closed_trades == []
    assert len(res.equity_curve) == 2  # start + the open only
    assert final_equity(res) == Decimal("10000")


def test_duplicate_source_ref_first_occurrence_wins():
    first = ev("o", "open", 1, pid="p1")
    dup = ev("o", "open", 1, pid="p2", market="ETH")
    res = simulate([first, dup], FakeModel({"o": Decimal("100")}), CFG, FOLLOW)
    assert list(res.open_positions) == ["p1"]
    assert [f.ev_ref for f in res.fills] == ["o"]
    res2 = simulate([dup, first], FakeModel({"o": Decimal("100")}), CFG, FOLLOW)
    assert list(res2.open_positions) == ["p2"]


def test_short_liquidated_by_price_rise():
    events = [
        ev("o", "open", 1, direction="short", leverage="10"),
        ev("x", "open", 5, pid="other", market="ETH"),
    ]
    model = FakeModel({"o": Decimal("100"), "x": Decimal("10")})
    marks = {"BTC": Decimal("110"), "ETH": Decimal("10")}  # +10% vs a 10x short = whole margin
    res = simulate(events, model, CFG, FOLLOW, mark=lambda m, t: marks[m])
    assert [(c.position_id, c.pnl_usd) for c in res.closed_trades] == [("p1", Decimal("-500"))]
    assert "p1" not in res.open_positions
    assert res.fills[1].ev_ref == "liquidation:x"


def test_sizing_uses_marked_equity():
    events = [
        ev("o1", "open", 1, pid="a"),                 # 1x, qty 5 at 100
        ev("o2", "open", 3, pid="b", market="ETH"),
    ]
    model = FakeModel({"o1": Decimal("100"), "o2": Decimal("10")})
    marks = {"BTC": Decimal("200"), "ETH": Decimal("10")}
    res = simulate(events, model, CFG, FOLLOW, mark=lambda m, t: marks[m])
    # BTC marked at 200 -> unrealized +500 -> equity 10500 -> margin 525
    assert res.open_positions["b"].margin == Decimal("525")


def test_closed_trade_carries_group_id():
    events = [
        ev("o", "open", 1, direction="yes", market="tok", group="cond-1"),
        ev("c", "close", 2, direction="yes", market="tok", group="cond-1"),
    ]
    model = FakeModel({"o": Decimal("0.5"), "c": Decimal("0.6")}, max_leverage=Decimal("1"))
    res = simulate(events, model, CFG, FOLLOW)
    assert res.closed_trades[0].group_id == "cond-1"


class HourlyCostModel(FakeModel):
    def __init__(self, prices, rate):
        super().__init__(prices)
        self.rate = rate
        self.calls: list[tuple[str, datetime, datetime]] = []

    def holding_cost(self, pos: OpenPosition, start: datetime, end: datetime) -> Decimal:
        self.calls.append((pos.position_id, start, end))
        return self.rate * Decimal(int((end - start).total_seconds())) / Decimal(3600)


@pytest.mark.parametrize("rate", [Decimal("2.5"), Decimal("-1")])
def test_holding_cost_charged_per_hour(rate):
    events = [
        ev("o", "open", 0),
        ev("x", "open", 300, pid="other", market="ETH"),   # 5 h in: a step mid-hold
        ev("xc", "close", 360, pid="other", market="ETH"),
        ev("c", "close", 600),                               # 10 h hold
    ]
    prices = {"o": Decimal("100"), "x": Decimal("10"), "xc": Decimal("10"), "c": Decimal("100")}
    res = simulate(events, HourlyCostModel(prices, rate), CFG, FOLLOW)
    by_pid = {c.position_id: c.pnl_usd for c in res.closed_trades}
    assert by_pid["p1"] == -10 * rate
    assert by_pid["other"] == -1 * rate
    curve = dict(res.equity_curve)
    assert curve[T0 + timedelta(minutes=300, seconds=1)] == Decimal("10000") - 5 * rate
    assert final_equity(res) == Decimal("10000") - 11 * rate


def test_model_without_holding_cost_unchanged():
    events = [ev("o", "open", 0), ev("c", "close", 600)]
    res = simulate(events, FakeModel({"o": Decimal("100"), "c": Decimal("100")}), CFG, FOLLOW)
    assert res.closed_trades[0].pnl_usd == Decimal("0")
    assert final_equity(res) == Decimal("10000")


# --- fix round 1 (Task 5, ruling P21): seq orders same-time events -------------------


def test_same_observed_time_sorted_by_seq_before_source_ref():
    # source_ref order is reversed ("a-close" < "b-open"); seq says open first
    o = ev("b-open", "open", 1, seq=0)
    c = ev("a-close", "close", 1, seq=1)
    prices = {"b-open": Decimal("100"), "a-close": Decimal("110")}
    for events in ([c, o], [o, c]):
        res = simulate(events, FakeModel(prices), CFG, FOLLOW)
        assert [f.ev_ref for f in res.fills] == ["b-open", "a-close"]
        assert len(res.closed_trades) == 1


def test_seq_defaults_to_zero_and_sort_key_is_public():
    from hades_core.paper import event_sort_key

    e = ev("x", "open", 1)
    assert e.seq == 0
    assert event_sort_key(e) == (e.observed_time, 0, "x")


# -- P48: mark_times (periodic equity marks between events) ---------------------------

def test_mark_times_show_unrealized_drawdown_between_events():
    from hades_core.gate import DEFAULT_GATE, evaluate_gate

    events = [ev("o", "open", 1, leverage="10"), ev("c", "close", 120)]
    model = FakeModel({"o": Decimal("100"), "c": Decimal("100")})
    dip = T0 + timedelta(minutes=60)

    def mark(m, t):
        return Decimal("95") if t == dip else None

    plain = simulate(events, model, CFG, FOLLOW, mark=mark)
    marked = simulate(events, model, CFG, FOLLOW, mark=mark, mark_times=[dip])
    # qty 50 (margin 500 x10 / 100); a 5-point dip = -250 unrealized at the mark
    assert dict(marked.equity_curve)[dip] == Decimal("9750")
    assert dip not in dict(plain.equity_curve)
    now = T0 + timedelta(days=1)
    dd_plain = Decimal(evaluate_gate(plain, FOLLOW, now, DEFAULT_GATE).values["max_dd"])
    dd_marked = Decimal(evaluate_gate(marked, FOLLOW, now, DEFAULT_GATE).values["max_dd"])
    assert dd_plain == 0
    assert dd_marked == Decimal("0.025")
    assert final_equity(marked) == final_equity(plain) == Decimal("10000")
    # the curve stays time-ordered
    ts = [t for t, _ in marked.equity_curve]
    assert ts == sorted(ts)


def test_mark_times_liquidate_and_trailing_mark_is_last_point():
    events = [ev("o", "open", 1, leverage="10")]
    model = FakeModel({"o": Decimal("100")})
    m1, m2 = T0 + timedelta(minutes=30), T0 + timedelta(minutes=90)
    px = {m1: Decimal("97"), m2: Decimal("85")}
    res = simulate(events, model, CFG, FOLLOW, mark=lambda m, t: px.get(t),
                   mark_times=[m2, m1])  # unsorted input is fine
    assert dict(res.equity_curve)[m1] == Decimal("9850")
    assert res.equity_curve[-1] == (m2, Decimal("9500"))  # liquidated at the margin
    assert res.open_positions == {}
    assert [c.pnl_usd for c in res.closed_trades] == [Decimal("-500")]


def test_mark_times_before_follow_ignored_and_none_unchanged():
    events = [ev("o", "open", 1), ev("c", "close", 5)]
    model = FakeModel({"o": Decimal("100"), "c": Decimal("110")})
    base = simulate(events, model, CFG, FOLLOW)
    same = simulate(events, model, CFG, FOLLOW, mark_times=None)
    assert base.equity_curve == same.equity_curve
    assert base.fills == same.fills and base.closed_trades == same.closed_trades
    early = simulate(events, model, CFG, FOLLOW, mark_times=[T0 - timedelta(hours=1)])
    assert early.equity_curve == base.equity_curve


# -- final review I3 / P54: the display-only point never liquidates ---------------------

def test_display_point_never_liquidates_or_enters_history():
    events = [ev("o", "open", 1, leverage="10")]
    model = FakeModel({"o": Decimal("100")})
    m1, now = T0 + timedelta(minutes=30), T0 + timedelta(minutes=90)
    px = {m1: Decimal("97"), now: Decimal("85")}  # 85 is past the liquidation level

    def mark(m, t):
        return px.get(t)

    res = simulate(events, model, CFG, FOLLOW, mark=mark, mark_times=[m1], display_time=now)
    # shown at the capped loss (margin 500), but the copy is still open
    assert res.display_point == (now, Decimal("9500"))
    assert list(res.open_positions) == ["p1"] and res.closed_trades == []
    assert not any(f.ev_ref.startswith("liquidation") for f in res.fills)
    assert res.equity_curve[-1] == (m1, Decimal("9850"))  # history ends at the last mark
    assert now not in dict(res.equity_curve)
    # the same history as a run without the display point
    plain = simulate(events, model, CFG, FOLLOW, mark=mark, mark_times=[m1])
    assert plain.display_point is None
    assert (plain.equity_curve, plain.fills, plain.closed_trades, plain.open_positions) == (
        res.equity_curve, res.fills, res.closed_trades, res.open_positions)


def test_display_point_uses_last_price_without_a_mark_and_counts_holding_cost():
    events = [ev("o", "open", 0)]
    model = HourlyCostModel({"o": Decimal("100")}, rate=Decimal("5"))
    now = T0 + timedelta(hours=2, seconds=1)
    res = simulate(events, model, CFG, FOLLOW, display_time=now)
    # 2 h held since the open at T0 + 1 s, at 5 per hour, not yet charged to the book
    assert res.display_point == (now, Decimal("10000") - Decimal("10"))
    again = simulate(events, model, CFG, FOLLOW, display_time=now)
    assert again.display_point == res.display_point  # pure: nothing accrued by the display


def test_display_point_before_follow_is_ignored():
    res = simulate([], FakeModel({}), CFG, FOLLOW, display_time=T0 - timedelta(hours=1))
    assert res.display_point is None
