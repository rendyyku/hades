"""Ruling P65: the fills of ONE leader order become ONE event before sizing (same
``oid``; without ``oid`` same coin + direction in the same millisecond; live pieces
are buffered up to 2 s), and the copy is sized on that event. The caps themselves are
core's (``core/tests/test_paper.py``); here: the head's aggregation, its bookkeeping
(dedupe, cursor, P46/P47) and what the paper run shows."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from hades_core.paper import PaperConfig, event_sort_key, simulate
from hades_hl import api, follows as fl
from hades_hl import paper_runtime as pr
from hades_hl import runtime as rt
from hades_hl.config import Settings
from hades_hl.db import Fill, PaperEvent, PaperPiece, init_db, insert_fills, make_engine
from hades_hl.live import FillWatcher
from hades_hl.normalize import (AGG_WINDOW_S, fill_legs, fills_to_events, group_legs,
                                normalize_fills, order_key)
from hades_hl.pipeline import fill_dict, kv_get_json
from hlfills import ADDR, T0, mk, ms_dt
from test_live import FailRecovery, StubModel
from test_live import FakeClient as LiveClient
from test_paper_runtime import A, START, price, store
from test_paper_runtime import engine, session  # noqa: F401  (fixtures)

UTC = timezone.utc
NOW = START + timedelta(days=1)
S = timedelta(seconds=1)
D = Decimal


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Lev10(LiveClient):
    async def clearinghouse_state(self, addr):
        return {"assetPositions": [{"position": {"coin": "BTC", "leverage": {"value": 10}}}]}


def piece(sz, start, t, tid, *, oid=7001, px="60000", coin="BTC", side="B", **kw):
    f = mk(coin, side, sz, px, start, t, tid=tid, **kw)
    if oid is None:
        del f["oid"]  # an old stored fill, or a source that does not send it
    else:
        f["oid"] = oid
    return f


def smoke(t, *, oid=7001, step_ms=1, tid=9001):
    """The live smoke's order: BTC long opened in pieces 0.0002 + 0.5 + 1.2."""
    return [piece("0.0002", "0", t, tid, oid=oid, px="60000"),
            piece("0.5", "0.0002", t + step_ms, tid + 1, oid=oid, px="60001"),
            piece("1.2", "0.5002", t + 2 * step_ms, tid + 2, oid=oid, px="60002")]


def watcher(engine, clock, client=None, **kw):
    kw.setdefault("fetch_leverage", False)
    kw.setdefault("sz_decimals", {})
    return FillWatcher(engine, client or LiveClient(), {A: START}, start_time=START,
                       now=clock, **kw)


def rows(engine):
    with Session(engine) as s:
        return list(s.scalars(select(PaperEvent).order_by(PaperEvent.seq)))


def notional(pos: dict) -> Decimal:
    return D(pos["qty"]) * D(pos["entry_price"])


def near(value, want, tol="1e-9") -> bool:
    return abs(D(value) - D(want)) < D(tol)


# -- historical path: fills_to_events ---------------------------------------------------

def test_same_oid_pieces_become_one_event():
    fills = smoke(T0, step_ms=40)
    (e,) = fills_to_events(ADDR, fills, leverage={"BTC": D(10)})
    assert (e.side, e.direction, e.market_id) == ("open", "long", "BTC")
    assert e.size == D("1.7002")
    assert e.notional_usd == D("0.0002") * 60000 + D("0.5") * 60001 + D("1.2") * 60002
    assert e.price == e.notional_usd / D("1.7002")  # size-weighted average
    assert D("60001") < e.price < D("60002")
    assert e.fee_usd == D("0.3") and e.leverage == D(10)
    assert e.source_ref == "0xh9001:9001"  # the first piece: the old format, unchanged
    assert e.position_id == "BTC:9001" and e.seq == 0
    assert e.leader_time == ms_dt(T0)  # the order started here
    assert e.observed_time == ms_dt(T0 + 80)  # the LAST piece we aggregate
    (g,) = group_legs(fill_legs(ADDR, fills))
    assert g.tids == [9001, 9002, 9003] and g.oid == "7001"  # the evidence
    assert g.refs == ["0xh9001:9001", "0xh9002:9002", "0xh9003:9003"]


