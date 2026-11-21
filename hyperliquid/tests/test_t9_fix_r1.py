"""Task 9 fix round 1: settle-once paper prices and persisted hourly marks (P52, P48),
the all-coin recorder (P53), reconcile / snapshot / supervision fixes (I2-I4), the
recovery max age (P47), the per-position gap (P49), daily backfill (P50), re-follow
ladder history and alert watermark (P51), and the minors."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_core.events import TradeEvent
from hades_hl import follows as fl
from hades_hl import live
from hades_hl import paper_runtime as pr
from hades_hl import runtime as rt
from hades_hl.db import (Alert, Follow, Kv, LadderHistory, Mark, PaperEvent, Price, Wallet,
                         init_db, make_engine)
from hades_hl.live import FillWatcher, HLExecutionModel, PriceRecorder
from hades_hl.pipeline import kv_get_json
from hlfills import mk
from test_live import FakeClient as LiveClient
from test_paper_runtime import A, START, FakeClient, price, round_trip, store
from test_paper_runtime import engine, session  # noqa: F401  (fixtures)
from test_runtime import Clock, FakeRecorder, FakeWatcher, runtime

UTC = timezone.utc
H = timedelta(hours=1)
B = "0x" + "b2" * 20


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def fill_prices(snap: dict) -> list:
    return [(f["ref"], f["price"], f["missed"]) for f in snap["fills"]]


# -- C1 / P52: settle once, persist --------------------------------------------------

def test_c1_pruned_prices_do_not_rewrite_closed_trade(engine, session):
    """Reviewer scenario: a trade closes, the raw prices are pruned at +20 d, re-run:
    the closed trade and the net are unchanged."""
    round_trip(engine, session, 1, START + H)
    first = pr.run_paper(session, A, START + timedelta(days=1))
    snap1 = pr.snapshot(session, A)
    session.commit()
    assert first.closed_trades == 1 and first.net_pnl_usd > 0
    t20 = START + timedelta(days=20)
    rt.prune_prices(session, t20)
    session.commit()
    assert session.scalars(select(Price)).all() == []
    again = pr.run_paper(session, A, t20)
    snap2 = pr.snapshot(session, A)
    assert again.closed_trades == 1
    assert again.net_pnl_usd == first.net_pnl_usd
    assert again.equity_usd == first.equity_usd
    assert fill_prices(snap2) == fill_prices(snap1)
    assert snap2["missed"] == 0


def test_event_settled_once_and_never_repriced(engine, session):
    t = START + H
    price(session, "BTC", t + timedelta(seconds=1), 100)
    e = store(session, "open", t, start=0)
    pr.run_paper(session, A, t + timedelta(minutes=1))
    row = session.get(PaperEvent, (A, e.source_ref))
    assert row.settled_at == t + timedelta(minutes=1)
    assert row.missed is False and row.fill_price is not None
    stored = row.fill_price
    # the raw price changes (or disappears): the settled price is kept
    session.execute(Price.__table__.delete())
    price(session, "BTC", t + timedelta(seconds=2), 200)
    pr.run_paper(session, A, t + timedelta(hours=2))
    row = session.get(PaperEvent, (A, e.source_ref))
    assert row.fill_price == stored and row.settled_at == t + timedelta(minutes=1)
    assert pr.snapshot(session, A)["fills"][0]["price"] == str(stored)


def test_missed_settle_is_final_and_young_events_wait(engine, session):
    t = START + H
    e = store(session, "open", t, start=0)  # no price in the window
    young = store(session, "open", t + timedelta(minutes=10), coin="ETH", pid="ETH:1", start=0)
    now = t + timedelta(minutes=10, seconds=20)  # ETH's window is not over yet
    pr.run_paper(session, A, now)
    r = session.get(PaperEvent, (A, e.source_ref))
    assert r.missed is True and r.fill_price is None and r.settled_at == now
    y = session.get(PaperEvent, (A, young.source_ref))
    assert y.settled_at is None
    assert pr.snapshot(session, A)["events"] == 1
    # a late price row inside the missed event's window changes nothing
    price(session, "BTC", t + timedelta(seconds=5), 100)
    price(session, "ETH", t + timedelta(minutes=10, seconds=2), 10)
    pr.run_paper(session, A, t + timedelta(minutes=12))
    assert session.get(PaperEvent, (A, e.source_ref)).missed is True
    y = session.get(PaperEvent, (A, young.source_ref))
    assert y.settled_at is not None and y.missed is False
    snap = pr.snapshot(session, A)
    assert snap["events"] == 2 and snap["missed"] == 1
    assert [p["market_id"] for p in snap["open_positions"]] == ["ETH"]


def test_settle_window_is_31_seconds(engine, session):
    t = START + H
    e = store(session, "open", t, start=0)
    pr.run_paper(session, A, t + timedelta(seconds=30))
    assert session.get(PaperEvent, (A, e.source_ref)).settled_at is None
    price(session, "BTC", t + timedelta(seconds=30), 100)  # last moment of the window
    pr.run_paper(session, A, t + timedelta(seconds=31))
    r = session.get(PaperEvent, (A, e.source_ref))
    assert r.settled_at is not None and r.missed is False


def test_hourly_marks_persisted_and_used_after_prune(engine, session):
    """P48 + P52: marks for every hour with a recorded price are persisted; the curve
    shows the unrealized drawdown between events, also after the raw prices are gone."""
    t = START + H + timedelta(minutes=5)
    price(session, "BTC", t + timedelta(seconds=1), 100)
    store(session, "open", t, start=0, leverage=Decimal("1"))
    for h, mid in ((2, 100), (3, 80), (4, 100), (5, 100)):  # a dip at 03:00
        price(session, "BTC", START + h * H - timedelta(minutes=1), mid)
    now = START + timedelta(hours=5, minutes=30)
    pr.run_paper(session, A, now)
    session.commit()
    marks = {(m.coin, m.hour_ts): m.mid for m in session.scalars(select(Mark))}
    assert marks[("BTC", ms(START + 3 * H))] == Decimal("80")
    assert ("BTC", ms(START + 2 * H)) in marks and ("BTC", ms(START + 5 * H)) in marks
    snap1 = pr.snapshot(session, A)
    curve1 = dict(snap1["equity_curve"])
    at3 = (START + 3 * H).isoformat()
    assert Decimal(curve1[at3]) < Decimal("9910")  # 5 BTC x -20 = -100 unrealized
    dd1 = Decimal(snap1["gate"]["values"]["max_dd"])
    assert dd1 > Decimal("0.009")
    rt.prune_prices(session, START + timedelta(days=20))
    session.commit()
    pr.run_paper(session, A, now)
    snap2 = pr.snapshot(session, A)
    assert dict(snap2["equity_curve"])[at3] == curve1[at3]
    assert Decimal(snap2["gate"]["values"]["max_dd"]) == dd1


def test_prune_keeps_two_days_of_raw_prices_only(engine, session):
    now = START + timedelta(days=10)
    session.add_all([Price(coin="BTC", time=ms(now - timedelta(days=3)), mid="1"),
                     Price(coin="BTC", time=ms(now - timedelta(days=1)), mid="2"),
                     Mark(coin="BTC", hour_ts=ms(now - timedelta(days=9)), mid="3")])
    session.flush()
    assert rt.PRICE_KEEP_DAYS == 2
    assert rt.prune_prices(session, now) == 1
    assert [p.mid for p in session.scalars(select(Price))] == [Decimal("2")]
    assert len(session.scalars(select(Mark)).all()) == 1  # marks are persistent


def test_init_db_migrates_old_paper_events(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE paper_events (source_ref VARCHAR PRIMARY KEY, address VARCHAR, "
                "json JSON, observed_time DATETIME, delay_ms INTEGER, seq INTEGER)")
    con.execute("INSERT INTO paper_events VALUES ('r1', 'a', '{}', '2026-10-01 00:00:00', 1, 1)")
    con.commit()
    con.close()
    eng = make_engine(f"sqlite:///{path.as_posix()}")
    init_db(eng)
    init_db(eng)  # idempotent
    with eng.connect() as c:
        cols = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(paper_events)")}
        tables = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master")}
    assert {"fill_price", "missed", "settled_at"} <= cols
    assert "marks" in tables
    with Session(eng) as s:
        r = s.get(PaperEvent, ("a", "r1"))
        assert r.settled_at is None and r.fill_price is None and r.seq == 1


# -- I1 / P53: record every coin --------------------------------------------------------

def test_recorder_records_unwatched_coins_at_most_every_5_s(engine):
    t = START
    rec = PriceRecorder(engine, coins=lambda: {"BTC"}, other_interval_s=5.0)
    assert rec.record({"BTC": "100", "SOL": "10", "@1": "1"}, at=t) == 2
    assert rec.record({"BTC": "101", "SOL": "11"}, at=t + timedelta(seconds=1)) == 1
    assert rec.record({"BTC": "102", "SOL": "12"}, at=t + timedelta(seconds=5)) == 2
    with Session(engine) as s:
        sol = s.scalars(select(Price.time).where(Price.coin == "SOL")).all()
    assert sol == [ms(t), ms(t + timedelta(seconds=5))]


def test_fill_in_unwatched_coin_gets_a_price_within_window(engine):
    rec = PriceRecorder(engine, coins=lambda: {"BTC", "ETH"}, other_interval_s=5.0)
    obs = START + H
    for i in range(0, 31):
        rec.record({"BTC": "100", "WIF": str(2 + i / 100)}, at=obs + timedelta(seconds=i))
    ev = TradeEvent(head="hyperliquid", wallet=A, market_id="WIF", side="open",
                    direction="long", size=Decimal(1), price=Decimal(2),
                    notional_usd=Decimal(2), fee_usd=Decimal(0), leader_time=obs,
                    observed_time=obs, source_ref="x:1", position_id="WIF:1", seq=1)
    with Session(engine) as s:
        assert HLExecutionModel(pr.RecordedPrices(s)).fill_price(ev) is not None


def test_runtime_recorder_records_all_coins_and_on_fill_watches_coin(engine):
    r = runtime(engine, Clock(START))
    rec = r._default_recorder(lambda: r.watch_set)
    assert rec.other_min_interval_ms == 5000
    e = TradeEvent(head="hyperliquid", wallet=A, market_id="kPEPE", side="open",
                   direction="long", size=Decimal(1), price=Decimal(1),
                   notional_usd=Decimal(1), fee_usd=Decimal(0), leader_time=START,
                   observed_time=START, source_ref="x:2", position_id="kPEPE:1", seq=1)
    r.on_fill(e)
    assert "kPEPE" in r.watch_set


# -- I2: reconcile duplicates -----------------------------------------------------------

def _open_copy(session, t=START + H):
    price(session, "BTC", t + timedelta(seconds=1), 100)
    store(session, "open", t, coin="BTC", pid="BTC:1", start=0, size="2")
    pr.run_paper(session, A, t + timedelta(minutes=1))
    session.commit()


async def test_reconcile_skips_coin_with_unsettled_live_close(engine, session):
    _open_copy(session)
    now = START + 2 * H
    store(session, "close", now - timedelta(seconds=10), pid="BTC:1", start=2, size="2")
    session.commit()
    c = FakeClient()  # leader flat
    assert await pr.reconcile(session, c, A, now) == []


async def test_reconcile_skips_coin_with_event_after_snapshot(engine, session):
    _open_copy(session)
    now = START + 2 * H
    # settled by an earlier run that did not snapshot it (e.g. a crash): seq > last_seq
    e = store(session, "close", now - timedelta(minutes=5), pid="BTC:1", start=2, size="2")
    row = session.get(PaperEvent, (A, e.source_ref))
    row.settled_at, row.missed = now - timedelta(minutes=4), True
    session.commit()
    assert await pr.reconcile(session, FakeClient(), A, now) == []


# -- I3 + P51: re-follow ----------------------------------------------------------------

def test_refollow_clears_snapshot_sets_watermark_and_history(engine, session):
    _open_copy(session)
    f = session.get(Follow, A)
    f.ladder = "NOTIF"
    assert pr.snapshot(session, A)["open_positions"]
    t_rm = START + timedelta(days=1)
    fl.remove_follow(session, A, t_rm)
    assert session.get(Follow, A).ladder == "NOTIF"  # kept at removal
    # events of the gap (the old follow's watcher, a late write)
    store(session, "close", t_rm + H, pid="BTC:1", start=2, size="2")
    t_re = START + timedelta(days=2)
    fl.add_follow(session, A, t_re)
    session.commit()
    assert pr.snapshot(session, A) is None
    assert kv_get_json(session, fl.KV_FOLLOW_SEQ.format(A)) == 2
    assert fl.has_open_positions(session, A) is False
    h = session.scalars(select(LadderHistory).where(LadderHistory.address == A)
                        .order_by(LadderHistory.id.desc())).first()
    assert (h.from_, h.to, h.at) == ("NOTIF", "PAPER", t_re)
    # the watermark: no alert for events from before the follow start (events are
    # judged at the ladder before the run, so NOTIF is set before each run)
    session.get(Follow, A).ladder = "NOTIF"
    pr.run_paper(session, A, t_re + H)
    assert session.scalars(select(Alert).where(Alert.kind.in_(["open", "close"]))).all() == []
    price(session, "ETH", t_re + 2 * H + timedelta(seconds=1), 10)
    store(session, "open", t_re + 2 * H, coin="ETH", pid="ETH:1", start=0)
    session.get(Follow, A).ladder = "NOTIF"
    pr.run_paper(session, A, t_re + 3 * H)
    kinds = [a.kind for a in session.scalars(select(Alert).where(Alert.kind != "ladder"))]
    assert kinds == ["open"]


# -- I4: supervision ------------------------------------------------------------------------

class DyingWatcher(FakeWatcher):
    async def run(self, stop):
        raise RuntimeError("boom")


async def test_dead_watcher_task_is_restarted_on_next_sync(engine):
    with Session(engine) as s:
        fl.add_follow(s, A, START)
        s.commit()
    r = runtime(engine, Clock(START),
                watcher_factory=lambda follows, on_fill: DyingWatcher(follows, on_fill))
    await r.sync_watcher()
    await asyncio.sleep(0.01)
    assert r._watcher_task.done()
    await r.sync_watcher()
    assert r.watcher_starts == 2 and len(FakeWatcher.instances) == 2
    await r.stop_watcher()


class DyingRecorder(FakeRecorder):
    made = 0

    def __init__(self, coins):
        super().__init__(coins)
        DyingRecorder.made += 1

    async def run(self, stop):
        if DyingRecorder.made == 1:
            raise RuntimeError("recorder died")
        await super().run(stop)


async def test_dead_recorder_restarted_and_job_errors_keep_runtime_alive(engine):
    DyingRecorder.made = 0
    r = runtime(engine, Clock(START), recorder_factory=DyingRecorder, paper_interval_s=0.01,
                reconcile_tick_s=0.01, watch_refresh_s=0.01, run_daily=False)
    calls = []

    async def bad_tick():
        calls.append(1)
        raise RuntimeError("tick failed")

    r.paper_tick = bad_tick
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await asyncio.sleep(0.2)
    assert not task.done()
    assert len(calls) >= 3
    assert DyingRecorder.made >= 2 and r.recorder.ran
    stop.set()
    await asyncio.wait_for(task, 5)


async def test_persist_cursor_db_error_does_not_kill_watcher(engine, monkeypatch):
    got = []
    start = datetime.now(UTC) - H
    w = FillWatcher(engine, LiveClient(), {A: start}, start_time=start, fetch_leverage=False,
                    sz_decimals={}, on_fill=got.append)

    def broken(*a, **kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(live.pipeline, "kv_set_json", broken)
    w.last_seen_ms[A] += 1  # a cursor change to persist
    w._persist_cursor(A)  # logged, not raised
    monkeypatch.undo()
    f = mk("BTC", "B", "1", "100", "0", ms(start) + 60_000, tid=7)
    out = await w.ingest(A, [f], observed=start + 2 * H, via="ws")
    assert len(out) == 1


# -- P47: recovery max age ----------------------------------------------------------------

async def test_stale_recovered_fills_are_missed_and_never_priced(engine, session):
    now = START + timedelta(days=1)
    c = LiveClient()
    w = FillWatcher(engine, c, {A: START}, start_time=START, now=lambda: now,
                    fetch_leverage=False, sz_decimals={})
    old = mk("BTC", "B", "1", "100", "0", ms(now - timedelta(minutes=20)), tid=11)
    new = mk("ETH", "B", "1", "10", "0", ms(now - timedelta(minutes=1)), tid=12)
    c.fills[A] = [old, new]
    await w._recover([A])
    with Session(engine) as s:
        rows = {r.json["market_id"]: r for r in s.scalars(select(PaperEvent))}
        assert rows["BTC"].missed is True and rows["BTC"].settled_at == now
        assert rows["BTC"].fill_price is None
        assert not rows["ETH"].missed and rows["ETH"].settled_at is None
    price(engine, "BTC", now + timedelta(seconds=1), 100)  # would price it, if asked
    price(engine, "ETH", now + timedelta(seconds=1), 10)
    with Session(engine) as s:
        pr.run_paper(s, A, now + timedelta(minutes=1))
        snap = pr.snapshot(s, A)
    assert snap["missed"] == 1 and snap["events"] == 2
    assert [p["market_id"] for p in snap["open_positions"]] == ["ETH"]


# -- P49: per-position gap ------------------------------------------------------------------

def _store_cpnl(s, side, at, cpnl, **kw):
    e = store(s, side, at, **kw)
    row = s.get(PaperEvent, (A, e.source_ref))
    row.json = {**row.json, "leader_closed_pnl": str(cpnl)}
    s.flush()
    return e


def test_gap_is_per_copied_position_scaled(engine, session):
    t = START + H
    # pre-follow episode: its close is excluded from both numbers
    pre = store(session, "close", t, coin="SOL", pid="SOL:0", start=3, size="3")
    row = session.get(PaperEvent, (A, pre.source_ref))
    row.json = {**row.json, "leader_time": (START - H).isoformat(), "leader_closed_pnl": "1000"}
    # copied: leader 2 BTC, +20 closedPnl
    price(session, "BTC", t + timedelta(seconds=1), 100)
    _store_cpnl(session, "open", t, 0, coin="BTC", pid="BTC:1", start=0, size="2")
    price(session, "BTC", t + H + timedelta(seconds=1), 110)
    _store_cpnl(session, "close", t + H, 20, coin="BTC", pid="BTC:1", start=2, size="2")
    # missed open (no ETH price): excluded from the gap, in the raw leader number
    _store_cpnl(session, "open", t, 0, coin="ETH", pid="ETH:1", start=0, size="1")
    _store_cpnl(session, "close", t + H, 30, coin="ETH", pid="ETH:1", start=1, size="1")
    session.flush()
    summ = pr.run_paper(session, A, t + timedelta(days=1))
    snap = pr.snapshot(session, A)
    opened = [f for f in snap["fills"] if f["side"] == "open" and not f["missed"]]
    assert len(opened) == 1
    our_qty = Decimal(opened[0]["size"])
    our_pnl = summ.net_pnl_usd  # the only position: net = its pnl
    expected = Decimal(20) * our_qty / 2 - our_pnl
    assert abs(summ.gap_usd - expected) < Decimal("1e-9")
    assert Decimal(snap["gap_usd"]) == summ.gap_usd
    assert Decimal(snap["leader_realized_usd"]) == Decimal(50)


# -- P50: daily backfill before rescore --------------------------------------------------

async def test_daily_backfills_follows_and_top_candidates_before_rescore(engine, monkeypatch):
    calls = []
    with Session(engine) as s:
        fl.add_follow(s, A, START)
        for i in range(60):
            s.add(Wallet(address=f"0x{i + 1:040x}", score=float(i), stats_json={}))
        s.commit()

    async def fake_backfill(session, client, addr, days=90, *, now=None):
        calls.append(("backfill", addr))

    def fake_rescore(session, now=None, **kw):
        calls.append(("rescore", None))

    def fake_sync(session, now, **kw):
        calls.append(("sync", None))
        return fl.FollowDiff()

    monkeypatch.setattr(rt.pipeline, "backfill", fake_backfill)
    monkeypatch.setattr(rt.pipeline, "rescore", fake_rescore)
    monkeypatch.setattr(rt.follows, "sync_auto_follows", fake_sync)
    r = runtime(engine, Clock(START + timedelta(days=1)), backfill_pause_s=0)
    assert await r.daily() is True
    kinds = [k for k, _ in calls]
    assert kinds[-2:] == ["rescore", "sync"]
    bf = [a for k, a in calls if k == "backfill"]
    assert bf[0] == A and len(bf) == 51 and len(set(bf)) == 51
    assert f"0x{60:040x}" in bf and f"0x{1:040x}" not in bf  # the top 50 by score


async def test_daily_records_marks_before_pruning(engine, monkeypatch):
    now = START + timedelta(days=5)
    with Session(engine) as s:
        fl.add_follow(s, A, now - timedelta(days=4))
        t = now - timedelta(days=3, minutes=30)
        price(s, "BTC", t + timedelta(seconds=1), 100)
        store(s, "open", t, start=0)
        for h in range(3):  # 2 min before each of the next three hours
            price(s, "BTC", t + timedelta(minutes=28) + h * H, 100 + h)
        s.commit()
    monkeypatch.setattr(rt.pipeline, "rescore", lambda *a, **k: None)

    async def no_backfill(*a, **k):
        return None

    monkeypatch.setattr(rt.pipeline, "backfill", no_backfill)
    r = runtime(engine, Clock(now), backfill_pause_s=0)
    await r.daily()
    with Session(engine) as s:
        hours = sorted(m.hour_ts for m in s.scalars(select(Mark).where(Mark.coin == "BTC")))
        assert ms(t + timedelta(minutes=30)) in hours  # an hour older than the 2-day keep
        assert all(p.time >= ms(now - timedelta(days=2)) for p in s.scalars(select(Price)))


# -- minors -----------------------------------------------------------------------------------

async def test_startup_daily_does_not_block_shutdown(engine):
    r = runtime(engine, Clock(START), paper_interval_s=0.01, reconcile_tick_s=0.01,
                watch_refresh_s=0.01)
    started = asyncio.Event()

    async def slow_daily():
        started.set()
        await asyncio.sleep(30)
        return True

    r.daily = slow_daily
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.sleep(0.05)
    assert r.recorder is not None and r.recorder.ran  # the runtime is up meanwhile
    stop.set()
    await asyncio.wait_for(task, 2)
