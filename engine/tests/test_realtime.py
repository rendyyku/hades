"""Task 9: Helius webhook receiver, alerts, AlertBus/SSE, watchlist -> webhook sync."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from hades.api import main as api_main
from hades.api.main import create_app
from hades.config import Settings
from hades.db import Alert, Kv, Swap, Token, WalletStats, Watchlist, init_db, make_engine
from hades.realtime import AlertBus, alert_events
from hades.sources.helius import HeliusClient, HeliusError

FIXTURES = Path(__file__).parent / "fixtures"
TX = json.loads((FIXTURES / "real_pumpamm_buy_wrap.json").read_text(encoding="utf-8"))
TX_SELL = json.loads((FIXTURES / "real_pumpamm_sell_unwrap.json").read_text(encoding="utf-8"))
WALLET = TX["feePayer"]
MINT = "HKpDdvrzzYfFS8RwUPyRtAbjHBxa7P1YSxXvqQUnpump"  # bought in TX
SECRET = "whsec-test"
KEY = "k"
H = {"X-Hades-Key": KEY}
WH = {"Authorization": SECRET}
API = "https://api.helius.xyz"
OTHER = "B" * 32 + "b2"


def _settings(**kw) -> Settings:
    base = dict(DATABASE_URL="sqlite://", HADES_API_KEY=KEY, HELIUS_WEBHOOK_SECRET=SECRET,
                HELIUS_API_KEY="hkey-test-xyz", HELIUS_WEBHOOK_ID="", PUBLIC_WEBHOOK_URL="", _env_file=None)
    base.update(kw)
    return Settings(**base)


def _app(**kw):
    return create_app(_settings(**kw))


def _watch(app, address=WALLET):
    with Session(app.state.engine) as s:
        s.add(Watchlist(address=address))
        s.commit()


def _stats(app, score, address=WALLET):
    with Session(app.state.engine) as s:
        s.add(WalletStats(address=address, score=score))
        s.commit()


def _alerts(app) -> list[Alert]:
    with Session(app.state.engine) as s:
        return list(s.scalars(select(Alert).order_by(Alert.id)))


# -- webhook auth --------------------------------------------------------------------

def test_webhook_rejects_bad_secret():
    c = TestClient(_app())
    assert c.post("/webhook/helius", json=[TX], headers={"Authorization": "nope"}).status_code == 401
    assert c.post("/webhook/helius", json=[TX]).status_code == 401


def test_webhook_empty_secret_503():
    c = TestClient(_app(HELIUS_WEBHOOK_SECRET=""))
    assert c.post("/webhook/helius", json=[TX], headers={"Authorization": ""}).status_code == 503


def test_webhook_not_behind_api_key():
    c = TestClient(_app())
    r = c.post("/webhook/helius", json=[], headers=WH)  # no X-Hades-Key
    assert r.status_code == 200
    assert r.json() == {"received": 0, "swaps": 0, "alerts": 0}


def test_webhook_bad_body_400():
    c = TestClient(_app())
    r = c.post("/webhook/helius", content=b"not json", headers={**WH, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert c.post("/webhook/helius", json="str", headers=WH).status_code == 400


# -- alerts --------------------------------------------------------------------------

def test_webhook_creates_alert_for_watchlist():
    app = _app()
    _watch(app)
    with Session(app.state.engine) as s:
        s.add(Token(mint=MINT, symbol="HKP"))
        s.commit()
    r = TestClient(app).post("/webhook/helius", json=[TX], headers=WH)
    assert r.status_code == 200
    assert r.json() == {"received": 1, "swaps": 1, "alerts": 1}
    [a] = _alerts(app)
    assert (a.wallet, a.mint, a.side, a.signature) == (WALLET, MINT, "buy", TX["signature"])
    assert a.sol_amount == pytest.approx(1.00176)
    with Session(app.state.engine) as s:
        sw = s.get(Swap, (TX["signature"], WALLET, MINT))
        assert sw is not None and sw.source == "webhook"


def test_webhook_accepts_single_object():
    app = _app()
    _watch(app)
    r = TestClient(app).post("/webhook/helius", json=TX, headers=WH)
    assert r.json() == {"received": 1, "swaps": 1, "alerts": 1}


def test_webhook_idempotent():
    app = _app()
    _watch(app)
    c = TestClient(app)
    assert c.post("/webhook/helius", json=[TX], headers=WH).json()["alerts"] == 1
    assert c.post("/webhook/helius", json=[TX], headers=WH).json() == {
        "received": 1, "swaps": 1, "alerts": 0}
    # duplicate inside one batch
    assert c.post("/webhook/helius", json=[TX, TX], headers=WH).json()["alerts"] == 0
    assert len(_alerts(app)) == 1


def test_webhook_duplicate_in_first_batch_one_alert():
    app = _app()
    _watch(app)
    r = TestClient(app).post("/webhook/helius", json=[TX, TX], headers=WH)
    assert r.json() == {"received": 2, "swaps": 2, "alerts": 1}
    assert len(_alerts(app)) == 1


def test_non_watch_low_score_no_alert():
    app = _app()
    _stats(app, 69.9)
    r = TestClient(app).post("/webhook/helius", json=[TX], headers=WH)
    assert r.json() == {"received": 1, "swaps": 1, "alerts": 0}
    assert _alerts(app) == []
    with Session(app.state.engine) as s:  # swap still stored
        assert s.scalar(select(func.count()).select_from(Swap)) == 1


def test_high_score_wallet_alerts():
    app = _app()
    _stats(app, 70.0)
    r = TestClient(app).post("/webhook/helius", json=[TX, TX_SELL], headers=WH)
    assert r.json() == {"received": 2, "swaps": 2, "alerts": 2}
    assert sorted(a.side for a in _alerts(app)) == ["buy", "sell"]


def test_webhook_skips_malformed_tx():
    app = _app()
    _watch(app)
    r = TestClient(app).post("/webhook/helius", json=[42, {"garbage": True}, TX], headers=WH)
    assert r.status_code == 200
    assert r.json() == {"received": 3, "swaps": 1, "alerts": 1}


# -- bus / SSE -----------------------------------------------------------------------

async def test_sse_receives_alert():
    app = _app()
    _watch(app)
    with Session(app.state.engine) as s:
        s.add(Token(mint=MINT, symbol="HKP"))
        s.commit()
    q = app.state.bus.subscribe()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/webhook/helius", json=[TX], headers=WH)
    assert r.json()["alerts"] == 1
    item = await asyncio.wait_for(q.get(), timeout=2)
    assert set(item) == {"id", "wallet", "mint", "symbol", "side", "sol_amount", "signature", "block_time"}
    assert item["wallet"] == WALLET and item["mint"] == MINT and item["symbol"] == "HKP"
    assert item["side"] == "buy" and item["signature"] == TX["signature"]
    assert item["block_time"].startswith("2026-06-06T18:38:30")
    assert isinstance(item["id"], int)
    # duplicate post publishes nothing
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/webhook/helius", json=[TX], headers=WH)
    assert q.empty()


async def test_alert_events_generator_alert_ping_and_unsubscribe():
    bus = AlertBus()

    async def never_disconnected() -> bool:
        return False

    gen = alert_events(bus, never_disconnected, ping_interval=0.05)
    first = asyncio.ensure_future(gen.__anext__())
    for _ in range(50):  # wait until the generator has subscribed
        if bus.subscriber_count:
            break
        await asyncio.sleep(0)
    assert bus.subscriber_count == 1
    bus.publish({"id": 1, "wallet": "w"})
    ev = await asyncio.wait_for(first, timeout=2)
    assert ev["event"] == "alert" and json.loads(ev["data"]) == {"id": 1, "wallet": "w"}
    ev = await asyncio.wait_for(gen.__anext__(), timeout=2)
    assert ev["event"] == "ping" and "t" in json.loads(ev["data"])
    await gen.aclose()
    assert bus.subscriber_count == 0


async def test_alert_events_stops_on_disconnect():
    bus = AlertBus()

    async def disconnected() -> bool:
        return True

    events = [e async for e in alert_events(bus, disconnected, ping_interval=0.01)]
    assert events == []
    assert bus.subscriber_count == 0


def test_stream_requires_key():
    c = TestClient(_app())
    assert c.get("/api/stream").status_code == 401


def test_ping_interval_overridable():
    app = _app()
    assert app.state.ping_interval == api_main.PING_INTERVAL_SEC == 15


async def test_bus_drop_oldest():
    bus = AlertBus()
    q = bus.subscribe()
    q2 = bus.subscribe()
    assert q.maxsize == 100
    for i in range(105):
        bus.publish({"i": i})
    got = [q.get_nowait()["i"] for _ in range(q.qsize())]
    assert got == list(range(5, 105))
    assert q2.qsize() == 100
    bus.unsubscribe(q)
    bus.unsubscribe(q)  # idempotent
    bus.publish({"i": 999})
    assert q.empty()
    assert bus.subscriber_count == 1


# -- watchlist -> Helius webhook sync -----------------------------------------------

def test_sync_webhook_called_on_watchlist_add(monkeypatch):
    calls = []

    async def fake(self, addresses, url, secret, webhook_id=None):
        calls.append((addresses, url, secret, webhook_id))
        return "wh-1"

    monkeypatch.setattr(HeliusClient, "upsert_webhook", fake)
    app = _app(PUBLIC_WEBHOOK_URL="https://example.ngrok.app/")
    c = TestClient(app)
    r = c.post("/api/watchlist", json={"address": WALLET}, headers=H)
    assert r.status_code == 201
    assert r.headers["X-Webhook-Sync"] == "ok"
    assert calls == [([WALLET], "https://example.ngrok.app/webhook/helius", SECRET, None)]
    deleted = []

    async def fake_delete(self, webhook_id):
        deleted.append(webhook_id)

    monkeypatch.setattr(HeliusClient, "delete_webhook", fake_delete)
    r = c.delete(f"/api/watchlist/{WALLET}", headers=H)
    assert r.status_code == 204 and r.headers["X-Webhook-Sync"] == "ok"
    assert len(calls) == 1 and deleted == ["wh-1"]  # empty list -> DELETE, never PUT []


def test_public_url_with_webhook_path_not_doubled(monkeypatch):
    calls = []

    async def fake(self, addresses, url, secret, webhook_id=None):
        calls.append(url)
        return "wh-1"

    monkeypatch.setattr(HeliusClient, "upsert_webhook", fake)
    c = TestClient(_app(PUBLIC_WEBHOOK_URL="https://example.ngrok.app/webhook/helius/"))
    assert c.post("/api/watchlist", json={"address": WALLET}, headers=H).headers["X-Webhook-Sync"] == "ok"
    assert calls == ["https://example.ngrok.app/webhook/helius"]


def test_sync_skipped_without_public_url(monkeypatch):
    async def boom(self, *a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(HeliusClient, "upsert_webhook", boom)
    c = TestClient(_app())
    r = c.post("/api/watchlist", json={"address": WALLET}, headers=H)
    assert r.status_code == 201 and r.headers["X-Webhook-Sync"] == "skipped"
    r = c.delete(f"/api/watchlist/{WALLET}", headers=H)
    assert r.status_code == 204 and r.headers["X-Webhook-Sync"] == "skipped"


def test_sync_failure_does_not_fail_request(monkeypatch, caplog):
    async def fail(self, *a, **k):
        raise HeliusError("POST https://api.helius.xyz/v0/webhooks -> HTTP 500")

    monkeypatch.setattr(HeliusClient, "upsert_webhook", fail)
    c = TestClient(_app(PUBLIC_WEBHOOK_URL="https://x.example"))
    with caplog.at_level("WARNING"):
        r = c.post("/api/watchlist", json={"address": WALLET}, headers=H)
    assert r.status_code == 201 and r.headers["X-Webhook-Sync"] == "error"
    assert c.get("/api/watchlist", headers=H).json()[0]["address"] == WALLET
    assert "HeliusError" in caplog.text
    assert "hkey-test-xyz" not in caplog.text and SECRET not in caplog.text

    async def fail2(self, *a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(HeliusClient, "upsert_webhook", fail2)
    r = c.delete(f"/api/watchlist/{WALLET}", headers=H)
    assert r.status_code == 204 and r.headers["X-Webhook-Sync"] == "error"


@respx.mock
def test_webhook_id_persisted_in_kv():
    respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{API}/v0/webhooks").mock(
        return_value=httpx.Response(200, json={"webhookID": "wh-new"}))
    update = respx.put(f"{API}/v0/webhooks/wh-new").mock(
        return_value=httpx.Response(200, json={"webhookID": "wh-new"}))
    app = _app(PUBLIC_WEBHOOK_URL="https://x.example")
    c = TestClient(app)
    assert c.post("/api/watchlist", json={"address": WALLET}, headers=H).headers["X-Webhook-Sync"] == "ok"
    assert create.call_count == 1 and update.call_count == 0
    with Session(app.state.engine) as s:
        assert s.get(Kv, "helius_webhook_id").value == "wh-new"
    assert c.post("/api/watchlist", json={"address": OTHER}, headers=H).headers["X-Webhook-Sync"] == "ok"
    assert create.call_count == 1 and update.call_count == 1
    body = json.loads(update.calls[-1].request.content)
    assert sorted(body["accountAddresses"]) == sorted([WALLET, OTHER])


@respx.mock
def test_settings_webhook_id_wins_over_kv():
    update = respx.put(f"{API}/v0/webhooks/wh-env").mock(return_value=httpx.Response(200, json={}))
    app = _app(PUBLIC_WEBHOOK_URL="https://x.example", HELIUS_WEBHOOK_ID="wh-env")
    with Session(app.state.engine) as s:
        s.add(Kv(key="helius_webhook_id", value="wh-kv"))
        s.commit()
    r = TestClient(app).post("/api/watchlist", json={"address": WALLET}, headers=H)
    assert r.headers["X-Webhook-Sync"] == "ok" and update.call_count == 1


@respx.mock
async def test_upsert_webhook_create_and_update():
    listing = respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[
        {"webhookID": "other", "webhookURL": "https://elsewhere/webhook/helius"}]))
    create = respx.post(f"{API}/v0/webhooks").mock(
        return_value=httpx.Response(200, json={"webhookID": "id1"}))
    update = respx.put(f"{API}/v0/webhooks/id1").mock(return_value=httpx.Response(200, json={}))
    async with HeliusClient("hk", rps=100) as h:
        assert await h.upsert_webhook(["a"], "https://u/webhook/helius", "sec", None) == "id1"
        assert await h.upsert_webhook(["a", "b"], "https://u/webhook/helius", "sec", "id1") == "id1"
    req = create.calls[0].request
    assert req.url.params["api-key"] == "hk"
    assert json.loads(req.content) == {
        "webhookURL": "https://u/webhook/helius", "transactionTypes": ["SWAP"],
        "accountAddresses": ["a"], "webhookType": "enhanced", "authHeader": "sec"}
    assert json.loads(update.calls[0].request.content)["accountAddresses"] == ["a", "b"]
    assert listing.call_count == 1 and create.call_count == 1  # list-then-create; update path skips list


@respx.mock
async def test_upsert_webhook_error_redacts_key():
    respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/v0/webhooks").mock(return_value=httpx.Response(400, json={"error": "bad"}))
    async with HeliusClient("secretkey123", rps=100, max_retries=0) as h:
        with pytest.raises(HeliusError) as ei:
            await h.upsert_webhook(["a"], "https://u", "sec", None)
    assert "secretkey123" not in str(ei.value)


async def _nosleep(_):
    return None


@respx.mock
async def test_upsert_reuses_existing_webhook_by_url():
    respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[
        {"webhookID": "other", "webhookURL": "https://elsewhere/webhook/helius"},
        {"webhookID": "found-1", "webhookURL": "https://u/webhook/helius"}]))
    create = respx.post(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json={"webhookID": "dup"}))
    update = respx.put(f"{API}/v0/webhooks/found-1").mock(return_value=httpx.Response(200, json={}))
    async with HeliusClient("hk", rps=100) as h:
        assert await h.upsert_webhook(["a"], "https://u/webhook/helius", "sec", None) == "found-1"
    assert create.call_count == 0 and update.call_count == 1


@respx.mock
async def test_upsert_create_post_timeout_not_retried():
    respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{API}/v0/webhooks").mock(side_effect=httpx.ReadTimeout("slow"))
    async with HeliusClient("hk", rps=100, max_retries=5, sleep=_nosleep) as h:
        with pytest.raises(HeliusError):
            await h.upsert_webhook(["a"], "https://u/webhook/helius", "sec", None)
    assert create.call_count == 1


@respx.mock
async def test_upsert_create_list_get_is_retried():
    listing = respx.get(f"{API}/v0/webhooks").mock(side_effect=[
        httpx.Response(503), httpx.Response(200, json=[])])
    create = respx.post(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json={"webhookID": "n1"}))
    async with HeliusClient("hk", rps=100, max_retries=2, sleep=_nosleep) as h:
        assert await h.upsert_webhook(["a"], "https://u/webhook/helius", "sec", None) == "n1"
    assert listing.call_count == 2 and create.call_count == 1


@respx.mock
async def test_upsert_put_404_falls_back_to_create_path():
    respx.put(f"{API}/v0/webhooks/gone").mock(return_value=httpx.Response(404, json={"error": "not found"}))
    respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json={"webhookID": "n2"}))
    async with HeliusClient("hk", rps=100, sleep=_nosleep) as h:
        assert await h.upsert_webhook(["a"], "https://u/webhook/helius", "sec", "gone") == "n2"
    assert create.call_count == 1


@respx.mock
async def test_delete_webhook_empty_body_and_404_ok():
    respx.delete(f"{API}/v0/webhooks/w1").mock(return_value=httpx.Response(200))
    respx.delete(f"{API}/v0/webhooks/w2").mock(return_value=httpx.Response(404))
    async with HeliusClient("hk", rps=100, sleep=_nosleep) as h:
        await h.delete_webhook("w1")
        await h.delete_webhook("w2")  # already gone: not an error


@respx.mock
def test_last_removal_deletes_webhook_and_clears_kv_then_add_recreates():
    respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[]))
    create = respx.post(f"{API}/v0/webhooks").mock(side_effect=[
        httpx.Response(200, json={"webhookID": "wh-a"}), httpx.Response(200, json={"webhookID": "wh-b"})])
    put = respx.put(url__regex=rf"{API}/v0/webhooks/.*").mock(return_value=httpx.Response(200, json={}))
    delete = respx.delete(f"{API}/v0/webhooks/wh-a").mock(return_value=httpx.Response(200, json={}))
    app = _app(PUBLIC_WEBHOOK_URL="https://x.example")
    c = TestClient(app)
    assert c.post("/api/watchlist", json={"address": WALLET}, headers=H).headers["X-Webhook-Sync"] == "ok"
    r = c.delete(f"/api/watchlist/{WALLET}", headers=H)
    assert r.status_code == 204 and r.headers["X-Webhook-Sync"] == "ok"
    assert delete.call_count == 1 and put.call_count == 0
    with Session(app.state.engine) as s:
        assert s.get(Kv, "helius_webhook_id") is None
    assert c.post("/api/watchlist", json={"address": OTHER}, headers=H).headers["X-Webhook-Sync"] == "ok"
    assert create.call_count == 2 and put.call_count == 0
    assert json.loads(create.calls[-1].request.content)["accountAddresses"] == [OTHER]
    with Session(app.state.engine) as s:
        assert s.get(Kv, "helius_webhook_id").value == "wh-b"


@respx.mock
def test_empty_watchlist_without_webhook_is_noop():
    listing = respx.get(f"{API}/v0/webhooks").mock(return_value=httpx.Response(200, json=[]))
    app = _app(PUBLIC_WEBHOOK_URL="https://x.example")
    _watch(app)
    with Session(app.state.engine) as s:
        s.delete(s.get(Watchlist, WALLET))
        s.commit()
    from hades.realtime import sync_webhook
    import asyncio as _a
    app.state.sync_lock = _a.Lock()
    assert _a.run(sync_webhook(app)) == "ok"
    assert listing.call_count == 1  # looked for an orphan by URL, found none, nothing deleted


def test_kv_write_failure_after_create_logs_and_returns_ok(monkeypatch, caplog):
    async def fake(self, addresses, url, secret, webhook_id=None):
        return "wh-z"

    def broken(engine, webhook_id):
        raise RuntimeError("disk full")

    monkeypatch.setattr(HeliusClient, "upsert_webhook", fake)
    from hades import realtime
    monkeypatch.setattr(realtime, "_store_webhook_id", broken)
    c = TestClient(_app(PUBLIC_WEBHOOK_URL="https://x.example"))
    with caplog.at_level("WARNING"):
        r = c.post("/api/watchlist", json={"address": WALLET}, headers=H)
    assert r.status_code == 201 and r.headers["X-Webhook-Sync"] == "ok"
    assert "could not persist webhook id" in caplog.text


# -- migration -----------------------------------------------------------------------

def test_alerts_unique_index_migration():
    eng = make_engine("sqlite:///:memory:")
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet VARCHAR, mint VARCHAR, "
            "side VARCHAR, sol_amount FLOAT, signature VARCHAR, block_time DATETIME, "
            "created_at DATETIME, delivered_tg BOOLEAN)"
        ))
        for _ in range(2):  # pre-existing duplicate
            c.execute(text("INSERT INTO alerts (wallet, mint, side, signature) VALUES ('w','m','buy','s')"))
        c.execute(text("INSERT INTO alerts (wallet, mint, side, signature) VALUES ('w','m2','buy','s')"))
    init_db(eng)
    init_db(eng)  # idempotent
    with eng.connect() as c:
        names = {r[1] for r in c.execute(text("PRAGMA index_list(alerts)"))}
        n = c.execute(text("SELECT count(*) FROM alerts")).scalar()
    assert "ux_alerts_sig_wallet_mint" in names
    assert n == 2
    with pytest.raises(IntegrityError):
        with eng.begin() as c:
            c.execute(text("INSERT INTO alerts (wallet, mint, side, signature) VALUES ('w','m','buy','s')"))
    eng.dispose()


def test_fresh_db_has_unique_index_and_kv():
    eng = make_engine("sqlite:///:memory:")
    init_db(eng)
    with eng.connect() as c:
        names = {r[1] for r in c.execute(text("PRAGMA index_list(alerts)"))}
        tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "ux_alerts_sig_wallet_mint" in names and "kv" in tables
    eng.dispose()


# -- folded hardening ----------------------------------------------------------------

def test_docs_disabled():
    c = TestClient(_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_watchlist_note_max_length():
    c = TestClient(_app())
    assert c.post("/api/watchlist", json={"address": WALLET, "note": "x" * 281}, headers=H).status_code == 422
    assert c.post("/api/watchlist", json={"address": WALLET, "note": "x" * 280}, headers=H).status_code == 201


def test_lazy_app_attribute_error_becomes_runtime_error(monkeypatch):
    def broken(settings=None):
        raise AttributeError("inner")

    monkeypatch.setattr(api_main, "create_app", broken)
    monkeypatch.setattr(api_main, "_app", None)
    with pytest.raises(RuntimeError):
        api_main.app  # noqa: B018
    with pytest.raises(AttributeError):
        api_main.nope  # noqa: B018