def test_observed_time_fn_gives_the_last_piece_s_observed_time():
    later = lambda f: ms_dt(int(f["time"]) + 700)  # noqa: E731
    (e,) = fills_to_events(ADDR, smoke(T0), observed_time_fn=later)
    assert e.observed_time == ms_dt(T0 + 2 + 700) and e.leader_time == ms_dt(T0)


def test_without_oid_same_coin_and_direction_in_the_same_ms_is_one_event():
    same_ms = smoke(T0, oid=None, step_ms=0)
    (e,) = fills_to_events(ADDR, same_ms)
    assert e.side == "open" and e.size == D("1.7002")
    # ... but not across milliseconds: nothing says these are one order
    evs = fills_to_events(ADDR, smoke(T0, oid=None, step_ms=1))
    assert [(x.side, x.size) for x in evs] == [
        ("open", D("0.0002")), ("increase", D("0.5")), ("increase", D("1.2"))]


def test_single_fill_event_is_unchanged():
    f = mk("BTC", "B", "0.1", "100.5", "0", T0, tid=5)
    (e,) = fills_to_events(ADDR, [f])
    assert (e.size, e.price, e.notional_usd) == (D("0.1"), D("100.5"), D("0.1") * D("100.5"))
    assert e.source_ref == "0xh5:5" and e.leader_time == e.observed_time == ms_dt(T0)


def test_different_orders_are_not_merged():
    a = piece("1", "0", T0, 1, oid=10)
    b = piece("1", "1", T0, 2, oid=11)  # same ms, another order
    assert [e.size for e in fills_to_events(ADDR, [a, b])] == [D(1), D(1)]
    # P69: an order of another coin in between does NOT end the order (each order has
    # its own buffer); events come out by their last piece
    x1 = piece("1", "0", T0, 3, oid=20)
    y = piece("5", "0", T0 + 1, 4, oid=21, coin="ETH", px="3000")
    x2 = piece("1", "1", T0 + 2, 5, oid=20)
    evs = fills_to_events(ADDR, [x1, y, x2])
    assert [(e.market_id, e.side, e.size) for e in evs] == [
        ("ETH", "open", D(5)), ("BTC", "open", D(2))]


def test_pieces_more_than_the_window_apart_are_separate_events():
    assert AGG_WINDOW_S == 2.0
    gap = int(AGG_WINDOW_S * 1000)
    a, b, c = smoke(T0, step_ms=gap)  # exactly 2 s apart: still one order
    assert len(fills_to_events(ADDR, [a, b, c])) == 1
    a, b, c = smoke(T0, step_ms=gap + 1)
    # P69: late pieces of the order that opened the position adjust, they do not add
    assert [e.side for e in fills_to_events(ADDR, [a, b, c])] == ["open", "adjust", "adjust"]


def test_one_order_that_flips_gives_one_close_and_one_open():
    fills = [
        mk("BTC", "B", "1", "100", "0", T0, tid=1),
        piece("0.4", "1", T0 + 1000, 2, oid=30, side="A", px="110", closed_pnl="4"),
        piece("1", "0.6", T0 + 1001, 3, oid=30, side="A", px="111", closed_pnl="6.6"),
        piece("0.5", "-0.4", T0 + 1002, 4, oid=30, side="A", px="112"),
    ]
    evs = fills_to_events(ADDR, fills)
    assert [(e.side, e.direction, e.size, e.source_ref) for e in evs] == [
        ("open", "long", D(1), "0xh1:1"),
        ("close", "long", D(1), "0xh2:2"),          # reduce 0.4 + the flip's close 0.6
        ("open", "short", D("0.9"), "0xh3:3:open"),  # the flip's open 0.4 + increase 0.5
    ]
    assert evs[1].price == (D("0.4") * 110 + D("0.6") * 111) / D(1)
    assert evs[1].position_id == "BTC:1" and evs[2].position_id == "BTC:3f"
    assert [e.seq for e in evs] == [0, 1, 2]
    close, opn = group_legs(fill_legs(ADDR, fills))[1:]
    assert close.closed_pnl == D("10.6") and opn.closed_pnl == 0
    assert (close.start, close.end, opn.start, opn.end) == (D(1), 0, 0, D("-0.9"))
    # the simulator sees no phantom: a close and one open
    res = simulate(evs, StubModel(), PaperConfig(), ms_dt(T0 - 1000))
    assert list(res.open_positions) == ["BTC:3f"] and len(res.closed_trades) == 1


