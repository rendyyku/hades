"""Regression tests for the final-review findings (alert floods, CT watchlist, truncation, loops, eval)."""

import asyncio
import json
import re
from datetime import timedelta

import httpx
import pytest
import respx

from conftest import NOW, make_signal
from hades.config import HOT_LABELS, ChainCfg, RepoTarget
from hades.enrich import check
from hades.evaluate import evaluate
from hades.github import GitHub
from hades.http import Http
from hades.models import Enrichment, ListingEvent, WatchResult
from hades.notify import TG_MAX, format_digest, format_signal
from hades.runner import Runner
from hades.watchers.base import Watcher
from hades.watchers.ct import CtWatcher
from hades.watchers.repo_diff import build_repo_watchers

API = "https://api.github.com"
CHAINS = {"ethereum": ChainCfg(evm=True), "monad": ChainCfg(evm=True)}


async def _nosleep(_):
    return None


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield Http(c, rates={"api.github.com": 10_000, "crt.sh": 10_000, "api.geckoterminal.com": 10_000,
                             "api.dexscreener.com": 10_000}, sleep=_nosleep)


def _tokenlist(n, start=0):
    return json.dumps({"tokens": [{"chainId": 1, "address": f"0x{i:040x}", "symbol": f"T{i}", "name": "t"}
                                  for i in range(start, start + n)]})


def _list_watcher(http):
    t = RepoTarget(repo="u/list", path="tokens.json", mode="json_tokenlist", chain_map={"1": "ethereum"})
    [w] = build_repo_watchers(t, GitHub(http), 900, CHAINS)
    return w


def _routes(repo, shas, files: dict[str, str]):
    respx.get(f"{API}/repos/{repo}/commits").mock(return_value=httpx.Response(
        200, json=[{"sha": s, "html_url": f"https://github.com/{repo}/commit/{s}"} for s in shas]))
    respx.get(f"{API}/repos/{repo}/pulls").mock(return_value=httpx.Response(200, json=[]))
    for sha, text in files.items():
        respx.get(f"{API}/repos/{repo}/contents/tokens.json", params={"ref": sha}).mock(
            return_value=httpx.Response(200, text=text))
        respx.get(f"{API}/repos/{repo}/commits/{sha}/pulls").mock(return_value=httpx.Response(200, json=[]))


# -- Important 1: a broken/empty parse must not wipe the running keys -------------

@respx.mock
async def test_tokenlist_broken_commit_does_not_flood(http):
    w = _list_watcher(http)
    keys = sorted({f"ethereum:0x{i:040x}" for i in range(100)})
    _routes("u/list", ["s2", "s1", "s0"], {"s1": "{not json", "s2": _tokenlist(101)})
    state = {"head": "s0", "prs": {}, "keys": keys}
    res = w.diff(state, await w.fetch(None, state), NOW)
    assert [s.symbol for s in res.signals] == ["T100"]
    assert len(res.new_state["keys"]) == 101


@respx.mock
async def test_tokenlist_shrunk_commit_is_skipped(http):
    w = _list_watcher(http)
    keys = sorted({f"ethereum:0x{i:040x}" for i in range(100)})
    _routes("u/list", ["s2", "s1", "s0"], {"s1": _tokenlist(10), "s2": _tokenlist(100)})
    state = {"head": "s0", "prs": {}, "keys": keys}
    res = w.diff(state, await w.fetch(None, state), NOW)
    assert res.signals == []
    assert len(res.new_state["keys"]) == 100


@respx.mock
async def test_tokenlist_empty_baseline_raises(http):
    from hades.http import SourceError
    w = _list_watcher(http)
    _routes("u/list", ["s0"], {"s0": "{broken"})
    with pytest.raises(SourceError):
        await w.fetch(None, None)


@respx.mock
async def test_big_batch_commit_is_downgraded_to_low(http):
    w = _list_watcher(http)
    keys = sorted({f"ethereum:0x{i:040x}" for i in range(100)})
    _routes("u/list", ["s1", "s0"], {"s1": _tokenlist(160)})
    state = {"head": "s0", "prs": {}, "keys": keys}
    res = w.diff(state, await w.fetch(None, state), NOW)
    assert len(res.signals) == 60
    assert {s.severity for s in res.signals} == {"low"}


# -- Important 2: a force-push answers compare with 200 + diverged ------------------

