"""Ruling P73: the copy price window of an order starts no earlier than the moment we
could know the order was complete. The event's ``observed_time`` is the EMIT time (when
the aggregation buffer flushes: the 2 s window plus the timer tick after the last
piece, or the arrival of the piece that ended the order), not the last piece's arrival;
the price window is ``[emit + 1 s, emit + 30 s]``."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from hades_hl import paper_runtime as pr
from test_p65_sizing import Clock, Lev10, ms, piece, rows, smoke, watcher
from test_paper_runtime import A, START, price
from test_paper_runtime import engine, session  # noqa: F401  (fixtures)

NOW = START + timedelta(days=1)  # "t": when the LAST piece reaches us
S = timedelta(seconds=1)
EMIT = NOW + 2.25 * S  # the flush: 2 s window + 0.25 s timer tick
D = Decimal


async def emitted(engine):
    """The smoke's three-piece order: last piece observed at t, flushed at t + 2.25 s."""
    clock = Clock(NOW - 0.6 * S)
    w = watcher(engine, clock, Lev10(), fetch_leverage=True)
    for p in smoke(ms(NOW) - 900):
        assert await w.ingest(A, [p], observed=clock.t, via="ws") == []
        clock.t += 0.3 * S
    clock.t = EMIT
    (ev,) = await w.flush_due()
    return ev


def run(engine, at):
    with Session(engine) as s:
        pr.run_paper(s, A, at)
        snap = pr.snapshot(s, A)
        s.commit()
    return rows(engine)[0], snap


async def test_p73_observed_time_is_the_flush_not_the_last_piece(engine):
    ev = await emitted(engine)
    assert ev.observed_time == EMIT and ev.size == D("1.7002")
    (row,) = rows(engine)
    assert row.observed_time == EMIT
    # evidence kept: when the last piece reached us, and the feed delay of that piece
    assert row.json["last_piece_observed"] == ms(NOW)
    assert row.delay_ms == ms(NOW) - (ms(NOW) - 898)


async def test_p73_a_price_before_the_order_was_known_complete_is_not_used(engine):
    await emitted(engine)
    price(engine, "BTC", NOW + 1.5 * S, 70000)  # inside [t + 1 s, t + 30 s]: hindsight
    price(engine, "BTC", NOW + 3.3 * S, 60000)  # the first mid in [emit + 1 s, emit + 30 s]
    row, snap = run(engine, NOW + timedelta(minutes=1))
    assert row.missed is False and D(60000) < row.fill_price < D(60300)
    (pos,) = snap["open_positions"]
    assert D(pos["entry_price"]) == row.fill_price and snap["missed"] == 0


async def test_p73_an_event_settles_31_s_after_the_flush(engine):
    await emitted(engine)
    price(engine, "BTC", NOW + 3.3 * S, 60000)
    # 31 s after the LAST PIECE the window [emit + 1 s, emit + 30 s] is still open
    row, _ = run(engine, NOW + 32 * S)
    assert row.settled_at is None and row.fill_price is None
    row, _ = run(engine, EMIT + 31 * S)
    assert row.settled_at == EMIT + 31 * S and row.missed is False


async def test_p73_no_price_within_30_s_of_the_flush_is_missed(engine):
    await emitted(engine)
    price(engine, "BTC", NOW + 1.5 * S, 70000)   # before the window: never used
    price(engine, "BTC", EMIT + 30.05 * S, 60000)  # after it
    row, snap = run(engine, NOW + timedelta(minutes=2))
    assert row.missed is True and row.fill_price is None
    assert snap["open_positions"] == [] and snap["missed_reasons"] == {"no_price": 1}


@pytest.mark.parametrize("offset, used", [(0.999, False), (1, True), (30, True), (30.001, False)])
async def test_p73_window_edges_are_emit_plus_1_s_and_plus_30_s(engine, offset, used):
    await emitted(engine)
    price(engine, "BTC", EMIT + offset * S, 60000)
    row, _ = run(engine, NOW + timedelta(minutes=2))
    assert row.missed is (not used)


async def test_p73_an_order_ended_by_another_order_is_observed_when_that_piece_arrived(engine):
    """An opposite order on the same position ends a buffered order at once: the
    moment we know it is complete is that piece's arrival, not the timer."""
    clock = Clock(NOW)
    w = watcher(engine, clock)
    t = ms(NOW) - 500
    await w.ingest(A, [piece("1", "0", t, 1, oid=50)], observed=NOW, via="ws")
    red = piece("0.4", "1", t + 1, 2, oid=51, side="A")
    (opn,) = await w.ingest(A, [red], observed=NOW + 0.5 * S, via="ws")
    assert (opn.side, opn.observed_time) == ("open", NOW + 0.5 * S)
    assert rows(engine)[0].json["last_piece_observed"] == ms(NOW)


async def test_p73_emit_time_never_runs_against_seq(engine):
    """A WS batch received before a timer flush but stored after it (the leverage call
    in between) emits with an observed_time no earlier than the event already out."""
    clock = Clock(NOW)
    w = watcher(engine, clock)
    t = ms(NOW) - 500
    await w.ingest(A, [piece("2", "0", t, 1, oid=60, coin="ETH", px="3000")],
                   observed=NOW, via="ws")
    await w.ingest(A, [piece("1", "0", t + 1, 2, oid=61)], observed=NOW + 1 * S, via="ws")
    clock.t = NOW + 2.5 * S
    (eth,) = await w.flush_due()
    red = piece("0.4", "1", t + 2, 3, oid=62, side="A")
    (opn,) = await w.ingest(A, [red], observed=NOW + 2.2 * S, via="ws")
    assert (eth.seq, eth.observed_time) == (1, NOW + 2.5 * S)
    assert (opn.seq, opn.side, opn.observed_time) == (2, "open", NOW + 2.5 * S)
