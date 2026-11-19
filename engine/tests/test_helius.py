import json

import httpx
import pytest
import respx

from hades.sources.helius import HeliusClient, HeliusError, HeliusRateLimited

API = "https://api.helius.xyz"
RPC = "https://mainnet.helius-rpc.com/"
ADDR = "AddrXYZ"
KEY = "secret-test-key"


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(sleep: FakeSleep | None = None, **kw) -> HeliusClient:
    return HeliusClient(KEY, rps=1000, sleep=sleep or FakeSleep(), **kw)


def page(prefix: str, n: int = 100) -> list[dict]:
    return [{"signature": f"{prefix}-{i}"} for i in range(n)]


def test_rate_limited_is_helius_error():
    assert issubclass(HeliusRateLimited, HeliusError)


@respx.mock
async def test_pagination_stops_at_max_pages():
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json=page(f"p{counter['n']}"))

    route = respx.get(f"{API}/v0/addresses/{ADDR}/transactions").mock(side_effect=handler)
    async with make_client() as client:
        txs = await client.address_transactions(ADDR, max_pages=3)
    assert route.call_count == 3
    assert len(txs) == 300
    first, second = route.calls[0].request.url, route.calls[1].request.url
    assert first.params["type"] == "SWAP"
    assert first.params["limit"] == "100"
    assert first.params["api-key"] == KEY
    assert "before" not in first.params
    # next page passes before = last signature of the previous page
    assert second.params["before"] == "p1-99"


@respx.mock
async def test_pagination_stops_on_empty_page():
    route = respx.get(f"{API}/v0/addresses/{ADDR}/transactions").mock(
        side_effect=[httpx.Response(200, json=page("a", 100)), httpx.Response(200, json=[])]
    )
    async with make_client() as client:
        txs = await client.address_transactions(ADDR, max_pages=10)
    assert route.call_count == 2
    assert len(txs) == 100


@respx.mock
async def test_retry_then_raise(caplog):
    caplog.set_level("DEBUG")
    sleep = FakeSleep()
    route = respx.get(f"{API}/v0/addresses/{ADDR}/transactions").mock(
        return_value=httpx.Response(429, json={"error": "rate limited"})
    )
    async with make_client(sleep) as client:
        with pytest.raises(HeliusRateLimited) as exc:
            await client.address_transactions(ADDR, max_pages=1)
    assert route.call_count == 6
    assert sleep.calls == [0.5, 1.0, 2.0, 4.0, 8.0]
    assert KEY not in str(exc.value)
    assert caplog.records, "retries should be logged"
    assert KEY not in caplog.text


@respx.mock
async def test_retry_recovers():
    data = page("ok", 5)
    route = respx.get(f"{API}/v0/addresses/{ADDR}/transactions").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json=data),
        ]
    )
    async with make_client() as client:
        txs = await client.address_transactions(ADDR, max_pages=1)
    assert route.call_count == 3
    assert txs == data


@respx.mock
async def test_persistent_5xx_raises_helius_error():
    route = respx.post(f"{API}/v0/transactions").mock(return_value=httpx.Response(503))
    async with make_client() as client:
        with pytest.raises(HeliusError) as exc:
            await client.parse_transactions(["s1"])
    assert not isinstance(exc.value, HeliusRateLimited)
    assert route.call_count == 6


@respx.mock
async def test_parse_transactions_chunks_by_100():
    def handler(request: httpx.Request) -> httpx.Response:
        sigs = json.loads(request.content)["transactions"]
        return httpx.Response(200, json=[{"signature": s} for s in sigs])

    route = respx.post(f"{API}/v0/transactions").mock(side_effect=handler)
    sigs = [f"s{i}" for i in range(250)]
    async with make_client() as client:
        txs = await client.parse_transactions(sigs)
    assert route.call_count == 3
    sizes = [len(json.loads(c.request.content)["transactions"]) for c in route.calls]
    assert sizes == [100, 100, 50]
    assert [t["signature"] for t in txs] == sigs


def rpc_handler(pages: list[list[str]], seen: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        sigs = pages[len(seen) - 1]
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": [{"signature": s, "slot": 1} for s in sigs]},
        )

    return handler


@respx.mock
async def test_signatures_oldest_pages_backward_and_reverses():
    # newest-first pages: page1 = n0..n999, page2 = n1000..n1499 (short -> stop)
    newest_first = [f"n{i}" for i in range(1500)]
    pages = [newest_first[:1000], newest_first[1000:]]
    seen: list[dict] = []
    route = respx.post(RPC).mock(side_effect=rpc_handler(pages, seen))
    async with make_client() as client:
        sigs = await client.signatures_oldest(ADDR, max_pages=5)
    assert route.call_count == 2
    assert seen[0]["method"] == "getSignaturesForAddress"
    assert seen[0]["params"][0] == ADDR
    assert seen[0]["params"][1]["limit"] == 1000
    assert "before" not in seen[0]["params"][1]
    assert seen[1]["params"][1]["before"] == "n999"
    assert route.calls[0].request.url.params["api-key"] == KEY
    assert sigs == list(reversed(newest_first))


@respx.mock
async def test_signatures_oldest_respects_max_pages():
    pages = [[f"p{j}-{i}" for i in range(1000)] for j in range(5)]
    seen: list[dict] = []
    route = respx.post(RPC).mock(side_effect=rpc_handler(pages, seen))
    async with make_client() as client:
        sigs = await client.signatures_oldest(ADDR, max_pages=2)
    assert route.call_count == 2
    assert len(sigs) == 2000
    assert sigs[0] == "p1-999"


@respx.mock
async def test_first_incoming_sol_returns_oldest_funder():
    # oldest -> newest: o0 (outgoing), o1 (incoming from FUNDER), o2 (incoming from OTHER)
    seen: list[dict] = []
    respx.post(RPC).mock(side_effect=rpc_handler([["o2", "o1", "o0"]], seen))
    txs = {
        "o0": {"signature": "o0", "slot": 10, "timestamp": 1, "transactionError": None,
               "nativeTransfers": [{"fromUserAccount": ADDR, "toUserAccount": "X", "amount": 5}]},
        "o1": {"signature": "o1", "slot": 11, "timestamp": 2, "transactionError": None,
               "nativeTransfers": [{"fromUserAccount": "FUNDER", "toUserAccount": ADDR, "amount": 10**9}]},
        "o2": {"signature": "o2", "slot": 12, "timestamp": 3, "transactionError": None,
               "nativeTransfers": [{"fromUserAccount": "OTHER", "toUserAccount": ADDR, "amount": 10**9}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        sigs = json.loads(request.content)["transactions"]
        assert sigs == ["o0", "o1", "o2"]
        # API may return in any order; client must order chronologically
        return httpx.Response(200, json=[txs["o2"], txs["o1"], txs["o0"]])

    respx.post(f"{API}/v0/transactions").mock(side_effect=handler)
    async with make_client() as client:
        assert await client.first_incoming_sol(ADDR) == ("FUNDER", "o1", 11)


@respx.mock
async def test_first_incoming_sol_none():
    seen: list[dict] = []
    respx.post(RPC).mock(side_effect=rpc_handler([["z0"]], seen))
    respx.post(f"{API}/v0/transactions").mock(
        return_value=httpx.Response(
            200, json=[{"signature": "z0", "slot": 1, "timestamp": 1, "nativeTransfers": []}]
        )
    )
    async with make_client() as client:
        assert await client.first_incoming_sol(ADDR) is None


async def test_api_key_not_in_repr():
    client = make_client()
    try:
        assert KEY not in repr(client)
    finally:
        await client.aclose()
