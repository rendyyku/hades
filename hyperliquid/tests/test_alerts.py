"""Task 9: alerts -- NOTIF/COPY event alerts (25% size rule), ladder alerts, dedupe."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_core.events import TradeEvent
from hades_hl import alerts as al
from hades_hl import follows as fl
from hades_hl.db import Alert, Follow, PaperEvent, init_db, make_engine
from hades_hl.live import event_to_json

UTC = timezone.utc
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
A = "0x" + "a1" * 20


@pytest.fixture
def session(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    init_db(eng)
    with Session(eng) as s:
        fl.add_follow(s, A, NOW - timedelta(days=1))
        yield s


def set_ladder(s, ladder):
    s.get(Follow, A).ladder = ladder
    s.flush()


_seq = [0]


def ev(side, size, *, start=None, ref=None, pid="BTC:1", direction="long", store=True,
       s=None) -> TradeEvent:
    _seq[0] += 1
    e = TradeEvent(head="hyperliquid", wallet=A, market_id="BTC", side=side, direction=direction,
                   size=Decimal(size), price=Decimal("100"), notional_usd=Decimal(size) * 100,
                   fee_usd=Decimal("0"), leader_time=NOW, observed_time=NOW,
                   source_ref=ref or f"0xh{_seq[0]}:{_seq[0]}", position_id=pid, seq=_seq[0])
    if store and s is not None:
        js = event_to_json(e)
        if start is not None:
            js["leader_start"] = str(start)
        s.add(PaperEvent(source_ref=e.source_ref, address=A, json=js,
                         observed_time=e.observed_time, seq=e.seq))
        s.flush()
    return e


def kinds(s):
    return [(a.kind, a.source_ref) for a in s.scalars(select(Alert).order_by(Alert.id))]


def test_paper_wallet_gets_no_event_alerts(session):
    e = ev("open", "1", start=0, s=session)
    assert al.emit_alerts(session, A, [e], None) == []
    assert kinds(session) == []


@pytest.mark.parametrize("ladder", ["NOTIF", "COPY"])
def test_notif_and_copy_alert_open_and_close(session, ladder):
    set_ladder(session, ladder)
    o = ev("open", "4", start=0, s=session)
    c = ev("close", "4", start=4, s=session)
    out = al.emit_alerts(session, A, [o, c], None)
    assert [a.kind for a in out] == ["open", "close"]
    row = session.scalars(select(Alert).where(Alert.kind == "open")).one()
    assert row.payload_json["head"] == "hyperliquid"
    assert row.coin == "BTC" and row.source_ref == o.source_ref
    assert row.payload_json["ladder"] == ladder


def test_size_change_20_percent_no_alert_25_percent_alert(session):
    set_ladder(session, "NOTIF")
    small = ev("increase", "2", start=10, s=session)  # 20% of 10
    big = ev("increase", "3", start=12, s=session)  # 25% of 12
    red_small = ev("reduce", "2", start=15, s=session)  # 13.3%
    red_big = ev("reduce", "5", start=13, direction="long", s=session)  # 38%
    out = al.emit_alerts(session, A, [small, big, red_small, red_big], None)
    assert [(a.kind, a.source_ref) for a in out] == [("increase", big.source_ref),
                                                    ("reduce", red_big.source_ref)]
    assert Decimal(out[0].payload_json["change_pct"]) == Decimal("0.25")


def test_previous_size_reconstructed_from_episode_when_not_stored(session):
    set_ladder(session, "NOTIF")
    ev("open", "4", s=session, pid="BTC:9")  # no leader_start stored
    inc = ev("increase", "1", s=session, pid="BTC:9")  # 1 / 4 = 25%
    inc2 = ev("increase", "1", s=session, pid="BTC:9")  # 1 / 5 = 20%
    out = al.emit_alerts(session, A, [inc, inc2], None)
    assert [a.source_ref for a in out] == [inc.source_ref]


def test_unknown_previous_size_alerts(session):
    """A pre-follow episode (size unknown): the change cannot be ruled small -> alert."""
    set_ladder(session, "NOTIF")
    inc = ev("increase", "1", s=session, pid="BTC:pre7")
    assert [a.kind for a in al.emit_alerts(session, A, [inc], None)] == ["increase"]


def test_ladder_change_always_alerts(session):
    ch = al.LadderChange(from_="PAPER", to="NOTIF", at=NOW, history_id=7,
                         gate={"full": {"passed": True}})
    out = al.emit_alerts(session, A, [], ch)
    assert [a.kind for a in out] == ["ladder"]
    row = out[0]
    assert row.source_ref == f"ladder:{A}:7"
    assert row.payload_json == {"head": "hyperliquid", "from": "PAPER", "to": "NOTIF",
                                "at": NOW.isoformat(), "gate": {"full": {"passed": True}}}


def test_unique_key_prevents_duplicates(session):
    set_ladder(session, "NOTIF")
    o = ev("open", "1", start=0, s=session)
    ch = al.LadderChange("NOTIF", "PAPER", NOW, 9, {})
    assert len(al.emit_alerts(session, A, [o], ch)) == 2
    assert al.emit_alerts(session, A, [o], ch) == []
    assert len(kinds(session)) == 2


def test_reconcile_close_alerts_as_close(session):
    set_ladder(session, "NOTIF")
    c = ev("close", "1", ref=f"reconcile:{A}:BTC:1:123", s=session)
    out = al.emit_alerts(session, A, [c], None)
    assert out[0].kind == "close" and out[0].payload_json["reconcile"] is True


def test_alert_contract_shape(session):
    set_ladder(session, "NOTIF")
    al.emit_alerts(session, A, [ev("open", "1", start=0, s=session)], None)
    row = session.scalars(select(Alert)).one()
    a = al.to_contract(row)
    assert a.head == "hyperliquid" and a.wallet == A and a.market_id == "BTC"
    assert a.kind == "open" and a.id == row.id
