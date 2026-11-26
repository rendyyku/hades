import json

import httpx
import pytest
import respx

from conftest import NOW
from hades.config import ChainCfg, RepoTarget
from hades.github import GitHub
from hades.http import Http
from hades.watchers.repo_diff import build_repo_watchers

API = "https://api.github.com"
TW = "trustwallet/assets"
CHAINS = {"monad": ChainCfg(evm=True, priority=True), "ethereum": ChainCfg(evm=True)}
INFO = {"0xAbC": {"symbol": "CAT", "name": "Cash Cat", "website": "https://www.cat.xyz/"},
        "0xDeF": {"symbol": "DOG", "name": "Dog", "website": ""}}


async def _nosleep(_):
    return None


@pytest.fixture
async def gh():
    async with httpx.AsyncClient() as c:
        yield GitHub(Http(c, rates={"api.github.com": 10_000}, sleep=_nosleep), token="t")


def _tw_watcher(gh):
    t = RepoTarget(repo=TW, path="blockchains/{chain}/assets", mode="dir_added", chains=["monad"])
    [w] = build_repo_watchers(t, gh, 900, CHAINS)
    return w


def _commits(repo, *shas):
    respx.get(f"{API}/repos/{repo}/commits").mock(return_value=httpx.Response(
        200, json=[{"sha": s, "html_url": f"https://github.com/{repo}/commit/{s}"} for s in shas]))


def _no_prs(repo):
    respx.get(f"{API}/repos/{repo}/pulls").mock(return_value=httpx.Response(200, json=[]))


def _commit_pulls(repo, sha, pulls=()):
    respx.get(f"{API}/repos/{repo}/commits/{sha}/pulls").mock(return_value=httpx.Response(200, json=list(pulls)))


def _info_routes(ref):
    for addr, info in INFO.items():
        respx.get(f"{API}/repos/{TW}/contents/blockchains/monad/assets/{addr}/info.json",
                  params={"ref": ref}).mock(return_value=httpx.Response(200, text=json.dumps(info)))


async def _run(w, state):
    raw = await w.fetch(None, state)
    return w.diff(state, w.parse(raw), NOW)


@respx.mock
async def test_dir_added_emits_one_signal_per_new_dir(gh):
    w = _tw_watcher(gh)
    assert w.name == "repo_diff:trustwallet/assets:blockchains/monad/assets"
    _commits(TW, "s1", "s0")
    respx.get(f"{API}/repos/{TW}/commits/s1").mock(return_value=httpx.Response(200, json={"files": [
        {"filename": "blockchains/monad/assets/0xAbC/info.json", "status": "added"},
        {"filename": "blockchains/monad/assets/0xAbC/logo.png", "status": "added"},
        {"filename": "blockchains/monad/assets/0xDeF/info.json", "status": "added"},
        {"filename": "blockchains/monad/assets/0x111/info.json", "status": "modified"},
    ]}))
    _info_routes("s1")
    _commit_pulls(TW, "s1")
    _no_prs(TW)
    res = await _run(w, {"head": "s0", "prs": {}})
    assert [(s.kind, s.chain, s.symbol) for s in res.signals] == [("merged", "monad", "CAT"), ("merged", "monad", "DOG")]
    cat = res.signals[0]
    assert cat.evidence_url == "https://github.com/trustwallet/assets/commit/s1"
    assert cat.project_key == "monad:0xabc"
    assert cat.dedupe_key == "repo_diff:trustwallet/assets:monad:0xabc"
    assert cat.website == "cat.xyz"
    assert res.domains == ["cat.xyz"]
    assert res.new_state["head"] == "s1"


@respx.mock
async def test_commit_in_pr_uses_pr_url(gh):
    w = _tw_watcher(gh)
    _commits(TW, "s1", "s0")
    respx.get(f"{API}/repos/{TW}/commits/s1").mock(return_value=httpx.Response(200, json={"files": [
        {"filename": "blockchains/monad/assets/0xAbC/info.json", "status": "added"}]}))
    _info_routes("s1")
    _commit_pulls(TW, "s1", [{"number": 9, "html_url": "https://github.com/trustwallet/assets/pull/9"}])
    _no_prs(TW)
    res = await _run(w, {"head": "s0", "prs": {}})
    assert res.signals[0].evidence_url == "https://github.com/trustwallet/assets/pull/9"


@respx.mock
async def test_baseline_records_head_and_open_prs(gh):
    w = _tw_watcher(gh)
    _commits(TW, "s1", "s0")
    respx.get(f"{API}/repos/{TW}/pulls").mock(return_value=httpx.Response(200, json=[
        {"number": 7, "html_url": "u", "head": {"sha": "p1"}}]))
    res = await _run(w, None)
    assert res.signals == []
    assert res.new_state == {"head": "s1", "prs": {"7": "p1"}, "keys": []}


def _tokenlist(*tokens):
    return json.dumps({"tokens": [dict(zip(("chainId", "address", "symbol", "name"), t)) for t in tokens]})


@respx.mock
async def test_tokenlist_diff_only_added(gh):
    t = RepoTarget(repo="u/list", path="tokens.json", mode="json_tokenlist", chain_map={"1": "ethereum"})
    [w] = build_repo_watchers(t, gh, 900, CHAINS)
    _commits("u/list", "s1", "s0")
    respx.get(f"{API}/repos/u/list/contents/tokens.json", params={"ref": "s1"}).mock(return_value=httpx.Response(
        200, text=_tokenlist((1, "0xB", "B", "b"), (1, "0xC", "C", "c"), (999, "0xD", "D", "d"))))
    _commit_pulls("u/list", "s1")
    _no_prs("u/list")
    res = await _run(w, {"head": "s0", "prs": {}, "keys": ["ethereum:0xa", "ethereum:0xb"]})
    assert [s.symbol for s in res.signals] == ["C"]
    assert res.signals[0].chain == "ethereum"
    assert sorted(res.new_state["keys"]) == ["ethereum:0xb", "ethereum:0xc"]


