import json
from decimal import Decimal

import httpx
import pytest
import respx

from hades_hl.client import (
    FILLS_HISTORY_CAP,
    FILLS_PAGE_LIMIT,
    HLClient,
    HLError,
    HLPage,
    HLRateLimited,
    HLUnavailable,
    WeightBucket,
    info_weight,
    item_weight,
)
from hades_hl.config import Settings

INFO = "https://api.hyperliquid.xyz/info"
LB = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
ADDR = "0x" + "ab" * 20


class FakeClock:
    """Monotonic clock + async sleep that advances it (no real waiting)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def make_client(clock: FakeClock | None = None, **kw) -> HLClient:
    clock = clock or FakeClock()
    return HLClient(sleep=clock.sleep, clock=clock, **kw)


def fill(tid: int, t: int, coin: str = "BTC") -> dict:
    return {"coin": coin, "px": "100.0", "sz": "1.0", "side": "B", "time": t,
            "startPosition": "0.0", "dir": "Open Long", "closedPnl": "0.0",
            "hash": "0x00", "oid": tid, "crossed": True, "fee": "0.01", "tid": tid,
            "feeToken": "USDC"}


def body(call) -> dict:
    return json.loads(call.request.content)


# -- settings ---------------------------------------------------------------

def test_settings_defaults(monkeypatch):
    for k in ("HL_DATABASE_URL", "HL_INFO_URL", "HL_WS_URL", "HL_MAX_FOLLOWS",
              "HL_AUTO_FOLLOW_N", "HL_RATE_WEIGHT_PER_MIN"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.DATABASE_URL == "sqlite:///hades_hl.db"
    assert s.INFO_URL == "https://api.hyperliquid.xyz/info"
    assert s.WS_URL == "wss://api.hyperliquid.xyz/ws"
    assert s.MAX_FOLLOWS == 25
    assert s.AUTO_FOLLOW_N == 10
    assert s.RATE_WEIGHT_PER_MIN == 1200


def test_settings_env_prefix(monkeypatch):
    monkeypatch.setenv("HL_MAX_FOLLOWS", "7")
    monkeypatch.setenv("HL_API_KEY", "secret-xyz")
    s = Settings(_env_file=None)
    assert s.MAX_FOLLOWS == 7
    assert s.API_KEY == "secret-xyz"
    assert "secret-xyz" not in repr(s)


# -- weights ----------------------------------------------------------------

def test_info_weights_match_docs():
    for t in ("allMids", "clearinghouseState", "l2Book", "orderStatus",
              "spotClearinghouseState", "exchangeStatus"):
        assert info_weight(t) == 2
    assert info_weight("userRole") == 60
    for t in ("meta", "userFillsByTime", "userFunding", "candleSnapshot"):
        assert info_weight(t) == 20
    assert item_weight("userFillsByTime", 2000) == 100
    assert item_weight("userFunding", 500) == 25
    assert item_weight("userFunding", 19) == 0
    assert item_weight("candleSnapshot", 120) == 2
    assert item_weight("meta", 5000) == 0


async def test_bucket_waits_when_budget_spent():
    clock = FakeClock()
    b = WeightBucket(1200, sleep=clock.sleep, clock=clock)  # 20 weight/s
    await b.acquire(1200)
    assert clock.sleeps == []
    await b.acquire(20)  # empty bucket: wait ~1 s for 20 weight
    assert sum(clock.sleeps) == pytest.approx(1.0)
    b.charge(100)  # post-hoc per-item weight puts the bucket in debt
    await b.acquire(20)
    assert sum(clock.sleeps) == pytest.approx(1.0 + 6.0)


# -- fixture-backed endpoints -------------------------------------------------

@respx.mock
async def test_all_mids_decimal(fixture):
    raw = fixture("all_mids.json")
    respx.post(INFO).mock(return_value=httpx.Response(200, json=raw))
    async with make_client() as c:
        mids = await c.all_mids()
    assert mids["BTC"] == Decimal(raw["BTC"])
    assert all(isinstance(v, Decimal) for v in mids.values())
    assert len(mids) == len(raw)


@respx.mock
async def test_meta_and_clearinghouse(fixture):
    meta, chs = fixture("meta.json"), fixture("clearinghouse_state.json")
    route = respx.post(INFO).mock(side_effect=[httpx.Response(200, json=meta),
                                              httpx.Response(200, json=chs)])
    async with make_client() as c:
        m = await c.meta()
        s = await c.clearinghouse_state(ADDR)
    assert any(u["name"] == "BTC" for u in m["universe"])
    assert "marginSummary" in s and "assetPositions" in s
    assert body(route.calls[0]) == {"type": "meta"}
    assert body(route.calls[1]) == {"type": "clearinghouseState", "user": ADDR}


@respx.mock
async def test_user_fills_by_time_real_fixture(fixture):
    rows = fixture("user_fills_by_time.json")
    meta = fixture("fixture_meta.json")
    assert 0 < len(rows) <= FILLS_PAGE_LIMIT
    route = respx.post(INFO).mock(return_value=httpx.Response(200, json=rows))
    async with make_client() as c:
        page = await c.user_fills_by_time(meta["address"], meta["start_ms"], meta["end_ms"])
    assert isinstance(page, HLPage)
    if len(rows) < FILLS_PAGE_LIMIT:
        assert route.call_count == 1
        assert len(page.fills) == len(rows)
        assert page.truncated is False
    sent = body(route.calls[0])
    assert sent == {"type": "userFillsByTime", "user": meta["address"],
                    "startTime": meta["start_ms"], "endTime": meta["end_ms"]}
    for f in page.fills:
        assert {"tid", "coin", "px", "sz", "side", "time", "dir", "closedPnl", "fee",
                "startPosition", "hash", "crossed"} <= f.keys()


@respx.mock
async def test_user_funding_real_fixture(fixture):
    rows = fixture("user_funding.json")
    meta = fixture("fixture_meta.json")
    respx.post(INFO).mock(return_value=httpx.Response(200, json=rows[:100]))
    async with make_client() as c:
        out = await c.user_funding(meta["address"], meta["start_ms"])
    assert len(out) == min(100, len(rows))
    assert all(r["delta"]["type"] == "funding" for r in out)


@respx.mock
async def test_leaderboard_real_fixture(fixture):
    raw = fixture("leaderboard_sample.json")
    respx.get(LB).mock(return_value=httpx.Response(200, json=raw))
    async with make_client() as c:
        rows = await c.leaderboard()
    assert len(rows) == len(raw["leaderboardRows"]) == 50
    assert rows[0]["ethAddress"].startswith("0x")
    assert "windowPerformances" in rows[0]


@respx.mock
async def test_leaderboard_unavailable_raises():
    respx.get(LB).mock(return_value=httpx.Response(503))
    async with make_client() as c:
        with pytest.raises(HLUnavailable):
            await c.leaderboard()


@respx.mock
async def test_leaderboard_malformed_raises():
    respx.get(LB).mock(return_value=httpx.Response(200, json={"nope": []}))
    async with make_client() as c:
        with pytest.raises(HLUnavailable):
            await c.leaderboard()


# -- paging -----------------------------------------------------------------

@respx.mock
async def test_fills_paging_stops_below_2000():
    t0 = 1_700_000_000_000
    page1 = [fill(i, t0 + i) for i in range(2000)]
    # restart is inclusive of page1's last ms, so HL re-sends tid 1999 first
    page2 = [fill(1999, t0 + 1999)] + [fill(2000 + i, t0 + 2000 + i) for i in range(5)]
    route = respx.post(INFO).mock(side_effect=[httpx.Response(200, json=page1),
                                              httpx.Response(200, json=page2)])
    async with make_client() as c:
        page = await c.user_fills_by_time(ADDR, t0)
    assert route.call_count == 2
    assert body(route.calls[0])["startTime"] == t0
    assert "endTime" not in body(route.calls[0])
    assert body(route.calls[1])["startTime"] == t0 + 1999
    assert len(page.fills) == 2005
    assert len({f["tid"] for f in page.fills}) == 2005
    assert page.truncated is False


@respx.mock
async def test_fills_same_ms_across_page_boundary_not_lost():
    t0 = 1_700_000_000_000
    page1 = [fill(i, t0 + i) for i in range(1998)] + [fill(5000, t0 + 1998), fill(5001, t0 + 1998)]
    page2 = [fill(5000, t0 + 1998), fill(5001, t0 + 1998), fill(5002, t0 + 1998), fill(5003, t0 + 2001)]
    respx.post(INFO).mock(side_effect=[httpx.Response(200, json=page1),
                                       httpx.Response(200, json=page2)])
    async with make_client() as c:
        page = await c.user_fills_by_time(ADDR, t0)
    tids = [f["tid"] for f in page.fills]
    assert 5002 in tids and 5003 in tids
    assert len(tids) == len(set(tids)) == 2002


@respx.mock
async def test_fills_truncated_at_10k():
    t0 = 1_700_000_000_000
    pages = [[fill(p * 2000 + i, t0 + p * 2000 + i) for i in range(2000)] for p in range(8)]
    route = respx.post(INFO).mock(side_effect=[httpx.Response(200, json=pg) for pg in pages])
    async with make_client() as c:
        page = await c.user_fills_by_time(ADDR, t0, t0 + 10**9)
    assert len(page.fills) == FILLS_HISTORY_CAP == 10_000
    assert page.truncated is True
    assert route.call_count == 5
    assert body(route.calls[4])["endTime"] == t0 + 10**9


@respx.mock
async def test_fills_page_of_identical_ms_still_advances():
    t0 = 1_700_000_000_000
    page1 = [fill(i, t0) for i in range(2000)]  # all in one ms: inclusive restart would loop
    page2 = [fill(9999, t0 + 5)]
    route = respx.post(INFO).mock(side_effect=[httpx.Response(200, json=page1),
                                              httpx.Response(200, json=page2)])
    async with make_client() as c:
        page = await c.user_fills_by_time(ADDR, t0)
    assert route.call_count == 2
    assert body(route.calls[1])["startTime"] == t0 + 1
    assert len(page.fills) == 2001


@respx.mock
async def test_user_funding_pages_on_500():
    t0 = 1_700_000_000_000
    def fr(t, coin):
        return {"time": t, "hash": "0x0", "delta": {"type": "funding", "coin": coin, "usdc": "1.0",
                                                    "szi": "1.0", "fundingRate": "0.0001"}}
    page1 = [fr(t0 + i, "BTC") for i in range(500)]
    page2 = [fr(t0 + 499, "BTC"), fr(t0 + 499, "ETH"), fr(t0 + 600, "BTC")]
    route = respx.post(INFO).mock(side_effect=[httpx.Response(200, json=page1),
                                              httpx.Response(200, json=page2)])
    async with make_client() as c:
        out = await c.user_funding(ADDR, t0)
    assert route.call_count == 2
    assert body(route.calls[1])["startTime"] == t0 + 499
    assert len(out) == 502


# -- 429 / retries ------------------------------------------------------------

@respx.mock
async def test_429_retry_after_is_honoured_then_succeeds():
    clock = FakeClock()
    route = respx.post(INFO).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"BTC": "1.5"}),
    ])
    async with make_client(clock) as c:
        mids = await c.all_mids()
    assert mids == {"BTC": Decimal("1.5")}
    assert route.call_count == 2
    assert 7.0 in clock.sleeps


@respx.mock
async def test_429_without_retry_after_backs_off_exponentially():
    clock = FakeClock()
    respx.post(INFO).mock(side_effect=[httpx.Response(429)] * 3 + [httpx.Response(200, json={})])
    async with make_client(clock, base_delay=1.0) as c:
        await c.all_mids()
    backoffs = [s for s in clock.sleeps if s in (1.0, 2.0, 4.0)]
    assert backoffs == [1.0, 2.0, 4.0]


@respx.mock
async def test_429_five_times_raises_rate_limited():
    clock = FakeClock()
    route = respx.post(INFO).mock(return_value=httpx.Response(429, headers={"Retry-After": "2"}))
    async with make_client(clock) as c:
        with pytest.raises(HLRateLimited):
            await c.meta()
    assert route.call_count == 5
    assert clock.sleeps.count(2.0) == 4


@respx.mock
async def test_5xx_exhausted_raises_hlerror():
    respx.post(INFO).mock(return_value=httpx.Response(502))
    async with make_client() as c:
        with pytest.raises(HLError) as ei:
            await c.meta()
    assert not isinstance(ei.value, HLRateLimited)


@respx.mock
async def test_4xx_not_retried():
    route = respx.post(INFO).mock(return_value=httpx.Response(422, text="bad"))
    async with make_client() as c:
        with pytest.raises(HLError):
            await c.clearinghouse_state("not-an-address")
    assert route.call_count == 1


@respx.mock
async def test_requests_are_charged_to_weight_bucket():
    clock = FakeClock()
    rows = [fill(i, 1_700_000_000_000 + i) for i in range(100)]
    respx.post(INFO).mock(return_value=httpx.Response(200, json=rows))
    async with make_client(clock, weight_per_min=1200) as c:
        await c.user_fills_by_time(ADDR, 1_700_000_000_000)
        # 20 base + 100//20 = 5 per-item -> 25 spent from 1200
        assert c._bucket.tokens == pytest.approx(1200 - 25)
