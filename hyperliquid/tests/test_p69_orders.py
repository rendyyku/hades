"""Ruling P69 (follow-up to P65): orders are aggregated per (address, order), so
interleaved orders of one wallet each become one event; and a late piece of the order
that OPENED a position is an ``adjust`` event: no copy trade, the copy ratio is re-based
on the leader's new size. Late pieces of an increase / reduce order keep their side
and scale by that ratio. The historical path emits the same structure."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_core.paper import PaperConfig, event_sort_key, simulate
from hades_hl import paper_runtime as pr
from hades_hl.db import PaperEvent, PaperPiece
from hades_hl.normalize import fill_legs, fills_to_events, group_legs, normalize_fills, order_key
from hlfills import ADDR, T0, ms_dt
from test_live import StubModel
from test_p65_sizing import Clock, Lev10, ms, near, notional, piece, rows, watcher
from test_paper_runtime import A, START, price, store
from test_paper_runtime import engine, session  # noqa: F401  (fixtures)

NOW = START + timedelta(days=1)
S = timedelta(seconds=1)
H = timedelta(hours=1)
D = Decimal


def shape(evs):
    return [(e.market_id, e.side, e.size) for e in evs]


# -- historical path: per-order aggregation ------------------------------------------------

def test_interleaved_orders_of_one_wallet_are_one_event_each():
    x1 = piece("1", "0", T0, 3, oid=20, px="60000")
    y1 = piece("5", "0", T0 + 1, 4, oid=21, coin="ETH", px="3000")
    x2 = piece("1", "1", T0 + 2, 5, oid=20, px="60002")
    y2 = piece("1", "5", T0 + 3, 6, oid=21, coin="ETH", px="3006")
    btc, eth = fills_to_events(ADDR, [x1, y1, x2, y2])
    assert shape([btc, eth]) == [("BTC", "open", D(2)), ("ETH", "open", D(6))]
    assert (btc.price, eth.price) == (D(60001), D(3001))  # size-weighted, per order
    assert (btc.source_ref, eth.source_ref) == ("0xh3:3", "0xh4:4")
    assert (btc.leader_time, btc.observed_time) == (ms_dt(T0), ms_dt(T0 + 2))
    assert (eth.leader_time, eth.observed_time) == (ms_dt(T0 + 1), ms_dt(T0 + 3))
    assert (btc.seq, eth.seq) == (0, 1)  # the order their last pieces came in
    b, e = group_legs(fill_legs(ADDR, [x1, y1, x2, y2]))
    assert (b.tids, b.oid, e.tids, e.oid) == ([3, 5], "20", [4, 6], "21")


def test_orders_interleaved_on_one_position_in_the_same_direction():
    fills = [
        piece("4", "0", T0, 1, oid=10),                    # the open
        piece("1", "4", T0 + 1000, 2, oid=11),             # two buy orders, interleaved
        piece("2", "5", T0 + 1001, 3, oid=12),
        piece("1", "7", T0 + 1002, 4, oid=11),
        piece("2", "8", T0 + 5000, 5, oid=13, side="A"),   # two sell orders, interleaved
        piece("3", "6", T0 + 5001, 6, oid=14, side="A"),
        piece("3", "3", T0 + 5002, 7, oid=13, side="A"),   # ... the first one ends flat
    ]
    evs = fills_to_events(ADDR, fills)
    assert [(e.side, e.size, e.source_ref) for e in evs] == [
        ("open", D(4), "0xh1:1"),
        ("increase", D(2), "0xh3:3"),
        ("increase", D(2), "0xh2:2"),
        ("reduce", D(3), "0xh6:6"),
        ("close", D(5), "0xh5:5"),
    ]
    assert sorted(evs, key=event_sort_key) == evs
    res = simulate(evs, StubModel(), PaperConfig(), ms_dt(T0 - 1000))
    assert res.open_positions == {} and len(res.closed_trades) == 1
    assert not [f for f in res.fills if f.missed]


def test_opposite_orders_on_one_position_keep_the_chain_order():
    """A buy order and a sell order filling in turns on one position are NOT merged
    across each other: summed, the sell could look bigger than the position was."""
    fills = [
        piece("1", "0", T0, 1, oid=10),
        piece("5", "1", T0 + 1000, 2, oid=11),
        piece("3", "6", T0 + 1001, 3, oid=12, side="A"),  # more than the leader had at T0
        piece("5", "3", T0 + 1002, 4, oid=11),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [(e.side, e.size) for e in evs] == [
        ("open", D(1)), ("increase", D(5)), ("reduce", D(3)), ("increase", D(5))]
    res = simulate(evs, StubModel(), PaperConfig(), ms_dt(T0 - 1000))
    (pos,) = res.open_positions.values()
    assert pos.leader_size == D(8)  # still copying
    assert [f.side for f in res.fills if not f.missed] == [
        "open", "increase", "reduce", "increase"]


# -- historical path: late pieces ----------------------------------------------------------

def late_open(t, late_ms=5000):
    return [piece("0.0002", "0", t, 9001, px="60000"),
            piece("0.5", "0.0002", t + 1, 9002, px="60001"),
            piece("1.2", "0.5002", t + 1 + late_ms, 9003, px="60002")]


def test_late_piece_of_the_opening_order_is_an_adjust_event():
    opn, adj = fills_to_events(ADDR, late_open(T0), leverage={"BTC": D(10)})
    assert (opn.side, opn.size, opn.source_ref) == ("open", D("0.5002"), "0xh9001:9001")
    assert (adj.side, adj.size, adj.source_ref) == ("adjust", D("1.2"), "0xh9003:9003")
    assert adj.position_id == opn.position_id == "BTC:9001" and adj.direction == "long"
    assert adj.leader_time == adj.observed_time == ms_dt(T0 + 5001)
    assert (adj.price, adj.notional_usd) == (D(60002), D("1.2") * 60002)
    _, g = group_legs(fill_legs(ADDR, late_open(T0)))
    assert (g.oid, g.tids, g.start, g.end) == ("7001", [9003], D("0.5002"), D("1.7002"))
    res = simulate([opn, adj], StubModel(), PaperConfig(), ms_dt(T0 - 1000))
    (pos,) = res.open_positions.values()
    assert pos.qty * pos.entry_price == D(5000)  # 5% x 10,000 x 10: the adjust adds nothing
    assert pos.leader_size == D("1.7002") and pos.ratio == pos.qty / D("1.7002")
    # inside the window it is still simply the open
    (one,) = fills_to_events(ADDR, late_open(T0, late_ms=2000))
    assert (one.side, one.size) == ("open", D("1.7002"))


def test_a_piece_of_the_opening_order_after_another_order_is_an_adjust():
    o1 = piece("1", "0", T0, 1, oid=7)
    red = piece("0.4", "1", T0 + 1, 2, oid=8, side="A")
    o2 = piece("1", "0.6", T0 + 2, 3, oid=7)
    assert [(e.side, e.size) for e in fills_to_events(ADDR, [o1, red, o2])] == [
        ("open", D(1)), ("reduce", D("0.4")), ("adjust", D(1))]
    # another BUY order on the position ends the open as well: an increase sized before
    # its open would be dropped
    inc = piece("1", "1", T0 + 1, 2, oid=9)
    o2 = piece("1", "2", T0 + 2, 3, oid=7)
    evs = fills_to_events(ADDR, [o1, inc, o2])
    assert [(e.side, e.size) for e in evs] == [
        ("open", D(1)), ("increase", D(1)), ("adjust", D(1))]
    assert sorted(evs, key=event_sort_key) == evs


def test_late_pieces_of_an_increase_or_reduce_order_keep_their_side():
    fills = [
        piece("4", "0", T0, 1, oid=1),
        piece("1", "4", T0 + 10_000, 2, oid=2),
        piece("1", "5", T0 + 15_000, 3, oid=2),             # 5 s late
        piece("1", "6", T0 + 20_000, 4, oid=3, side="A"),
        piece("1", "5", T0 + 25_000, 5, oid=3, side="A"),   # 5 s late
    ]
    assert [(e.side, e.size) for e in fills_to_events(ADDR, fills)] == [
        ("open", D(4)), ("increase", D(1)), ("increase", D(1)),
        ("reduce", D(1)), ("reduce", D(1))]


def test_adjust_needs_the_opening_order_of_that_episode():
    # a flip: the order that flipped opened the new episode; its late piece adjusts it
    fills = [
        piece("1", "0", T0, 1, oid=1),
        piece("1.4", "1", T0 + 1000, 2, oid=30, side="A"),
        piece("0.5", "-0.4", T0 + 6000, 3, oid=30, side="A"),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [(e.side, e.direction, e.size, e.position_id) for e in evs] == [
        ("open", "long", D(1), "BTC:1"), ("close", "long", D(1), "BTC:1"),
        ("open", "short", D("0.4"), "BTC:2f"), ("adjust", "short", D("0.5"), "BTC:2f")]
    # the same oid in a LATER episode is not that episode's opening order
    fills = [
        piece("1", "0", T0, 1, oid=1),
        piece("1", "1", T0 + 1000, 2, oid=2, side="A"),
        piece("1", "0", T0 + 2000, 3, oid=3),
        piece("1", "1", T0 + 9000, 4, oid=1),
    ]
    assert [e.side for e in fills_to_events(ADDR, fills)] == [
        "open", "close", "open", "increase"]
    # history starting mid-position: the opening order is unknown
    fills = [piece("1", "5", T0, 1, oid=5), piece("1", "6", T0 + 5000, 2, oid=5)]
    assert [e.side for e in fills_to_events(ADDR, fills)] == ["increase", "increase"]
    # no oid: nothing says the late fill is the same order
    fills = [piece("1", "0", T0, 1, oid=None), piece("1", "1", T0 + 5000, 2, oid=None)]
    assert [e.side for e in fills_to_events(ADDR, fills)] == ["open", "increase"]


def test_real_fixture_per_order_events_replay_to_the_leader_s_positions(fixture):
    fills = fixture("user_fills_by_time.json")
    addr = fixture("fixture_meta.json")["address"]
    legs = normalize_fills(addr, fills).legs
    groups = group_legs(legs)
    evs = fills_to_events(addr, fills)
    assert len(evs) == len(groups) < len(legs)
    # every leg is in exactly one event
    assert sorted(id(lg) for g in groups for lg in g.legs) == sorted(id(lg) for lg in legs)
    assert all(len({order_key(lg) for lg in g.legs}) == 1 for g in groups)
    # the simulator's order is the emitted order
    assert sorted(evs, key=event_sort_key) == evs
    # replaying the events gives the leader's position after every episode's last leg
    end = {lg.position_id: abs(lg.end) for lg in legs}
    size: dict[str, Decimal] = {}
    for lg in legs:
        size.setdefault(lg.position_id, abs(lg.start))
    opened_by: dict[str, str | None] = {}
    seen: set[str] = set()
    for g, e in zip(groups, evs):
        pid = e.position_id
        if pid not in seen:
            seen.add(pid)
            if ":pre" not in pid and ":gap" not in pid:
                assert e.side == "open"
        if e.side == "open":
            opened_by[pid] = g.oid
        if e.side == "adjust":  # only ever a piece of the order that opened the episode
            assert g.oid is not None and opened_by.get(pid) == g.oid
        if e.side in ("open", "increase", "adjust"):
            size[pid] += e.size
        else:
            size[pid] -= e.size
            assert size[pid] >= 0
            assert (e.side == "close") == (size[pid] == 0)
    assert size == end


# -- live path: per-order buffers ----------------------------------------------------------

async def test_interleaved_orders_aggregate_independently(engine):
    clock = Clock(NOW)
    w = watcher(engine, clock)
    t = ms(NOW) - 500
    a1 = piece("1", "0", t, 1, oid=50, px="60000")
    b1 = piece("2", "0", t + 1, 2, oid=51, coin="ETH", px="3000")
    a2 = piece("1", "1", t + 2, 3, oid=50, px="60002")
    b2 = piece("2", "2", t + 3, 4, oid=51, coin="ETH", px="3002")
    for i, f in enumerate([a1, b1, a2, b2]):  # observed at +0, +0.2, +0.4, +0.6 s
        assert await w.ingest(A, [f], observed=NOW + i * 0.2 * S, via="ws") == []
    clock.t = NOW + 2.45 * S  # 2 s after the BTC order's last piece, not yet the ETH one's
    (btc,) = await w.flush_due()
    assert (btc.market_id, btc.side, btc.size, btc.price, btc.seq) == (
        "BTC", "open", D(2), D(60001), 1)
    assert btc.observed_time == NOW + 0.4 * S and w.has_buffered(A)
    clock.t = NOW + 2.6 * S
    (eth,) = await w.flush_due()
    assert (eth.market_id, eth.side, eth.size, eth.price, eth.seq) == (
        "ETH", "open", D(4), D(3001), 2)
    assert eth.observed_time == NOW + 0.6 * S and not w.has_buffered(A)
    r1, r2 = rows(engine)
    assert (r1.json["tids"], r1.json["oid"]) == ([1, 3], "50")
    assert (r2.json["tids"], r2.json["oid"]) == ([2, 4], "51")
    with Session(engine) as s:
        assert len(list(s.scalars(select(PaperPiece)))) == 4


async def test_another_order_on_the_same_position_ends_an_open_at_once(engine):
    clock = Clock(NOW)
    w = watcher(engine, clock)
    t = ms(NOW) - 500
    await w.ingest(A, [piece("1", "0", t, 1, oid=50)], observed=NOW, via="ws")
    # a sell order on that position: the open is complete as far as we can tell
    red = piece("0.4", "1", t + 1, 2, oid=51, side="A")
    (opn,) = await w.ingest(A, [red], observed=NOW + 0.5 * S, via="ws")
    assert (opn.side, opn.size, opn.seq, opn.observed_time) == ("open", D(1), 1, NOW)
    # one more piece of the opening order: it can no longer join the open
    late = piece("1", "0.6", t + 2, 3, oid=50)
    (r,) = await w.ingest(A, [late], observed=NOW + 1 * S, via="ws")
    assert (r.side, r.size, r.seq) == ("reduce", D("0.4"), 2)
    clock.t = NOW + 3 * S
    (adj,) = await w.flush_due()
    assert (adj.side, adj.size, adj.seq, adj.position_id) == ("adjust", D(1), 3, "BTC:1")


async def test_seq_never_runs_against_observed_time(engine):
    """A complete order waits for an older order that is still filling: events are
    emitted in the simulator's order."""
    clock = Clock(NOW)
    w = watcher(engine, clock)
    t = ms(NOW) - 500
    eth = piece("2", "0", t, 1, oid=60, coin="ETH", px="3000")
    opn = piece("1", "0", t + 1, 2, oid=61)
    red = piece("0.4", "1", t + 2, 3, oid=62, side="A")
    assert await w.ingest(A, [eth], observed=NOW, via="ws") == []
    assert await w.ingest(A, [opn], observed=NOW + 0.5 * S, via="ws") == []
    # the BTC open is complete, but the ETH order before it may still take pieces
    assert await w.ingest(A, [red], observed=NOW + 1 * S, via="ws") == []
    clock.t = NOW + 2 * S
    out = await w.flush_due()
    assert [(e.market_id, e.side, e.seq) for e in out] == [("ETH", "open", 1), ("BTC", "open", 2)]
    clock.t = NOW + 3 * S
    (r,) = await w.flush_due()
    assert (r.side, r.seq) == ("reduce", 3)
    stored = rows(engine)
    assert [x.observed_time for x in stored] == sorted(x.observed_time for x in stored)


