import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import NOW, make_signal
from hades.api import create_app, signal_events
from hades.bus import Bus
from hades.config import Settings
from hades.models import Enrichment, ListingEvent

KEY = "test-key"
H = {"X-Hades-Key": KEY}


@pytest.fixture
def client(store):
    app = create_app(store, Bus(), Settings(HADES_API_KEY=KEY), clock=lambda: NOW)
    return TestClient(app)


def test_healthz_public(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_api_requires_key(client, store):
    assert client.get("/api/signals").status_code == 401
    assert client.get("/api/signals", headers={"X-Hades-Key": "nope"}).status_code == 401
    open_app = TestClient(create_app(store, Bus(), Settings(HADES_API_KEY="")))
    assert open_app.get("/api/signals", headers=H).status_code == 503


def test_api_signals_filter_min_score(client, store):
    a = store.insert_signal(make_signal(dedupe_key="a"))
    store.insert_signal(make_signal(dedupe_key="b"))
    store.set_score(a, 80, {"total": 80})
    store.add_enrichment(a, Enrichment(provider="gt", checked_at=NOW, buyable=True, liquidity_usd=5000.0))
    rows = client.get("/api/signals", params={"min_score": 50}, headers=H).json()
    assert [r["id"] for r in rows] == [a]
    assert rows[0]["enrichment"]["liquidity_usd"] == 5000.0
    assert "raw" not in rows[0]


def test_api_signal_detail(client, store):
    a = store.insert_signal(make_signal(website="cat.xyz"))
    store.add_alias("monad:0xabc", "dom:cat.xyz")
    b = store.insert_signal(make_signal(source="ct", kind="ct_high", project_key="dom:cat.xyz", dedupe_key="ct:x"))
    body = client.get(f"/api/signals/{a}", headers=H).json()
    assert body["signal"]["id"] == a
    assert [f["id"] for f in body["fusion"]] == [b]
    assert body["enrichments"] == []


def test_api_signal_detail_404(client):
    assert client.get("/api/signals/999", headers=H).status_code == 404


def test_api_listings_with_earliest_signal(client, store):
    store.insert_signal(make_signal(symbol="CAT", observed_at=NOW - timedelta(hours=50)))
    store.insert_listing(ListingEvent("coinbase", "CAT-USD", "CAT", NOW, {}))
    store.insert_listing(ListingEvent("kraken", "DOG/USD", "DOG", NOW, {}))
    rows = client.get("/api/listings", headers=H).json()
    by = {r["base_asset"]: r for r in rows}
    assert by["CAT"]["earliest_signal"]["lead_h"] == 50
    assert by["DOG"]["earliest_signal"] is None


def test_api_overview(client, store):
    store.insert_signal(make_signal())
    store.record_ok("ct", NOW)
    body = client.get("/api/overview", headers=H).json()
    assert body["counts"]["signals"] == 1
    assert body["watchers"][0]["name"] == "ct"
    assert len(body["top"]) == 1


def test_api_watchlist_post_validates(client, store):
    assert client.post("/api/watchlist", json={"domain": "not a domain"}, headers=H).status_code == 400
    r = client.post("/api/watchlist", json={"domain": "https://Cat.XYZ/x"}, headers=H)
    assert r.json() == {"domain": "cat.xyz", "added": True}
    assert client.get("/api/watchlist", headers=H).json()[0]["origin"] == "manual"
    assert client.delete("/api/watchlist/cat.xyz", headers=H).status_code == 200
    assert client.delete("/api/watchlist/cat.xyz", headers=H).status_code == 404


def test_api_mutes(client, store):
    r = client.post("/api/mutes", json={"target": "monad", "hours": 2}, headers=H)
    assert r.status_code == 200
    [m] = client.get("/api/mutes", headers=H).json()
    assert m["target"] == "monad"
    assert client.post("/api/mutes", json={"target": "", "hours": 2}, headers=H).status_code == 422
    assert client.delete("/api/mutes/monad", headers=H).status_code == 200
    assert client.get("/api/mutes", headers=H).json() == []


def test_api_eval(client, store):
    store.insert_signal(make_signal())
    body = client.get("/api/eval", headers=H).json()
    assert body["by_source"][0]["source"] == "repo_diff"


async def test_api_stream_emits_signal():
    bus = Bus()

    async def connected():
        return False

    gen = signal_events(bus, connected, ping_interval=5)
    first = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    bus.publish("signal", {"id": 1, "observed_at": NOW})
    ev = await asyncio.wait_for(first, 1)
    assert ev["event"] == "signal"
    assert '"id": 1' in ev["data"]
    await gen.aclose()


async def test_api_stream_pings_when_idle():
    async def connected():
        return False

    gen = signal_events(Bus(), connected, ping_interval=0.01)
    ev = await asyncio.wait_for(gen.__anext__(), 1)
    assert ev["event"] == "ping"
    await gen.aclose()


def test_api_watchers_stale(store):
    store.record_ok("ct", NOW - timedelta(hours=5))
    app = create_app(store, Bus(), Settings(HADES_API_KEY=KEY), clock=lambda: NOW, intervals={"ct": 600})
    [w] = TestClient(app).get("/api/watchers", headers=H).json()
    assert w["stale"] is True and w["healthy"] is False
