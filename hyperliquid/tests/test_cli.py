"""Task 6: the hades-hl CLI (typer CliRunner, respx for HTTP)."""

import json

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from hades_hl import cli
from hades_hl.client import HLClient
from hades_hl.config import get_settings
from hades_hl.db import Fill, Wallet, make_engine

INFO = "https://api.hyperliquid.xyz/info"
LB = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
VAULTS = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
KEY = "sekrit-hl-key-0123456789"
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20

runner = CliRunner()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.now += s


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    monkeypatch.setenv("HL_DATABASE_URL", url)
    monkeypatch.setenv("HL_API_KEY", KEY)
    get_settings.cache_clear()

    def fake_make_client(settings):
        clock = FakeClock()
        return HLClient.from_settings(settings, sleep=clock.sleep, clock=clock, max_attempts=2)

    monkeypatch.setattr(cli, "make_client", fake_make_client)
    yield url
    get_settings.cache_clear()


def invoke(*args):
    res = runner.invoke(cli.app, list(args))
    assert KEY not in res.output
    if res.exception is not None and not isinstance(res.exception, SystemExit):
        assert KEY not in repr(res.exception)
    return res


def lb_row(addr, pnl):
    return {"ethAddress": addr, "accountValue": "100000", "displayName": None, "prize": 0,
            "windowPerformances": [["month", {"pnl": pnl, "roi": "0.1", "vlm": "1000"}]]}


def fill(tid, t):
    return {"coin": "BTC", "px": "100.0", "sz": "1.0", "side": "B" if tid % 2 == 0 else "A",
            "time": t, "startPosition": "0.0" if tid % 2 == 0 else "1.0",
            "dir": "Open Long" if tid % 2 == 0 else "Close Long", "closedPnl": "0.0",
            "hash": "0x00", "oid": tid, "crossed": True, "fee": "0.01", "tid": tid}


@respx.mock
def test_full_flow_never_prints_key_and_backfill_all_survives_429(env):
    import time
    now_ms = int(time.time() * 1000)
    respx.get(LB).mock(return_value=httpx.Response(
        200, json={"leaderboardRows": [lb_row(A, "9"), lb_row(B, "5")]}))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[]))

    a_calls = [0]

    def info(request):
        body = json.loads(request.content)
        if body["type"] == "meta":
            return httpx.Response(200, json={"universe": [{"name": "BTC", "szDecimals": 5}]})
        if body["type"] == "userFills":  # the discovery activity probe (P30): human pace
            return httpx.Response(200, json=[fill(i, now_ms - 86_400_000 + i * 3_600_000)
                                             for i in range(4)])
        if body["user"] == A:  # wallet A: one partial page, then rate limited (M12)
            a_calls[0] += 1
            if a_calls[0] == 1 and body["type"] == "userFillsByTime":
                rows = [fill(100 + i, now_ms - 600_000 + i * 1000) for i in range(3)]
                return httpx.Response(200, json=rows)
            return httpx.Response(429)
        if body["type"] == "userFillsByTime":  # honours the window, like HL
            rows = [fill(i, now_ms - 86_400_000 + i * 3_600_000) for i in range(4)]
            rows = [f for f in rows if body["startTime"] <= f["time"] <= body["endTime"]]
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json=[])

    respx.post(INFO).mock(side_effect=info)

    assert invoke("init-db").exit_code == 0
    r = invoke("discover", "--limit", "5")
    assert r.exit_code == 0, r.output
    assert "discovered 2" in r.output
    assert "prefilter" in r.output and "vaults checked: yes" in r.output
    assert "volume>30x AV" in r.output
    assert "probe: 2 probed (0 cached), 0 rejected, 2 accepted" in r.output
    r = invoke("backfill", "--all", "--days", "30")
    assert r.exit_code == 0, r.output
    assert "rate limited" in r.output.lower()
    r = invoke("rescore")
    assert r.exit_code == 0, r.output
    assert "scored" in r.output

    eng = make_engine(env)
    with Session(eng) as s:
        fills = list(s.scalars(select(Fill)))
        assert {f.address for f in fills} == {A, B}
        assert sorted(f.tid for f in fills if f.address == A) == [100, 101, 102]  # kept
        w = s.get(Wallet, B)
        assert w.stats_json["score_reason"] == "n<30"
        assert w.scored_at is not None


@respx.mock
def test_discover_prints_probe_reject_breakdown(env):
    import time
    now_ms = int(time.time() * 1000)
    respx.get(LB).mock(return_value=httpx.Response(
        200, json={"leaderboardRows": [lb_row(A, "9"), lb_row(B, "5")]}))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[]))

    def info(request):
        body = json.loads(request.content)
        if body["type"] == "meta":
            return httpx.Response(200, json={"universe": [{"name": "BTC", "szDecimals": 5}]})
        assert body["type"] == "userFills"
        if body["user"] == A:  # 2000 fills in ~33 minutes: a bot
            return httpx.Response(200, json=[fill(i, now_ms - 2_000_000 + i * 1000)
                                             for i in range(2000)])
        return httpx.Response(200, json=[])  # no fills at all: inactive

    respx.post(INFO).mock(side_effect=info)
    r = invoke("discover", "--limit", "5")
    assert r.exit_code == 0, r.output
    assert "discovered 0" in r.output
    assert "probe: 2 probed (0 cached), 2 rejected (fills_per_day 1, inactive 1), 0 accepted"         in r.output


