from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from hades_hl.normalize import (
    HEAD,
    fill_legs,
    fills_to_events,
    is_own_liquidation,
    is_spot,
    normalize_fills,
    leverage_by_coin,
    order_fills,
)
from hlfills import ADDR, T0, mk


def ev_tuple(e):
    return (e.side, e.direction, e.size, e.position_id)


# -- side / direction mapping ---------------------------------------------------

def test_open_increase_reduce_close_long():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, tid=101),  # Open Long, start 0 -> open
        mk("BTC", "B", "2", "110", "1", T0 + 1),      # Open Long, start 1 -> increase
        mk("BTC", "A", "1.5", "120", "3", T0 + 2),    # Close Long, -> reduce
        mk("BTC", "A", "1.5", "130", "1.5", T0 + 3),  # Close Long, -> close (to 0)
    ]
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("open", "long", Decimal("1"), "BTC:101"),
        ("increase", "long", Decimal("2"), "BTC:101"),
        ("reduce", "long", Decimal("1.5"), "BTC:101"),
        ("close", "long", Decimal("1.5"), "BTC:101"),
    ]
    assert fills[1]["dir"] == "Open Long"  # HL says "Open"; startPosition makes it increase


def test_short_mapping_and_size_is_absolute():
    fills = [
        mk("ETH", "A", "2", "10", "0", T0, tid=201),
        mk("ETH", "A", "1", "10", "-2", T0 + 1),
        mk("ETH", "B", "3", "9", "-3", T0 + 2),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("open", "short", Decimal("2"), "ETH:201"),
        ("increase", "short", Decimal("1"), "ETH:201"),
        ("close", "short", Decimal("3"), "ETH:201"),
    ]
    assert all(e.size > 0 for e in evs)


def test_event_fields():
    f = mk("BTC", "B", "0.5", "60000", "0", T0, fee="1.25", hash="0xabc", tid=77)
    (e,) = fills_to_events(ADDR, [f])
    assert e.head == HEAD == "hyperliquid"
    assert e.wallet == ADDR
    assert e.market_id == "BTC"
    assert e.price == Decimal("60000")
    assert e.notional_usd == Decimal("30000.0")
    assert e.fee_usd == Decimal("1.25")
    assert e.leader_time == datetime.fromtimestamp(T0 / 1000, tz=timezone.utc)
    assert e.observed_time == e.leader_time
    assert e.source_ref == "0xabc:77"
    assert e.group_id is None
    assert e.leverage is None


def test_observed_time_fn_and_leverage():
    f = mk("BTC", "B", "1", "100", "0", T0)
    later = lambda fill: datetime.fromtimestamp(fill["time"] / 1000, tz=timezone.utc) + timedelta(seconds=2)
    (e,) = fills_to_events(ADDR, [f], observed_time_fn=later, leverage={"BTC": Decimal("3")})
    assert e.observed_time - e.leader_time == timedelta(seconds=2)
    assert e.leverage == Decimal("3")


def test_position_id_is_coin_and_opening_tid():
    # P22: position_id = "{coin}:{tid of the opening fill}" (not a running counter)
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, tid=301),
        mk("BTC", "A", "1", "101", "1", T0 + 1, tid=302),
        mk("ETH", "B", "1", "10", "0", T0 + 2, tid=303),
        mk("BTC", "A", "1", "102", "0", T0 + 3, tid=304),
        mk("BTC", "B", "1", "100", "-1", T0 + 4, tid=305),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [e.position_id for e in evs] == ["BTC:301", "BTC:301", "ETH:303", "BTC:304", "BTC:304"]


def test_history_starting_mid_position_gets_pre_id():
    # first fill we see already has a position: increase/reduce of "BTC:pre{tid}"
    fills = [mk("BTC", "A", "1", "100", "3", T0, tid=401), mk("BTC", "A", "2", "100", "2", T0 + 1)]
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("reduce", "long", Decimal("1"), "BTC:pre401"),
        ("close", "long", Decimal("2"), "BTC:pre401"),
    ]


# -- flip ----------------------------------------------------------------------