def test_reduce_pieces_are_one_reduce_and_ending_flat_one_close():
    opn = mk("ETH", "A", "3", "10", "0", T0, tid=1)
    red = [piece("1", "-3", T0 + 5000, 2, oid=40, coin="ETH", px="9"),
           piece("1", "-2", T0 + 5001, 3, oid=40, coin="ETH", px="9")]
    evs = fills_to_events(ADDR, [opn, *red])
    assert [(e.side, e.direction, e.size) for e in evs] == [
        ("open", "short", D(3)), ("reduce", "short", D(2))]
    last = piece("1", "-1", T0 + 5002, 4, oid=40, coin="ETH", px="9")
    evs = fills_to_events(ADDR, [opn, *red, last])
    assert [(e.side, e.size) for e in evs] == [("open", D(3)), ("close", D(3))]


def test_order_key_separates_position_side_and_order():
    legs = fill_legs(ADDR, [piece("1", "0", T0, 1, oid=5), piece("1", "1", T0, 2, oid=5),
                            piece("1", "2", T0, 3, oid=6),
                            piece("1", "3", T0, 4, oid=6, side="A")])
    k = [order_key(lg) for lg in legs]
    assert k[0] == k[1] and len({k[1], k[2], k[3]}) == 3


def test_real_fixture_aggregates_without_losing_a_leg(fixture):
    fills = fixture("user_fills_by_time.json")
    addr = fixture("fixture_meta.json")["address"]
    legs = normalize_fills(addr, fills).legs
    groups = group_legs(legs)
    evs = fills_to_events(addr, fills)
    assert len(evs) == len(groups) < len(legs)  # real orders do fill in pieces
    assert sum(len(g.legs) for g in groups) == len(legs)
    # P69: orders that fill in turns are merged across each other; no leg is lost
    assert sorted(id(lg) for g in groups for lg in g.legs) == sorted(id(lg) for lg in legs)
    assert len({e.source_ref for e in evs}) == len(evs)
    for g, e in zip(groups, evs):
        assert e.size == sum(lg.size for lg in g.legs)
        assert e.notional_usd == sum(lg.size * lg.price for lg in g.legs)
        assert len({order_key(lg) for lg in g.legs}) == 1
        assert min(lg.price for lg in g.legs) <= e.price <= max(lg.price for lg in g.legs)
    # core's processing order is the emitted order
    resorted = sorted(evs, key=event_sort_key)
    assert [e.source_ref for e in resorted] == [e.source_ref for e in evs]
    # an episode still starts with its open
    first: dict[str, str] = {}
    for e in evs:
        first.setdefault(e.position_id, e.side)
    assert {s for pid, s in first.items() if ":pre" not in pid and ":gap" not in pid} == {"open"}


# -- storage ----------------------------------------------------------------------------

def test_fills_store_and_return_the_oid(engine):
    with Session(engine) as s:
        insert_fills(s, A, [piece("1", "0", ms(NOW), 1, oid=611184666209),
                            piece("1", "1", ms(NOW), 2, oid=None)])
        s.commit()
        got = {r.tid: fill_dict(r) for r in s.scalars(select(Fill))}
    assert got[1]["oid"] == 611184666209 and "oid" not in got[2]


def test_old_database_gains_the_oid_column_and_the_pieces_table(tmp_path):
    path = tmp_path / "old.db"
    eng = make_engine(f"sqlite:///{path.as_posix()}")
    init_db(eng)
    eng.dispose()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE fills DROP COLUMN oid")
    con.execute("DROP TABLE paper_event_pieces")
    con.execute("INSERT INTO fills (address, tid, coin, px, sz, side, dir, start_position, "
                "closed_pnl, fee, time, hash, crossed, liquidation) VALUES "
                "(?, 1, 'BTC', '100', '1', 'B', 'Open Long', '0', '0', '0', 5, '0xh', 1, 0)",
                (A,))
    con.commit()
    con.close()
    eng = make_engine(f"sqlite:///{path.as_posix()}")
    init_db(eng)
    insp = inspect(eng)
    assert "oid" in {c["name"] for c in insp.get_columns("fills")}
    assert "paper_event_pieces" in insp.get_table_names()
    with Session(eng) as s:
        assert "oid" not in fill_dict(s.scalars(select(Fill)).one())  # old row: unknown


# -- live path: the smoke scenario -------------------------------------------------------

