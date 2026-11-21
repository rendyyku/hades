"""Task 10: the head API contract (spec 9) -- key guard, wallets, follows, paper, alerts,
SSE stream."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from hades_core.events import TradeEvent
from hades_hl import api
from hades_hl import follows as fl
from hades_hl import paper_runtime as pr
from hades_hl.config import Settings
from hades_hl.db import Alert, Fill, Follow, Funding, PaperEvent, Price, Tag, Wallet
from hades_hl.live import event_to_json
from hades_hl.pipeline import kv_set_json

UTC = timezone.utc
START = datetime(2026, 10, 1, tzinfo=UTC)
KEY = "test-key"
H = {"X-Hades-Key": KEY}


def addr(i: int) -> str:
    return f"0x{i:040x}"


A = addr(1)


def make_app(tmp_path, key: str = KEY):
    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    app = api.create_app(Settings(API_KEY=key, DATABASE_URL=url, MAX_FOLLOWS=2))
    return app


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
def client(app):
    return TestClient(app)


def db(app) -> Session:
    return Session(app.state.engine)


def stats(**kw) -> dict:
    base = {"net_pnl_usd": 1000.0, "trades": 40, "winrate": 0.6, "n": 30, "avg_leverage": 3.5,
            "liquidations": 1, "funding_usd": -12.5, "window_days": 30.0, "max_dd": 0.1}
    base.update(kw)
    return base


# -- key guard -----------------------------------------------------------------------

ROUTES = ["/api/health", "/api/wallets", f"/api/wallets/{A}", "/api/follows",
          f"/api/paper/{A}", "/api/alerts", "/api/stream"]


@pytest.mark.parametrize("path", ROUTES)
def test_401_without_or_with_wrong_key(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Hades-Key": "nope"}).status_code == 401


@pytest.mark.parametrize("path", ROUTES)
def test_503_when_no_key_configured(tmp_path, path):
    c = TestClient(make_app(tmp_path, key=""))
    assert c.get(path, headers=H).status_code == 503


def test_write_routes_need_key(client):
    assert client.post(f"/api/follows/{A}").status_code == 401
    assert client.delete(f"/api/follows/{A}").status_code == 401


def test_docs_and_openapi_disabled(client):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404


def test_heartbeat_default_15s(app):
    assert api.HEARTBEAT_S == 15 and app.state.heartbeat_s == 15


# -- health --------------------------------------------------------------------------

def test_health(app, client):
    r = client.get("/api/health", headers=H)
    assert r.status_code == 200
    assert r.json() == {"head": "hyperliquid", "ok": True, "last_ingest_at": None,
                        "version": "0.1.0"}
    with db(app) as s:
        s.add(Fill(address=A, tid=1, coin="BTC", px=Decimal(1), sz=Decimal(1), side="B",
                   dir="Open Long", start_position=Decimal(0), closed_pnl=Decimal(0),
                   fee=Decimal(0), time=int(START.timestamp() * 1000), crossed=True))
        s.commit()
    got = client.get("/api/health", headers=H).json()["last_ingest_at"]
    assert datetime.fromisoformat(got.replace("Z", "+00:00")) == START


# -- wallets -------------------------------------------------------------------------

def seed_wallets(app):
    with db(app) as s:
        s.add(Wallet(address=addr(1), score=90.0, stats_json=stats(net_pnl_usd=100.0)))
        s.add(Wallet(address=addr(2), score=50.0, stats_json=stats(net_pnl_usd=5000.0)))
        s.add(Wallet(address=addr(3), score=None, stats_json=stats(n=0, winrate=None,
                                                                   net_pnl_usd=7.0)))
        s.add(Wallet(address=addr(4), score=95.0, is_bot=True, stats_json=stats()))
        s.add(Tag(address=addr(1), tag="pre_move",
                  evidence_json=[{"tid": 7, "hash": "0xabc", "coin": "BTC", "time": 1,
                                  "detail": {"interval": "5m"}}]))
        s.commit()


def test_wallets_sorted_by_score_and_pnl(app, client):
    seed_wallets(app)
    rows = client.get("/api/wallets", headers=H).json()
    assert [r["address"] for r in rows] == [addr(1), addr(2), addr(3)]  # bots excluded
    rows = client.get("/api/wallets?sort=pnl", headers=H).json()
    assert [r["address"] for r in rows] == [addr(2), addr(1), addr(3)]
    assert client.get("/api/wallets?limit=1", headers=H).json()[0]["address"] == addr(1)
    assert client.get("/api/wallets?limit=201", headers=H).status_code == 422
    assert client.get("/api/wallets?sort=nope", headers=H).status_code == 422


def test_winrate_comes_with_n(app, client):
    seed_wallets(app)
    with db(app) as s:  # a stored winrate with n = 0 is never served
        s.add(Wallet(address=addr(5), score=10.0, stats_json=stats(n=0, winrate=0.5)))
        s.commit()
    rows = {r["address"]: r for r in client.get("/api/wallets", headers=H).json()}
    for r in rows.values():
        assert "n" in r and "winrate" in r
        assert r["winrate"] is None or r["n"] > 0
    assert rows[addr(1)]["winrate"] == 0.6 and rows[addr(1)]["n"] == 30
    assert rows[addr(3)]["winrate"] is None and rows[addr(3)]["n"] == 0
    assert rows[addr(5)]["winrate"] is None
    d = client.get(f"/api/wallets/{addr(1)}", headers=H).json()
    assert d["winrate"] == 0.6 and d["n"] == 30


def test_wallet_common_shape(app, client):
    seed_wallets(app)
    r = client.get("/api/wallets", headers=H).json()[0]
    assert {"address", "score", "net_pnl_usd", "trades", "winrate", "n", "tags",
            "extra"} <= set(r)
    assert r["tags"] == ["pre_move"] and Decimal(r["net_pnl_usd"]) == 100
    assert r["trades"] == 40


def test_wallet_detail_extra(app, client):
    seed_wallets(app)
    t0 = int(START.timestamp() * 1000)
    with db(app) as s:
        w = s.get(Wallet, addr(1))
        w.scored_at = START + timedelta(days=3)
        for i, (day, cpnl, fee) in enumerate([(0, "0", "1"), (1, "50", "1"), (2, "-20", "2")]):
            s.add(Fill(address=addr(1), tid=i + 1, coin="BTC", px=Decimal(100), sz=Decimal(1),
                       side="B", dir="Close Long", start_position=Decimal(1),
                       closed_pnl=Decimal(cpnl), fee=Decimal(fee),
                       time=t0 + day * 86_400_000 + 1000, crossed=True))
        s.add(Funding(address=addr(1), coin="BTC", time=t0 + 86_400_000 + 5, usdc=Decimal("-3")))
        s.commit()
    d = client.get(f"/api/wallets/{addr(1).upper().replace('0X', '0x')}", headers=H).json()
    ex = d["extra"]
    assert ex["avg_leverage"] == 3.5 and ex["liquidations"] == 1 and ex["funding_usd"] == -12.5
    assert ex["tags"] == [{"tag": "pre_move", "evidence": [
        {"tid": 7, "hash": "0xabc", "coin": "BTC", "time": 1, "detail": {"interval": "5m"}}],
        "created_at": ex["tags"][0]["created_at"]}]
    # cumulative daily net (closedPnl - fee + funding) over the stats window
    assert [Decimal(v) for _, v in ex["pnl_curve"]] == [Decimal(-1), Decimal(45), Decimal(23)]
    assert ex["follow"] is None


def test_wallet_detail_404_and_400(client):
    assert client.get(f"/api/wallets/{addr(9)}", headers=H).status_code == 404
    assert client.get("/api/wallets/notanaddress", headers=H).status_code == 400


# -- follows -------------------------------------------------------------------------

def test_follow_add_list_remove(app, client):
    r = client.post(f"/api/follows/{A}", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["address"] == A and body["origin"] == "manual" and body["ladder"] == "PAPER"
    rows = client.get("/api/follows", headers=H).json()
    assert [x["address"] for x in rows] == [A] and rows[0]["summary"] is None
    r = client.delete(f"/api/follows/{A}", headers=H)
    assert r.status_code == 200 and r.json() == {"address": A, "removed": True}
    assert client.get("/api/follows", headers=H).json() == []
    with db(app) as s:
        assert s.get(Follow, A).removed_manually is True  # never auto-followed again


def test_follow_cap_returns_409(client):
    assert client.post(f"/api/follows/{addr(1)}", headers=H).status_code == 200
    assert client.post(f"/api/follows/{addr(2)}", headers=H).status_code == 200
    r = client.post(f"/api/follows/{addr(3)}", headers=H)
    assert r.status_code == 409
    # re-following an active one is not a new slot
    assert client.post(f"/api/follows/{addr(1)}", headers=H).status_code == 200


def test_follow_bad_address_400(client):
    assert client.post("/api/follows/xyz", headers=H).status_code == 400
    assert client.delete("/api/follows/xyz", headers=H).status_code == 400


# -- paper ---------------------------------------------------------------------------

def store(s, side, at, seq, *, delay_ms, pid="BTC:1"):
    e = TradeEvent(head="hyperliquid", wallet=A, market_id="BTC", side=side, direction="long",
                   size=Decimal(1), price=Decimal(100), notional_usd=Decimal(100),
                   fee_usd=Decimal(0), leader_time=at - timedelta(seconds=1), observed_time=at,
                   source_ref=f"0xh{seq}:{seq}", position_id=pid, seq=seq)
    js = event_to_json(e)
    js["leader_start"] = "0" if side == "open" else "1"
    s.add(PaperEvent(source_ref=e.source_ref, address=A, json=js, observed_time=at,
                     delay_ms=delay_ms, seq=seq))
    kv_set_json(s, f"hl:seq:{A}", seq)


def test_paper_detail(app, client):
    t1 = START + timedelta(hours=1)
    with db(app) as s:
        fl.add_follow(s, A, START)
        s.add(Price(coin="BTC", time=int((t1 + timedelta(seconds=1)).timestamp() * 1000),
                    mid=Decimal(100)))
        s.add(Price(coin="BTC", time=int((t1 + timedelta(hours=1, seconds=1)).timestamp()
                                         * 1000), mid=Decimal(105)))
        store(s, "open", t1, 1, delay_ms=1000)
        store(s, "close", t1 + timedelta(hours=1), 2, delay_ms=3000)
        # no price recorded for this one: missed
        store(s, "open", START + timedelta(hours=5), 3, delay_ms=2000, pid="BTC:2")
        pr.run_paper(s, A, START + timedelta(days=2))
        s.commit()
    r = client.get(f"/api/paper/{A}", headers=H)
    assert r.status_code == 200
    p = r.json()
    assert p["missed_count"] == 1
    assert p["median_delay_ms"] == 2000
    assert p["summary"]["address"] == A and p["summary"]["closed_trades"] == 1
    assert p["winrate"] == 1.0 and p["n"] == 1
    assert len(p["fills"]) == 3 and p["equity_curve"]
    assert set(p["gate"]) >= {"full", "rolling"}
    assert set(p["gate"]["full"]) == {"passed", "checks", "values"}
    assert p["gate"]["full"]["passed"] is False and "net_pnl" in p["gate"]["rolling"]["checks"]
    assert [(h["from"], h["to"]) for h in p["ladder_history"]] == [(None, "PAPER")]
    assert "gap_usd" in p and "leader_realized_usd" in p
    rows = client.get("/api/follows", headers=H).json()
    assert rows[0]["summary"]["closed_trades"] == 1 and rows[0]["sparkline"]


def test_paper_followed_but_not_run_yet(app, client):
    with db(app) as s:
        fl.add_follow(s, A, START)
        s.commit()
    p = client.get(f"/api/paper/{A}", headers=H).json()
    assert p["summary"] is None and p["missed_count"] == 0 and p["median_delay_ms"] is None
    assert p["winrate"] is None and p["n"] == 0


def test_paper_404_never_followed(client):
    assert client.get(f"/api/paper/{A}", headers=H).status_code == 404


# -- alerts --------------------------------------------------------------------------

def add_alert(app, kind="open", wallet=A) -> int:
    with db(app) as s:
        a = Alert(kind=kind, address=wallet, coin="BTC", source_ref=f"r:{kind}",
                  payload_json={"x": 1})
        s.add(a)
        s.commit()
        return a.id


def test_alerts_newest_first_with_head(app, client):
    ids = [add_alert(app, k) for k in ("open", "close", "ladder")]
    rows = client.get("/api/alerts?limit=2", headers=H).json()
    assert [r["id"] for r in rows] == ids[::-1][:2]
    assert rows[0]["head"] == "hyperliquid" and rows[0]["wallet"] == A
    assert rows[0]["market_id"] == "BTC" and rows[0]["payload"] == {"x": 1}
    assert client.get("/api/alerts?limit=0", headers=H).status_code == 422


# -- SSE -----------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, app):
        self.app = app
        self.headers = {}
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


async def _frames(resp, n):
    out = []
    async for chunk in resp.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        out.append(text)
        if len(out) >= n:
            break
    return out


def _parse(frame: str) -> dict:
    lines = dict(line.split(": ", 1) for line in frame.strip().splitlines())
    return lines


async def test_sse_emits_inserted_alert(app):
    app.state.heartbeat_s = 30  # no ping should be needed
    app.state.poll_s = 0.01
    add_alert(app, "old")  # before the stream started: not replayed
    req = FakeRequest(app)
    resp = await api.stream(req)
    assert resp.media_type == "text/event-stream"
    task = asyncio.create_task(_frames(resp, 1))
    await asyncio.sleep(0.05)
    new_id = add_alert(app, "close")
    frames = await asyncio.wait_for(task, 5)
    f = _parse(frames[0])
    assert f["event"] == "alert" and f["id"] == str(new_id)
    data = json.loads(f["data"])
    assert data["id"] == new_id and data["head"] == "hyperliquid" and data["kind"] == "close"


async def test_sse_heartbeat(app):
    app.state.heartbeat_s = 0.02
    app.state.poll_s = 0.01
    resp = await api.stream(FakeRequest(app))
    frames = await asyncio.wait_for(_frames(resp, 1), 5)
    assert _parse(frames[0])["event"] == "ping"


async def test_sse_stops_on_disconnect(app):
    app.state.poll_s = 0.01
    req = FakeRequest(app)
    req.disconnected = True
    resp = await api.stream(req)
    assert await asyncio.wait_for(_frames(resp, 1), 5) == []
