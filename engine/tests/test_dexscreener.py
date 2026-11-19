import json
from pathlib import Path

import httpx
import pytest
import respx

from hades.sources.dexscreener import DexScreener, DexScreenerError, best_pair, is_runner

BASE = "https://api.dexscreener.com"
DAY_MS = 86_400_000
NOW_MS = 1_763_596_800_000  # 2025-11-20T00:00Z
MEME = "MemeMint1111111111111111111111111111111111"
FIXTURE = Path(__file__).parent / "fixtures" / "dex_pairs.json"


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(sleep: FakeSleep | None = None, **kw) -> DexScreener:
    return DexScreener(rps=1000, sleep=sleep or FakeSleep(), **kw)


def pairs_fixture() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def pair(liq=None, h24=None, fdv=None, age_days=None) -> dict:
    p: dict = {"chainId": "solana", "baseToken": {"address": MEME}}
    if liq is not None:
        p["liquidity"] = {"usd": liq}
    if h24 is not None:
        p["priceChange"] = {"h24": h24}
    if fdv is not None:
        p["fdv"] = fdv
    if age_days is not None:
        p["pairCreatedAt"] = NOW_MS - int(age_days * DAY_MS)
    return p


# -- runner filter -----------------------------------------------------------


def test_runner_by_change():
    assert is_runner(pair(liq=40_000, h24=350), NOW_MS) is True


def test_runner_by_fdv_young():
    assert is_runner(pair(liq=50_000, fdv=2_000_000, age_days=3), NOW_MS) is True


def test_not_runner_low_liq():
    assert is_runner(pair(liq=10_000, h24=900), NOW_MS) is False


def test_not_runner_old_fdv():
    assert is_runner(pair(liq=50_000, fdv=5_000_000, age_days=30, h24=20), NOW_MS) is False


def test_runner_boundaries_inclusive():
    assert is_runner(pair(liq=30_000, h24=300), NOW_MS) is True
    assert is_runner(pair(liq=30_000, fdv=1_000_000, age_days=7), NOW_MS) is True
    assert is_runner(pair(liq=29_999.99, h24=300), NOW_MS) is False
    assert is_runner(pair(liq=30_000, h24=299.9), NOW_MS) is False
    assert is_runner(pair(liq=30_000, fdv=999_999, age_days=1), NOW_MS) is False
    assert is_runner(pair(liq=30_000, fdv=1_000_000, age_days=7.01), NOW_MS) is False


def test_runner_missing_created_at_disables_fdv_branch():
    assert is_runner(pair(liq=50_000, fdv=10_000_000), NOW_MS) is False


def test_runner_missing_fields_never_crash():
    assert is_runner({}, NOW_MS) is False
    assert is_runner({"liquidity": None, "priceChange": None, "fdv": None}, NOW_MS) is False
    assert is_runner({"liquidity": {"usd": "50000"}, "priceChange": {"h24": "400"}}, NOW_MS) is True
    assert is_runner({"liquidity": {"usd": "bad"}, "priceChange": {"h24": 400}}, NOW_MS) is False


def test_runner_fixture_pairs():
    ps = pairs_fixture()
    assert is_runner(ps[0], NOW_MS) is True  # h24 350, liq 85k
    assert is_runner(ps[1], NOW_MS) is False  # liq 12k
    assert is_runner(ps[3], NOW_MS) is False  # old, h24 20
    assert is_runner(ps[4], NOW_MS) is False  # no fields


# -- best_pair ---------------------------------------------------------------


def test_best_pair():
    ps = pairs_fixture()
    assert best_pair(ps)["pairAddress"] == "PairOldSol1111111111111111111111111111111"
    assert best_pair(ps[:2])["pairAddress"] == "PairMemeSolRaydium111111111111111111111111"
    assert best_pair([ps[4], ps[1]])["pairAddress"] == ps[1]["pairAddress"]
    assert best_pair([ps[4]]) is ps[4]
    assert best_pair([]) is None


# -- client ------------------------------------------------------------------


@respx.mock
async def test_token_pairs_chunks_by_30():
    mints = [f"M{i}" for i in range(65)]
    route = respx.get(url__startswith=f"{BASE}/tokens/v1/solana/").mock(
        side_effect=lambda req: httpx.Response(
            200, json=[{"pairAddress": req.url.path.rsplit("/", 1)[1].split(",")[0]}]
        )
    )
    async with make_client() as dex:
        out = await dex.token_pairs(mints)
    assert route.call_count == 3
    paths = [c.request.url.path.rsplit("/", 1)[1].split(",") for c in route.calls]
    assert [len(p) for p in paths] == [30, 30, 5]
    assert paths[0][0] == "M0" and paths[2][-1] == "M64"
    assert [p["pairAddress"] for p in out] == ["M0", "M30", "M60"]


