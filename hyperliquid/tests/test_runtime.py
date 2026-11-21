"""Task 9: the serve runtime (scheduler) -- watch set (P42), price pruning, daily jobs,
watcher restarts on follow changes, paper ticks, fast alerts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_hl import follows as fl
from hades_hl import paper_runtime as pr
from hades_hl import runtime as rt
from hades_hl.client import FundingPage
from hades_hl.db import Alert, Fill, Follow, PaperEvent, Price, init_db, make_engine
from hades_hl.db import fill_row
from hades_hl.pipeline import kv_get_json, kv_set_json
from hlfills import mk
from test_paper_runtime import price, store

UTC = timezone.utc
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20


def ms(dt):
    return int(dt.timestamp() * 1000)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    init_db(eng)
    return eng


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeClient:
    def __init__(self):
        self.funding_calls = []
        self.state_calls = []

    async def funding_history(self, coin, start_ms, end_ms=None):
        self.funding_calls.append(coin)
        return FundingPage([])

    async def clearinghouse_state(self, addr):
        self.state_calls.append(addr)
        return {"marginSummary": {"accountValue": "1000", "totalNtlPos": "0"},
                "assetPositions": []}

    async def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5}]}


class FakeWatcher:
    instances: list["FakeWatcher"] = []

    def __init__(self, follows, on_fill):
        self.follows = dict(follows)
        self.on_fill = on_fill
        self.stopped = False
        self.recovery_floor_ms = {}
        self.extra_weight_per_min = 0.0
        FakeWatcher.instances.append(self)

    async def run(self, stop):
        await stop.wait()
        self.stopped = True


class FakeRecorder:
    def __init__(self, coins):
        self.coins = coins
        self.ran = False

    async def run(self, stop):
        self.ran = True
        await stop.wait()


def runtime(engine, clock, **kw):
    FakeWatcher.instances = []
    kw.setdefault("watcher_factory", lambda follows, on_fill: FakeWatcher(follows, on_fill))
    kw.setdefault("recorder_factory", lambda coins: FakeRecorder(coins))
    return rt.HLRuntime(engine, FakeClient(), now=clock, **kw)


def add_fill(s, addr, coin, t):
    f = mk(coin, "B", "1", "100", "0", ms(t))
    s.execute(Fill.__table__.insert(), [fill_row(addr, f)])


# -- watch set (P42) + pruning -------------------------------------------------------

def test_watch_coins_follow_trades_open_positions_and_majors(engine):
    with Session(engine) as s:
        fl.add_follow(s, A, NOW - timedelta(days=40))
        fl.add_follow(s, B, NOW - timedelta(days=40))
        fl.remove_follow(s, B, NOW - timedelta(days=1))
        add_fill(s, A, "SOL", NOW - timedelta(days=3))
        add_fill(s, A, "DOGE", NOW - timedelta(days=31))  # too old
        add_fill(s, B, "PEPE", NOW - timedelta(days=2))  # removed follow
        add_fill(s, A, "@107", NOW - timedelta(days=1))  # spot: never recorded
        kv_set_json(s, pr.KV_PAPER.format(A), {"computed_at": NOW.isoformat(), "open_positions": [
            {"position_id": "WIF:1", "market_id": "WIF"}]})
        s.commit()
        assert rt.watch_coins(s, NOW) == {"BTC", "ETH", "SOL", "WIF"}


def test_prune_prices_keep_days_parameter(engine):
    # the default is 2 days since fix round 1 (P52; see test_t9_fix_r1)
    with Session(engine) as s:
        s.add_all([Price(coin="BTC", time=ms(NOW - timedelta(days=15)), mid="1"),
                   Price(coin="BTC", time=ms(NOW - timedelta(days=13)), mid="2")])
        s.commit()
        assert rt.prune_prices(s, NOW, keep_days=14) == 1
        s.commit()
        assert [p.mid for p in s.scalars(select(Price))] == [Decimal("2")]


# -- daily jobs ----------------------------------------------------------------------

async def test_daily_rescore_then_follow_sync_once_a_day(engine, monkeypatch):
    calls = []

    def fake_rescore(session, now=None, **kw):
        calls.append(("rescore", now))

    def fake_sync(session, now, **kw):
        calls.append(("sync", now))
        return fl.FollowDiff()

    monkeypatch.setattr(rt.pipeline, "rescore", fake_rescore)
    monkeypatch.setattr(rt.follows, "sync_auto_follows", fake_sync)
    clock = Clock(NOW)
    r = runtime(engine, clock)
    assert await r.daily() is True
    assert [c[0] for c in calls] == ["rescore", "sync"]
    clock.t = NOW + timedelta(hours=23)
    assert await r.daily() is False
    r2 = runtime(engine, clock)  # restart: the last run time is persisted
    assert await r2.daily() is False
    clock.t = NOW + timedelta(days=1, minutes=1)
    assert await r2.daily() is True and len(calls) == 4


# -- watcher (re)starts ----------------------------------------------------------------

async def test_watcher_restarted_when_follows_change(engine):
    with Session(engine) as s:
        fl.add_follow(s, A, NOW)
        s.commit()
    r = runtime(engine, Clock(NOW))
    await r.sync_watcher()
    assert len(FakeWatcher.instances) == 1 and set(FakeWatcher.instances[0].follows) == {A}
    await r.sync_watcher()  # unchanged: no restart
    assert len(FakeWatcher.instances) == 1
    with Session(engine) as s:
        fl.add_follow(s, B, NOW)
        s.commit()
    await r.sync_watcher()
    assert len(FakeWatcher.instances) == 2
    await asyncio.sleep(0)
    assert FakeWatcher.instances[0].stopped
    assert set(FakeWatcher.instances[1].follows) == {A, B}
    assert FakeWatcher.instances[1].follows[A] == NOW
    await r.stop_watcher()
    assert FakeWatcher.instances[1].stopped


# -- paper tick + fast alerts ------------------------------------------------------------

async def test_paper_tick_runs_follows_with_new_events(engine):
    with Session(engine) as s:
        fl.add_follow(s, A, NOW - timedelta(days=1))
        fl.add_follow(s, B, NOW - timedelta(days=1))
        s.commit()
    # A has an event; B has none but was never run either -> both run once
    with Session(engine) as s:
        price(s, "BTC", NOW - timedelta(hours=2) + timedelta(seconds=1), 100)
        store(s, "open", NOW - timedelta(hours=2), start=0)
        s.commit()
    clock = Clock(NOW)
    r = runtime(engine, clock)
    assert sorted(await r.paper_tick()) == sorted([A, B])
    clock.t = NOW + timedelta(seconds=60)
    assert await r.paper_tick() == []  # nothing new
    with Session(engine) as s:
        assert pr.snapshot(s, A)["open_positions"][0]["market_id"] == "BTC"
    assert r.client.funding_calls == ["BTC"]


async def test_on_fill_alerts_notif_wallet_immediately(engine):
    with Session(engine) as s:
        fl.add_follow(s, A, NOW - timedelta(days=1))
        s.get(Follow, A).ladder = "NOTIF"
        e = store(s, "open", NOW, start=0)
        s.commit()
    r = runtime(engine, Clock(NOW))
    r.on_fill(e)
    with Session(engine) as s:
        assert [a.kind for a in s.scalars(select(Alert))] == ["open"]
    assert A in r.dirty


async def test_reconcile_tick_only_wallets_with_open_positions(engine):
    with Session(engine) as s:
        fl.add_follow(s, A, NOW - timedelta(days=1))
        fl.add_follow(s, B, NOW - timedelta(days=1))
        price(s, "BTC", NOW - timedelta(hours=2) + timedelta(seconds=1), 100)
        store(s, "open", NOW - timedelta(hours=2), start=0)
        pr.run_paper(s, A, NOW - timedelta(hours=1))
        pr.run_paper(s, B, NOW - timedelta(hours=1))
        s.commit()
    r = runtime(engine, Clock(NOW))
    evs = await r.reconcile_tick()
    assert r.client.state_calls == [A]
    assert [e.side for e in evs] == ["close"] and A in r.dirty


async def test_run_starts_and_stops_cleanly(engine):
    with Session(engine) as s:
        fl.add_follow(s, A, NOW)
        s.commit()
    r = runtime(engine, Clock(NOW), paper_interval_s=0.01, reconcile_tick_s=0.01,
                watch_refresh_s=0.01, daily_interval_s=3600, run_daily=False)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert r.recorder.ran and FakeWatcher.instances and FakeWatcher.instances[-1].stopped
    assert "BTC" in r.watch_set and "ETH" in r.watch_set