async def test_interleaved_batch_with_a_zero_window_is_one_event_per_order(engine):
    w = watcher(engine, Clock(NOW), aggregate_window_s=0)
    t = ms(NOW) - 500
    batch = [piece("1", "0", t, 1, oid=50),
             piece("2", "0", t + 1, 2, oid=51, coin="ETH", px="3000"),
             piece("1", "1", t + 2, 3, oid=50),
             piece("2", "2", t + 3, 4, oid=51, coin="ETH", px="3000")]
    out = await w.ingest(A, batch, observed=NOW, via="recovery")
    assert [(e.market_id, e.side, e.size, e.seq) for e in out] == [
        ("BTC", "open", D(2), 1), ("ETH", "open", D(4), 2)]
    assert not w.has_buffered(A)


# -- live path to the paper run: late pieces -----------------------------------------------

async def test_open_piece_5s_late_adjusts_the_ratio_and_later_orders_follow_it(engine):
    clock = Clock(NOW)
    got = []
    w = watcher(engine, clock, Lev10(), fetch_leverage=True, on_fill=got.append)
    p1, p2, p3 = late_open(ms(NOW) - 300)
    await w.ingest(A, [p1, p2], observed=NOW, via="ws")
    clock.t = NOW + 2 * S
    (opn,) = await w.flush_due()
    assert (opn.side, opn.size) == ("open", D("0.5002"))
    late = NOW + 5 * S
    assert await w.ingest(A, [p3], observed=late, via="ws") == []
    clock.t = late + 2 * S
    (adj,) = await w.flush_due()
    assert (adj.side, adj.size, adj.seq, adj.leverage) == ("adjust", D("1.2"), 2, D(10))
    assert adj.position_id == opn.position_id and adj.observed_time == late
    assert adj.source_ref == "0xh9003:9003"
    js = rows(engine)[1].json  # the evidence
    assert (js["side"], js["oid"], js["tids"]) == ("adjust", "7001", [9003])
    assert (js["leader_start"], js["leader_end"]) == ("0.5002", "1.7002")

    # only the open has a recorded price; the adjust's window [+6 s, +35 s] is empty
    price(engine, "BTC", NOW + S, 60000)
    with Session(engine) as s:
        pr.run_paper(s, A, NOW + 40 * S)
        snap = pr.snapshot(s, A)
        s.commit()
    (pos,) = snap["open_positions"]
    q0 = D(pos["qty"])
    assert near(notional(pos), 5000) and D(pos["margin"]) == D(500)  # OUR size: the open's
    assert D(pos["leader_size"]) == D("1.7002")
    assert near(D(pos["ratio"]), q0 / D("1.7002"), "1e-18")  # ours / the leader's after it
    assert snap["missed"] == 0 and snap["missed_reasons"] == {}
    assert [(f["side"], f["missed"]) for f in snap["fills"]] == [
        ("open", False), ("adjust", False)]
    f = snap["fills"][1]
    assert (f["ref"], f["size"], f["price"], f["fee"], f["reason"]) == (
        "0xh9003:9003", "0", None, "0", None)
    row = rows(engine)[1]
    assert row.missed is False and row.fill_price is None and row.settled_at is not None

    # a later increase by half the leader's position: ours grows by half, by the NEW r
    t_inc = NOW + 60 * S
    inc = piece("0.8501", "1.7002", ms(t_inc) - 300, 9010, oid=7002, px="60000")
    await w.ingest(A, [inc], observed=t_inc, via="ws")
    clock.t = t_inc + 2 * S
    (e_inc,) = await w.flush_due()
    assert e_inc.side == "increase"
    price(engine, "BTC", t_inc + S, 60000)
    with Session(engine) as s:
        pr.run_paper(s, A, t_inc + 40 * S)
        snap = pr.snapshot(s, A)
        s.commit()
    (pos,) = snap["open_positions"]
    assert near(pos["qty"], q0 * D("1.5"), "1e-18")  # the first piece's r would give x2.7
    assert D(pos["leader_size"]) == D("2.5503")
    assert near(D(pos["ratio"]), q0 / D("1.7002"), "1e-18")

    # ... and a reduce of half the leader's position halves ours
    t_red = NOW + 120 * S
    red = piece("1.27515", "2.5503", ms(t_red) - 300, 9011, oid=7003, side="A", px="60000")
    await w.ingest(A, [red], observed=t_red, via="ws")
    clock.t = t_red + 2 * S
    await w.flush_due()
    price(engine, "BTC", t_red + S, 60000)
    with Session(engine) as s:
        pr.run_paper(s, A, t_red + 40 * S)
        snap = pr.snapshot(s, A)
    (pos,) = snap["open_positions"]
    assert near(pos["qty"], q0 * D("0.75"), "1e-18")
    assert D(pos["leader_size"]) == D("1.27515")
    assert [f["side"] for f in snap["fills"]] == ["open", "adjust", "increase", "reduce"]
    assert snap["missed"] == 0 and snap["n"] == 0 and snap["summary"]["closed_trades"] == 0
    assert [e.side for e in got] == ["open", "adjust", "increase", "reduce"]


