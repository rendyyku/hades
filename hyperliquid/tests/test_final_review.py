"""Final-review fix wave (engine): C1 the P47 staleness rule on polled follows, I1 no
ladder flapping, I2 paper_events keyed per wallet, I3 the display-only "now" point
(P54), I4 reconcile never closes on an unusable clearinghouseState (P11)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from hades_hl import paper_runtime as pr
from hades_hl.client import HLClient, HLError
from hades_hl.db import Alert, Follow, Kv, LadderHistory, PaperEvent, init_db, make_engine
from hades_hl.live import FillWatcher
from hlfills import mk
from test_live import FakeClient as LiveClient
from test_paper_runtime import A, START, open_copy, price, round_trip, store
from test_paper_runtime import engine, session  # noqa: F401  (fixtures)

UTC = timezone.utc
H = timedelta(hours=1)
MIN = timedelta(minutes=1)
B = "0x" + "b2" * 20
INFO = "https://api.hyperliquid.xyz/info"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def polled(engine, client, clock, **kw):
    """A watcher whose only follow is POLLED (no WS slot), as the 11th+ follow is."""
    w = FillWatcher(engine, client, {A: START}, start_time=clock.t, now=clock,
                    ws_max_user_subs=0, fetch_leverage=False, sz_decimals={}, **kw)
    assert w.poll_addresses == [A] and w.ws_addresses == []
    return w


# -- C1: P47 on polled follows ----------------------------------------------------------

async def test_c1_polled_follow_stale_fill_after_restart_is_missed_never_priced(engine):
    t0 = START + timedelta(days=1)
    c = LiveClient()
    polled(engine, c, Clock(t0))  # first watch: persists the cursor, then the process stops
    now = t0 + 2 * H  # restart after a 2 h outage
    old = mk("BTC", "B", "1", "100", "0", ms(now - 20 * MIN), tid=11)
    fresh = mk("ETH", "B", "1", "10", "0", ms(now - MIN), tid=12)
    c.fills[A] = [old, fresh]
    got = []
    w = polled(engine, c, Clock(now), on_fill=got.append)
    await w._poll_once()
    assert [e.market_id for e in got] == ["BTC", "ETH"]  # both recorded, in order
    assert w.stale_missed == 1
    with Session(engine) as s:
        rows = {r.json["market_id"]: r for r in s.scalars(select(PaperEvent))}
        assert rows["BTC"].missed is True and rows["BTC"].settled_at == now
        assert rows["BTC"].fill_price is None and rows["BTC"].json["via"] == "poll"
        assert rows["BTC"].json["stale_recovery"] is True
        assert not rows["ETH"].missed and rows["ETH"].settled_at is None
    price(engine, "BTC", now + timedelta(seconds=1), 100)  # would price it, if asked
    price(engine, "ETH", now + timedelta(seconds=1), 10)
    with Session(engine) as s:
        pr.run_paper(s, A, now + MIN)
        snap = pr.snapshot(s, A)
    assert snap["missed"] == 1 and snap["events"] == 2
    assert [p["market_id"] for p in snap["open_positions"]] == ["ETH"]


async def test_c1_round_trip_made_during_outage_is_not_a_closed_paper_trade(engine):
    t0 = START + timedelta(days=1)
    c = LiveClient()
    polled(engine, c, Clock(t0))
    now = t0 + 2 * H
    c.fills[A] = [mk("BTC", "B", "1", "100", "0", ms(now - 90 * MIN), tid=21),
                  mk("BTC", "A", "1", "101", "1", ms(now - 30 * MIN), tid=22)]
    w = polled(engine, c, Clock(now))
    await w._poll_once()
    price(engine, "BTC", now + timedelta(seconds=1), 100)
    with Session(engine) as s:
        assert [r.missed for r in s.scalars(select(PaperEvent).order_by(PaperEvent.seq))] \
            == [True, True]
        summ = pr.run_paper(s, A, now + MIN)
        snap = pr.snapshot(s, A)
    assert summ.closed_trades == 0 and snap["n"] == 0 and snap["missed"] == 1
    assert snap["open_positions"] == []


@pytest.mark.parametrize("via", ["poll", "ws"])
async def test_c1_staleness_applies_to_every_live_ingest_path(engine, via):
    now = START + timedelta(days=1)
    w = FillWatcher(engine, LiveClient(), {A: START}, start_time=START, now=lambda: now,
                    fetch_leverage=False, sz_decimals={})
    old = mk("BTC", "B", "1", "100", "0", ms(now - 11 * MIN), tid=31)
    edge = mk("ETH", "B", "1", "10", "0", ms(now - 10 * MIN), tid=32)  # exactly 10 min: kept
    await w.ingest(A, [old, edge], observed=now, via=via)
    with Session(engine) as s:
        rows = {r.json["market_id"]: r for r in s.scalars(select(PaperEvent))}
    assert rows["BTC"].missed is True and rows["BTC"].settled_at == now
    assert rows["ETH"].missed is None and rows["ETH"].settled_at is None


# -- I1: no ladder flapping -------------------------------------------------------------

def _thirty_winners(engine, session):
    for i in range(30):
        round_trip(engine, session, i, START + timedelta(hours=8 * i + 1))


def _ladder_rows(session):
    return [(h.from_, h.to) for h in session.scalars(
        select(LadderHistory).where(LadderHistory.address == A).order_by(LadderHistory.id))]


def test_i1_rolling_demotion_is_not_undone_by_the_next_run(engine, session):
    _thirty_winners(engine, session)  # all in the first 10 days
    session.get(Follow, A).ladder = "NOTIF"
    session.flush()
    now = START + timedelta(days=45)  # idle for 35 days: rolling net is exactly 0
    summ = pr.run_paper(session, A, now)
    snap = pr.snapshot(session, A)
    assert snap["gate"]["passed"] is True and snap["rolling"]["checks"]["net_pnl"] is False
    assert summ.ladder == "PAPER"
    for k in range(1, 4):  # the hourly re-runs must not promote it back
        assert pr.run_paper(session, A, now + k * H).ladder == "PAPER"
    assert _ladder_rows(session) == [(None, "PAPER"), ("NOTIF", "PAPER")]
    assert len(session.scalars(select(Alert).where(Alert.kind == "ladder")).all()) == 1


def test_i1_paper_wallet_with_failing_rolling_gate_is_not_promoted(engine, session):
    _thirty_winners(engine, session)
    summ = pr.run_paper(session, A, START + timedelta(days=45))
    assert pr.snapshot(session, A)["gate"]["passed"] is True
    assert summ.ladder == "PAPER" and _ladder_rows(session) == [(None, "PAPER")]


def test_i1_repromotion_waits_for_the_cooldown_after_a_demotion(engine, session):
    _thirty_winners(engine, session)
    session.get(Follow, A).ladder = "NOTIF"
    session.flush()
    t = START + timedelta(days=45)
    assert pr.run_paper(session, A, t).ladder == "PAPER"  # demoted (idle window)
    # one hour later the rolling window is profitable again: both gates pass
    round_trip(engine, session, 99, t + 10 * MIN)
    assert pr.run_paper(session, A, t + 2 * H).ladder == "PAPER"  # cooling down
    snap = pr.snapshot(session, A)
    assert snap["gate"]["passed"] is True and snap["rolling"]["passed"] is True
    assert snap["ladder_cooldown_until"] == (t + pr.REPROMOTE_COOLDOWN).isoformat()
    just_before = t + pr.REPROMOTE_COOLDOWN - MIN
    assert pr.run_paper(session, A, just_before).ladder == "PAPER"
    assert pr.run_paper(session, A, t + pr.REPROMOTE_COOLDOWN).ladder == "NOTIF"
    assert pr.snapshot(session, A)["ladder_cooldown_until"] is None
    assert _ladder_rows(session) == [(None, "PAPER"), ("NOTIF", "PAPER"), ("PAPER", "NOTIF")]


# -- I2: paper_events are keyed per wallet ----------------------------------------------

async def test_i2_counterparties_of_one_trade_both_get_their_event(engine):
    now = START + timedelta(days=1)
    w = FillWatcher(engine, LiveClient(), {A: START, B: START}, start_time=START,
                    now=lambda: now, fetch_leverage=False, sz_decimals={})
    t = ms(now) - 1000
    buy = mk("BTC", "B", "1", "100", "0", t, tid=77, hash="0xsame")
    sell = mk("BTC", "A", "1", "100", "0", t, tid=77, hash="0xsame")
    got_a = await w.ingest(A, [buy], observed=now, via="ws")
    got_b = await w.ingest(B, [sell], observed=now, via="ws")
    assert [e.source_ref for e in got_a] == [e.source_ref for e in got_b] == ["0xsame:77"]
    assert (got_a[0].direction, got_b[0].direction) == ("long", "short")
    with Session(engine) as s:
        rows = {r.address: r for r in s.scalars(select(PaperEvent))}
        assert set(rows) == {A, B} and rows[A].seq == rows[B].seq == 1
        assert s.get(PaperEvent, (B, "0xsame:77")).json["direction"] == "short"
    # a replay of either side is still dropped
    assert await w.ingest(B, [sell], observed=now + MIN, via="ws") == []


def _old_paper_events_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE paper_events (source_ref VARCHAR NOT NULL, address VARCHAR "
                "NOT NULL, json JSON, observed_time DATETIME, delay_ms INTEGER, seq INTEGER, "
                "fill_price VARCHAR, missed BOOLEAN, settled_at DATETIME, "
                "PRIMARY KEY (source_ref))")
    con.execute("CREATE INDEX ix_paper_events_address ON paper_events (address)")
    con.execute("INSERT INTO paper_events VALUES ('0xh1:1', ?, '{\"market_id\": \"BTC\"}', "
                "'2026-10-01 00:00:00.000000', 1200, 1, '100.5', 0, "
                "'2026-10-01 00:01:00.000000')", (A,))
    con.execute("INSERT INTO paper_events VALUES ('0xh2:2', ?, '{}', "
                "'2026-10-01 00:02:00.000000', NULL, 2, NULL, 1, "
                "'2026-10-01 00:03:00.000000')", (A,))
    con.commit()
    con.close()


def test_i2_migration_rekeys_existing_paper_events_and_keeps_rows(tmp_path):
    path = tmp_path / "old.db"
    _old_paper_events_db(path)
    eng = make_engine(f"sqlite:///{path.as_posix()}")
    init_db(eng)
    init_db(eng)  # idempotent
    with eng.connect() as c:
        assert inspect(c).get_pk_constraint("paper_events")["constrained_columns"] \
            == ["address", "source_ref"]
        names = {r[0] for r in c.exec_driver_sql("SELECT name FROM sqlite_master")}
    assert "paper_events__new" not in names and "ix_paper_events_address" in names
    with Session(eng) as s:
        rows = s.scalars(select(PaperEvent).order_by(PaperEvent.seq)).all()
        assert [(r.source_ref, r.address, r.seq, r.missed) for r in rows] \
            == [("0xh1:1", A, 1, False), ("0xh2:2", A, 2, True)]
        assert rows[0].fill_price == Decimal("100.5") and rows[0].delay_ms == 1200
        assert rows[0].json == {"market_id": "BTC"}
        assert rows[0].settled_at == datetime(2026, 10, 1, 0, 1, tzinfo=UTC)
        # the same source_ref can now exist for another wallet
        s.add(PaperEvent(source_ref="0xh1:1", address=B, json={}, observed_time=START, seq=1))
        s.commit()
        assert len(s.scalars(select(PaperEvent)).all()) == 3


def test_i2_fresh_db_has_the_per_wallet_key(engine):
    with engine.connect() as c:
        assert inspect(c).get_pk_constraint("paper_events")["constrained_columns"] \
            == ["address", "source_ref"]


# -- I3 / P54: the "now" point is display-only -------------------------------------------

def _open_10x(session, t):
    price(session, "BTC", t + timedelta(seconds=1), 100)
    store(session, "open", t, coin="BTC", pid="BTC:1", start=0, leverage=Decimal(10))


def test_i3_now_price_past_liquidation_never_liquidates(engine, session):
    t = START + H + 5 * MIN
    _open_10x(session, t)
    now = t + 20 * MIN  # no hour boundary between the runs below
    price(session, "BTC", now - timedelta(seconds=5), 80)  # a wick: 20% under the entry
    summ = pr.run_paper(session, A, now)
    snap = pr.snapshot(session, A)
    assert summ.closed_trades == 0 and snap["n"] == 0
    assert [p["position_id"] for p in snap["open_positions"]] == ["BTC:1"]
    assert not any(f["ref"].startswith("liquidation") for f in snap["fills"])
    # shown at the capped loss: the whole margin (5% of 10k), plus the entry fee
    assert Decimal("9490") < summ.equity_usd < Decimal("9500")
    assert snap["equity_curve"][-1] == [now.isoformat(), str(summ.equity_usd)]
    # the wick is gone a few minutes later: the same position, never a vanished trade
    later = now + 10 * MIN
    price(session, "BTC", later - timedelta(seconds=5), 100)
    summ2 = pr.run_paper(session, A, later)
    snap2 = pr.snapshot(session, A)
    assert summ2.closed_trades == 0
    assert [p["position_id"] for p in snap2["open_positions"]] == ["BTC:1"]
    assert snap2["fills"] == snap["fills"]
    assert summ2.equity_usd > Decimal("9990")
    # the first run's display point is not part of the later history
    assert now.isoformat() not in [p[0] for p in snap2["equity_curve"]]


def test_i3_now_point_never_changes_the_gates_or_the_ladder(engine, session):
    for i in range(3):  # a profitable rolling window
        round_trip(engine, session, i, START + timedelta(days=1, hours=2 * i))
    session.get(Follow, A).ladder = "NOTIF"
    t = START + timedelta(days=2, minutes=5)
    _open_10x(session, t)
    now = t + 20 * MIN
    price(session, "BTC", now - timedelta(seconds=5), 91)  # -9% x 10 on the live point
    summ = pr.run_paper(session, A, now)
    snap = pr.snapshot(session, A)
    assert summ.net_pnl_usd < 0  # the display shows the unrealized loss ...
    assert snap["rolling"]["passed"] is True  # ... the gates read persisted points only
    assert Decimal(snap["rolling"]["values"]["net_pnl"]) > 0
    assert summ.ladder == "NOTIF"
    assert session.scalars(select(Alert).where(Alert.kind == "ladder")).all() == []
    # once an hourly mark has persisted the loss, the rolling gate demotes (P15)
    hour = START + timedelta(days=2, hours=1)
    price(session, "BTC", hour - timedelta(seconds=30), 91)
    summ = pr.run_paper(session, A, hour + 2 * MIN)
    assert summ.ladder == "PAPER"
    assert pr.snapshot(session, A)["rolling"]["checks"]["net_pnl"] is False


# -- I4 / P11: reconcile never closes on an unusable state --------------------------------

class StateClient:
    def __init__(self, state=None, by_dex=None, fail=False):
        self.state, self.by_dex, self.fail = state, by_dex or {}, fail
        self.calls: list[tuple[str, str | None]] = []

    async def clearinghouse_state(self, addr, dex=None):
        self.calls.append((addr, dex))
        if self.fail:
            raise HLError("boom")
        return self.by_dex[dex] if dex in self.by_dex else self.state


def state(positions=(), ntl="0"):
    return {"marginSummary": {"accountValue": "1000", "totalNtlPos": ntl},
            "assetPositions": [{"type": "oneWay", "position": {"coin": c, "szi": szi}}
                               for c, szi in positions], "time": 1}


BAD_STATES = [
    None, {}, [], "ok", {"assetPositions": None}, {"assetPositions": []},
    {"marginSummary": {"accountValue": "1"}},  # no position list at all
    {"marginSummary": {}, "assetPositions": {}},
    {"marginSummary": {"accountValue": "1"}, "assetPositions": [{"position": {"coin": "ETH"}}]},
    {"marginSummary": {"accountValue": "1"}, "assetPositions": [None]},
    {"marginSummary": {"accountValue": "1"},
     "assetPositions": [{"position": {"coin": "ETH", "szi": "abc"}}]},
    # "no positions" but a non-zero position notional: a partial answer
    {"marginSummary": {"accountValue": "1", "totalNtlPos": "5000.5"}, "assetPositions": []},
]


@pytest.mark.parametrize("bad", BAD_STATES)
async def test_i4_reconcile_skips_unusable_state(engine, session, bad, caplog):
    open_copy(engine, session)
    c = StateClient(bad)
    with caplog.at_level("WARNING"):
        assert await pr.reconcile(session, c, A, START + 2 * H) == []
    assert c.calls == [(A, None)]
    assert session.scalars(select(PaperEvent).where(
        PaperEvent.source_ref.like("reconcile:%"))).all() == []
    assert session.get(Kv, f"hl:seq:{A}").value == "1"  # no seq taken
    assert "reconcile" in caplog.text


async def test_i4_reconcile_skips_on_failed_call(engine, session):
    open_copy(engine, session)
    assert await pr.reconcile(session, StateClient(fail=True), A, START + 2 * H) == []


async def test_i4_well_formed_flat_state_still_closes(engine, session):
    open_copy(engine, session)
    evs = await pr.reconcile(session, StateClient(state()), A, START + 2 * H)
    assert [(e.side, e.market_id) for e in evs] == [("close", "BTC")]


async def test_i4_hip3_position_is_checked_against_its_own_dex(engine, session):
    t = START + H
    price(session, "xyz:TSLA", t + timedelta(seconds=1), 100)
    store(session, "open", t, coin="xyz:TSLA", pid="xyz:TSLA:1", start=0)
    price(session, "BTC", t + timedelta(seconds=1), 100)
    store(session, "open", t, coin="BTC", pid="BTC:1", start=0)
    pr.run_paper(session, A, t + MIN)
    session.commit()
    # the main dex never lists a HIP-3 position: only the dex's own state can say "flat"
    c = StateClient(state([("BTC", "1")]), by_dex={"xyz": state([("xyz:TSLA", "2")])})
    assert await pr.reconcile(session, c, A, START + 2 * H) == []
    assert sorted(c.calls, key=str) == sorted([(A, None), (A, "xyz")], key=str)
    # the dex state is unusable: its coin is skipped, the main dex coin still reconciles
    c = StateClient(state(), by_dex={"xyz": {}})
    evs = await pr.reconcile(session, c, A, START + 2 * H + 6 * MIN)
    assert [e.market_id for e in evs] == ["BTC"]


@respx.mock
@pytest.mark.parametrize("body", [None, {}, [], {"assetPositions": []},
                                  {"marginSummary": {}, "assetPositions": None}])
async def test_client_clearinghouse_state_rejects_malformed_answers(body):
    respx.post(INFO).mock(return_value=httpx.Response(200, json=body))
    async with HLClient(max_attempts=1) as c:
        with pytest.raises(HLError):
            await c.clearinghouse_state(A)


@respx.mock
async def test_client_clearinghouse_state_passes_the_dex(fixture):
    route = respx.post(INFO).mock(return_value=httpx.Response(
        200, json=fixture("clearinghouse_state.json")))
    async with HLClient(max_attempts=1) as c:
        s = await c.clearinghouse_state(A, dex="xyz")
        await c.clearinghouse_state(A)
    assert isinstance(s["assetPositions"], list)
    assert json.loads(route.calls[0].request.content) == {
        "type": "clearinghouseState", "user": A, "dex": "xyz"}
    assert json.loads(route.calls[1].request.content) == {
        "type": "clearinghouseState", "user": A}


def test_i1_api_serves_the_promotion_cooldown(engine, session):
    from fastapi.testclient import TestClient

    from hades_hl import api
    from hades_hl.config import Settings

    _thirty_winners(engine, session)
    session.get(Follow, A).ladder = "NOTIF"
    session.flush()
    t = START + timedelta(days=45)
    pr.run_paper(session, A, t)
    session.commit()
    app = api.create_app(Settings(API_KEY="k", DATABASE_URL=engine.url.render_as_string(False)))
    client = TestClient(app)
    p = client.get(f"/api/paper/{A}", headers={"X-Hades-Key": "k"}).json()
    assert p["ladder_cooldown_until"] is None  # the demoting run itself
    round_trip(engine, session, 99, t + 10 * MIN)
    pr.run_paper(session, A, t + 2 * H)
    session.commit()
    p = client.get(f"/api/paper/{A}", headers={"X-Hades-Key": "k"}).json()
    assert p["summary"]["ladder"] == "PAPER" and p["gate"]["full"]["passed"] is True
    assert datetime.fromisoformat(p["ladder_cooldown_until"]) == t + pr.REPROMOTE_COOLDOWN


async def test_i4_event_arriving_during_the_state_call_keeps_its_coin_busy(engine, session):
    open_copy(engine, session)
    now = START + 2 * H

    class Racing(StateClient):
        async def clearinghouse_state(self, addr, dex=None):
            # the leader's real close is ingested while we wait for the answer
            store(session, "close", now - timedelta(seconds=2), coin="BTC", pid="BTC:1", start=2)
            return await super().clearinghouse_state(addr, dex)

    assert await pr.reconcile(session, Racing(state()), A, now) == []
