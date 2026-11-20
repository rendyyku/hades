from datetime import datetime, timedelta, timezone

import pytest

from hades_hl.scoring import (
    REASON_BOT,
    REASON_FEW,
    REASON_SHORT_HISTORY,
    is_bot,
    score,
    score_detailed,
)
from hades_hl.stats import wallet_stats
from hlfills import ADDR, DAY_MS, T0, funding, mk, round_trip

NOW = datetime.fromtimestamp((T0 + 30 * DAY_MS) / 1000, tz=timezone.utc)

KEYS = {"net_pnl_usd", "roi_on_margin", "daily_sharpe", "max_dd", "trades", "winrate", "n",
        "avg_leverage", "median_fills_per_day", "median_hold_s", "maker_share",
        "history_partial", "window_days"}


def trader(n: int, *, hold_ms=3_600_000, win_every=2, crossed=True, start=T0 + DAY_MS):
    """n closed round trips spread across the window, alternating wins/losses."""
    fills = []
    for i in range(n):
        pnl = "5" if i % win_every == 0 else "-2"
        fills += round_trip("BTC", start + i * (DAY_MS // 2), hold_ms, pnl, crossed=crossed)
    return fills


# -- stats ---------------------------------------------------------------------

def test_stats_keys_and_basic_values():
    fills = trader(4)
    st = wallet_stats(ADDR, fills, [funding("BTC", "-0.5", T0 + DAY_MS + 1000)], NOW)
    assert KEYS <= st.keys()
    assert st["n"] == 4
    assert st["trades"] == 8
    assert st["winrate"] == 0.5
    # closedPnl 5-2+5-2 = 6, fees 8*0.1, funding -0.5
    assert st["net_pnl_usd"] == pytest.approx(6 - 0.8 - 0.5)
    assert st["median_hold_s"] == 3600.0
    assert st["maker_share"] == 0.0
    assert st["median_fills_per_day"] == 4.0  # 2 trips per day, 2 fills each
    assert st["history_partial"] is False
    assert st["window_days"] == 30
    assert st["avg_leverage"] is None
    # margin = peak notional 100 at leverage 1 -> ROI = net / 100
    assert st["roi_on_margin"] == pytest.approx((6 - 0.8 - 0.5) / 100)
    assert st["daily_sharpe"] is not None
    assert 0 <= st["max_dd"] < 1


def test_winrate_none_when_n_zero():
    st = wallet_stats(ADDR, [mk("BTC", "B", "1", "100", "0", T0 + DAY_MS)], [], NOW)
    assert st["n"] == 0
    assert st["winrate"] is None
    assert st["median_hold_s"] is None


def test_empty_wallet():
    st = wallet_stats(ADDR, [], [], NOW)
    assert st["n"] == 0 and st["trades"] == 0
    assert st["winrate"] is None and st["maker_share"] is None
    assert st["median_fills_per_day"] is None
    assert st["net_pnl_usd"] == 0.0
    assert st["roi_on_margin"] is None


def test_window_filters_old_fills():
    old = round_trip("BTC", T0 - 5 * DAY_MS, 1000, "100")
    st = wallet_stats(ADDR, old + trader(2), [], NOW)
    assert st["n"] == 2


def test_leverage_from_clearinghouse():
    ch = {"assetPositions": [{"type": "oneWay", "position": {
        "coin": "BTC", "szi": "1", "leverage": {"type": "cross", "value": 5}}}]}
    st = wallet_stats(ADDR, trader(4), [], NOW, clearinghouse=ch)
    assert st["avg_leverage"] == 5.0
    assert st["roi_on_margin"] == pytest.approx((6 - 0.8) / 20)  # margin = 100/5


def test_max_dd_after_losses():
    fills = (round_trip("BTC", T0 + DAY_MS, 1000, "-50")
             + round_trip("BTC", T0 + 2 * DAY_MS, 1000, "10"))
    st = wallet_stats(ADDR, fills, [], NOW)
    # equity starts at margin 100; trough after the 2nd open fee: 100-0.1-50.1-0.1 = 49.7
    assert st["max_dd"] == pytest.approx(0.503, abs=1e-9)
    assert st["closedpnl_mismatch"] is False


def test_daily_sharpe_none_when_flat():
    st = wallet_stats(ADDR, [], [], NOW)
    assert st["daily_sharpe"] is None


def test_partial_history_window_is_covered_span():
    fills = trader(4, start=T0 + 20 * DAY_MS)
    st = wallet_stats(ADDR, fills, [], NOW, history_partial=True)
    assert st["history_partial"] is True
    assert st["window_days"] == pytest.approx(10.0)


def test_maker_share_and_fills_per_day():
    st = wallet_stats(ADDR, trader(4, crossed=False), [], NOW)
    assert st["maker_share"] == 1.0


def test_real_fixture_stats(fixture):
    fills = fixture("user_fills_by_time.json")
    fund = fixture("user_funding.json")
    meta = fixture("fixture_meta.json")
    ch = fixture("clearinghouse_state.json")
    now = datetime.fromtimestamp(meta["end_ms"] / 1000, tz=timezone.utc)
    from hades_hl.normalize import sz_decimals_by_coin

    st = wallet_stats(meta["address"], fills, fund, now, window_days=7,
                      history_partial=True, clearinghouse=ch,
                      sz_decimals=sz_decimals_by_coin(fixture("meta.json")))
    assert KEYS <= st.keys()
    assert st["trades"] == 1312
    assert st["history_partial"] is True
    assert st["window_days"] < 1  # hyperactive: 1312 fills in ~3.6 h
    assert 0 < st["maker_share"] < 0.1  # 37 of 1312 fills were maker
    assert st["avg_leverage"] is not None
    assert st["closedpnl_mismatch"] is False


def test_partial_span_from_oldest_fetched_fill():
    # P17 minor: the covered span starts at the oldest *fetched* fill, which may lie
    # before the first fill inside the window
    from hlfills import ms_dt

    fills = trader(4, start=T0 + 20 * DAY_MS)
    st = wallet_stats(ADDR, fills, [], NOW, history_partial=True,
                      oldest_fill_time=ms_dt(T0 + 15 * DAY_MS))
    assert st["window_days"] == pytest.approx(15.0)


def test_chain_gaps_in_stats():
    fills = [mk("BTC", "B", "1", "100", "0", T0 + DAY_MS),
             mk("BTC", "A", "1", "110", "3", T0 + DAY_MS + 5)]
    assert wallet_stats(ADDR, fills, [], NOW)["chain_gaps"] == 1
    assert wallet_stats(ADDR, trader(2), [], NOW)["chain_gaps"] == 0


def test_stats_addr_required_positional():
    import inspect

    p = inspect.signature(wallet_stats).parameters["addr"]
    assert p.default is inspect.Parameter.empty
    assert list(inspect.signature(wallet_stats).parameters)[0] == "addr"


def test_percentiles_sort_based_mid_rank():
    import time

    from hades_hl.scoring import _percentiles

    assert _percentiles([3, None, 3, 1]) == [0.75, 0.125, 0.75, 0.375]
    assert _percentiles([]) == []
    vals = [float(i % 997) for i in range(6000)] + [None] * 10
    t = time.perf_counter()
    out = _percentiles(vals)
    assert time.perf_counter() - t < 1.0
    assert out[0] == pytest.approx((10 + 0.5 * 7) / 6010)  # value 0: 10 Nones below, 7 ties


# -- bot rule ------------------------------------------------------------------

BASE = {"median_fills_per_day": 10.0, "median_hold_s": 3600.0, "maker_share": 0.2}


@pytest.mark.parametrize("key,ok,bot", [
    ("median_fills_per_day", 300.0, 300.01),
    ("median_hold_s", 60.0, 59.9),
    ("maker_share", 0.70, 0.7001),
])
def test_bot_rule_thresholds(key, ok, bot):
    assert is_bot({**BASE, key: ok}, is_vault=False) is False
    assert is_bot({**BASE, key: bot}, is_vault=False) is True


def test_vault_is_bot():
    assert is_bot(BASE, is_vault=True) is True
    assert is_bot(BASE, is_vault=False) is False


def test_bot_rule_ignores_none_metrics():
    assert is_bot({"median_fills_per_day": None, "median_hold_s": None,
                   "maker_share": None}, is_vault=False) is False


# -- score ---------------------------------------------------------------------

def row(addr, *, n=40, pnl=100.0, sharpe=1.0, roi=0.1, dd=0.1, **kw):
    return {"address": addr, "n": n, "net_pnl_usd": pnl, "daily_sharpe": sharpe,
            "roi_on_margin": roi, "max_dd": dd, **BASE, "history_partial": False,
            "window_days": 30, **kw}


def test_score_none_when_n_below_30():
    out = score_detailed([row("a", n=29), row("b", n=30)])
    assert out["a"].score is None and out["a"].score_reason == REASON_FEW == "n<30"
    assert out["b"].score is not None and out["b"].score_reason is None
    assert score([row("a", n=29)]) == {"a": None}


def test_bots_get_none():
    out = score_detailed([row("v", is_vault=True), row("m", maker_share=0.9), row("ok")])
    assert out["v"].score is None and out["v"].score_reason == REASON_BOT == "bot"
    assert out["m"].score is None and out["m"].score_reason == "bot"
    assert out["ok"].score is not None


def test_partial_history_short_span_unscored():
    out = score_detailed([
        row("short", history_partial=True, window_days=6.9),
        row("long", history_partial=True, window_days=7.0),
    ])
    assert out["short"].score is None
    assert out["short"].score_reason == REASON_SHORT_HISTORY == "history too short (hyperactive)"
    assert out["long"].score is not None


def test_score_weights_and_dd_inverted():
    rows = [
        row("best", pnl=300, sharpe=3, roi=0.3, dd=0.05),
        row("mid", pnl=200, sharpe=2, roi=0.2, dd=0.10),
        row("worst", pnl=100, sharpe=1, roi=0.1, dd=0.30),
    ]
    s = score(rows)
    assert s["best"] > s["mid"] > s["worst"]
    assert all(0 <= v <= 100 for v in s.values())
    # only drawdown differs: lower drawdown scores higher, by exactly the 15% weight share
    s2 = score([row("a", dd=0.05), row("b", dd=0.5)])
    assert s2["a"] > s2["b"]
    assert s2["a"] - s2["b"] == pytest.approx(15 * 0.5 * 2 / 2)


def test_score_pnl_weight_40():
    s = score([row("a", pnl=10), row("b", pnl=20)])
    # percentiles 0.25 / 0.75 on pnl, ties elsewhere -> difference 0.5 * 40
    assert s["b"] - s["a"] == pytest.approx(20.0)


def test_score_accepts_mapping():
    s = score({"x": row("x"), "y": row("y", pnl=1)})
    assert set(s) == {"x", "y"}


def test_none_metric_ranks_lowest():
    s = score([row("a", sharpe=None), row("b", sharpe=0.0)])
    assert s["b"] > s["a"]


def test_sz_decimals_reach_the_chain_in_stats_and_pnl():
    """P28: wallet_stats / wallet_pnl pass sz_decimals down to the chain tolerance."""
    from hades_hl.pnl import wallet_pnl

    fills = [mk("XYZ", "B", "1", "100", "0", T0 + DAY_MS),
             mk("XYZ", "A", "1", "110", "1.004", T0 + DAY_MS + 5, closed_pnl="10")]
    assert wallet_stats(ADDR, fills, [], NOW, sz_decimals={"XYZ": 2})["chain_gaps"] == 0
    assert wallet_stats(ADDR, fills, [], NOW, sz_decimals={"XYZ": 2})["n"] == 1
    assert wallet_stats(ADDR, fills, [], NOW)["chain_gaps"] == 1
    assert wallet_pnl(ADDR, fills, [], sz_decimals={"XYZ": 2}).chain_gaps == 0