async def test_late_piece_of_an_increase_order_is_an_increase_scaled_by_r(engine):
    clock = Clock(NOW)
    got = []
    w = watcher(engine, clock, on_fill=got.append)
    t = ms(NOW) - 300
    await w.ingest(A, [piece("4", "0", t, 1, oid=1)], observed=NOW, via="ws")
    await w.ingest(A, [piece("1", "4", t + 9000, 2, oid=2)], observed=NOW + 10 * S, via="ws")
    clock.t = NOW + 12 * S
    await w.flush_due()
    await w.ingest(A, [piece("1", "5", t + 14000, 3, oid=2)], observed=NOW + 15 * S, via="ws")
    clock.t = NOW + 17 * S
    await w.flush_due()
    assert [(e.side, e.size) for e in got] == [
        ("open", D(4)), ("increase", D(1)), ("increase", D(1))]
    res = simulate(got, StubModel(), PaperConfig(), START)
    (pos,) = res.open_positions.values()
    assert pos.qty == pos.ratio * 6 and pos.leader_size == D(6)  # each piece x the fixed r
    assert [f.side for f in res.fills] == ["open", "increase", "increase"]


async def test_a_stale_late_open_piece_is_still_an_adjust_and_never_missed(engine):
    """P47 + P69: a piece of the opening order recovered 20 min late is no trade we
    could miss; the leader did grow, so the ratio follows."""
    t_open = NOW - timedelta(minutes=30)
    clock = Clock(t_open)
    w = watcher(engine, clock, aggregate_window_s=0)
    o1 = piece("1", "0", ms(t_open) - 300, 1, oid=80)
    (opn,) = await w.ingest(A, [o1], observed=t_open, via="ws")
    o2 = piece("1", "1", ms(NOW - timedelta(minutes=20)), 2, oid=80)
    clock.t = NOW
    (adj,) = await w.ingest(A, [o2], observed=NOW, via="recovery")
    assert (opn.side, adj.side, adj.size) == ("open", "adjust", D(1))
    row = rows(engine)[1]
    assert row.missed is False and row.settled_at == NOW and row.json["stale_recovery"]
    assert w.stale_missed == 0
    price(engine, "BTC", t_open + S, 60000)
    with Session(engine) as s:
        pr.run_paper(s, A, NOW + timedelta(minutes=1))
        snap = pr.snapshot(s, A)
    (pos,) = snap["open_positions"]
    assert D(pos["leader_size"]) == D(2) and near(D(pos["ratio"]) * 2, pos["qty"])
    assert snap["missed"] == 0 and snap["missed_reasons"] == {}


