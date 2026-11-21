"""Task 14 (ruling P64): GET /api/wallets/{address}/positions -- a wallet's live open
positions from Hyperliquid ``clearinghouseState``, cached 5 s per address."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from hades_hl import api, cli, positions
from hades_hl.client import HLClient, HLError
from hades_hl.config import Settings
from hades_hl.db import Wallet

UTC = timezone.utc
KEY = "test-key"
H = {"X-Hades-Key": KEY}
INFO = "https://api.hyperliquid.xyz/info"
NOW = datetime(2026, 11, 22, 4, 0, tzinfo=UTC)
OTHER = "0x" + "0b" * 20


class FakeHL:
    """``clearinghouse_state`` double: a state dict, or an exception to raise."""

    def __init__(self, answer):
        self.answer = answer
        self.calls: list[str] = []

    async def clearinghouse_state(self, addr, dex=None):
        self.calls.append(addr)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Mono:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def fx(fixture):
    return fixture("clearinghouse_state_positions.json")


@pytest.fixture
def app(tmp_path, fx):
    url = f"sqlite:///{(tmp_path / 'pos.db').as_posix()}"
    app = api.create_app(Settings(API_KEY=KEY, DATABASE_URL=url))
    app.state.clock = lambda: NOW
    app.state.positions = positions.PositionsCache(clock=Mono())
    with Session(app.state.engine) as s:
        s.add(Wallet(address=fx["address"], stats_json={}))
        s.add(Wallet(address=OTHER, stats_json={}))
        s.commit()
    return app


def get(app, addr, headers=H):
    return TestClient(app).get(f"/api/wallets/{addr}/positions", headers=headers)


def empty_state():
    return {"marginSummary": {"accountValue": "1500.25", "totalNtlPos": "0.0",
                              "totalRawUsd": "1500.25", "totalMarginUsed": "0.0"},
            "assetPositions": [], "time": 1795293641294}


# -- guards --------------------------------------------------------------------------

def test_requires_the_key(app, fx):
    app.state.hl_client = FakeHL(fx["state"])
    assert get(app, fx["address"], headers={}).status_code == 401
    assert get(app, fx["address"], headers={"X-Hades-Key": "nope"}).status_code == 401
    assert app.state.hl_client.calls == []


@pytest.mark.parametrize("bad", ["0x12", "not-an-address", "0x" + "zz" * 20, "0x" + "ab" * 21])
def test_400_on_a_malformed_address_before_any_upstream_call(app, fx, bad):
    app.state.hl_client = FakeHL(fx["state"])
    r = get(app, bad)
    assert r.status_code == 400 and r.json() == {"detail": "not a Hyperliquid address"}
    assert app.state.hl_client.calls == []


def test_404_for_a_wallet_the_head_does_not_know(app, fx):
    app.state.hl_client = FakeHL(fx["state"])
    r = get(app, "0x" + "cd" * 20)
    assert r.status_code == 404
    assert app.state.hl_client.calls == []


# -- the real fixture ------------------------------------------------------------------

def test_positions_from_the_recorded_state(app, fx):
    app.state.hl_client = FakeHL(fx["state"])
    r = get(app, fx["address"].upper().replace("0X", "0x"))  # any case: normalized
    assert r.status_code == 200
    p = r.json()
    raw = {a["position"]["coin"]: a["position"] for a in fx["state"]["assetPositions"]}
    assert p["address"] == fx["address"]
    assert Decimal(p["account_value"]) == Decimal(fx["state"]["marginSummary"]["accountValue"])
    assert Decimal(p["total_unrealized_pnl"]) == sum(
        Decimal(x["unrealizedPnl"]) for x in raw.values())
    assert datetime.fromisoformat(p["as_of"]) == datetime.fromtimestamp(
        fx["state"]["time"] / 1000, tz=UTC)
    assert p["scope"] == "main perp dex"
    # largest position value first
    assert [x["coin"] for x in p["positions"]] == sorted(
        raw, key=lambda c: Decimal(raw[c]["positionValue"]), reverse=True)
    assert set(p["positions"][0]) == {
        "coin", "side", "size", "entry_px", "mark_px", "position_value", "unrealized_pnl",
        "roe_pct", "leverage", "liquidation_px", "margin_used"}
    by = {x["coin"]: x for x in p["positions"]}
    for coin, src in raw.items():
        x = by[coin]
        szi = Decimal(src["szi"])
        assert x["side"] == ("long" if szi > 0 else "short")
        assert Decimal(x["size"]) == abs(szi)
        assert Decimal(x["entry_px"]) == Decimal(src["entryPx"])
        assert Decimal(x["position_value"]) == Decimal(src["positionValue"])
        assert Decimal(x["unrealized_pnl"]) == Decimal(src["unrealizedPnl"])
        assert Decimal(x["roe_pct"]) == Decimal(src["returnOnEquity"]) * 100
        assert Decimal(x["margin_used"]) == Decimal(src["marginUsed"])
        assert x["leverage"] == {"type": src["leverage"]["type"],
                                 "value": str(src["leverage"]["value"])}
        # HL reports no mark: position value / size, to 8 significant digits
        mark = Decimal(src["positionValue"]) / abs(szi)
        assert abs(Decimal(x["mark_px"]) - mark) <= abs(mark) * Decimal("1e-7")
        if src["liquidationPx"] is None:
            assert x["liquidation_px"] is None
        else:
            assert Decimal(x["liquidation_px"]) == Decimal(src["liquidationPx"])


def test_fixture_covers_the_cases_the_page_must_render(fx):
    ps = [a["position"] for a in fx["state"]["assetPositions"]]
    assert {p["leverage"]["type"] for p in ps} == {"cross", "isolated"}
    assert {Decimal(p["szi"]) > 0 for p in ps} == {True, False}
    assert any(p["liquidationPx"] is None for p in ps)


def test_flat_account_has_no_positions(app, fx):
    app.state.hl_client = FakeHL(empty_state())
    p = get(app, fx["address"]).json()
    assert p["positions"] == [] and Decimal(p["total_unrealized_pnl"]) == 0
    assert Decimal(p["account_value"]) == Decimal("1500.25")


def test_as_of_falls_back_to_our_clock_without_a_state_time(app, fx):
    st = empty_state()
    del st["time"]
    app.state.hl_client = FakeHL(st)
    assert datetime.fromisoformat(get(app, fx["address"]).json()["as_of"]) == NOW


# -- cache ---------------------------------------------------------------------------

def test_cached_5_s_per_address(app, fx):
    hl = app.state.hl_client = FakeHL(fx["state"])
    mono = app.state.positions.clock
    assert positions.POSITIONS_TTL_S == 5.0
    for _ in range(3):
        assert get(app, fx["address"]).status_code == 200
    assert hl.calls == [fx["address"]]
    get(app, OTHER)  # another address has its own entry
    assert hl.calls == [fx["address"], OTHER]
    mono.t += 4.9
    get(app, fx["address"])
    assert len(hl.calls) == 2
    mono.t += 0.2  # 5.1 s after the first fetch
    get(app, fx["address"])
    assert hl.calls == [fx["address"], OTHER, fx["address"]]


async def test_concurrent_requests_share_one_upstream_call():
    cache = positions.PositionsCache()
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return "value"

    got = await asyncio.gather(*(cache.get("0xa", fetch) for _ in range(5)))
    assert got == ["value"] * 5 and calls == 1


def test_cache_is_bounded():
    mono = Mono()
    cache = positions.PositionsCache(clock=mono, max_entries=3)

    async def run():
        for i in range(10):
            mono.t += 6  # every earlier entry has expired
            await cache.get(f"0x{i}", lambda: asyncio.sleep(0, "v"))

    asyncio.run(run())
    assert len(cache) <= 3


# -- upstream failures -----------------------------------------------------------------

SECRET = "upstream said: internal 10.0.0.7 token=abc123"


@pytest.mark.parametrize("answer", [
    HLError(SECRET), RuntimeError(SECRET), asyncio.TimeoutError(),
    None, {}, [], {"assetPositions": []}, {"marginSummary": {}, "assetPositions": []},
    {"marginSummary": {"accountValue": "x"}, "assetPositions": []},
    {"marginSummary": {"accountValue": "1"}, "assetPositions": [{"position": {"coin": "BTC"}}]},
    {"marginSummary": {"accountValue": "1"}, "assetPositions": [None]},
    {"marginSummary": {"accountValue": "1"},
     "assetPositions": [{"position": {"coin": "BTC", "szi": "1", "positionValue": "oops",
                                      "unrealizedPnl": "0", "marginUsed": "1",
                                      "leverage": {"type": "cross", "value": 3}}}]},
])
def test_502_fixed_message_never_the_upstream_body(app, fx, answer):
    app.state.hl_client = FakeHL(answer)
    r = get(app, fx["address"])
    assert r.status_code == 502
    assert r.json() == {"detail": "hyperliquid upstream unavailable"}
    assert "abc123" not in r.text and "10.0.0.7" not in r.text


def test_a_failure_is_not_retried_within_the_ttl_then_recovers(app, fx):
    hl = app.state.hl_client = FakeHL(HLError(SECRET))
    mono = app.state.positions.clock
    assert get(app, fx["address"]).status_code == 502
    assert get(app, fx["address"]).status_code == 502
    assert len(hl.calls) == 1  # a broken upstream is asked once per 5 s, not per poll
    hl.answer = fx["state"]
    mono.t += 5.1
    assert get(app, fx["address"]).status_code == 200
    assert len(hl.calls) == 2


def test_a_hung_upstream_times_out_as_502(app, fx, monkeypatch):
    class Hung(FakeHL):
        async def clearinghouse_state(self, addr, dex=None):
            self.calls.append(addr)
            await asyncio.sleep(30)

    monkeypatch.setattr(positions, "UPSTREAM_TIMEOUT_S", 0.05)
    app.state.hl_client = Hung(None)
    r = get(app, fx["address"])
    assert r.status_code == 502 and r.json() == {"detail": "hyperliquid upstream unavailable"}


# -- the real client ---------------------------------------------------------------------

@respx.mock
def test_uses_the_shared_hl_client_and_its_weight_bucket(app, fx):
    route = respx.post(INFO).mock(return_value=httpx.Response(200, json=fx["state"]))
    client = app.state.hl_client = HLClient(max_attempts=1)
    before = client._bucket.tokens
    r = get(app, fx["address"])
    assert r.status_code == 200 and len(r.json()["positions"]) == 3
    assert json.loads(route.calls[0].request.content) == {
        "type": "clearinghouseState", "user": fx["address"]}
    assert client._bucket.tokens < before  # weight 2 charged to the shared limiter


@respx.mock
def test_real_client_upstream_error_is_502_without_the_body(app, fx):
    respx.post(INFO).mock(return_value=httpx.Response(422, text=SECRET))
    app.state.hl_client = HLClient(max_attempts=1)
    r = get(app, fx["address"])
    assert r.status_code == 502 and "abc123" not in r.text


def test_app_without_a_shared_client_makes_its_own_lazily(tmp_path):
    url = f"sqlite:///{(tmp_path / 'own.db').as_posix()}"
    app = api.create_app(Settings(API_KEY=KEY, DATABASE_URL=url))
    assert app.state.hl_client is None
    c = api.hl_client(app)
    assert isinstance(c, HLClient) and api.hl_client(app) is c


def test_serve_shares_the_runtime_client_with_the_api():
    class Cfg:
        app = api.FastAPI()

    class Server:
        config = Cfg()

    client = object()
    cli.attach_client(Server(), client)
    assert Cfg.app.state.hl_client is client
    cli.attach_client(object(), client)  # a server without an app: ignored
