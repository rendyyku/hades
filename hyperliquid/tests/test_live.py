"""Task 8: live layer -- PriceRecorder, FillWatcher, HLExecutionModel."""

from __future__ import annotations

import asyncio
import base64
import json
import zlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from websockets.asyncio.server import serve

from hades_core.events import TradeEvent
from hades_core.paper import OpenPosition, PaperConfig, simulate
from hades_hl import live
from hades_hl.client import FundingPage, HLPage
from hades_hl.db import Fill, Kv, PaperEvent, Price, init_db, make_engine
from hades_hl.live import (
    FillWatcher,
    FundingRates,
    HLExecutionModel,
    PriceRecorder,
    event_from_json,
)
from hlfills import mk

UTC = timezone.utc
T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
T0_MS = int(T0.timestamp() * 1000)
H_MS = 3_600_000
A = "0x" + "a1" * 20


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'hl.db'}")
    init_db(eng)
    return eng


# -- fakes ---------------------------------------------------------------------------

class FakeClient:
    """REST double: ``fills[addr]`` is what userFillsByTime knows (filtered by time)."""

    def __init__(self) -> None:
        self.fills: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, int]] = []
        self.funding: dict[str, list[dict]] = {}
        self.funding_calls: list[tuple[str, int, int | None]] = []

    async def user_fills_by_time(self, addr, start_ms, end_ms=None, *, on_page=None):
        self.calls.append((addr, int(start_ms)))
        out = [f for f in self.fills.get(addr, []) if int(f["time"]) >= int(start_ms)
               and (end_ms is None or int(f["time"]) <= end_ms)]
        return HLPage(out)

    async def clearinghouse_state(self, addr):
        return {"assetPositions": [{"position": {"coin": "BTC",
                                                 "leverage": {"value": 5}}}]}

    async def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5}]}

    async def funding_history(self, coin, start_ms, end_ms=None):
        self.funding_calls.append((coin, start_ms, end_ms))
        rows = [r for r in self.funding.get(coin, []) if int(r["time"]) >= start_ms
                and (end_ms is None or int(r["time"]) <= end_ms)]
        return FundingPage(rows)


