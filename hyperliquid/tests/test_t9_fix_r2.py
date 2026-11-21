"""Task 9 fix round 2: event-time marks never use a stale hourly mark (N1), batched
price pruning (P55), and the minors (no duplicate first curve point, a not-yet-final
hour is not a curve point, empty mark hours are remembered)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from hades_hl import paper_runtime as pr
from hades_hl import runtime as rt
from hades_hl.db import Mark, Price
from test_paper_runtime import A, START, ms, price, store
from test_paper_runtime import engine, session  # noqa: F401  (fixtures)

H = timedelta(hours=1)


def curve(snap: dict) -> list[tuple[str, Decimal]]:
    return [(t, Decimal(eq)) for t, eq in snap["equity_curve"]]


# -- N1: no stale hourly mark at event times -----------------------------------------

def test_n1_open_far_from_previous_hour_mark_is_not_liquidated(engine, session):
    """10:00 mark BTC = 85; a 10x BTC long fills at 10:30 at ~100; an ETH open at
    10:40. The 10:00 mark predates the fill and must not be applied at 10:40."""
    h10 = START + 10 * H
    session.add(Mark(coin="BTC", hour_ts=ms(h10), mid="85"))
    session.flush()
    price(session, "BTC", h10 + timedelta(minutes=30, seconds=1), 100)
    price(session, "ETH", h10 + timedelta(minutes=40, seconds=1), 10)
    store(session, "open", h10 + timedelta(minutes=30), coin="BTC", pid="BTC:1", start=0,
          leverage=Decimal("10"))
    store(session, "open", h10 + timedelta(minutes=40), coin="ETH", pid="ETH:1", start=0,
          leverage=Decimal("10"))
    summ = pr.run_paper(session, A, h10 + timedelta(minutes=50))
    snap = pr.snapshot(session, A)
    assert not any(f["ref"].startswith("liquidation") for f in snap["fills"])
    assert sorted(p["market_id"] for p in snap["open_positions"]) == ["BTC", "ETH"]
    assert summ.closed_trades == 0
    pts = dict(curve(snap))
    at40 = (h10 + timedelta(minutes=40)).isoformat()
    # equity at the ETH event: bankroll minus the two open fees, BTC at its fill price
    assert Decimal("9990") < pts[at40] < Decimal("10000")
    assert summ.net_pnl_usd > Decimal("-10")


def test_n1_hourly_mark_after_the_fill_still_applies(engine, session):
    """A persisted mark at an hour after the fill is used at that hour (the drawdown is
    in the curve), and the next event does not re-apply it."""
    t = START + 10 * H + timedelta(minutes=30)
    price(session, "BTC", t + timedelta(seconds=1), 100)
    store(session, "open", t, coin="BTC", pid="BTC:1", start=0, leverage=Decimal("1"))
    session.add(Mark(coin="BTC", hour_ts=ms(START + 11 * H), mid="80"))
    session.flush()
    price(session, "ETH", START + 11 * H + timedelta(minutes=10, seconds=1), 10)
    store(session, "open", START + 11 * H + timedelta(minutes=10), coin="ETH", pid="ETH:1",
          start=0, leverage=Decimal("1"))
    pr.run_paper(session, A, START + 11 * H + timedelta(minutes=30))
    pts = dict(curve(pr.snapshot(session, A)))
    at11 = (START + 11 * H).isoformat()
    at1110 = (START + 11 * H + timedelta(minutes=10)).isoformat()
    assert pts[at11] < Decimal("9910")  # 1x long, 100 -> 80 on a 100 margin: -20
    # the ETH event carries BTC's last known price (the 11:00 mark), not an older one
    assert abs(pts[at1110] - pts[at11]) < Decimal("1")


def test_marks_call_returns_hourly_only_at_final_hour_boundaries(engine, session):
    now = START + 12 * H + timedelta(seconds=30)
    for h, mid in ((10, "85"), (11, "90"), (12, "95")):
        session.add(Mark(coin="BTC", hour_ts=ms(START + h * H), mid=mid))
    session.flush()
    m = pr.Marks(session, {"BTC"}, START, now)
    assert m("BTC", START + 10 * H) == Decimal("85")
    assert m("BTC", START + 10 * H + timedelta(minutes=40)) is None  # event time
    assert m("BTC", START + 11 * H) == Decimal("90")
    assert m("BTC", START + 12 * H) is None  # not final yet (< 60 s past)


# -- minors --------------------------------------------------------------------------

def test_follow_started_on_the_hour_has_no_duplicate_first_point(engine, session):
    assert START.minute == 0 and START.second == 0
    t = START + timedelta(minutes=5)
    price(session, "BTC", t + timedelta(seconds=1), 100)
    store(session, "open", t, start=0)
    pr.run_paper(session, A, START + 3 * H + timedelta(minutes=5))
    ts = [t for t, _ in curve(pr.snapshot(session, A))]
    assert len(ts) == len(set(ts))
    assert ts[0] == START.isoformat()


def test_hour_not_final_is_not_a_curve_point(engine, session):
    t = START + H + timedelta(minutes=5)
    price(session, "BTC", t + timedelta(seconds=1), 100)
    store(session, "open", t, start=0)
    price(session, "BTC", START + 2 * H - timedelta(seconds=5), 90)
    at2 = (START + 2 * H).isoformat()
    pr.run_paper(session, A, START + 2 * H + timedelta(seconds=30))
    assert at2 not in dict(curve(pr.snapshot(session, A)))
    pr.run_paper(session, A, START + 2 * H + timedelta(seconds=90))
    pts = dict(curve(pr.snapshot(session, A)))
    assert at2 in pts and pts[at2] < Decimal("9990")  # marked at 90 once final


def test_record_marks_remembers_empty_hours(engine, session, monkeypatch):
    calls: list = []
    orig = pr.RecordedPrices.mark

    def spy(self, coin, at):
        calls.append((coin, at))
        return orig(self, coin, at)

    monkeypatch.setattr(pr.RecordedPrices, "mark", spy)
    now = START + 10 * H + timedelta(minutes=5)
    price(session, "BTC", START + 5 * H - timedelta(minutes=1), 100)
    assert pr.record_marks(session, {"BTC", "ETH"}, START, now) == 1  # BTC 05:00 only
    first = len(calls)
    assert first == 2 * 11  # hours 00:00 .. 10:00 for two coins
    calls.clear()
    assert pr.record_marks(session, {"BTC", "ETH"}, START, now) == 0
    assert calls == []  # every hour is either marked or remembered empty
    later = now + H
    pr.record_marks(session, {"BTC", "ETH"}, START, later)
    assert sorted(c for c, _ in calls) == ["BTC", "ETH"]  # only the new 11:00 hour


# -- P55: batched prune --------------------------------------------------------------

def test_prune_prices_deletes_in_batches_committing_each(engine, session, monkeypatch):
    now = START + timedelta(days=10)
    old = [Price(coin="BTC", time=ms(now - timedelta(days=3)) + i, mid="1") for i in range(5)]
    session.add_all(old + [Price(coin="BTC", time=ms(now - timedelta(days=1)), mid="2")])
    session.commit()
    commits: list[int] = []
    orig = session.commit
    monkeypatch.setattr(session, "commit", lambda: (commits.append(1), orig())[1])
    assert rt.prune_prices(session, now, batch_size=2) == 5
    assert len(commits) == 3  # 2 + 2 + 1
    assert [p.mid for p in session.scalars(select(Price))] == [Decimal("2")]
    assert rt.PRUNE_BATCH == 50_000