@respx.mock
def test_discover_leaderboard_down_exit_0_with_message(env):
    respx.get(LB).mock(return_value=httpx.Response(503))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[]))
    r = invoke("discover", "--limit", "5")
    assert r.exit_code == 0
    assert "leaderboard unavailable" in r.output.lower()


@respx.mock
def test_discover_vault_list_down_says_unchecked(env):
    respx.get(LB).mock(return_value=httpx.Response(
        200, json={"leaderboardRows": [lb_row(A, "9")]}))
    respx.get(VAULTS).mock(return_value=httpx.Response(503))
    now_ms = int(__import__("time").time() * 1000)
    respx.post(INFO).mock(return_value=httpx.Response(
        200, json=[fill(i, now_ms - 86_400_000 + i * 3_600_000) for i in range(4)]))
    r = invoke("discover", "--limit", "5")
    assert r.exit_code == 0, r.output
    assert "vaults checked: no" in r.output


def test_init_db_masks_password():
    shown = cli.masked_url("postgresql://hades:s3cretpw@db.local:5432/hl")
    assert "s3cretpw" not in shown and "db.local" in shown
    win = "sqlite:///C:/Users/x/hl.db"  # no password: printed as given (no %3A escaping)
    assert cli.masked_url(win) == win


def test_backfill_requires_address_or_all(env):
    r = invoke("backfill")
    assert r.exit_code != 0


@pytest.mark.parametrize("cmd", ["serve", "follow-sync", "paper-run"])
def test_later_task_commands_are_stubs(env, cmd):
    r = invoke(cmd)
    assert r.exit_code == 2
    assert "not implemented in Task 6" in r.output


def test_serve_defaults_localhost_8010(env):
    r = invoke("serve", "--help")
    assert r.exit_code == 0
    assert "127.0.0.1" in r.output and "8010" in r.output


def test_typer_hides_locals():
    assert cli.app.pretty_exceptions_show_locals is False


# -- Task 7: tag ----------------------------------------------------------------------

def test_tag_requires_address_or_all(env):
    assert invoke("tag").exit_code != 0
    assert invoke("tag", "--address", A, "--all").exit_code != 0


def _seed_wallets(url, addrs):
    from hades_hl import pipeline
    from hades_hl.db import init_db
    eng = make_engine(url)
    init_db(eng)
    with Session(eng) as s:
        for a in addrs:
            s.add(Wallet(address=a, stats_json={}))
            pipeline.kv_set_json(s, f"hl:cov:{a}", {"start": 0, "end": 1, "capped": False})
        s.commit()


def test_tag_all_continues_after_rate_limit(env, monkeypatch):
    from hades_hl import insider
    from hades_hl.client import HLRateLimited
    from hades_hl.insider import Tag
    _seed_wallets(env, [A, B])
    seen = []

    async def fake_tag_wallet(session, client, addr, *, now=None, sz_decimals=None):
        seen.append(addr)
        if addr == A:
            raise HLRateLimited("429", status=429)
        return {"pre_move": Tag(addr, "pre_move", [
            {"tid": 1, "hash": "0xh", "coin": "BTC", "time": 1, "detail": {}}] * 3)}

    async def fake_szd(session, client, now=None):
        return {}

    monkeypatch.setattr(insider, "tag_wallet", fake_tag_wallet)
    monkeypatch.setattr(cli.pipeline, "load_sz_decimals", fake_szd)
    r = invoke("tag", "--all")
    assert r.exit_code == 0, r.output
    assert seen == sorted([A, B])
    assert "rate limited" in r.output
    assert "pre_move 1" in r.output and "1 ok, 1 failed" in r.output


def test_tag_all_continues_after_value_error(env, monkeypatch):
    from hades_hl import insider
    from hades_hl.insider import Tag
    _seed_wallets(env, [A, B])

    async def fake_tag_wallet(session, client, addr, *, now=None, sz_decimals=None):
        if addr == A:
            raise ValueError("tag pre_move: evidence row with an empty hash")
        return {"pre_move": Tag(addr, "pre_move", [
            {"tid": 1, "hash": "0xh", "coin": "BTC", "time": 1, "detail": {}}] * 3)}

    async def fake_szd(session, client, now=None):
        return {}

    monkeypatch.setattr(insider, "tag_wallet", fake_tag_wallet)
    monkeypatch.setattr(cli.pipeline, "load_sz_decimals", fake_szd)
    r = invoke("tag", "--all")
    assert r.exit_code == 0, r.output
    assert "invalid evidence" in r.output and "1 ok, 1 failed" in r.output
