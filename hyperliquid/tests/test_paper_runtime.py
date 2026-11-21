"""Task 9: paper runtime -- run_paper (simulate + gates + ladder), P11 reconcile,
funding ensure, and an end-to-end follow -> fills -> promotion integration test."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_core.events import TradeEvent
from hades_hl import follows as fl
from hades_hl import paper_runtime as pr
from hades_hl.client import FundingPage
from hades_hl.db import (Alert, Follow, Kv, LadderHistory, PaperEvent, Price, init_db,
                         make_engine)
from hades_hl.live import FillWatcher, FundingRates, PriceRecorder, event_to_json
from hades_hl.pipeline import kv_get_json, kv_set_json
from hlfills import mk

UTC = timezone.utc
START = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
A = "0x" + "a1" * 20


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    init_db(eng)
    with Session(eng) as s:
        fl.add_follow(s, A, START)
        s.commit()
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def price(target, coin, at, mid):
    """Record a mid: through ``target`` when it is a Session (it may hold writes), else
    with a PriceRecorder on the engine."""
    if isinstance(target, Session):
        target.add(Price(coin=coin, time=ms(at), mid=str(mid)))
        target.flush()
        return
    PriceRecorder(target, min_interval_s=0, refresh_s=0).record({coin: str(mid)}, at=at)


def store(s, side, at, *, coin="BTC", direction="long", size="1", pid=None, start=None,
          leverage=None) -> TradeEvent:
    key = f"hl:seq:{A}"
    seq = int(kv_get_json(s, key) or 0) + 1
    kv_set_json(s, key, seq)
    e = TradeEvent(head="hyperliquid", wallet=A, market_id=coin, side=side, direction=direction,
                   size=Decimal(size), price=Decimal("100"), notional_usd=Decimal(size) * 100,
                   fee_usd=Decimal("0"), leader_time=at - timedelta(seconds=1), observed_time=at,
                   source_ref=f"0xh{seq}:{seq}", leverage=leverage,
                   position_id=pid or f"{coin}:1", seq=seq)
    js = event_to_json(e)
    if start is not None:
        js["leader_start"] = str(start)
    s.add(PaperEvent(source_ref=e.source_ref, address=A, json=js, observed_time=at,
                     delay_ms=1000, seq=seq))
    s.flush()
    return e


def round_trip(engine, s, i, t_open, pnl_up=True, coin="BTC"):
    """Leader opens and closes one long; our recorded mid moves 100 -> 105 (or 95)."""
    t_close = t_open + timedelta(hours=1)
    price(s, coin, t_open + timedelta(seconds=1), 100)
    price(s, coin, t_close + timedelta(seconds=1), 105 if pnl_up else 90)
    store(s, "open", t_open, coin=coin, pid=f"{coin}:{i}", start=0)
    store(s, "close", t_close, coin=coin, pid=f"{coin}:{i}", start=1)


# -- run_paper -----------------------------------------------------------------------

def test_run_paper_simulates_and_snapshots(engine, session):
    round_trip(engine, session, 1, START + timedelta(hours=1))
    now = START + timedelta(days=2)
    summ = pr.run_paper(session, A, now)
    assert summ.address == A and summ.ladder == "PAPER" and summ.origin == "manual"
    assert summ.closed_trades == 1 and summ.net_pnl_usd > 0
    assert summ.equity_usd == Decimal("10000") + summ.net_pnl_usd
    assert summ.days_followed == 2 and Decimal("0") < summ.max_dd < Decimal("0.001")  # fees only
    snap = pr.snapshot(session, A)
    assert snap["summary"]["closed_trades"] == 1
    assert snap["last_seq"] == 2 and snap["open_positions"] == []
    assert snap["winrate"] == 1.0 and snap["n"] == 1
    assert snap["gate"]["passed"] is False and snap["rolling"]["checks"]["net_pnl"] is True
    assert snap["computed_at"] == now.isoformat()


def test_run_paper_ignores_events_after_now(engine, session):
    round_trip(engine, session, 1, START + timedelta(hours=1))
    summ = pr.run_paper(session, A, START + timedelta(hours=1, minutes=30))
    assert summ.closed_trades == 0
    assert [p["position_id"] for p in pr.snapshot(session, A)["open_positions"]] == ["BTC:1"]


def test_run_paper_marks_open_positions_from_recorded_prices(engine, session):
    t1 = START + timedelta(hours=1)
    price(session, "BTC", t1 + timedelta(seconds=1), 100)
    store(session, "open", t1, coin="BTC", pid="BTC:1", start=0)
    price(session, "BTC", t1 + timedelta(minutes=10), 50)  # BTC halves meanwhile
    t2 = t1 + timedelta(minutes=20)
    price(session, "ETH", t2 + timedelta(seconds=1), 10)
    store(session, "open", t2, coin="ETH", pid="ETH:1", start=0)
    pr.run_paper(session, A, t2 + timedelta(minutes=1))
    snap = pr.snapshot(session, A)
    # the BTC copy (500 margin, 1x) lost ~250 by the mark taken at the ETH event
    assert Decimal(snap["summary"]["equity_usd"]) < Decimal("9760")


def test_promotion_writes_history_row_and_alert(engine, session):
    for i in range(30):
        round_trip(engine, session, i, START + timedelta(days=i, hours=1))
    now = START + timedelta(days=30)
    summ = pr.run_paper(session, A, now)
    assert summ.ladder == "NOTIF" and summ.closed_trades == 30
    f = session.get(Follow, A)
    assert f.ladder == "NOTIF" and f.ladder_changed_at == now
    hist = session.scalars(select(LadderHistory).where(LadderHistory.address == A)
                           .order_by(LadderHistory.id)).all()
    assert [(h.from_, h.to) for h in hist] == [(None, "PAPER"), ("PAPER", "NOTIF")]
    assert hist[-1].gate_json["full"]["passed"] is True
    assert hist[-1].gate_json["full"]["checks"]["closed_trades"] is True
    assert "rolling" in hist[-1].gate_json
    alerts = session.scalars(select(Alert).where(Alert.kind == "ladder")).all()
    assert len(alerts) == 1 and alerts[0].payload_json["to"] == "NOTIF"
    assert alerts[0].payload_json["head"] == "hyperliquid"
    # a re-run changes nothing
    pr.run_paper(session, A, now + timedelta(minutes=1))
    assert len(session.scalars(select(LadderHistory)).all()) == 2
    assert len(session.scalars(select(Alert)).all()) == 1


def test_not_promoted_before_28_days(engine, session):
    for i in range(30):
        round_trip(engine, session, i, START + timedelta(hours=i * 2 + 1))
    summ = pr.run_paper(session, A, START + timedelta(days=10))
    assert summ.ladder == "PAPER" and summ.closed_trades == 30


def test_demotion_on_rolling_loss(engine, session):
    f = session.get(Follow, A)
    f.ladder = "NOTIF"
    session.flush()
    round_trip(engine, session, 1, START + timedelta(days=40), pnl_up=False)
    now = START + timedelta(days=41)
    summ = pr.run_paper(session, A, now)
    assert summ.ladder == "PAPER"
    h = session.scalars(select(LadderHistory).order_by(LadderHistory.id.desc())).first()
    assert (h.from_, h.to) == ("NOTIF", "PAPER")
    assert h.gate_json["rolling"]["checks"]["net_pnl"] is False
    assert session.scalars(select(Alert).where(Alert.kind == "ladder")).one()


def test_notif_wallet_new_events_alert_once(engine, session):
    session.get(Follow, A).ladder = "NOTIF"
    for i in range(3):  # keep the rolling window profitable
        round_trip(engine, session, i, START + timedelta(days=1, hours=i * 2))
    pr.run_paper(session, A, START + timedelta(days=2))
    assert len(session.scalars(select(Alert).where(Alert.kind.in_(["open", "close"]))).all()) == 6
    round_trip(engine, session, 9, START + timedelta(days=2, hours=1))
    pr.run_paper(session, A, START + timedelta(days=3))
    assert len(session.scalars(select(Alert).where(Alert.kind.in_(["open", "close"]))).all()) == 8


def test_run_paper_unknown_follow_raises(session):
    with pytest.raises(KeyError):
        pr.run_paper(session, "0x" + "ff" * 20, START)


def test_needs_run(engine, session):
    assert pr.needs_run(session, A, START) is True  # never run
    store(session, "open", START + timedelta(hours=1), start=0)
    pr.run_paper(session, A, START + timedelta(hours=2))
    assert pr.needs_run(session, A, START + timedelta(hours=2, minutes=1)) is False
    store(session, "close", START + timedelta(hours=3), start=1)
    assert pr.needs_run(session, A, START + timedelta(hours=3, minutes=1)) is True
    pr.run_paper(session, A, START + timedelta(hours=4))
    # no new events, but the gate depends on time too: re-run at least hourly
    assert pr.needs_run(session, A, START + timedelta(hours=5, minutes=1)) is True


# -- funding (P43) -------------------------------------------------------------------

class FakeClient:
    def __init__(self, positions=None):
        self.positions = positions if positions is not None else []
        self.state_calls: list[str] = []
        self.funding_calls: list[tuple[str, int, int | None]] = []

    async def clearinghouse_state(self, addr):
        self.state_calls.append(addr)
        return {"assetPositions": [{"position": {"coin": c, "szi": szi,
                                                 "leverage": {"value": 3}}}
                                   for c, szi in self.positions]}

    async def funding_history(self, coin, start_ms, end_ms=None):
        self.funding_calls.append((coin, start_ms, end_ms))
        h = start_ms // 3_600_000 * 3_600_000 + 3_600_000
        rows = []
        while end_ms is not None and h <= end_ms:
            rows.append({"coin": coin, "fundingRate": "0.0001", "premium": "0", "time": h})
            h += 3_600_000
        return FundingPage(rows)


async def test_run_paper_live_ensures_funding_before_simulate(engine, session):
    round_trip(engine, session, 1, START + timedelta(hours=1))
    c = FakeClient()
    funding = FundingRates(engine)
    session.commit()
    summ = await pr.run_paper_live(session, c, A, START + timedelta(days=1), funding=funding)
    assert [x[0] for x in c.funding_calls] == ["BTC"]
    assert c.funding_calls[0][1] <= ms(START + timedelta(hours=1))
    # 1 hour long at 0.0001/h on ~500 notional: funding is charged, so net is lower
    plain = pr.run_paper(session, A, START + timedelta(days=1))
    assert summ.net_pnl_usd < plain.net_pnl_usd


# -- P11 reconcile -------------------------------------------------------------------

def open_copy(engine, s, t=START + timedelta(hours=1)):
    price(s, "BTC", t + timedelta(seconds=1), 100)
    store(s, "open", t, coin="BTC", pid="BTC:1", start=0, size="2")
    pr.run_paper(s, A, t + timedelta(minutes=1))
    s.commit()


async def test_reconcile_emits_synthetic_close_when_leader_flat(engine, session):
    open_copy(engine, session)
    now = START + timedelta(hours=2)
    c = FakeClient(positions=[("ETH", "1.0")])  # flat in BTC
    evs = await pr.reconcile(session, c, A, now)
    assert len(evs) == 1
    e = evs[0]
    assert e.side == "close" and e.direction == "long" and e.market_id == "BTC"
    assert e.source_ref == f"reconcile:{A}:BTC:1:{ms(now)}"
    assert e.observed_time == now and e.seq == 2 and e.size == Decimal("2")
    assert e.position_id == "BTC:1"
    row = session.get(PaperEvent, e.source_ref)
    assert row.seq == 2 and row.json["via"] == "reconcile"
    assert session.get(Kv, f"hl:seq:{A}").value == "2"
    # priced by the model like any event
    price(session, "BTC", now + timedelta(seconds=1), 110)
    summ = pr.run_paper(session, A, now + timedelta(minutes=1))
    assert summ.closed_trades == 1 and pr.snapshot(session, A)["open_positions"] == []


async def test_reconcile_no_event_while_leader_holds(engine, session):
    open_copy(engine, session)
    c = FakeClient(positions=[("BTC", "2.0")])
    assert await pr.reconcile(session, c, A, START + timedelta(hours=2)) == []


async def test_reconcile_leader_flipped_counts_as_flat(engine, session):
    open_copy(engine, session)
    c = FakeClient(positions=[("BTC", "-1.0")])
    evs = await pr.reconcile(session, c, A, START + timedelta(hours=2))
    assert [e.side for e in evs] == ["close"]


async def test_reconcile_not_reemitted_until_processed(engine, session):
    open_copy(engine, session)
    c = FakeClient()
    now = START + timedelta(hours=2)
    assert len(await pr.reconcile(session, c, A, now)) == 1
    assert await pr.reconcile(session, c, A, now + timedelta(minutes=6)) == []
    # missed (no price): the copy stays open -> after processing, it may retry
    pr.run_paper(session, A, now + timedelta(minutes=7))
    assert len(await pr.reconcile(session, c, A, now + timedelta(minutes=12))) == 1


async def test_reconcile_skipped_without_open_positions_or_when_pending(engine, session):
    c = FakeClient()
    assert await pr.reconcile(session, c, A, START) == [] and c.state_calls == []
    open_copy(engine, session)
    assert await pr.reconcile(session, c, A, START + timedelta(hours=2),
                              pending=lambda a: True) == []
    assert c.state_calls == []


async def test_reconciler_throttles_to_once_per_5_min(engine, session):
    open_copy(engine, session)
    c = FakeClient(positions=[("BTC", "2")])
    r = pr.Reconciler()
    t = START + timedelta(hours=2)
    await r.maybe(session, c, A, t)
    await r.maybe(session, c, A, t + timedelta(minutes=4))
    assert len(c.state_calls) == 1
    await r.maybe(session, c, A, t + timedelta(minutes=5))
    assert len(c.state_calls) == 2


# -- integration ---------------------------------------------------------------------

class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


async def test_integration_follow_fills_paper_promotion(engine):
    """follow -> fake fills through the watcher -> paper_events -> run_paper ->
    promotion after the gate passes (fake clock) -> ladder alert."""
    clock = Clock(START)
    w = FillWatcher(engine, None, {A: START}, start_time=START, now=clock,
                    fetch_leverage=False, sz_decimals={})
    tid = 1000
    for i in range(30):
        t_open = START + timedelta(days=i, hours=1)
        t_close = t_open + timedelta(hours=2)
        for t, side, start_pos, mid in ((t_open, "B", "0", 100), (t_close, "A", "1", 104)):
            tid += 1
            f = mk("BTC", side, "1", str(mid), start_pos, ms(t), tid=tid)
            obs = t + timedelta(milliseconds=800)
            clock.t = obs
            price(engine, "BTC", obs + timedelta(seconds=1), mid)
            out = await w.ingest(A, [f], observed=obs, via="ws")
            assert len(out) == 1
    with Session(engine) as s:
        assert len(s.scalars(select(PaperEvent)).all()) == 60
        row = s.scalars(select(PaperEvent).order_by(PaperEvent.seq)).first()
        assert row.json["leader_start"] == "0"  # stored for the 25% alert rule
        summ = pr.run_paper(s, A, START + timedelta(days=27))
        assert summ.ladder == "PAPER"  # gate: < 28 days
        summ = pr.run_paper(s, A, START + timedelta(days=30))
        s.commit()
        assert summ.ladder == "NOTIF" and summ.closed_trades == 30
        assert summ.net_pnl_usd > 0
        al = s.scalars(select(Alert)).all()
        assert [(a.kind, a.payload_json["from"], a.payload_json["to"]) for a in al] == [
            ("ladder", "PAPER", "NOTIF")]