async def test_smoke_split_order_is_one_copy_of_5pct_margin_times_leverage(engine):
    """The live smoke's bug: 0.0002 + 0.5 + 1.2 BTC, one order at 10x. Before P65 the
    copy was sized on the 0.0002 piece and scaled up to ~$1.7M; now it is ONE open of
    5% x equity x 10 = $5,000."""
    clock = Clock(NOW)
    got = []
    w = watcher(engine, clock, Lev10(), fetch_leverage=True, on_fill=got.append)
    p1, p2, p3 = smoke(ms(NOW) - 300)
    assert await w.ingest(A, [p1], observed=clock.t, via="ws") == []
    clock.t += 0.3 * S
    assert await w.ingest(A, [p2], observed=clock.t, via="ws") == []
    clock.t += 0.3 * S
    last = clock.t
    assert await w.ingest(A, [p3], observed=last, via="ws") == []
    assert got == [] and rows(engine) == [] and w.has_buffered(A)
    clock.t = last + 1.9 * S
    assert await w.flush_due() == []  # the order may still be filling
    emit = clock.t = last + AGG_WINDOW_S * S
    (ev,) = await w.flush_due()
    assert got == [ev] and not w.has_buffered(A)
    assert (ev.side, ev.size, ev.leverage) == ("open", D("1.7002"), D(10))
    assert ev.observed_time == emit  # P73: when the buffer flushed, not the last piece
    assert ev.leader_time == ms_dt(ms(NOW) - 300)
    assert ev.source_ref == "0xh9001:9001" and ev.seq == 1
    (row,) = rows(engine)
    assert row.observed_time == emit and row.delay_ms == ms(last) - (ms(NOW) - 298)
    js = row.json
    assert js["tids"] == [9001, 9002, 9003] and js["oid"] == "7001"
    assert [p["ref"] for p in js["pieces"]] == ["0xh9001:9001", "0xh9002:9002", "0xh9003:9003"]
    assert [p["sz"] for p in js["pieces"]] == ["0.0002", "0.5", "1.2"]
    assert (js["leader_start"], js["leader_end"]) == ("0", "1.7002")
    with Session(engine) as s:
        assert sorted(s.scalars(select(PaperPiece.piece_ref))) == [
            "0xh9001:9001", "0xh9002:9002", "0xh9003:9003"]
    # our price rule is unchanged (P45) and keyed to the event's observed_time (P73:
    # the emit time)
    price(engine, "BTC", emit + S, 60000)
    with Session(engine) as s:
        pr.run_paper(s, A, last + timedelta(minutes=1))
        snap = pr.snapshot(s, A)
    (pos,) = snap["open_positions"]
    cap = D("0.05") * D(10000) * 10
    assert near(notional(pos), cap) and notional(pos) <= cap + D("1e-9")
    assert D(pos["margin"]) == D(500) and D(pos["leverage"]) == 10
    assert D(pos["leader_size"]) == D("1.7002")
    assert near(D(pos["ratio"]) * D("1.7002"), pos["qty"])
    assert snap["events"] == 1 and snap["missed"] == 0 and snap["missed_reasons"] == {}
    assert D(snap["summary"]["equity_usd"]) > D(9990)  # one fee; not the smoke's -40%


async def test_smoke_split_order_without_oid_falls_back_to_the_same_millisecond(engine):
    clock = Clock(NOW)
    w = watcher(engine, clock, Lev10(), fetch_leverage=True)
    pieces = smoke(ms(NOW) - 300, oid=None, step_ms=0)
    assert await w.ingest(A, pieces, observed=NOW, via="ws") == []
    clock.t = NOW + 2 * S
    (ev,) = await w.flush_due()
    assert ev.side == "open" and ev.size == D("1.7002")
    assert rows(engine)[0].json["oid"] is None
    price(engine, "BTC", NOW + 3 * S, 60000)  # P73: emitted at NOW + 2 s
    with Session(engine) as s:
        pr.run_paper(s, A, NOW + timedelta(minutes=1))
        (pos,) = pr.snapshot(s, A)["open_positions"]
    assert near(notional(pos), 5000)