@respx.mock
async def test_unmapped_chainid_skipped(gh):
    t = RepoTarget(repo="u/list", path="tokens.json", mode="json_tokenlist", chain_map={"1": "ethereum"})
    [w] = build_repo_watchers(t, gh, 900, CHAINS)
    _commits("u/list", "s1", "s0")
    respx.get(f"{API}/repos/u/list/contents/tokens.json", params={"ref": "s1"}).mock(
        return_value=httpx.Response(200, text=_tokenlist((999, "0xD", "D", "d"))))
    _commit_pulls("u/list", "s1")
    _no_prs("u/list")
    res = await _run(w, {"head": "s0", "prs": {}, "keys": []})
    assert res.signals == []


@respx.mock
async def test_open_pr_emits_pr_opened_once(gh):
    w = _tw_watcher(gh)
    _commits(TW, "s0")
    respx.get(f"{API}/repos/{TW}/pulls").mock(return_value=httpx.Response(200, json=[
        {"number": 7, "html_url": "https://github.com/trustwallet/assets/pull/7", "head": {"sha": "p1"}}]))
    files = respx.get(f"{API}/repos/{TW}/pulls/7/files").mock(return_value=httpx.Response(200, json=[
        {"filename": "blockchains/monad/assets/0xAbC/info.json", "status": "added"}]))
    _info_routes("p1")
    res = await _run(w, {"head": "s0", "prs": {}})
    assert [(s.kind, s.evidence_url) for s in res.signals] == [
        ("pr_opened", "https://github.com/trustwallet/assets/pull/7")]
    assert res.signals[0].dedupe_key == "repo_diff:trustwallet/assets:monad:0xabc"
    again = await _run(w, res.new_state)
    assert again.signals == []
    assert files.call_count == 1


@respx.mock
async def test_force_push_rebaselines(gh):
    w = _tw_watcher(gh)
    _commits(TW, "s1")
    respx.get(f"{API}/repos/{TW}/compare/deadbeef...s1").mock(return_value=httpx.Response(404))
    _no_prs(TW)
    res = await _run(w, {"head": "deadbeef", "prs": {}})
    assert res.signals == []
    assert res.new_state["head"] == "s1"


@respx.mock
async def test_at_most_ten_commits_per_run(gh):
    w = _tw_watcher(gh)
    shas = [f"s{i}" for i in range(12, -1, -1)]  # s12 newest ... s0 = head
    _commits(TW, *shas)
    for i in range(1, 13):
        respx.get(f"{API}/repos/{TW}/commits/s{i}").mock(return_value=httpx.Response(200, json={"files": []}))
    _no_prs(TW)
    res = await _run(w, {"head": "s0", "prs": {}})
    assert res.new_state["head"] == "s10"


@respx.mock
async def test_regex_entries(gh):
    t = RepoTarget(repo="BitGo/BitGoJS", path="coins.ts", mode="regex_entries",
                   regex=r"symbol: '(?P<symbol>\w+)', contractAddress: '(?P<address>0x\w+)', network: '(?P<chain>\w+)'",
                   chain_map={"monad": "monad"})
    [w] = build_repo_watchers(t, gh, 900, CHAINS)
    _commits("BitGo/BitGoJS", "s1", "s0")
    respx.get(f"{API}/repos/BitGo/BitGoJS/contents/coins.ts", params={"ref": "s1"}).mock(return_value=httpx.Response(
        200, text="{ symbol: 'CAT', contractAddress: '0xabc', network: 'monad' }\n"))
    _commit_pulls("BitGo/BitGoJS", "s1")
    _no_prs("BitGo/BitGoJS")
    res = await _run(w, {"head": "s0", "prs": {}, "keys": []})
    assert [(s.symbol, s.chain, s.address) for s in res.signals] == [("CAT", "monad", "0xabc")]


@respx.mock
async def test_regex_bitgo_hoodeth_format(gh):
    # real BitGo statics shape (hoodethTokens.ts), regex from config.example.yaml
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "config.example.yaml").read_text(encoding="utf-8"))
    tgt = next(t for t in cfg["repo_diff"]["targets"] if t["repo"] == "BitGo/BitGoJS")
    t = RepoTarget(**tgt)
    [w] = build_repo_watchers(t, gh, 900, {"robinhood": ChainCfg(evm=True)})
    text = """
  erc20Token(
    'uuid-1',
    'hoodeth:cat',
    "Cash Cat's Coin",
    18,
    '0x86923f96303d656e4aa86d9d42d1e57ad2023fdc',
    UnderlyingAsset['hoodeth:cat'],
  ),
  erc20Token(
    'uuid-2',
    'thoodeth:tsla',
    'Tesla',
    18,
    '0xc9f9c86933092bbbfff3ccb4b105a4a94bf3bd4e',
  ),
"""
    path = tgt["path"]
    _commits("BitGo/BitGoJS", "s1", "s0")
    respx.get(f"{API}/repos/BitGo/BitGoJS/contents/{path}", params={"ref": "s1"}).mock(
        return_value=httpx.Response(200, text=text))
    _commit_pulls("BitGo/BitGoJS", "s1")
    _no_prs("BitGo/BitGoJS")
    res = await _run(w, {"head": "s0", "prs": {}, "keys": []})
    assert [(s.symbol, s.name, s.chain) for s in res.signals] == [("cat", "Cash Cat's Coin", "robinhood")]