def test_flip_splits_into_close_and_open():
    fills = [
        mk("BTC", "B", "2", "100", "0", T0, tid=501),
        mk("BTC", "A", "5", "110", "2", T0 + 1, fee="1.0", closed_pnl="20", hash="0xf", tid=9),
    ]
    assert fills[1]["dir"] == "Long > Short"
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("open", "long", Decimal("2"), "BTC:501"),
        ("close", "long", Decimal("2"), "BTC:501"),
        ("open", "short", Decimal("3"), "BTC:9f"),
    ]
    close, opn = evs[1], evs[2]
    assert close.source_ref == "0xf:9:close"
    assert opn.source_ref == "0xf:9:open"
    assert close.source_ref != opn.source_ref
    assert close.seq < opn.seq  # same time: the close leg is processed first
    # fee is split pro rata by size and sums back to the fill fee
    assert close.fee_usd == Decimal("0.4") and opn.fee_usd == Decimal("0.6")
    assert close.notional_usd == Decimal("220") and opn.notional_usd == Decimal("330")


def test_short_to_long_flip():
    fills = [mk("SOL", "B", "4", "10", "-1", T0, tid=601)]
    assert fills[0]["dir"] == "Short > Long"
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("close", "short", Decimal("1"), "SOL:pre601"),
        ("open", "long", Decimal("3"), "SOL:601f"),
    ]


# -- liquidation, spot ----------------------------------------------------------

LIQ = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}


def test_partial_liquidation_is_reduce():
    # P18: position after the fill != 0 (same sign) -> reduce; the episode continues
    fills = [
        mk("BTC", "B", "3", "100", "0", T0, tid=701),
        mk("BTC", "A", "1", "90", "3", T0 + 1, closed_pnl="-10", liquidation=LIQ),
        mk("BTC", "A", "2", "95", "2", T0 + 2),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [(e.side, e.position_id) for e in evs] == [
        ("open", "BTC:701"), ("reduce", "BTC:701"), ("close", "BTC:701")]
    legs = fill_legs(ADDR, fills)
    assert [lg.liquidation for lg in legs] == [False, True, False]


def test_full_liquidation_is_close():
    fills = [
        mk("BTC", "B", "3", "100", "0", T0, tid=711),
        mk("BTC", "A", "3", "90", "3", T0 + 1, closed_pnl="-30", liquidation=LIQ),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [(e.side, e.direction, e.position_id) for e in evs] == [
        ("open", "long", "BTC:711"), ("close", "long", "BTC:711")]


def test_flip_liquidation_only_close_leg_is_liquidated():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, tid=721),
        mk("BTC", "A", "3", "90", "1", T0 + 1, closed_pnl="-10", liquidation=LIQ, tid=722),
    ]
    legs = fill_legs(ADDR, fills)
    assert [(lg.side, lg.position_id, lg.liquidation) for lg in legs] == [
        ("open", "BTC:721", False), ("close", "BTC:721", True), ("open", "BTC:722f", False)]


def test_liquidator_side_fill_is_not_own_liquidation():
    other = {"liquidatedUser": "0x" + "cd" * 20, "markPx": "90", "method": "backstop"}
    f = mk("BTC", "A", "1", "90", "2", T0 + 1, liquidation=other)
    assert is_own_liquidation(f, ADDR) is False
    assert is_own_liquidation(mk("BTC", "A", "1", "90", "2", T0, liquidation=LIQ), ADDR) is True
    legs = fill_legs(ADDR, [mk("BTC", "B", "2", "90", "0", T0), f])
    assert [(lg.side, lg.liquidation) for lg in legs] == [("open", False), ("reduce", False)]


@pytest.mark.parametrize("fn", [is_own_liquidation, fill_legs, fills_to_events])
def test_addr_is_required_positional(fn):
    import inspect

    p = inspect.signature(fn).parameters["addr"]
    assert p.default is inspect.Parameter.empty
    assert p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_builder_fee_added_to_fee():
    f = mk("BTC", "B", "1", "100", "0", T0, fee="0.1")
    f["builderFee"] = "0.05"
    (e,) = fills_to_events(ADDR, [f])
    assert e.fee_usd == Decimal("0.15")


def test_spot_fills_dropped():
    fills = [
        mk("@107", "B", "1", "10", "0", T0, dir="Buy"),
        mk("PURR/USDC", "A", "1", "10", "0", T0 + 1, dir="Sell"),
        mk("BTC", "B", "1", "100", "0", T0 + 2),
    ]
    assert is_spot(fills[0]) and is_spot(fills[1]) and not is_spot(fills[2])
    evs = fills_to_events(ADDR, fills)
    assert [e.market_id for e in evs] == ["BTC"]


