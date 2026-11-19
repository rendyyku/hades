from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from hades.db import Wallet, WalletStats
from hades.scoring import WalletStatsData, persist_stats, score_wallets, wallet_stats

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


@dataclass
class S:
    signature: str
    mint: str
    block_time: datetime
    side: str
    token_amount: float
    sol_amount: float | None
    slot: int = 0
    wallet: str = "W"


_counter = [0]


def sw(mint, when, side, tokens, sol):
    _counter[0] += 1
    return S(f"sig{_counter[0]:06d}", mint, when, side, tokens, sol, slot=_counter[0])


def round_trip(mint, start, cost, proceeds, hold_sec=60.0):
    return [
        sw(mint, start, "buy", 100.0, cost),
        sw(mint, start + timedelta(seconds=hold_sec), "sell", 100.0, proceeds),
    ]


def trips(prefix, results, *, start=NOW - timedelta(days=1), hold_sec=60.0):
    """results: list of (cost, proceeds); one closed round trip per entry, hourly."""
    out = []
    for i, (c, p) in enumerate(results):
        out += round_trip(f"{prefix}{i}", start + timedelta(hours=i), c, p, hold_sec)
    return out


def test_bot_by_frequency():
    start = NOW - timedelta(days=30)
    step = timedelta(days=30) / 4600
    swaps = [sw(f"M{i}", start + step * i, "buy", 10.0, 0.1) for i in range(4600)]
    st = wallet_stats("bot", swaps, {}, NOW)
    assert st.trades_per_day == pytest.approx(4600 / 30)
    assert st.median_hold_sec is None
    assert st.is_bot is True

    st2 = wallet_stats("human", swaps[:4400], {}, NOW)
    assert st2.trades_per_day == pytest.approx(4400 / 30)
    assert st2.is_bot is False


def test_trades_per_day_counts_unpriced_swaps():
    swaps = trips("A", [(1.0, 2.0)]) + [sw("X", NOW - timedelta(days=2), "buy", 5.0, None)]
    st = wallet_stats("w", swaps, {}, NOW)
    assert st.trades_per_day == pytest.approx(3 / 30)
    assert st.tokens_traded == 1


def test_bot_by_hold():
    st = wallet_stats("w", trips("A", [(1.0, 1.1)] * 5, hold_sec=10), {}, NOW)
    assert st.median_hold_sec == pytest.approx(10.0)
    assert st.is_bot is True

    st2 = wallet_stats("w", trips("A", [(1.0, 1.1)] * 5, hold_sec=30), {}, NOW)
    assert st2.median_hold_sec == pytest.approx(30.0)
    assert st2.is_bot is False


def test_basic_stats_fields():
    swaps = trips("A", [(1.0, 3.0), (1.0, 0.5), (2.0, 2.0)]) + [
        sw("OPEN", NOW - timedelta(hours=3), "buy", 100.0, 1.0)
    ]
    st = wallet_stats("w", swaps, {"OPEN": 0.03}, NOW)
    assert isinstance(st, WalletStatsData)
    assert st.address == "w"
    assert st.window_days == 30
    assert st.tokens_traded == 4
    assert st.tokens_closed == 3
    assert st.win_rate == pytest.approx(1 / 3)
    assert st.realized_pnl_sol == pytest.approx(1.5)
    assert st.unrealized_pnl_sol == pytest.approx(2.0)
    assert st.total_pnl_sol == pytest.approx(3.5)
    assert st.median_hold_sec == pytest.approx(60.0)
    assert st.median_roi == pytest.approx(0.0)
    assert st.score is None
    assert st.computed_at == NOW


def test_empty_wallet():
    st = wallet_stats("w", [], {}, NOW)
    assert st.tokens_traded == 0
    assert st.tokens_closed == 0
    assert st.win_rate == 0.0
    assert st.median_hold_sec is None
    assert st.median_roi is None
    assert st.consistency == 0.0
    assert st.is_bot is False


def test_window_excludes_old():
    old = trips("OLD", [(1.0, 10.0)] * 3, start=NOW - timedelta(days=40))
    recent = trips("NEW", [(1.0, 2.0)])
    st = wallet_stats("w", old + recent, {}, NOW)
    assert st.tokens_traded == 1
    assert st.tokens_closed == 1
    assert st.realized_pnl_sol == pytest.approx(1.0)
    assert st.trades_per_day == pytest.approx(2 / 30)


def test_window_splits_position_across_boundary():
    # bought 40 days ago (outside), sold yesterday: the sell alone is untracked.
    swaps = [
        sw("M", NOW - timedelta(days=40), "buy", 100.0, 1.0),
        sw("M", NOW - timedelta(days=1), "sell", 100.0, 5.0),
    ]
    st = wallet_stats("w", swaps, {}, NOW)
    assert st.realized_pnl_sol == pytest.approx(0.0)
    assert st.tokens_closed == 0