async def test_unmerged_pieces_are_still_bounded_by_the_caps(engine, session):
    """Rows stored before P65 (one event per piece) are not rewritten (P52); the
    core caps bound their copy at 20% margin and the snapshot says why."""
    t = START + timedelta(hours=1)
    lev = D(10)
    store(session, "open", t, size="0.0002", pid="BTC:1", start=0, leverage=lev)
    store(session, "increase", t + S, size="0.5", pid="BTC:1", start="0.0002", leverage=lev)
    store(session, "increase", t + 2 * S, size="1.2", pid="BTC:1", start="0.5002", leverage=lev)
    for i in (1, 2, 3):  # a recorded price in each event's own window
        price(session, "BTC", t + i * S, 60000)
    pr.run_paper(session, A, t + timedelta(minutes=5))
    snap = pr.snapshot(session, A)
    (pos,) = snap["open_positions"]
    # 20% of the equity when the margin was added (fees had already lowered it)
    assert D(1990) < D(pos["margin"]) < D("0.20") * D(10000)
    assert notional(pos) < D("0.20") * D(10000) * 10  # under $20k, not $1.7M
    assert D(snap["summary"]["equity_usd"]) > D(9950)
    assert snap["missed"] == 2 and snap["missed_reasons"] == {"cap": 2}
    assert [(f["ref"], f["reason"]) for f in snap["fills"] if f["missed"]] == [
        ("0xh2:2", "cap"), ("0xh3:3", "cap")]
    copied = [f for f in snap["fills"] if not f["missed"]]
    assert all(f["reason"] is None for f in copied)
    assert sum(D(f["size"]) * D(f["price"]) for f in copied) < D(20000)


def test_snapshot_and_api_split_missed_by_reason(tmp_path):
    from fastapi.testclient import TestClient

    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    app = api.create_app(Settings(API_KEY="k", DATABASE_URL=url))
    t = START + timedelta(hours=1)
    with Session(app.state.engine) as s:
        fl.add_follow(s, A, START)
        price(s, "BTC", t + S, 100)
        price(s, "BTC", t + 2 * S, 100)
        store(s, "open", t, size="1", pid="BTC:1", start=0)
        store(s, "increase", t + S, size="9", pid="BTC:1", start=1)  # beyond the cap
        store(s, "open", t + timedelta(hours=2), coin="ETH", pid="ETH:1", start=0)  # no price
        stale = store(s, "open", t + timedelta(hours=3), coin="SOL", pid="SOL:1", start=0)
        row = s.get(PaperEvent, (A, stale.source_ref))
        row.json = {**row.json, "stale_recovery": True}
        row.missed, row.settled_at = True, t + timedelta(hours=3)
        pr.run_paper(s, A, START + timedelta(days=1))
        snap = pr.snapshot(s, A)
        s.commit()
    assert snap["missed_reasons"] == {"cap": 1, "no_price": 1, "stale": 1}
    assert snap["missed"] == 3
    p = TestClient(app).get(f"/api/paper/{A}", headers={"X-Hades-Key": "k"}).json()
    assert p["missed_count"] == 3
    assert p["missed_reasons"] == {"cap": 1, "no_price": 1, "stale": 1}


# -- live path: the buffer ---------------------------------------------------------------

async def test_an_order_of_another_coin_does_not_flush_the_buffer(engine):
    """P69 (was: a different order flushes the buffer at once): each order has its own
    buffer and its own 2 s."""
    clock = Clock(NOW)
    w = watcher(engine, clock)
    t = ms(NOW) - 500
    a1 = piece("1", "0", t, 1, oid=50)
    a2 = piece("1", "1", t + 2, 3, oid=50)
    b = piece("2", "0", t + 1, 2, oid=51, coin="ETH", px="3000")
    await w.ingest(A, [a1], observed=NOW, via="ws")
    assert await w.ingest(A, [b], observed=NOW + 0.5 * S, via="ws") == []
    obs_a2 = NOW + 1 * S
    assert await w.ingest(A, [a2], observed=obs_a2, via="ws") == []  # still one BTC order
    clock.t = NOW + 2.5 * S
    (ev,) = await w.flush_due()  # the ETH order: 2 s after ITS last piece
    assert (ev.market_id, ev.side, ev.size, ev.seq) == ("ETH", "open", D(2), 1)
    assert ev.observed_time == NOW + 2.5 * S and w.has_buffered(A)  # P73: the flush
    clock.t = NOW + 3 * S
    (ev2,) = await w.flush_due()
    assert (ev2.market_id, ev2.side, ev2.size, ev2.seq) == ("BTC", "open", D(2), 2)
    assert ev2.observed_time == NOW + 3 * S
    assert rows(engine)[1].json["last_piece_observed"] == ms(obs_a2)
    assert [r.seq for r in rows(engine)] == [1, 2]