def test_hip3_coin_is_perp():
    assert not is_spot(mk("xyz:TSLA", "B", "1", "400", "0", T0))


def test_missing_tid_raises():
    f = mk("BTC", "B", "1", "100", "0", T0)
    del f["tid"]
    with pytest.raises(ValueError):
        fills_to_events(ADDR, [f])


def test_duplicate_tid_dropped():
    f = mk("BTC", "B", "1", "100", "0", T0)
    assert len(fills_to_events(ADDR, [f, dict(f)])) == 1


# -- ordering ---------------------------------------------------------------------

def test_same_ms_fills_chained_by_start_position_not_tid():
    # HL tids are NOT monotonic inside one ms; the order is the startPosition chain
    a = mk("BTC", "B", "1", "100", "0", T0, tid=900)
    b = mk("BTC", "B", "1", "100", "1", T0, tid=5)
    c = mk("BTC", "B", "1", "100", "2", T0, tid=40)
    for perm in ([a, b, c], [c, b, a], [b, c, a]):
        assert [f["tid"] for f in order_fills(perm)] == [900, 5, 40]


def test_leverage_by_coin_from_clearinghouse(fixture):
    ch = fixture("clearinghouse_state.json")
    lev = leverage_by_coin(ch)
    assert lev["BTC"] == Decimal("3")
    assert all(isinstance(v, Decimal) and v > 0 for v in lev.values())
    assert leverage_by_coin(None) == {}


# -- real fixture -------------------------------------------------------------------

def test_real_fixture_events(fixture, caplog):
    fills = fixture("user_fills_by_time.json")
    meta = fixture("fixture_meta.json")
    with caplog.at_level("WARNING", logger="hades_hl.normalize"):
        evs = fills_to_events(meta["address"], fills)
    n_flips = sum(1 for f in fills if ">" in f["dir"])
    assert n_flips == 29
    assert len(evs) == len(fills) + n_flips
    assert len({e.source_ref for e in evs}) == len(evs)
    assert {e.side for e in evs} <= {"open", "increase", "reduce", "close"}
    times = [e.leader_time for e in evs]
    assert times == sorted(times)
    # the startPosition chain is continuous after ordering: no fill contradicts it
    pos: dict[str, Decimal] = {}
    for f in order_fills(fills):
        s = Decimal(f["startPosition"])
        if f["coin"] in pos:
            assert pos[f["coin"]] == s
        d = Decimal(f["sz"]) * (1 if f["side"] == "B" else -1)
        pos[f["coin"]] = s + d
    # HL's "dir" agrees with our startPosition-derived direction on every fill
    assert not [r for r in caplog.records if "disagrees" in r.getMessage()]
    assert {e.direction for e in evs} == {"long", "short"}


# -- fix round 1: seq, stable ids, gaps, chain robustness ---------------------------

def test_seq_is_monotonic_over_events():
    fills = [mk("BTC", "B", "1", "100", "0", T0), mk("BTC", "A", "3", "100", "1", T0),
             mk("ETH", "B", "1", "10", "0", T0)]
    evs = fills_to_events(ADDR, fills)
    assert [e.seq for e in evs] == list(range(len(evs)))


def test_missing_close_gives_next_open_a_new_id(caplog):
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, tid=801),
        # the close of 801 is missing: this open starts from 0, not from 1
        mk("BTC", "B", "1", "100", "0", T0 + 5, tid=802),
        mk("BTC", "A", "1", "100", "1", T0 + 6, tid=803),
    ]
    with caplog.at_level("WARNING", logger="hades_hl.normalize"):
        res = normalize_fills(ADDR, fills)
    assert [(lg.side, lg.position_id) for lg in res.legs] == [
        ("open", "BTC:801"), ("open", "BTC:802"), ("close", "BTC:802")]  # P25
    assert res.chain_gaps == 1
    assert any("gap" in r.getMessage() for r in caplog.records)