@respx.mock
async def test_force_push_diverged_rebaselines(http):
    t = RepoTarget(repo="trustwallet/assets", path="blockchains/{chain}/assets", mode="dir_added", chains=["monad"])
    [w] = build_repo_watchers(t, GitHub(http), 900, CHAINS)
    respx.get(f"{API}/repos/trustwallet/assets/commits").mock(return_value=httpx.Response(
        200, json=[{"sha": "n2"}, {"sha": "n1"}]))
    respx.get(f"{API}/repos/trustwallet/assets/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/repos/trustwallet/assets/compare/old...n2").mock(
        return_value=httpx.Response(200, json={"status": "diverged", "ahead_by": 3, "behind_by": 2}))
    state = {"head": "old", "prs": {}, "keys": []}
    res = w.diff(state, await w.fetch(None, state), NOW)
    assert res.signals == []
    assert res.new_state["head"] == "n2"


# -- Important 3: CT watchlist poisoning and starvation ------------------------------

@respx.mock
async def test_pr_websites_and_shared_hosts_not_auto_watched(http):
    t = RepoTarget(repo="trustwallet/assets", path="blockchains/{chain}/assets", mode="dir_added", chains=["monad"])
    [w] = build_repo_watchers(t, GitHub(http), 900, CHAINS)
    infos = {"0xA": "https://linktr.ee/cat", "0xB": "https://cat.xyz", "0xC": "https://dog.xyz"}
    respx.get(f"{API}/repos/trustwallet/assets/commits").mock(return_value=httpx.Response(200, json=[{"sha": "s1"}, {"sha": "s0"}]))
    respx.get(f"{API}/repos/trustwallet/assets/commits/s1").mock(return_value=httpx.Response(200, json={"files": [
        {"filename": f"blockchains/monad/assets/{a}/info.json", "status": "added"} for a in ("0xA", "0xB")]}))
    respx.get(f"{API}/repos/trustwallet/assets/commits/s1/pulls").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/repos/trustwallet/assets/pulls").mock(return_value=httpx.Response(200, json=[
        {"number": 9, "html_url": "https://github.com/trustwallet/assets/pull/9", "head": {"sha": "p9"}}]))
    respx.get(f"{API}/repos/trustwallet/assets/pulls/9/files").mock(return_value=httpx.Response(200, json=[
        {"filename": "blockchains/monad/assets/0xC/info.json", "status": "added"}]))
    for a, site in infos.items():
        for ref in ("s1", "p9"):
            respx.get(f"{API}/repos/trustwallet/assets/contents/blockchains/monad/assets/{a}/info.json",
                      params={"ref": ref}).mock(return_value=httpx.Response(200, text=json.dumps({"symbol": a, "website": site})))
    state = {"head": "s0", "prs": {}, "keys": []}
    res = w.diff(state, await w.fetch(None, state), NOW)
    assert len(res.signals) == 3
    assert res.domains == ["cat.xyz"]  # linktr.ee denied; dog.xyz only from an unmerged PR


def _crt(domain, *certs, status=200, text=None):
    resp = httpx.Response(status, text=text) if text is not None else httpx.Response(status, json=list(certs))
    return respx.get("https://crt.sh/", params={"q": f"%.{domain}", "output": "json"}).mock(return_value=resp)


class _Sink:
    async def handle(self, sig):
        pass


@respx.mock
async def test_ct_due_domains_oldest_first_and_failing_backoff(store, http):
    for d in ("a.xyz", "b.xyz", "c.xyz"):
        store.add_domain(d, "manual")
    clock = {"now": NOW}
    w = CtWatcher(store, HOT_LABELS, per_domain_interval_s=21600, clock=lambda: clock["now"], max_per_run=2)
    r = Runner(store, http, [w], _Sink(), clock=lambda: clock["now"], sleep=_nosleep)
    a = _crt("a.xyz", text="<html>")
    _crt("b.xyz", {"id": 1, "name_value": "www.b.xyz", "not_after": "2027-01-01T00:00:00"})
    c = _crt("c.xyz", {"id": 2, "name_value": "www.c.xyz", "not_after": "2027-01-01T00:00:00"})
    await r.run_once(w)                      # a fails, b ok; c not reached (max 2)
    clock["now"] = NOW + timedelta(minutes=10)
    await r.run_once(w)                      # a in backoff -> c gets polled
    assert c.call_count == 1
    assert a.call_count == 1


# -- Important 4: truncation keeps the evidence and valid HTML ---------------------

def _row(**kw):
    row = {"id": 1, "source": "repo_diff", "kind": "pr_opened", "chain": "monad", "symbol": "CAT",
           "name": "Cash Cat", "address": "0xabc", "project_key": "monad:0xabc",
           "evidence_url": "https://github.com/trustwallet/assets/pull/1", "observed_at": NOW, "score": 72}
    row.update(kw)
    return row


