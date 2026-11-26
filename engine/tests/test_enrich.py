import httpx
import pytest
import respx

from conftest import NOW
from hades.config import ChainCfg
from hades.enrich import check
from hades.http import Http

GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com"
MONAD = ChainCfg(geckoterminal="monad", dexscreener="monad", priority=True)
PRIORITY = {"monad": MONAD, "megaeth": ChainCfg(geckoterminal="megaeth", priority=True)}


async def _nosleep(_):
    return None


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield Http(c, rates={"api.geckoterminal.com": 10_000, "api.dexscreener.com": 10_000}, sleep=_nosleep)


def _pool(addr, reserve, dex="uniswap-v3", name="CAT / WMON", vol="300"):
    return {"id": f"monad_{addr}", "type": "pool",
            "attributes": {"address": addr, "name": name, "reserve_in_usd": str(reserve), "volume_usd": {"h24": vol}},
            "relationships": {"dex": {"data": {"id": dex, "type": "dex"}}}}


def _gt_token(*pools, status=200):
    return respx.get(f"{GT}/networks/monad/tokens/0xabc/pools").mock(
        return_value=httpx.Response(status, json={"data": list(pools)}))


async def _check(http, address="0xabc", symbol="CAT", chain=MONAD):
    return await check(http, chain, address, symbol, min_liq=1000, priority_chains=PRIORITY, now=NOW)


@respx.mock
async def test_gt_buyable_top_pool(http):
    _gt_token(_pool("0xp1", 800), _pool("0xp2", 12500, dex="pancake"))
    e = await _check(http)
    assert (e.provider, e.buyable, e.liquidity_usd, e.pool_count, e.dex) == ("geckoterminal", True, 12500.0, 2, "pancake")
    assert e.pool_url == "https://www.geckoterminal.com/monad/pools/0xp2"
    assert e.volume_24h == 300.0
    assert e.verified is True


@respx.mock
async def test_gt_below_threshold_not_buyable(http):
    _gt_token(_pool("0xp1", 800))
    e = await _check(http)
    assert e.buyable is False and e.liquidity_usd == 800.0


@respx.mock
async def test_gt_404_not_buyable(http):
    respx.get(f"{GT}/networks/monad/tokens/0xabc/pools").mock(return_value=httpx.Response(404))
    e = await _check(http)
    assert (e.buyable, e.pool_count, e.error) == (False, 0, None)


@respx.mock
async def test_fallback_to_dexscreener_on_gt_500(http):
    _gt_token(status=500)
    respx.get(f"{DS}/token-pairs/v1/monad/0xabc").mock(return_value=httpx.Response(200, json=[
        {"dexId": "kuru", "url": "https://dexscreener.com/monad/0xp", "liquidity": {"usd": 5000}, "volume": {"h24": 70}},
        {"dexId": "x", "url": "u2", "liquidity": {"usd": None}},
    ]))
    e = await _check(http)
    assert (e.provider, e.buyable, e.liquidity_usd, e.dex, e.pool_url, e.pool_count) == (
        "dexscreener", True, 5000.0, "kuru", "https://dexscreener.com/monad/0xp", 2)


@respx.mock
async def test_symbol_only_candidates_unverified(http):
    respx.get(f"{GT}/search/pools", params={"query": "CAT", "network": "monad"}).mock(return_value=httpx.Response(
        200, json={"data": [_pool("0xp1", 20000), _pool("0xp9", 99999, name="CATS / WMON")]}))
    respx.get(f"{GT}/search/pools", params={"query": "CAT", "network": "megaeth"}).mock(
        return_value=httpx.Response(200, json={"data": []}))
    e = await _check(http, address=None, chain=None)
    assert e.verified is False
    assert e.buyable is True
    assert [c["pool_url"] for c in e.candidates] == ["https://www.geckoterminal.com/monad/pools/0xp1"]
    assert e.candidates[0]["chain"] == "monad"


@respx.mock
async def test_both_fail_returns_error_not_raise(http):
    _gt_token(status=500)
    respx.get(f"{DS}/token-pairs/v1/monad/0xabc").mock(return_value=httpx.Response(500))
    e = await _check(http)
    assert e.buyable is None and e.error


async def test_nothing_to_check(http):
    e = await _check(http, address=None, symbol=None, chain=None)
    assert e.buyable is None and e.error == "no address or symbol"