class FakeHL:
    """Fake HL WS server. ``scripts`` is a list (one per connection) of async callables
    ``script(ws, subs)``; after the last script the connection just stays open."""

    def __init__(self, scripts=()) -> None:
        self.scripts = list(scripts)
        self.connections = 0
        self.subs: list[list[dict]] = []
        self.server = None
        self.url = ""

    async def handler(self, ws):
        idx = self.connections
        self.connections += 1
        subs: list[dict] = []
        self.subs.append(subs)

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("method") == "subscribe":
                    subs.append(msg["subscription"])
                    await ws.send(json.dumps({"channel": "subscriptionResponse",
                                              "data": msg}))
                elif msg.get("method") == "ping":
                    await ws.send(json.dumps({"channel": "pong"}))

        rt = asyncio.create_task(reader())
        try:
            if idx < len(self.scripts):
                await self.scripts[idx](ws, subs)
            else:
                await ws.wait_closed()  # stay open until closed
        finally:
            rt.cancel()

    async def __aenter__(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


async def wait_for(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def wait_subs(subs, n):
    await wait_for(lambda: len(subs) >= n)


def fills_msg(user, fills, snapshot=False):
    return json.dumps({"channel": "userFills",
                       "data": {"isSnapshot": snapshot, "user": user, "fills": fills}})


# -- PriceRecorder -------------------------------------------------------------------

def test_price_at_within_30s_and_none_outside(engine):
    rec = PriceRecorder(engine)
    assert rec.price_at("BTC", T0) is None  # before recording starts
    rec.record({"BTC": "100.5"}, at=T0)
    assert rec.price_at("BTC", T0) == Decimal("100.5")
    assert rec.price_at("BTC", T0 - timedelta(seconds=30)) == Decimal("100.5")
    assert rec.price_at("BTC", T0 - timedelta(seconds=31)) is None  # not within 30 s
    assert rec.price_at("BTC", T0 + timedelta(milliseconds=1)) is None  # never backward
    assert rec.price_at("ETH", T0) is None


def test_price_at_returns_first_recorded_mid_after_t(engine):
    rec = PriceRecorder(engine)
    rec.record({"BTC": "100"}, at=T0)
    rec.record({"BTC": "101"}, at=T0 + timedelta(seconds=2))
    assert rec.price_at("BTC", T0 + timedelta(seconds=1)) == Decimal("101")


def test_recorder_throttles_writes_per_coin_per_second(engine):
    rec = PriceRecorder(engine)
    assert rec.record({"BTC": "100", "ETH": "10"}, at=T0) == 2
    assert rec.record({"BTC": "100.1", "ETH": "10.1"}, at=T0 + timedelta(milliseconds=500)) == 0
    assert rec.record({"BTC": "100.2"}, at=T0 + timedelta(milliseconds=999)) == 0
    assert rec.record({"BTC": "100.3"}, at=T0 + timedelta(seconds=1)) == 1
    with Session(engine) as s:
        rows = s.execute(select(Price.coin, Price.time, Price.mid)
                         .order_by(Price.coin, Price.time)).all()
    assert [(c, t - T0_MS, m) for c, t, m in rows] == [
        ("BTC", 0, Decimal("100")), ("BTC", 1000, Decimal("100.3")), ("ETH", 0, Decimal("10"))]


def test_recorder_skips_spot_and_respects_coin_filter(engine):
    rec = PriceRecorder(engine, coins=lambda: {"BTC"})
    assert rec.record({"BTC": "1", "ETH": "2", "@107": "3", "PURR/USDC": "4"}, at=T0) == 1
    rec2 = PriceRecorder(engine)
    assert rec2.record({"@107": "3", "#114910": "0.5", "xyz:ANT": "5"},
                       at=T0 + timedelta(seconds=5)) == 1  # spot and outcome tokens skipped


def test_recorder_rewrites_unchanged_mid_only_after_refresh(engine):
    rec = PriceRecorder(engine, refresh_s=10)
    rec.record({"BTC": "100"}, at=T0)
    assert rec.record({"BTC": "100"}, at=T0 + timedelta(seconds=5)) == 0  # unchanged
    assert rec.record({"BTC": "100"}, at=T0 + timedelta(seconds=10)) == 1  # refresh
    # every unchanged stretch still leaves a row within each 30 s window
    assert rec.price_at("BTC", T0 + timedelta(seconds=1)) == Decimal("100")


async def test_recorder_subscribes_all_mids_over_ws(engine):
    async def script(ws, subs):
        await wait_subs(subs, 1)
        await ws.send(json.dumps({"channel": "allMids", "data": {"mids": {"BTC": "99.5"}}}))
        await ws.wait_closed()

    async with FakeHL([script]) as hl:
        rec = PriceRecorder(engine, ws_url=hl.url)
        stop = asyncio.Event()
        task = asyncio.create_task(rec.run(stop))
        await wait_for(lambda: rec.rows_written >= 1)
        stop.set()
        await task
        assert hl.subs[0] == [{"type": "allMids"}]
    assert rec.price_at("BTC", datetime.now(UTC) - timedelta(seconds=5)) == Decimal("99.5")


def deflate_b64(obj) -> str:
    c = zlib.compressobj(wbits=-15)
    return base64.b64encode(c.compress(json.dumps(obj).encode()) + c.flush()).decode()


async def test_recorder_fast_asset_ctxs_feed_merges_diffs(engine):
    async def script(ws, subs):
        await wait_subs(subs, 1)
        await ws.send(json.dumps({"channel": "fastAssetCtxs", "data": deflate_b64(
            {"BTC": {"markPx": "1", "midPx": "100"}, "ETH": {"markPx": "2", "midPx": "10"}})}))
        await ws.send(json.dumps({"channel": "fastAssetCtxs", "data": deflate_b64(
            {"BTC": {"markPx": "1.1"}, "ETH": {"midPx": None}})}))
        await ws.wait_closed()

    async with FakeHL([script]) as hl:
        rec = PriceRecorder(engine, ws_url=hl.url, feed="fastAssetCtxs", min_interval_s=0,
                            refresh_s=0)
        stop = asyncio.Event()
        task = asyncio.create_task(rec.run(stop))
        await wait_for(lambda: rec.messages >= 2)
        stop.set()
        await task
        assert hl.subs[0] == [{"type": "fastAssetCtxs"}]
    assert rec._mids == {"BTC": "100"}  # ETH lost its mid; BTC kept its last midPx
    with pytest.raises(ValueError):
        PriceRecorder(engine, feed="l2Book")


# -- HLExecutionModel ----------------------------------------------------------------

def ev(side="open", direction="long", observed=T0, market="BTC", size="1", px="100",
       ref="r1", leverage=None, seq=0):
    return TradeEvent(head="hyperliquid", wallet=A, market_id=market, side=side,
                      direction=direction, size=Decimal(size), price=Decimal(px),
                      notional_usd=Decimal(size) * Decimal(px), fee_usd=Decimal("0"),
                      leader_time=observed - timedelta(seconds=2), observed_time=observed,
                      source_ref=ref, leverage=leverage, seq=seq)


@pytest.fixture
def priced(engine):
    rec = PriceRecorder(engine)
    rec.record({"BTC": "100"}, at=T0 + timedelta(seconds=1))
    return rec


@pytest.mark.parametrize("side,direction,worse", [
    ("open", "long", "above"), ("increase", "long", "above"),
    ("reduce", "long", "below"), ("close", "long", "below"),
    ("open", "short", "below"), ("increase", "short", "below"),
    ("reduce", "short", "above"), ("close", "short", "above"),
])
def test_model_price_worse_than_mid_both_directions(priced, side, direction, worse):
    m = HLExecutionModel(priced)
    p = m.fill_price(ev(side, direction))
    assert p is not None
    assert (p > Decimal("100")) if worse == "above" else (p < Decimal("100"))


def test_model_price_adjustment_values(priced):
    m = HLExecutionModel(priced, copy_notional_usd=Decimal("100000"))
    # half-spread 0.02% + 0.0005 * sqrt(100k / 100k) = 0.07%
    assert m.fill_price(ev("open", "long")) == Decimal("100.07")
    assert m.fill_price(ev("open", "short")) == Decimal("99.93")
    m2 = HLExecutionModel(priced, copy_notional_usd=Decimal("400000"))  # sqrt(4) = 2
    assert m2.fill_price(ev("open", "long")) == Decimal("100.12")


def test_model_uses_price_at_observed_plus_one_second(engine):
    rec = PriceRecorder(engine)
    rec.record({"BTC": "50"}, at=T0 + timedelta(milliseconds=500))  # before observed + 1 s
    m = HLExecutionModel(rec)
    assert m.fill_price(ev()) is None  # nothing recorded in [obs + 1 s, obs + 31 s]
    rec.record({"BTC": "100"}, at=T0 + timedelta(seconds=31))
    assert m.fill_price(ev()) is not None
    eng2 = make_engine("sqlite://")
    init_db(eng2)
    rec2 = PriceRecorder(eng2)
    assert HLExecutionModel(rec2).fill_price(ev()) is None  # no price recorded: missed


def test_model_fee_and_leverage(priced):
    m = HLExecutionModel(priced)
    assert m.fee(Decimal("10000")) == Decimal("4.5")  # base taker 0.045%
    assert m.max_leverage == Decimal("10")


def funding_rows(coin, hours, rate):
    # HL stamps funding a few ms after the hour
    return [{"coin": coin, "fundingRate": str(rate), "premium": "0", "time": T0_MS + h * H_MS + 37}
            for h in hours]


def pos(direction, qty="2", px="100"):
    return OpenPosition(position_id="p", market_id="BTC", direction=direction, qty=Decimal(qty),
                        entry_price=Decimal(px), margin=Decimal("20"), leverage=Decimal("10"),
                        leader_size=Decimal("1"))


async def test_funding_holding_cost_sign_long_and_short(engine, priced):
    client = FakeClient()
    client.funding["BTC"] = funding_rows("BTC", range(0, 6), "0.0001")
    fr = FundingRates(engine)
    await fr.ensure(client, "BTC", T0, T0 + timedelta(hours=6), now=T0 + timedelta(hours=7))
    m = HLExecutionModel(priced, funding=fr)
    start, end = T0 + timedelta(minutes=30), T0 + timedelta(hours=3, minutes=30)
    # hours 01:00, 02:00, 03:00 are in [start, end): 3 x 0.0001 x notional 200
    long_cost = m.holding_cost(pos("long"), start, end)
    short_cost = m.holding_cost(pos("short"), start, end)
    assert isinstance(long_cost, Decimal)
    assert long_cost == Decimal("0.06")
    assert short_cost == Decimal("-0.06")
    # negative rates: the long receives, the short pays
    client.funding["ETH"] = [dict(r, coin="ETH", fundingRate="-0.0002")
                             for r in funding_rows("ETH", range(0, 6), "0")]
    await fr.ensure(client, "ETH", T0, T0 + timedelta(hours=6), now=T0 + timedelta(hours=7))
    p = OpenPosition("q", "ETH", "long", Decimal("1"), Decimal("1000"), Decimal("100"),
                     Decimal("10"), Decimal("1"))
    assert m.holding_cost(p, T0, T0 + timedelta(hours=1)) == Decimal("-0.2")  # hour 00:00


async def test_funding_rates_cached_in_kv(engine):
    client = FakeClient()
    client.funding["BTC"] = funding_rows("BTC", range(0, 3), "0.0001")
    now = T0 + timedelta(days=2)
    await FundingRates(engine).ensure(client, "BTC", T0, T0 + timedelta(hours=3), now=now)
    n = len(client.funding_calls)
    assert n == 1
    fr2 = FundingRates(engine)  # a new instance reads kv
    await fr2.ensure(client, "BTC", T0, T0 + timedelta(hours=3), now=now)
    assert len(client.funding_calls) == n
    assert fr2.rates("BTC", T0, T0 + timedelta(hours=3)) == [Decimal("0.0001")] * 3
    with Session(engine) as s:
        assert s.scalars(select(Kv.key).where(Kv.key.like("hl:funding:BTC:%"))).all()


def test_holding_cost_without_funding_is_zero(priced):
    assert HLExecutionModel(priced).holding_cost(pos("long"), T0, T0 + timedelta(hours=5)) \
        == Decimal(0)


def test_model_runs_in_core_simulator(priced):
    m = HLExecutionModel(priced)
    res = simulate([ev("open", "long", ref="a"),
                    ev("close", "long", ref="b", seq=1)], m, PaperConfig(), T0 - timedelta(hours=1))
    assert [f.missed for f in res.fills] == [False, False]
    assert res.closed_trades[0].pnl_usd < 0  # spread + slippage + fees, same mid


# -- FillWatcher: ingest ---------------------------------------------------------------

def watcher(engine, client=None, follows=None, **kw):
    kw.setdefault("start_time", T0)
    kw.setdefault("fetch_leverage", False)
    return FillWatcher(engine, client or FakeClient(), follows or {A: T0}, **kw)


async def test_ingest_stores_and_emits_with_honest_delay(engine):
    got: list[TradeEvent] = []
    w = watcher(engine, on_fill=got.append)
    f = mk("BTC", "B", "1", "100", "0", T0_MS + 1000, tid=1)
    obs = T0 + timedelta(seconds=3)
    evs = await w.ingest(A, [f], observed=obs, via="ws")
    assert got == evs and len(evs) == 1
    assert evs[0].observed_time == obs and evs[0].leader_time == T0 + timedelta(seconds=1)
    with Session(engine) as s:
        row = s.get(PaperEvent, evs[0].source_ref)
        assert row.delay_ms == 2000 and row.seq == evs[0].seq and row.address == A
        assert event_from_json(row.json) == evs[0]
        assert s.scalar(select(Fill.tid).where(Fill.address == A)) == 1
    # idempotent on source_ref: the same fill again is not re-emitted
    assert await w.ingest(A, [f], observed=obs + timedelta(seconds=9), via="poll") == []
    assert len(got) == 1


async def test_ingest_ignores_fills_before_start(engine):
    w = watcher(engine, follows={A: T0 + timedelta(minutes=5)})
    old = mk("BTC", "B", "1", "100", "0", T0_MS + 60_000, tid=2)  # after watcher start,
    evs = await w.ingest(A, [old], observed=T0 + timedelta(minutes=6), via="ws")
    assert evs == []  # but before the follow started
    with Session(engine) as s:
        assert s.scalar(select(Fill.tid)) == 2  # stored anyway


async def test_snapshot_is_stored_never_emitted(engine):
    w = watcher(engine)
    f = mk("BTC", "B", "1", "100", "0", T0_MS + 1000, tid=3)
    assert await w.ingest(A, [f], observed=T0 + timedelta(seconds=5), via="ws",
                          snapshot=True) == []
    with Session(engine) as s:
        assert s.scalar(select(Fill.tid)) == 3
        assert s.scalars(select(PaperEvent)).all() == []


async def test_seq_persisted_across_batches_and_restarts(engine):
    w = watcher(engine)
    # a flip: close leg must get a lower seq than the open leg
    b1 = [mk("BTC", "B", "1", "100", "0", T0_MS + 1000, tid=10),
          mk("BTC", "A", "3", "101", "1", T0_MS + 2000, tid=11)]
    e1 = await w.ingest(A, b1, observed=T0 + timedelta(seconds=3), via="ws")
    assert [(e.side, e.direction) for e in e1] == [("open", "long"), ("close", "long"),
                                                   ("open", "short")]
    assert [e.seq for e in e1] == [1, 2, 3]
    e2 = await w.ingest(A, [mk("BTC", "B", "2", "99", "-2", T0_MS + 4000, tid=12)],
                        observed=T0 + timedelta(seconds=5), via="ws")
    assert [e.seq for e in e2] == [4]
    # the continuing short episode keeps its position id (context from stored fills)
    assert e2[0].side == "close" and e2[0].position_id == e1[2].position_id
    w2 = watcher(engine)  # restart: the kv counter survives
    e3 = await w2.ingest(A, [mk("BTC", "B", "1", "99", "0", T0_MS + 6000, tid=13)],
                         observed=T0 + timedelta(seconds=7), via="ws")
    assert [e.seq for e in e3] == [5]
    with Session(engine) as s:
        assert s.get(Kv, "hl:seq:" + A).value == "5"


async def test_leverage_attached_when_fetched(engine):
    w = watcher(engine, fetch_leverage=True)
    evs = await w.ingest(A, [mk("BTC", "B", "1", "100", "0", T0_MS + 1, tid=20)],
                         observed=T0 + timedelta(seconds=1), via="ws")
    assert evs[0].leverage == Decimal("5")


# -- FillWatcher: WS / reconnect / polling ---------------------------------------------

async def test_ws_live_fill_emitted_snapshot_suppressed(engine):
    snap = mk("BTC", "B", "1", "100", "0", T0_MS - 60_000, tid=30)
    live_fill = {}

    async def script(ws, subs):
        await wait_subs(subs, 1)
        await ws.send(fills_msg(A, [snap], snapshot=True))
        live_fill["f"] = mk("BTC", "B", "1", "100", "0", ms(datetime.now(UTC)) - 200, tid=31)
        await ws.send(fills_msg(A, [live_fill["f"]]))
        await ws.wait_closed()

    got: list[TradeEvent] = []
    async with FakeHL([script]) as hl:
        w = watcher(engine, ws_url=hl.url, on_fill=got.append,
                    start_time=datetime.now(UTC) - timedelta(minutes=5),
                    follows={A: None})
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await wait_for(lambda: len(got) >= 1)
        await asyncio.sleep(0.05)
        stop.set()
        await task
        assert hl.subs[0] == [{"type": "userFills", "user": A}]
    assert [e.source_ref for e in got] == [f"0xh31:31"]
    with Session(engine) as s:
        assert set(s.scalars(select(Fill.tid))) == {30, 31}
        row = s.get(PaperEvent, "0xh31:31")
        assert 0 <= row.delay_ms < 5000


async def test_reconnect_backfill_observed_is_reconnect_time(engine):
    start = datetime.now(UTC) - timedelta(hours=1)
    gap_fill = mk("BTC", "B", "1", "100", "0", ms(start) + 10 * 60_000, tid=40)
    client = FakeClient()

    async def first(ws, subs):
        await wait_subs(subs, 1)
        await ws.send(fills_msg(A, [], snapshot=True))
        client.fills[A] = [gap_fill]  # a fill happens while we are disconnected
        await ws.close()

    async def second(ws, subs):
        await wait_subs(subs, 1)
        await ws.send(fills_msg(A, [gap_fill], snapshot=True))  # in the snapshot too
        await ws.wait_closed()

    got: list[TradeEvent] = []
    async with FakeHL([first, second]) as hl:
        w = watcher(engine, client, ws_url=hl.url, on_fill=got.append, start_time=start,
                    follows={A: None}, reconnect_base_s=0.01)
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await wait_for(lambda: len(got) >= 1)
        stop.set()
        await task
    assert hl.connections == 2 and len(w.connects) == 2
    e = got[0]
    assert e.source_ref == "0xh40:40"
    # the recovery time (REST answer right after the reconnect), not the leader time
    assert w.connects[1] <= e.observed_time < w.connects[1] + timedelta(seconds=2)
    assert e.observed_time - e.leader_time > timedelta(minutes=40)
    # backfill asked REST from the last seen fill time (here: the watcher start)
    assert (A, ms(start)) in client.calls
    with Session(engine) as s:
        row = s.get(PaperEvent, "0xh40:40")
        assert row.delay_ms == ms(e.observed_time) - gap_fill["time"]
        assert row.json["via"] == "recovery"


async def test_polling_fallback_beyond_subscription_cap(engine):
    addrs = [f"0x{i:040x}" for i in range(1, 12)]  # 11 follows, WS cap 10
    follows = {a: None for a in addrs}
    client = FakeClient()
    start = datetime.now(UTC) - timedelta(minutes=1)
    polled = addrs[-1]
    client.fills[polled] = [mk("BTC", "A", "1", "100", "0", ms(start) + 1000, tid=50)]
    got: list[TradeEvent] = []
    async with FakeHL() as hl:
        w = watcher(engine, client, follows=follows, ws_url=hl.url, on_fill=got.append,
                    start_time=start, poll_interval_s=0.05, weight_per_min=10**9)
        assert live.WS_MAX_USER_SUBS == 10
        assert w.ws_addresses == addrs[:10] and w.poll_addresses == [polled]
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await wait_for(lambda: len(got) >= 1 and len(hl.subs) >= 1 and len(hl.subs[0]) >= 10)
        await wait_for(lambda: sum(1 for a, _ in client.calls if a == polled) >= 3)
        stop.set()
        await task
        users = {s["user"] for s in hl.subs[0]}
    assert users == set(addrs[:10])  # never more than 10 users on WS
    assert got[0].wallet == polled and got[0].direction == "short"
    with Session(engine) as s:
        assert s.get(PaperEvent, got[0].source_ref).json["via"] == "poll"


def test_poll_interval_respects_weight_budget(engine):
    addrs = [f"0x{i:040x}" for i in range(1, 26)]  # 25 follows -> 15 polled
    w = watcher(engine, follows={a: None for a in addrs})
    # 15 x 20 weight per cycle against half of 1200/min -> one cycle per 30 s
    assert w.poll_interval() == pytest.approx(30.0)
    w2 = watcher(engine, follows={a: None for a in addrs[:11]})
    assert w2.poll_interval() == pytest.approx(5.0)  # 1 polled: the 5 s floor