def _balanced(text):
    return text.count("<a ") == text.count("</a>") and text.count("<b>") == text.count("</b>")


def test_long_name_keeps_evidence_line():
    text = format_signal(_row(name="&" * 6000, symbol="<" * 3000), None, [], now=NOW)
    assert len(text) <= TG_MAX
    assert "Bukti: <a href=" in text
    assert _balanced(text)
    assert not re.search(r"&[a-z]*$", text.split("\n")[1])  # no half entity


def test_digest_stays_under_limit_with_whole_lines():
    low = [_row(id=i, source="ct", kind="ct_low", name=f"{'x' * 150}{i}.cat.xyz",
                evidence_url=f"https://crt.sh/?id={i}") for i in range(30)]
    re_ = [(_row(id=100 + i, symbol="S" * 150), {"buyable": True, "liquidity_usd": 3000.0}) for i in range(30)]
    text = format_digest(low, re_, NOW)
    assert len(text) <= TG_MAX
    assert _balanced(text)
    assert "lainnya" in text


# -- Important 5: loops survive unexpected exceptions; stale watchers unhealthy -----

class _Boom(Watcher):
    name = "boom"
    interval_s = 1

    def __init__(self):
        self.runs = 0

    async def fetch(self, http, state):
        self.runs += 1
        return {}

    def parse(self, raw):
        return raw

    def diff(self, state, parsed, now):
        return WatchResult(new_state={"x": 1}, listings=[ListingEvent("cb", "A", "A", now, {})])


async def test_runner_loop_survives_store_error(store, monkeypatch):
    w = _Boom()
    calls = {"n": 0}
    real = store.insert_listing

    def flaky(ev):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return real(ev)

    monkeypatch.setattr(store, "insert_listing", flaky)

    async def tick(_):
        await asyncio.sleep(0)

    r = Runner(store, None, [w], _Sink(), clock=lambda: NOW, sleep=tick, stagger_s=0)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run_forever(stop))
    for _ in range(100):
        await asyncio.sleep(0)
        if w.runs >= 3:
            break
    stop.set()
    await asyncio.wait_for(task, 1)
    assert w.runs >= 3


def test_stale_watcher_is_unhealthy(store):
    store.record_ok("ct", NOW - timedelta(hours=3))
    store.record_ok("ground_truth:okx", NOW - timedelta(minutes=5))
    rows = {w["name"]: w for w in store.watchers(now=NOW, intervals={"ct": 600, "ground_truth:okx": 600})}
    assert rows["ct"]["healthy"] is False and rows["ct"]["stale"] is True
    assert rows["ground_truth:okx"]["healthy"] is True


# -- Important 6: CT signals are credited through the project alias ---------------

def test_evaluate_credits_ct_via_alias(store):
    store.insert_signal(make_signal(symbol="CAT", observed_at=NOW, website="cat.xyz"))
    store.add_alias("monad:0xabc", "dom:cat.xyz")
    store.insert_signal(make_signal(source="ct", kind="ct_high", project_key="dom:cat.xyz", dedupe_key="ct:claim.cat.xyz",
                                    chain=None, address=None, symbol=None, name="claim.cat.xyz",
                                    observed_at=NOW - timedelta(days=2)))
    store.insert_listing(ListingEvent("coinbase", "CAT-USD", "CAT", NOW + timedelta(hours=10), {}))
    rep = evaluate(store, NOW + timedelta(days=40))
    ct = next(r for r in rep["by_source"] if r["source"] == "ct")
    assert ct["hits"] == 1 and ct["false_positives"] == 0
    assert ct["median_lead_h"] == 58


# -- re-graded Minor 1: GeckoTerminal 404 falls back to DexScreener ----------------

@respx.mock
async def test_gt_404_falls_back_to_dexscreener(http):
    respx.get("https://api.geckoterminal.com/api/v2/networks/monad/tokens/0xabc/pools").mock(
        return_value=httpx.Response(404))
    respx.get("https://api.dexscreener.com/token-pairs/v1/monad/0xabc").mock(return_value=httpx.Response(
        200, json=[{"dexId": "kuru", "url": "https://dexscreener.com/monad/0xp", "liquidity": {"usd": 9000}}]))
    e = await check(http, ChainCfg(geckoterminal="monad", dexscreener="monad"), "0xabc", "CAT",
                    min_liq=1000, priority_chains={}, now=NOW)
    assert (e.provider, e.buyable, e.liquidity_usd) == ("dexscreener", True, 9000.0)
