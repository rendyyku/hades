"""Task 9: follows -- auto top 10, manual add/remove, cap 25, open positions block drops."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_hl import follows as fl
from hades_hl import paper_runtime as pr
from hades_hl.db import Follow, LadderHistory, PaperEvent, Wallet, init_db, make_engine
from hades_hl.pipeline import kv_set_json

UTC = timezone.utc
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def addr(i: int) -> str:
    return f"0x{i:040x}"


@pytest.fixture
def session(tmp_path):
    eng = make_engine(f"sqlite:///{(tmp_path / 'f.db').as_posix()}")
    init_db(eng)
    with Session(eng) as s:
        yield s


def seed(s, n=15, bots=(), vaults=(), unscored=()):
    for i in range(1, n + 1):
        s.add(Wallet(address=addr(i), score=None if i in unscored else float(i),
                     is_bot=i in bots, is_vault=i in vaults, stats_json={}))
    s.flush()


def active(s) -> set[str]:
    return {f.address for f in s.scalars(select(Follow).where(Follow.removed_at.is_(None)))}


def test_constants():
    assert fl.AUTO_FOLLOW_N == 10 and fl.MAX_FOLLOWS == 25


def test_auto_top10_excludes_bots_vaults_and_unscored(session):
    seed(session, 16, bots=(16, 15), vaults=(14,), unscored=(13,))
    diff = fl.sync_auto_follows(session, NOW)
    want = {addr(i) for i in range(3, 13)}  # 12..3: ten best eligible
    assert active(session) == want
    assert set(diff.added) == want and diff.removed == []
    f = session.get(Follow, addr(12))
    assert f.origin == "auto" and f.started_at == NOW and f.ladder == "PAPER"
    assert f.removed_manually is False
    # the follow's entry onto the ladder is recorded
    h = session.scalars(select(LadderHistory).where(LadderHistory.address == addr(12))).all()
    assert [(r.from_, r.to) for r in h] == [(None, "PAPER")]


def test_sync_is_idempotent(session):
    seed(session)
    fl.sync_auto_follows(session, NOW)
    diff = fl.sync_auto_follows(session, NOW + timedelta(days=1))
    assert diff.added == [] and diff.removed == []
    assert session.get(Follow, addr(15)).started_at == NOW  # not reset


def test_manual_removal_is_sticky(session):
    seed(session)
    fl.sync_auto_follows(session, NOW)
    assert fl.remove_follow(session, addr(15), NOW) is True
    f = session.get(Follow, addr(15))
    assert f.removed_manually is True and f.removed_at == NOW
    diff = fl.sync_auto_follows(session, NOW + timedelta(days=1))
    assert addr(15) not in active(session)
    assert diff.added == [addr(5)]  # the next best fills the slot
    # still never re-auto-followed later
    fl.sync_auto_follows(session, NOW + timedelta(days=2))
    assert addr(15) not in active(session)


def test_removal_of_unknown_wallet_is_remembered(session):
    seed(session)
    fl.remove_follow(session, addr(15), NOW)  # never followed
    fl.sync_auto_follows(session, NOW)
    assert addr(15) not in active(session)


def test_auto_follow_dropped_when_out_of_top(session):
    seed(session)
    fl.sync_auto_follows(session, NOW)
    session.get(Wallet, addr(15)).score = 0.5
    diff = fl.sync_auto_follows(session, NOW + timedelta(days=1))
    assert diff.removed == [addr(15)] and diff.added == [addr(5)]
    f = session.get(Follow, addr(15))
    assert f.removed_at == NOW + timedelta(days=1) and f.removed_manually is False


def test_open_paper_positions_block_the_drop(session):
    seed(session)
    fl.sync_auto_follows(session, NOW)
    kv_set_json(session, pr.KV_PAPER.format(addr(15)), {
        "computed_at": NOW.isoformat(),
        "open_positions": [{"position_id": "BTC:1", "market_id": "BTC"}]})
    session.get(Wallet, addr(15)).score = 0.5
    diff = fl.sync_auto_follows(session, NOW + timedelta(days=1))
    assert addr(15) in active(session)
    assert diff.kept_open == [addr(15)] and addr(15) not in diff.removed


def test_unprocessed_events_count_as_possibly_open(session):
    """Events newer than the last paper snapshot may have opened a copy: keep."""
    seed(session)
    fl.sync_auto_follows(session, NOW)
    session.add(PaperEvent(source_ref="x:1", address=addr(15), json={},
                           observed_time=NOW + timedelta(hours=1), seq=1))
    session.get(Wallet, addr(15)).score = 0.5
    diff = fl.sync_auto_follows(session, NOW + timedelta(days=1))
    assert diff.kept_open == [addr(15)]


def test_cap_25_manual(session):
    for i in range(1, 26):
        fl.add_follow(session, addr(100 + i), NOW)
    assert len(active(session)) == 25
    with pytest.raises(fl.FollowCapError):
        fl.add_follow(session, addr(200), NOW)
    # re-adding an active follow is not a new follow (no cap error)
    fl.add_follow(session, addr(101), NOW)


def test_cap_limits_auto_follows(session):
    seed(session)
    for i in range(1, 21):
        fl.add_follow(session, addr(100 + i), NOW)
    diff = fl.sync_auto_follows(session, NOW)
    assert len(active(session)) == 25
    assert diff.added == [addr(i) for i in (15, 14, 13, 12, 11)]
    assert diff.skipped_cap == [addr(i) for i in (10, 9, 8, 7, 6)]


def test_manual_add_converts_auto_and_clears_removal(session):
    seed(session)
    fl.sync_auto_follows(session, NOW)
    fl.add_follow(session, addr(15), NOW + timedelta(hours=1))
    f = session.get(Follow, addr(15))
    assert f.origin == "manual" and f.started_at == NOW  # same follow, not restarted
    session.get(Wallet, addr(15)).score = 0.5
    fl.sync_auto_follows(session, NOW + timedelta(days=1))
    assert addr(15) in active(session)  # manual follows are never auto-dropped
    fl.remove_follow(session, addr(15), NOW + timedelta(days=2))
    fl.add_follow(session, addr(15), NOW + timedelta(days=3))
    f = session.get(Follow, addr(15))
    assert f.removed_manually is False and f.removed_at is None
    assert f.started_at == NOW + timedelta(days=3)  # a new follow starts now
    assert f.ladder == "PAPER"


def test_add_follow_lowercases_and_creates_wallet(session):
    a = "0x" + "AB" * 20
    fl.add_follow(session, a, NOW)
    assert session.get(Follow, a.lower()) is not None
    assert session.get(Wallet, a.lower()) is not None
    with pytest.raises(ValueError):
        fl.add_follow(session, "nope", NOW)
