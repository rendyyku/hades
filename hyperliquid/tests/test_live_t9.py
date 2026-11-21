"""Task 9 carried live-layer fixes: per-address recovery lock, persisted last_seen /
recovery floor (M6), poll budget (M7), and the P41 price-feed fallback."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from hades_hl import live
from hades_hl.client import HLError
from hades_hl.db import Kv, PaperEvent
from hades_hl.live import FillWatcher, PriceRecorder
from hlfills import mk
from test_live import FakeClient, FakeHL, wait_for, wait_subs, watcher  # noqa: F401
from test_live import engine  # noqa: F401  (fixture)

UTC = timezone.utc
A = "0x" + "a1" * 20


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Gated(FakeClient):
    """Each user_fills_by_time call blocks on its own gate ('ok' or 'fail'); the very
    first call fails immediately."""

    def __init__(self):
        super().__init__()
        self.gates: list[asyncio.Future] = []
        self.fail_first = True

    async def user_fills_by_time(self, addr, start_ms, end_ms=None, *, on_page=None):
        if self.fail_first:
            self.fail_first = False
            raise HLError("initial")
        fut = asyncio.get_running_loop().create_future()
        self.gates.append(fut)
        res = await fut
        if res == "fail":
            raise HLError("late failure")
        return await super().user_fills_by_time(addr, start_ms, end_ms)


def mkw(engine, c, got):
    start = datetime.now(UTC) - timedelta(hours=1)
    w = FillWatcher(engine, c, {A: None}, start_time=start, fetch_leverage=False,
                    sz_decimals={}, on_fill=got.append)
    return start, w


# -- (a) per-address recovery lock -----------------------------------------------------

async def test_late_failure_does_not_resurrect_stale_floor(engine):
    """Ported from the Task 8 race probe: two overlapping recoveries; the second fails
    AFTER the first succeeded. Without the lock it re-set the floor to the stale,
    pre-success value (computed before awaiting)."""
    c = Gated()
    got = []
    start, w = mkw(engine, c, got)
    o = mk("BTC", "B", "1", "100", "0", ms(start) + 60_000, tid=1)
    c.fills[A] = [o]
    await w._recover([A])  # fails -> floor = start
    floor0 = w.recovery_floor_ms[A]
    t1 = asyncio.create_task(w._recover([A]))  # e.g. ws reconnect
    t2 = asyncio.create_task(w._recover([A]))  # e.g. a second reconnect
    await asyncio.sleep(0.01)
    assert len(c.gates) == 1  # t2 waits on the per-address lock
    c.gates[0].set_result("ok")
    await wait_for(lambda: len(c.gates) == 2)
    assert A not in w.recovery_floor_ms and [e.source_ref for e in got] == ["0xh1:1"]
    c.gates[1].set_result("fail")
    await asyncio.gather(t1, t2)
    # the failed run's floor is the post-success last_seen, never the stale floor0
    assert w.recovery_floor_ms[A] == w.last_seen_ms[A] == ms(start) + 60_000
    assert w.recovery_floor_ms[A] != floor0


async def test_retry_skips_when_floor_already_cleared(engine):
    c = Gated()
    got = []
    start, w = mkw(engine, c, got)
    c.fills[A] = [mk("BTC", "B", "1", "100", "0", ms(start) + 60_000, tid=1)]
    await w._recover([A])  # fails -> floor
    t1 = asyncio.create_task(w._recover([A]))  # reconnect
    t2 = asyncio.create_task(w._recover([A], retry=True))  # retry loop, now redundant
    await asyncio.sleep(0.01)
    c.gates[0].set_result("ok")
    await asyncio.gather(t1, t2)
    assert len(c.gates) == 1  # the retry did not fetch again
    assert A not in w.recovery_floor_ms and len(got) == 1


# -- (b) M6: persisted last_seen / floor ---------------------------------------------

def kv(engine, key):
    with Session(engine) as s:
        row = s.get(Kv, key)
        return None if row is None else json.loads(row.value)


async def test_last_seen_and_floor_persisted_in_kv(engine):
    c = Gated()
    start, w = mkw(engine, c, [])
    f1 = mk("BTC", "B", "1", "100", "0", ms(start) + 60_000, tid=1)
    await w.ingest(A, [f1], observed=datetime.now(UTC), via="ws")
    assert kv(engine, f"hl:last_seen:{A}") == ms(start) + 60_000
    assert kv(engine, f"hl:floor:{A}") is None
    await w._recover([A])  # fails
    assert kv(engine, f"hl:floor:{A}") == ms(start) + 60_000
    c.fills[A] = [f1]
    task = asyncio.create_task(w._recover([A]))
    await wait_for(lambda: len(c.gates) == 1)
    c.gates[0].set_result("ok")
    await task
    assert kv(engine, f"hl:floor:{A}") is None


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def test_restart_recovers_fills_made_while_down(engine):
    t0 = datetime.now(UTC) - timedelta(hours=3)
    c = FakeClient()
    w1 = FillWatcher(engine, c, {A: t0}, start_time=t0, fetch_leverage=False, sz_decimals={})
    f1 = mk("BTC", "B", "1", "100", "0", ms(t0) + 60_000, tid=1)
    await w1.ingest(A, [f1], observed=t0 + timedelta(minutes=1, seconds=1), via="ws")
    # process down; the leader trades meanwhile
    f2 = mk("BTC", "A", "1", "101", "1", ms(t0) + 3_600_000, tid=2)
    c.fills[A] = [f1, f2]
    restart = t0 + timedelta(hours=2)
    clock = Clock(restart + timedelta(seconds=2))
    got = []
    w2 = FillWatcher(engine, c, {A: t0}, start_time=restart, now=clock, fetch_leverage=False,
                     sz_decimals={}, on_fill=got.append)
    assert w2.last_seen_ms[A] == ms(t0) + 60_000  # restored, not the restart time
    await w2._recover([A])
    assert [(e.source_ref, e.side) for e in got] == [("0xh2:2", "close")]
    assert got[0].observed_time == clock.t  # the recovery response time
    with Session(engine) as s:
        assert s.get(PaperEvent, "0xh2:2").json["via"] == "recovery"
    assert (A, ms(t0) + 60_000) in c.calls


async def test_restart_recovers_even_when_no_fill_was_seen_before(engine):
    t0 = datetime.now(UTC) - timedelta(hours=3)
    c = FakeClient()
    FillWatcher(engine, c, {A: t0}, start_time=t0, fetch_leverage=False, sz_decimals={})
    assert kv(engine, f"hl:last_seen:{A}") == ms(t0)  # the live start is persisted
    f = mk("BTC", "B", "1", "100", "0", ms(t0) + 3_600_000, tid=3)  # made while down
    c.fills[A] = [f]
    got = []
    w2 = FillWatcher(engine, c, {A: t0}, start_time=t0 + timedelta(hours=2),
                     fetch_leverage=False, sz_decimals={}, on_fill=got.append)
    await w2._recover([A])
    assert [e.source_ref for e in got] == ["0xh3:3"]


async def test_restart_restores_pending_floor_and_holds_live_fills(engine):
    t0 = datetime.now(UTC) - timedelta(hours=3)
    c = Gated()
    w1 = FillWatcher(engine, c, {A: t0}, start_time=t0, fetch_leverage=False, sz_decimals={})
    await w1._recover([A])  # fails -> floor persisted
    got = []
    w2 = FillWatcher(engine, c, {A: t0}, start_time=t0 + timedelta(hours=2),
                     fetch_leverage=False, sz_decimals={}, on_fill=got.append)
    assert w2.recovery_floor_ms[A] == ms(t0)
    live_f = mk("BTC", "B", "1", "100", "0", ms(t0) + 7_300_000, tid=9)
    assert await w2.ingest(A, [live_f], observed=datetime.now(UTC), via="ws") == []
    assert got == []  # held until the recovery succeeds


def test_persisted_last_seen_never_before_follow_start(engine):
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    with Session(engine) as s:
        s.add(Kv(key=f"hl:last_seen:{A}", value=str(ms(t0))))
        s.commit()
    later = t0 + timedelta(days=3)  # re-followed after the old cursor
    w = FillWatcher(engine, FakeClient(), {A: later}, start_time=later + timedelta(hours=1),
                    fetch_leverage=False, sz_decimals={})
    assert w.last_seen_ms[A] == ms(later) and w.live_from_ms[A] == ms(later)


# -- (c) M7: poll budget -------------------------------------------------------------

def test_poll_interval_counts_item_weight_and_state_calls(engine):
    addrs = [f"0x{i:040x}" for i in range(1, 26)]  # 25 follows -> 15 polled
    w = FillWatcher(engine, FakeClient(), {a: None for a in addrs}, fetch_leverage=True,
                    sz_decimals={})
    # 15 x 20 per cycle; clearinghouseState (weight 2) up to once a minute for each
    # of the 25 follows = 50/min out of the 600/min polling share
    assert w.poll_interval() == pytest.approx(300 * 60 / 550)
    w._note_poll(addrs[-1], 45)  # 45 fills returned: +2 item weight
    assert w.poll_interval() == pytest.approx(302 * 60 / 550)
    w.extra_weight_per_min = 10.0  # e.g. the runtime's reconcile calls
    assert w.poll_interval() == pytest.approx(302 * 60 / 540)


# -- P41: fastAssetCtxs primary, allMids fallback -----------------------------------

async def test_price_feed_falls_back_to_all_mids_after_repeated_errors(engine):
    async def bad(ws, subs):
        await wait_subs(subs, 1)
        await ws.send(json.dumps({"channel": "fastAssetCtxs", "data": "!!not-base64!!"}))
        await ws.wait_closed()

    async def good(ws, subs):
        await wait_subs(subs, 1)
        await ws.send(json.dumps({"channel": "allMids", "data": {"mids": {"BTC": "99"}}}))
        await ws.wait_closed()

    async with FakeHL([bad, bad, good]) as hl:
        rec = PriceRecorder(engine, ws_url=hl.url, feed="fastAssetCtxs",
                            fallback_feed="allMids", fallback_after=2,
                            reconnect_base_s=0.01, reconnect_max_s=0.02)
        stop = asyncio.Event()
        task = asyncio.create_task(rec.run(stop))
        await wait_for(lambda: rec.rows_written >= 1)
        stop.set()
        await task
    assert [s[0]["type"] for s in hl.subs[:3]] == ["fastAssetCtxs", "fastAssetCtxs", "allMids"]
    assert rec.feed == "allMids"


def test_price_recorder_rejects_bad_fallback(engine):
    with pytest.raises(ValueError):
        PriceRecorder(engine, feed="fastAssetCtxs", fallback_feed="l2Book")
