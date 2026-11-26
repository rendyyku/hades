import httpx
import pytest
import respx

from conftest import NOW
from hades.http import Http, SourceError
from hades.runner import Runner
from hades.watchers.ground_truth import EXCHANGES, GroundTruthWatcher

# Trimmed shapes of the real public responses.
FIXTURES = {
    "coinbase": [
        {"id": "BTC-USD", "base_currency": "BTC", "status": "online"},
        {"id": "CAT-USD", "base_currency": "CAT", "status": "online"},
        {"id": "OLD-USD", "base_currency": "OLD", "status": "delisted"},
    ],
    "kraken": {"error": [], "result": {
        "XXBTZUSD": {"wsname": "XBT/USD", "base": "XXBT", "status": "online"},
        "CATUSD": {"wsname": "CAT/USD", "base": "CAT", "status": "online"},
    }},
    "okx": {"code": "0", "data": [
        {"instId": "BTC-USDT", "baseCcy": "BTC", "state": "live"},
        {"instId": "CAT-USDT", "baseCcy": "CAT", "state": "live"},
        {"instId": "NEW-USDT", "baseCcy": "NEW", "state": "preopen"},
    ]},
    "binance": {"symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC"},
        {"symbol": "CATUSDT", "status": "TRADING", "baseAsset": "CAT"},
        {"symbol": "OLDUSDT", "status": "BREAK", "baseAsset": "OLD"},
    ]},
    "bybit": {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "baseCoin": "BTC", "status": "Trading"},
        {"symbol": "CATUSDT", "baseCoin": "CAT", "status": "Trading"},
    ]}},
}
EXPECTED = {
    "coinbase": {"BTC-USD": "BTC", "CAT-USD": "CAT"},
    "kraken": {"XBT/USD": "XBT", "CAT/USD": "CAT"},
    "okx": {"BTC-USDT": "BTC", "CAT-USDT": "CAT"},
    "binance": {"BTCUSDT": "BTC", "CATUSDT": "CAT"},
    "bybit": {"BTCUSDT": "BTC", "CATUSDT": "CAT"},
}


@pytest.mark.parametrize("exchange", sorted(EXPECTED))
def test_parse_exchange(exchange):
    w = GroundTruthWatcher(exchange, 600)
    assert w.name == f"ground_truth:{exchange}"
    assert w.parse(FIXTURES[exchange]) == EXPECTED[exchange]


def test_all_configured_exchanges_known():
    assert set(EXCHANGES) == set(EXPECTED)


def test_baseline_no_events():
    w = GroundTruthWatcher("binance", 600)
    res = w.diff(None, {"BTCUSDT": "BTC"}, NOW)
    assert res.listings == [] and res.signals == []
    assert res.new_state == {"symbols": {"BTCUSDT": "BTC"}}


def test_new_pair_emits_listing_event():
    w = GroundTruthWatcher("binance", 600)
    res = w.diff({"symbols": {"BTCUSDT": "BTC"}}, {"BTCUSDT": "BTC", "CATUSDT": "CAT"}, NOW)
    [ev] = res.listings
    assert (ev.exchange, ev.symbol, ev.base_asset, ev.first_seen_at) == ("binance", "CATUSDT", "CAT", NOW)
    assert res.signals == []


def test_removed_pair_kept_in_state():
    # a pair that disappears and comes back is not a new listing
    w = GroundTruthWatcher("binance", 600)
    state = {"symbols": {f"S{i}": "X" for i in range(10)}}
    res = w.diff(state, {f"S{i}": "X" for i in range(9)}, NOW)
    assert "S9" in res.new_state["symbols"]


def test_shrunken_snapshot_raises():
    w = GroundTruthWatcher("binance", 600)
    with pytest.raises(SourceError):
        w.diff({"symbols": {f"S{i}": "X" for i in range(100)}}, {f"S{i}": "X" for i in range(40)}, NOW)


def test_empty_baseline_raises():
    with pytest.raises(SourceError):
        GroundTruthWatcher("binance", 600).diff(None, {}, NOW)


async def _nosleep(_):
    return None


@respx.mock
async def test_runner_keeps_state_on_shrink(store):
    async with httpx.AsyncClient() as c:
        http = Http(c, rates={"api.binance.com": 10_000}, sleep=_nosleep)
        w = GroundTruthWatcher("binance", 600)
        route = respx.get("https://api.binance.com/api/v3/exchangeInfo").mock(return_value=httpx.Response(
            200, json={"symbols": [{"symbol": f"S{i}", "status": "TRADING", "baseAsset": "X"} for i in range(10)]}))
        r = Runner(store, http, [w], sink=None, clock=lambda: NOW, sleep=_nosleep)
        assert await r.run_once(w) is True
        route.mock(return_value=httpx.Response(200, json={"symbols": []}))
        assert await r.run_once(w) is False
        assert len(store.get_state("ground_truth:binance")["symbols"]) == 10
        assert store.list_listings() == []
