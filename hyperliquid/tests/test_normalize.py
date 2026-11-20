from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from hades_hl.normalize import (
    HEAD,
    fills_to_events,
    is_spot,
    leverage_by_coin,
    order_fills,
)
from hlfills import ADDR, T0, mk


def ev_tuple(e):
    return (e.side, e.direction, e.size, e.position_id)


# -- side / direction mapping ---------------------------------------------------

def test_open_increase_reduce_close_long():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0),          # Open Long, start 0 -> open
        mk("BTC", "B", "2", "110", "1", T0 + 1),      # Open Long, start 1 -> increase
        mk("BTC", "A", "1.5", "120", "3", T0 + 2),    # Close Long, -> reduce
        mk("BTC", "A", "1.5", "130", "1.5", T0 + 3),  # Close Long, -> close (to 0)
    ]
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("open", "long", Decimal("1"), "BTC:0"),
        ("increase", "long", Decimal("2"), "BTC:0"),
        ("reduce", "long", Decimal("1.5"), "BTC:0"),
        ("close", "long", Decimal("1.5"), "BTC:0"),
    ]
    assert fills[1]["dir"] == "Open Long"  # HL says "Open"; startPosition makes it increase


def test_short_mapping_and_size_is_absolute():
    fills = [
        mk("ETH", "A", "2", "10", "0", T0),
        mk("ETH", "A", "1", "10", "-2", T0 + 1),
        mk("ETH", "B", "3", "9", "-3", T0 + 2),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("open", "short", Decimal("2"), "ETH:0"),
        ("increase", "short", Decimal("1"), "ETH:0"),
        ("close", "short", Decimal("3"), "ETH:0"),
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


def test_position_id_increments_when_flat():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0),
        mk("BTC", "A", "1", "101", "1", T0 + 1),
        mk("ETH", "B", "1", "10", "0", T0 + 2),
        mk("BTC", "A", "1", "102", "0", T0 + 3),
        mk("BTC", "B", "1", "100", "-1", T0 + 4),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [e.position_id for e in evs] == ["BTC:0", "BTC:0", "ETH:0", "BTC:1", "BTC:1"]


def test_history_starting_mid_position():
    # first fill we see already has a position: it is an increase/reduce of BTC:0
    fills = [mk("BTC", "A", "1", "100", "3", T0), mk("BTC", "A", "2", "100", "2", T0 + 1)]
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("reduce", "long", Decimal("1"), "BTC:0"),
        ("close", "long", Decimal("2"), "BTC:0"),
    ]


# -- flip ----------------------------------------------------------------------

def test_flip_splits_into_close_and_open():
    fills = [
        mk("BTC", "B", "2", "100", "0", T0),
        mk("BTC", "A", "5", "110", "2", T0 + 1, fee="1.0", closed_pnl="20", hash="0xf", tid=9),
    ]
    assert fills[1]["dir"] == "Long > Short"
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("open", "long", Decimal("2"), "BTC:0"),
        ("close", "long", Decimal("2"), "BTC:0"),
        ("open", "short", Decimal("3"), "BTC:1"),
    ]
    close, opn = evs[1], evs[2]
    assert close.source_ref == "0xf:9:close"
    assert opn.source_ref == "0xf:9:open"
    assert close.source_ref != opn.source_ref
    # fee is split pro rata by size and sums back to the fill fee
    assert close.fee_usd == Decimal("0.4") and opn.fee_usd == Decimal("0.6")
    assert close.notional_usd == Decimal("220") and opn.notional_usd == Decimal("330")


def test_short_to_long_flip():
    fills = [mk("SOL", "B", "4", "10", "-1", T0)]
    assert fills[0]["dir"] == "Short > Long"
    evs = fills_to_events(ADDR, fills)
    assert [ev_tuple(e) for e in evs] == [
        ("close", "short", Decimal("1"), "SOL:0"),
        ("open", "long", Decimal("3"), "SOL:1"),
    ]


# -- liquidation, spot ----------------------------------------------------------

def test_liquidation_becomes_close():
    liq = {"liquidatedUser": ADDR, "markPx": "90", "method": "market"}
    fills = [
        mk("BTC", "B", "3", "100", "0", T0),
        # a partial liquidation fill still maps to close (paper copy exits fully)
        mk("BTC", "A", "1", "90", "3", T0 + 1, closed_pnl="-10", liquidation=liq),
    ]
    evs = fills_to_events(ADDR, fills)
    assert evs[1].side == "close"
    assert evs[1].direction == "long"
    assert evs[1].position_id == "BTC:0"


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