# -- paper runtime ---------------------------------------------------------------------------

def test_gap_scales_the_leader_pnl_by_the_whole_opening_order(session):
    """P49 with a late open piece: our share of the leader's position is our qty over
    the opening order's full size, not over its first event's."""
    t = START + H

    def cpnl(side, at, value, **kw):
        e = store(session, side, at, coin="BTC", pid="BTC:1", **kw)
        row = session.get(PaperEvent, (A, e.source_ref))
        row.json = {**row.json, "leader_closed_pnl": str(value)}
        return e

    price(session, "BTC", t + S, 100)
    cpnl("open", t, 0, start=0, size="1")
    cpnl("adjust", t + 10 * S, 0, start=1, size="1")
    price(session, "BTC", t + H + S, 110)
    cpnl("close", t + H, 20, start=2, size="2")
    session.flush()
    summ = pr.run_paper(session, A, t + timedelta(days=1))
    snap = pr.snapshot(session, A)
    assert [(f["side"], f["missed"]) for f in snap["fills"]] == [
        ("open", False), ("adjust", False), ("close", False)]
    our_qty = D(snap["fills"][0]["size"])
    expected = D(20) * our_qty / 2 - summ.net_pnl_usd
    assert abs(summ.gap_usd - expected) < D("1e-9")
    assert summ.closed_trades == 1 and snap["missed"] == 0