async def test_a_piece_arriving_after_the_window_is_a_separate_event(engine):
    clock = Clock(NOW)
    w = watcher(engine, clock)  # the timer is not running: nothing flushed the buffer
    a, b, c = smoke(ms(NOW) - 300)
    await w.ingest(A, [a], observed=NOW, via="ws")
    out = await w.ingest(A, [b], observed=NOW + 2.001 * S, via="ws")
    assert [(e.side, e.size) for e in out] == [("open", D("0.0002"))]
    clock.t = NOW + 10 * S
    (late,) = await w.flush_due()
    assert (late.side, late.size) == ("adjust", D("0.5"))  # P69: no second copy trade


async def test_window_zero_flushes_every_ingest_but_still_merges_a_batch(engine):
    w = watcher(engine, Clock(NOW), aggregate_window_s=0)
    out = await w.ingest(A, smoke(ms(NOW) - 300), observed=NOW, via="ws")
    assert [(e.side, e.size) for e in out] == [("open", D("1.7002"))]
    assert not w.has_buffered(A)


async def test_redelivered_pieces_are_not_emitted_or_buffered_twice(engine):
    clock = Clock(NOW)
    c = LiveClient()
    w = watcher(engine, clock, c)
    pieces = smoke(ms(NOW) - 300)
    await w.ingest(A, pieces[:2], observed=NOW, via="ws")
    await w.ingest(A, pieces[:2], observed=NOW + 0.1 * S, via="poll")  # buffered already
    await w.ingest(A, pieces[2:], observed=NOW + 0.2 * S, via="ws")
    clock.t = NOW + 3 * S
    (ev,) = await w.flush_due()
    assert ev.size == D("1.7002")
    assert rows(engine)[0].json["tids"] == [9001, 9002, 9003]
    # a recovery re-delivers every piece: the later ones are known by the pieces table
    c.fills[A] = pieces
    await w._recover([A])
    clock.t = NOW + 10 * S
    assert await w.flush_due() == [] and len(rows(engine)) == 1


async def test_cursor_never_passes_a_buffered_piece_and_a_restart_recovers_it(engine):
    clock = Clock(NOW)
    c = LiveClient()
    w = watcher(engine, clock, c)
    t = ms(NOW) - 300
    pieces = smoke(t)
    await w.ingest(A, pieces[:2], observed=NOW, via="ws")
    assert w.last_seen_ms[A] == t  # not t + 1: the pieces are not emitted yet
    with Session(engine) as s:
        assert kv_get_json(s, f"hl:last_seen:{A}") == t
    # the process dies here; the pieces are stored fills but no event
    assert rows(engine) == []
    c.fills[A] = pieces
    clock2 = Clock(NOW + timedelta(minutes=1))
    got = []
    w2 = watcher(engine, clock2, c, on_fill=got.append)
    await w2._recover([A])
    clock2.t += 2 * S
    await w2.flush_due()
    (ev,) = got
    # P73: observed when the buffer flushed, 2 s after the recovery response
    assert ev.size == D("1.7002") and ev.observed_time == NOW + timedelta(minutes=1) + 2 * S
    assert w2.last_seen_ms[A] == t + 2
    with Session(engine) as s:
        assert kv_get_json(s, f"hl:last_seen:{A}") == t + 2


async def test_stopping_the_watcher_emits_what_is_buffered(engine):
    clock = Clock(NOW)
    got = []
    w = watcher(engine, clock, ws_max_user_subs=0, poll_interval_s=30, on_fill=got.append)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.05)
    await w.ingest(A, smoke(ms(NOW) - 300), observed=NOW, via="ws")
    assert got == []
    stop.set()
    await asyncio.wait_for(task, 5)
    assert [e.size for e in got] == [D("1.7002")] and len(rows(engine)) == 1


async def test_the_timer_flushes_a_quiet_buffer(engine):
    t0 = datetime.now(UTC)
    got = []
    w = FillWatcher(engine, LiveClient(), {A: START}, start_time=START, fetch_leverage=False,
                    sz_decimals={}, ws_max_user_subs=0, poll_interval_s=30,
                    aggregate_window_s=0.2, on_fill=got.append)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    try:
        await w.ingest(A, smoke(ms(t0) - 300), observed=datetime.now(UTC), via="ws")
        assert got == []
        for _ in range(100):
            if got:
                break
            await asyncio.sleep(0.05)
        assert [e.size for e in got] == [D("1.7002")]
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