def test_consistency_by_week():
    def at(days):
        return NOW - timedelta(days=days)

    swaps = (
        trips("W0", [(1.0, 2.0)], start=at(1))  # week 0: +1
        + trips("W1", [(1.0, 0.5)], start=at(8))  # week 1: -0.5
        + trips("W2a", [(1.0, 3.0)], start=at(15))  # week 2: +2 -1 = +1
        + trips("W2b", [(2.0, 1.0)], start=at(16))
        + trips("W3", [(1.0, 1.0)], start=at(22))  # week 3: 0 -> not > 0
        + trips("W4", [(1.0, 9.0)], start=at(29))  # beyond 4 weeks: ignored
    )
    st = wallet_stats("w", swaps, {}, NOW)
    assert st.consistency == pytest.approx(0.5)


def test_value_error_propagates():
    with pytest.raises(ValueError):
        wallet_stats("w", [sw("M", NOW - timedelta(days=1), "swap", 1.0, 1.0)], {}, NOW)


def test_ineligible_under_5_closed():
    four = wallet_stats("four", trips("A", [(1.0, 5.0)] * 4), {}, NOW)
    five = wallet_stats("five", trips("B", [(1.0, 2.0)] * 5), {}, NOW)
    bot = wallet_stats("bot", trips("C", [(1.0, 5.0)] * 6, hold_sec=5), {}, NOW)
    assert bot.is_bot
    out = score_wallets([four, five, bot])
    by = {s.address: s for s in out}
    assert by["four"].score is None
    assert by["bot"].score is None
    # sole eligible wallet: pct = 1.0 → 40 + 25*1 + 20 + 15*0.25
    assert by["five"].score == pytest.approx(88.75)


def test_score_ordering():
    a = wallet_stats("A", trips("a", [(1.0, 3.0)] * 5), {}, NOW)
    b = wallet_stats("B", trips("b", [(1.0, 2.0)] * 3 + [(1.0, 0.5)] * 2), {}, NOW)
    c = wallet_stats("C", trips("c", [(1.0, 1.5)] + [(1.0, 0.5)] * 4), {}, NOW)
    out = score_wallets([c, a, b])
    by = {s.address: s for s in out}
    assert by["A"].score > by["B"].score > by["C"].score
    for s in out:
        assert 0 <= s.score <= 100
    assert by["A"].score == pytest.approx(88.75)
    assert by["B"].score == pytest.approx(48.75)
    assert by["C"].score == pytest.approx(5.0)


def test_score_ties_and_missing_roi():
    base = dict(
        window_days=30,
        realized_pnl_sol=0.0,
        unrealized_pnl_sol=0.0,
        tokens_traded=5,
        tokens_closed=5,
        win_rate=0.0,
        median_hold_sec=100.0,
        trades_per_day=1.0,
        is_bot=False,
        consistency=0.0,
        score=None,
        computed_at=NOW,
    )
    x = WalletStatsData(address="x", total_pnl_sol=1.0, median_roi=None, **base)
    y = WalletStatsData(address="y", total_pnl_sol=1.0, median_roi=-5.0, **base)
    z = WalletStatsData(address="z", total_pnl_sol=0.0, median_roi=-5.0, **base)
    by = {s.address: s for s in score_wallets([x, y, z])}
    # pnl pct: x,y = (1 + 0.5)/2 = 0.75, z = 0; roi pct: x = 0, y,z = 0.75
    assert by["x"].score == pytest.approx(30.0)
    assert by["y"].score == pytest.approx(45.0)
    assert by["z"].score == pytest.approx(15.0)


def test_persist_stats_upserts(session):
    st = wallet_stats("bot", trips("C", [(1.0, 5.0)] * 6, hold_sec=5), {}, NOW)
    persist_stats(session, [st])
    session.commit()
    st.realized_pnl_sol = 42.0
    persist_stats(session, [st])
    session.commit()

    assert session.scalar(select(func.count()).select_from(WalletStats)) == 1
    row = session.get(WalletStats, "bot")
    assert row.realized_pnl_sol == pytest.approx(42.0)
    assert row.tokens_closed == 6
    assert row.median_hold_sec == pytest.approx(5.0)
    assert row.score is None
    assert session.scalar(select(func.count()).select_from(Wallet)) == 1
    assert session.get(Wallet, "bot").is_bot is True


def test_persist_stats_keeps_existing_wallet_fields(session):
    session.add(Wallet(address="w", label="friend", is_bot=True))
    session.commit()
    st = wallet_stats("w", trips("A", [(1.0, 2.0)] * 5), {}, NOW)
    persist_stats(session, score_wallets([st]))
    session.commit()
    w = session.get(Wallet, "w")
    assert w.label == "friend"
    assert w.is_bot is False
    assert session.get(WalletStats, "w").score == pytest.approx(88.75)
