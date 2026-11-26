from datetime import timedelta

import httpx
import respx

from conftest import NOW, make_signal
from hades.evaluate import evaluate, price_move, write_report
from hades.http import Http
from hades.models import ListingEvent


def _listing(store, symbol, at, exchange="coinbase"):
    store.insert_listing(ListingEvent(exchange, f"{symbol}-USD", symbol, at, {}))


def _src(report, source, kind):
    return next(r for r in report["by_source"] if (r["source"], r["kind"]) == (source, kind))


def test_evaluate_hit_lead_time(store):
    store.insert_signal(make_signal(symbol="CAT", observed_at=NOW))
    _listing(store, "cat", NOW + timedelta(hours=50))
    rep = evaluate(store, NOW + timedelta(days=60))
    row = _src(rep, "repo_diff", "merged")
    assert (row["signals"], row["hits"], row["false_positives"], row["pending"]) == (1, 1, 0, 0)
    assert row["median_lead_h"] == 50 and row["min_lead_h"] == 50
    assert row["small_sample"] is True
    assert rep["hits"][0]["lead_h"] == 50 and rep["hits"][0]["weak"] is True
    assert rep["misses"] == []


def test_evaluate_listing_before_signal_is_not_a_hit(store):
    _listing(store, "CAT", NOW - timedelta(hours=1))
    store.insert_signal(make_signal(symbol="CAT", observed_at=NOW))
    rep = evaluate(store, NOW + timedelta(days=60))
    assert _src(rep, "repo_diff", "merged")["hits"] == 0
    assert len(rep["misses"]) == 1


def test_evaluate_false_positive_after_window(store):
    store.insert_signal(make_signal(symbol="CAT", observed_at=NOW))
    rep = evaluate(store, NOW + timedelta(days=31))
    assert _src(rep, "repo_diff", "merged")["false_positives"] == 1


def test_evaluate_pending_inside_window(store):
    store.insert_signal(make_signal(symbol="CAT", observed_at=NOW))
    rep = evaluate(store, NOW + timedelta(days=3))
    row = _src(rep, "repo_diff", "merged")
    assert (row["pending"], row["false_positives"]) == (1, 0)
    assert row["median_lead_h"] is None


def test_evaluate_miss(store):
    _listing(store, "DOG", NOW)
    rep = evaluate(store, NOW + timedelta(days=1))
    assert [m["base_asset"] for m in rep["misses"]] == ["DOG"]


def test_write_report(store, tmp_path):
    store.insert_signal(make_signal(symbol="CAT", observed_at=NOW))
    _listing(store, "CAT", NOW + timedelta(hours=5))
    md, csv = write_report(evaluate(store, NOW + timedelta(days=40)), tmp_path)
    text = md.read_text(encoding="utf-8")
    assert "sampel kecil, belum bisa disimpulkan" in text
    assert csv.read_text(encoding="utf-8").splitlines()[0].startswith("source,kind,signals,hits")


async def _nosleep(_):
    return None


@respx.mock
async def test_price_move():
    async with httpx.AsyncClient() as c:
        http = Http(c, rates={"api.geckoterminal.com": 10_000}, sleep=_nosleep)
        t0, t1 = NOW, NOW + timedelta(hours=10)
        respx.get("https://api.geckoterminal.com/api/v2/networks/monad/pools/0xp/ohlcv/hour").mock(
            return_value=httpx.Response(200, json={"data": {"attributes": {"ohlcv_list": [
                [int(t1.timestamp()), 2, 2, 2, 3.0, 1],
                [int((t0 + timedelta(hours=5)).timestamp()), 1, 1, 1, 2.0, 1],
                [int(t0.timestamp()), 1, 1, 1, 1.5, 1],
            ]}}}))
        assert await price_move(http, "monad", "0xp", t0, t1) == 100.0