# -- live path: P46 / P47 / follow start still hold --------------------------------------

async def test_p46_release_aggregates_and_keeps_chain_order(engine):
    """Gap fills + held live fills come out as ONE chain-ordered batch; inside it the
    pieces of an order are one event; seq follows the leader's order."""
    clock = Clock(NOW)
    c = FailRecovery()
    got = []
    w = watcher(engine, clock, c, aggregate_window_s=0, on_fill=got.append)
    t = ms(NOW) - 60_000
    o1, o2, o3 = smoke(t)                                             # the gap: an open
    r1 = piece("0.7002", "1.7002", t + 5000, 20, oid=7002, side="A", px="60100")
    r2 = piece("1", "1", t + 5001, 21, oid=7002, side="A", px="60101")  # held: a close
    await w._recover([A])  # fails: the floor is set
    assert A in w.recovery_floor_ms
    assert await w.ingest(A, [r1, r2], observed=NOW, via="ws") == [] and got == []
    c.fills[A] = [o1, o2, o3, r1, r2]
    clock.t = NOW + 30 * S
    await w._recover([A])
    assert [(e.side, e.size, e.seq) for e in got] == [
        ("open", D("1.7002"), 1), ("close", D("1.7002"), 2)]
    assert {e.observed_time for e in got} == {NOW + 30 * S}  # the release time (P44)
    assert A not in w.recovery_floor_ms and w.last_seen_ms[A] == t + 5001
    res = simulate(got, StubModel(), PaperConfig(), START)
    assert res.open_positions == {} and len(res.closed_trades) == 1  # no phantom


async def test_p47_stale_pieces_are_missed_and_not_merged_with_fresh_ones(engine):
    clock = Clock(NOW)
    w = watcher(engine, clock, aggregate_window_s=0)
    old = ms(NOW - timedelta(minutes=20))
    fresh = ms(NOW) - 1000
    pieces = [piece("1", "0", old, 1, oid=60), piece("1", "1", old + 1, 2, oid=60),
              piece("1", "2", fresh, 3, oid=60)]  # a resting order, filled over 20 min
    out = await w.ingest(A, pieces, observed=NOW, via="recovery")
    # P69: the fresh piece belongs to the order that opened: an adjust
    assert [(e.side, e.size) for e in out] == [("open", D(2)), ("adjust", D(1))]
    r_old, r_new = rows(engine)
    assert r_old.missed is True and r_old.settled_at == NOW and r_old.json["stale_recovery"]
    assert r_new.missed is None and r_new.settled_at is None
    assert w.stale_missed == 1
    price(engine, "BTC", NOW + S, 60000)
    with Session(engine) as s:
        pr.run_paper(s, A, NOW + timedelta(minutes=1))
        snap = pr.snapshot(s, A)
    # the open was missed, so the fresh piece is not ours either: no position
    assert snap["open_positions"] == [] and snap["missed_reasons"] == {"stale": 1}


async def test_order_straddling_the_follow_start_is_never_copied(engine):
    """Pieces before the follow start are not ours; the later pieces of that order
    belong to a position opened before we followed and stay ignored."""
    clock = Clock(START + timedelta(seconds=5))
    w = watcher(engine, clock, aggregate_window_s=0)
    t = ms(START)
    before = piece("0.0002", "0", t - 2, 1, oid=70)
    after = [piece("0.5", "0.0002", t + 1, 2, oid=70), piece("1.2", "0.5002", t + 2, 3, oid=70)]
    out = await w.ingest(A, [before, *after], observed=clock.t, via="ws")
    (ev,) = out
    assert (ev.side, ev.size) == ("increase", D("1.7")) and ":pre" in ev.position_id
    price(engine, "BTC", clock.t + S, 60000)
    with Session(engine) as s:
        pr.run_paper(s, A, clock.t + timedelta(minutes=1))
        snap = pr.snapshot(s, A)
    assert snap["open_positions"] == [] and snap["fills"] == []


def test_reconcile_waits_while_an_order_is_buffered():
    class W:
        recovery_floor_ms: dict = {}

        def has_buffered(self, addr):
            return addr == A

    r = rt.HLRuntime.__new__(rt.HLRuntime)
    r.watcher = W()
    assert r._pending(A) is True and r._pending("0xother") is False
    r.watcher = None
    assert r._pending(A) is False