@respx.mock
async def test_token_pairs_empty_makes_no_request():
    route = respx.get(url__startswith=BASE).mock(return_value=httpx.Response(200, json=[]))
    async with make_client() as dex:
        assert await dex.token_pairs([]) == []
    assert route.call_count == 0


@respx.mock
async def test_token_pairs_non_list_response_is_empty():
    respx.get(f"{BASE}/tokens/v1/solana/A").mock(return_value=httpx.Response(200, json={"pairs": None}))
    async with make_client() as dex:
        assert await dex.token_pairs(["A"]) == []


@respx.mock
async def test_boosted_tokens_dedup_solana_only():
    respx.get(f"{BASE}/token-boosts/top/v1").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"chainId": "solana", "tokenAddress": "A"},
                {"chainId": "ethereum", "tokenAddress": "E"},
                {"chainId": "solana", "tokenAddress": "B"},
                {"chainId": "solana", "tokenAddress": "A"},
                {"chainId": "solana"},
            ],
        )
    )
    respx.get(f"{BASE}/token-profiles/latest/v1").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"chainId": "solana", "tokenAddress": "C"},
                {"chainId": "solana", "tokenAddress": "B"},
                {"chainId": "bsc", "tokenAddress": "D"},
            ],
        )
    )
    async with make_client() as dex:
        assert await dex.boosted_tokens() == ["A", "B", "C"]


@respx.mock
async def test_price_native_uses_best_base_pair():
    respx.get(f"{BASE}/tokens/v1/solana/{MEME}").mock(return_value=httpx.Response(200, json=pairs_fixture()))
    async with make_client() as dex:
        # inverted SOL/MEME pair has the most liquidity but MEME is not its base
        assert await dex.price_native(MEME) == pytest.approx(0.000012)


@respx.mock
async def test_price_native_none_when_missing():
    respx.get(f"{BASE}/tokens/v1/solana/X").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/tokens/v1/solana/Y").mock(
        return_value=httpx.Response(200, json=[{"baseToken": {"address": "Y"}, "liquidity": {"usd": 5}}])
    )
    async with make_client() as dex:
        assert await dex.price_native("X") is None
        assert await dex.price_native("Y") is None


@respx.mock
async def test_retry_429_then_success():
    sleep = FakeSleep()
    route = respx.get(f"{BASE}/token-boosts/top/v1").mock(
        side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json=[])]
    )
    respx.get(f"{BASE}/token-profiles/latest/v1").mock(return_value=httpx.Response(200, json=[]))
    async with make_client(sleep=sleep, base_delay=0.5) as dex:
        assert await dex.boosted_tokens() == []
    assert route.call_count == 3
    assert sleep.calls == [0.5, 1.0]


@respx.mock
async def test_retry_exhausted_raises():
    sleep = FakeSleep()
    route = respx.get(f"{BASE}/tokens/v1/solana/A").mock(return_value=httpx.Response(429))
    async with make_client(sleep=sleep) as dex:
        with pytest.raises(DexScreenerError):
            await dex.token_pairs(["A"])
    assert route.call_count == 4  # 1 + 3 retries
    assert len(sleep.calls) == 3


@respx.mock
async def test_4xx_raises_without_retry():
    route = respx.get(f"{BASE}/tokens/v1/solana/A").mock(return_value=httpx.Response(404))
    async with make_client() as dex:
        with pytest.raises(DexScreenerError):
            await dex.token_pairs(["A"])
    assert route.call_count == 1


@respx.mock
async def test_network_error_retried_then_raises():
    route = respx.get(f"{BASE}/tokens/v1/solana/A").mock(side_effect=httpx.ConnectError("boom"))
    async with make_client() as dex:
        with pytest.raises(DexScreenerError):
            await dex.token_pairs(["A"])
    assert route.call_count == 4


@respx.mock
async def test_custom_base_url():
    respx.get("https://dex.test/tokens/v1/solana/A").mock(return_value=httpx.Response(200, json=[]))
    async with make_client(base_url="https://dex.test/") as dex:
        assert await dex.token_pairs(["A"]) == []


def test_default_rate_is_4_rps():
    dex = DexScreener(sleep=FakeSleep())
    assert dex.rps == 4