def test_gap_fill_from_flat_gets_plain_id_but_counts_as_gap():
    # ruling P25: a gap fill with startPosition == 0 opens a fresh episode -> {coin}:{tid}
    fills = [
        mk("ETH", "B", "2", "10", "0", T0, tid=901),
        mk("ETH", "A", "1", "10", "0", T0 + 5, tid=902),  # should start at 2: gap, from flat
        mk("ETH", "B", "1", "10", "-1", T0 + 6, tid=903),
        mk("ETH", "B", "1", "10", "3", T0 + 7, tid=904),  # gap mid-position: keeps gap id
    ]
    res = normalize_fills(ADDR, fills)
    assert [lg.position_id for lg in res.legs] == ["ETH:901", "ETH:902", "ETH:902", "ETH:gap904"]
    assert res.chain_gaps == 2


def test_ids_stable_when_history_starts_later(fixture):
    fills = fixture("user_fills_by_time.json")
    meta = fixture("fixture_meta.json")
    times = sorted({int(f["time"]) for f in fills})
    cut = times[len(times) // 3]
    full = fills_to_events(meta["address"], fills)
    later = fills_to_events(meta["address"], [f for f in fills if int(f["time"]) >= cut])

    def by_pos(evs):
        out: dict = {}
        for e in evs:
            out.setdefault(e.position_id, []).append(e.source_ref)
        return out

    a, b = by_pos(full), by_pos(later)
    inside = [pid for pid, refs in a.items()
              if all(ms >= cut for ms in (int(e.leader_time.timestamp() * 1000)
                                          for e in full if e.position_id == pid))]
    assert len(inside) > 10
    for pid in inside:
        assert b.get(pid) == a[pid], pid
    assert normalize_fills(meta["address"], fills).chain_gaps == 0


def test_chain_backtracks_out_of_greedy_dead_end():
    # valid order in the T0 ms: A(0->1) B(1->0) C(0->2) D(2->3); C listed first
    pre = mk("BTC", "B", "1", "100", "0", T0 - 1, tid=1)
    pre2 = mk("BTC", "A", "1", "100", "1", T0 - 1, tid=2)
    A = mk("BTC", "B", "1", "100", "0", T0, tid=11)
    Bf = mk("BTC", "A", "1", "100", "1", T0, tid=12)
    C = mk("BTC", "B", "2", "100", "0", T0, tid=13)
    D = mk("BTC", "B", "1", "100", "2", T0, tid=14)
    for perm in ([pre, pre2, C, D, A, Bf], [pre2, pre, D, C, Bf, A], [A, Bf, C, D, pre, pre2]):
        assert [f["tid"] for f in order_fills(perm)] == [1, 2, 11, 12, 13, 14]
    res = normalize_fills(ADDR, [pre, pre2, C, D, A, Bf])
    assert res.chain_gaps == 0
    assert [(lg.side, lg.position_id) for lg in res.legs][2:] == [
        ("open", "BTC:11"), ("close", "BTC:11"), ("open", "BTC:13"), ("increase", "BTC:13")]


def test_chain_cycle_is_input_order_independent():
    X = mk("ETH", "A", "1", "10", "1", T0, tid=21)  # 1 -> 0
    Y = mk("ETH", "B", "1", "10", "0", T0, tid=22)  # 0 -> 1
    assert [f["tid"] for f in order_fills([Y, X])] == [f["tid"] for f in order_fills([X, Y])]
    assert [f["tid"] for f in order_fills([X, Y])] == [22, 21]  # no prev end: start==0 first


def test_unchainable_group_falls_back_to_start_tid_order(caplog):
    fs = [mk("BTC", "B", "1", "100", "5", T0, tid=33), mk("BTC", "B", "1", "100", "0", T0, tid=31),
          mk("BTC", "B", "1", "100", "0", T0, tid=32)]
    with caplog.at_level("WARNING", logger="hades_hl.normalize"):
        orders = {tuple(f["tid"] for f in order_fills(p)) for p in (fs, fs[::-1])}
        res = normalize_fills(ADDR, fs)
    assert orders == {(31, 32, 33)}
    assert res.chain_gaps >= 1
    assert any("chain" in r.getMessage() for r in caplog.records)


def test_simulate_sort_keeps_per_position_order_on_fixture(fixture):
    # C1: core simulate sorts by (observed_time, seq, source_ref); that must not
    # reorder any position's events relative to the normalizer's chain order
    from hades_core.paper import event_sort_key

    fills = fixture("user_fills_by_time.json")
    meta = fixture("fixture_meta.json")
    evs = fills_to_events(meta["address"], fills)
    resorted = sorted(evs, key=event_sort_key)

    def sides(seq):
        out: dict = {}
        for e in seq:
            out.setdefault(e.position_id, []).append((e.side, e.source_ref))
        return out

    a, b = sides(evs), sides(resorted)
    assert len(a) > 40
    assert [k for k in a if a[k] != b[k]] == []


# -- ruling P28: tolerant startPosition chain --------------------------------------

def test_float_noise_start_positions_make_no_gaps(caplog):
    fills = [
        mk("BTC", "A", "613298.43", "10", "0", T0, tid=1),
        mk("BTC", "A", "1", "10", "-613298.4300000001", T0 + 5, tid=2),  # HL float noise
        mk("BTC", "B", "613299.43", "10", "-613299.43", T0 + 9, tid=3),
    ]
    with caplog.at_level("WARNING", logger="hades_hl.normalize"):
        res = normalize_fills(ADDR, fills, sz_decimals={"BTC": 2})
    assert res.chain_gaps == 0
    assert [(lg.side, lg.position_id) for lg in res.legs] == [
        ("open", "BTC:1"), ("increase", "BTC:1"), ("close", "BTC:1")]
    assert not any("gap" in r.getMessage() for r in caplog.records)
    # unknown szDecimals: 1e-9 fallback still absorbs the 1e-10 noise
    assert normalize_fills(ADDR, fills).chain_gaps == 0


def test_close_at_near_zero_end_is_a_close():
    fills = [mk("ETH", "A", "1", "10", "1.0000000001", T0, tid=11),  # e = 1e-10
             mk("ETH", "B", "2", "10", "0", T0 + 5, tid=12)]
    res = normalize_fills(ADDR, fills, sz_decimals={"ETH": 4})
    assert [(lg.side, lg.position_id) for lg in res.legs] == [
        ("close", "ETH:pre11"), ("open", "ETH:12")]
    assert res.legs[0].end == 0
    assert res.chain_gaps == 0


def test_same_ms_chain_tolerates_noise(caplog):
    fills = [mk("SOL", "B", "0.3", "10", "0", T0, tid=21),
             mk("SOL", "B", "0.3", "10", "0.30000000000000004", T0 + 1, tid=23),
             mk("SOL", "B", "0.4", "10", "0.6000000000000001", T0 + 1, tid=22)]
    with caplog.at_level("WARNING", logger="hades_hl.normalize"):
        ordered = order_fills(fills, sz_decimals={"SOL": 1})
    assert [f["tid"] for f in ordered] == [21, 23, 22]
    assert not any("no full startPosition chain" in r.getMessage() for r in caplog.records)


def test_tolerance_scales_with_sz_decimals():
    fills = [mk("XYZ", "B", "1", "10", "0", T0, tid=31),
             mk("XYZ", "B", "1", "10", "1.004", T0 + 5, tid=32)]  # within 0.005 at 2 dp
    assert normalize_fills(ADDR, fills, sz_decimals={"XYZ": 2}).chain_gaps == 0
    assert normalize_fills(ADDR, fills, sz_decimals={"XYZ": 3}).chain_gaps == 1
    assert normalize_fills(ADDR, fills).chain_gaps == 1  # 1e-9 fallback
    evs = fills_to_events(ADDR, fills, sz_decimals={"XYZ": 2})
    assert [e.position_id for e in evs] == ["XYZ:31", "XYZ:31"]


def test_fallback_tolerance_is_relative_to_position():  # P34
    from hades_hl.normalize import chain_tol
    assert chain_tol("X") == Decimal("1e-9")
    assert chain_tol("X", None, Decimal("-5e8")) == Decimal("0.5")
    assert chain_tol("X", {"X": 2}, Decimal("1e12")) == Decimal("0.005")  # szDecimals wins
    fills = [mk("BIG", "B", "300000000", "1", "0", T0, tid=41),
             mk("BIG", "B", "1", "1", "300000000.00000003", T0 + 5, tid=42)]  # 3e-8 noise
    assert normalize_fills(ADDR, fills).chain_gaps == 0
    gap = [mk("BIG", "B", "300000000", "1", "0", T0, tid=43),
           mk("BIG", "B", "1", "1", "300000000.5", T0 + 5, tid=44)]  # 0.5 > 0.3
    assert normalize_fills(ADDR, gap).chain_gaps == 1
