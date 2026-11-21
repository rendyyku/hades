"""Task 6: discovery, backfill, rescore (respx for HTTP)."""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hades_hl import pipeline
from hades_hl.client import HLClient, HLRateLimited
from hades_hl.db import Fill, Funding, Kv, Wallet, init_db, insert_fills, make_engine
from hlfills import DAY_MS, FakeFillsAPI, mk, round_trip

INFO = "https://api.hyperliquid.xyz/info"
LB = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
VAULTS = "https://stats-data.hyperliquid.xyz/Mainnet/vaults"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20
C = "0x" + "c3" * 20
V = "0x" + "dd" * 20  # a vault


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, s: float) -> None:
        self.now += s


def make_client(**kw) -> HLClient:
    clock = FakeClock()
    return HLClient(sleep=clock.sleep, clock=clock, **kw)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'hl.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def lb_row(addr: str, month_pnl: str, account_value: str = "20000", name=None,
           vlm: str = "100000") -> dict:
    return {"ethAddress": addr, "accountValue": account_value, "displayName": name, "prize": 0,
            "windowPerformances": [
                ["day", {"pnl": "1", "roi": "0.1", "vlm": "10"}],
                ["month", {"pnl": month_pnl, "roi": "0.2", "vlm": vlm}],
                ["allTime", {"pnl": "5", "roi": "0.3", "vlm": "1000"}]]}


def vault_row(addr: str) -> dict:
    return {"apr": 0.1, "pnls": [], "summary": {"name": "v", "vaultAddress": addr,
                                                "leader": A, "tvl": "1", "isClosed": False}}


def fill(tid: int, t: int, coin: str = "BTC") -> dict:
    # independent open/close pairs: even tid opens, odd closes
    return {"coin": coin, "px": "100.0", "sz": "1.0", "side": "B" if tid % 2 == 0 else "A",
            "time": t, "startPosition": "0.0" if tid % 2 == 0 else "1.0",
            "dir": "Open Long" if tid % 2 == 0 else "Close Long", "closedPnl": "0.0",
            "hash": "0x00", "oid": tid, "crossed": True, "fee": "0.01", "tid": tid,
            "feeToken": "USDC"}


def funding_row(t: int, coin: str = "BTC", usdc: str = "-0.5") -> dict:
    return {"time": t, "hash": "0x0", "delta": {"type": "funding", "coin": coin, "usdc": usdc,
                                                "szi": "1", "fundingRate": "0.0001"}}


def human_sample(now_ms: int = NOW_MS, days: int = 20, per_day: int = 2) -> list[dict]:
    """A human-looking ``userFills`` sample: a few 1 h round trips a day, takers."""
    out: list[dict] = []
    for d in range(days):
        for k in range(per_day):
            out += round_trip("BTC", now_ms - (d + 1) * DAY_MS + k * 4 * 3_600_000,
                              3_600_000, "5")
    return out


def sim_router(fills: dict[str, FakeFillsAPI] | None = None, funding=None, meta=None,
               fail_after: int | None = None, probes: dict[str, list] | None = None,
               probe_calls: list | None = None):
    """respx side effect: ``userFillsByTime`` served by per-address FakeFillsAPIs
    (HL semantics), ``userFunding`` from ``{addr: rows}`` (inclusive bounds),
    ``userFills`` (the discovery probe) from ``probes`` (default: a human sample).
    After ``fail_after`` requests every request gets HTTP 429."""
    fills = fills or {}
    funding = funding or {}
    probes = probes or {}
    n = [0]

    def handler(request):
        n[0] += 1
        if fail_after is not None and n[0] > fail_after:
            return httpx.Response(429)
        body = json.loads(request.content)
        t = body["type"]
        if t == "userFillsByTime":
            api = fills.get(body["user"]) or FakeFillsAPI()
            return httpx.Response(200, json=api.query(body["startTime"], body.get("endTime")))
        if t == "userFunding":
            end = body.get("endTime")
            rows = [r for r in funding.get(body["user"], []) if r["time"] >= body["startTime"]
                    and (end is None or r["time"] <= end)]
            return httpx.Response(200, json=rows)
        if t == "userFills":
            if probe_calls is not None:
                probe_calls.append(body["user"])
            sample = probes.get(body["user"])
            return httpx.Response(200, json=human_sample() if sample is None else sample)
        if t == "meta":
            return httpx.Response(200, json={"universe": []} if meta is None else meta)
        raise AssertionError(t)  # pragma: no cover

    return handler


def cov(session, addr):
    return pipeline.kv_get_json(session, f"hl:cov:{addr}")


def n_fills(session, addr=None):
    q = select(func.count()).select_from(Fill)
    if addr:
        q = q.where(Fill.address == addr)
    return session.scalar(q)


# -- discover -------------------------------------------------------------------

@respx.mock
async def test_discover_dedupes_ranks_and_excludes_vaults(session):
    rows = [lb_row(A, "50"), lb_row(B, "300"), lb_row(A.upper().replace("0X", "0x"), "50"),
            lb_row(V, "999"), lb_row(C, "10"), lb_row(B, "300")]
    respx.get(LB).mock(return_value=httpx.Response(200, json={"leaderboardRows": rows}))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[vault_row(V)]))
    respx.post(INFO).mock(side_effect=sim_router())
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=2, now=NOW)
    assert list(got) == [B, A]  # by month pnl, vault skipped, deduped case-insensitively
    assert got.error is None
    assert got.vaults_checked is True
    stored = {w.address: w for w in session.scalars(select(Wallet))}
    assert set(stored) == {A, B}
    assert stored[B].stats_json["leaderboard"]["pnl_month"] == 300.0
    assert stored[B].is_vault is False


@respx.mock
async def test_discover_prefilter_excludes_each_reason_before_cache_cap(
        session, monkeypatch, caplog):
    monkeypatch.setattr(pipeline, "CANDIDATE_CACHE_MAX", 3)
    g1, g2, g3, edge = ("0x" + h * 20 for h in ("01", "02", "03", "0e"))
    low_av, no_vlm, churn, loser = ("0x" + h * 20 for h in ("11", "12", "13", "14"))
    rows = [
        # excluded rows have the HIGHEST month pnl: a filter applied after the cache cap
        # would leave the cache full of them
        lb_row(low_av, "9000000", account_value="9999.99"),
        lb_row(no_vlm, "8000000", vlm="0"),
        lb_row(churn, "7000000", account_value="20000", vlm="600001"),  # > 30 x AV (P30)
        lb_row(V, "6000000"),
        lb_row(loser, "-5"),
        lb_row(g1, "500"), lb_row(g2, "400"), lb_row(g3, "300"),
        lb_row(edge, "10", account_value="10000", vlm="300000"),  # boundaries are kept
    ]
    respx.get(LB).mock(return_value=httpx.Response(200, json={"leaderboardRows": rows}))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[vault_row(V)]))
    respx.post(INFO).mock(side_effect=sim_router())
    async with make_client() as c:
        with caplog.at_level(logging.INFO, logger="hades_hl.pipeline"):
            got = await pipeline.discover(session, c, limit=10, now=NOW)
    assert list(got) == [g1, g2, g3]  # top 3 by month pnl among the 4 that pass
    cached = json.loads(session.get(Kv, pipeline.KV_LEADERBOARD).value)
    assert [r["address"] for r in cached["rows"]] == [g1, g2, g3]
    assert cached["prefilter"] == {"before": 9, "after": 4, "account_value": 1, "volume": 1,
                                   "churn": 1, "pnl": 1, "vault": 1}
    assert cached["vaults_checked"] is True
    assert got.prefilter == cached["prefilter"]
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "9" in msgs and "prefilter" in msgs


@respx.mock
async def test_discover_caches_candidates_not_raw_body_for_24h(session):
    lb = respx.get(LB).mock(return_value=httpx.Response(
        200, json={"leaderboardRows": [lb_row(A, "5"), lb_row(B, "7")]}))
    vr = respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[]))
    respx.post(INFO).mock(side_effect=sim_router())
    async with make_client() as c:
        await pipeline.discover(session, c, limit=5, now=NOW)
        await pipeline.discover(session, c, limit=5, now=NOW + timedelta(hours=23))
        assert lb.call_count == 1 and vr.call_count == 1
        await pipeline.discover(session, c, limit=5, now=NOW + timedelta(hours=25))
        assert lb.call_count == 2 and vr.call_count == 2
    cached = json.loads(session.get(Kv, pipeline.KV_LEADERBOARD).value)
    assert "leaderboardRows" not in cached
    assert [r["address"] for r in cached["rows"]] == [B, A]


@respx.mock
async def test_discover_leaderboard_unavailable_returns_empty(session, caplog):
    respx.get(LB).mock(return_value=httpx.Response(503))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[]))
    async with make_client(max_attempts=1) as c:
        with caplog.at_level("WARNING", logger="hades_hl.pipeline"):
            got = await pipeline.discover(session, c, limit=5, now=NOW)
    assert list(got) == []
    assert got.error and "leaderboard" in got.error
    assert any("leaderboard" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_discover_vault_list_unavailable_still_discovers_unchecked(session, caplog):
    respx.get(LB).mock(return_value=httpx.Response(
        200, json={"leaderboardRows": [lb_row(A, "5")]}))
    respx.get(VAULTS).mock(return_value=httpx.Response(503))
    respx.post(INFO).mock(side_effect=sim_router())
    async with make_client(max_attempts=1) as c:
        with caplog.at_level("WARNING", logger="hades_hl.pipeline"):
            got = await pipeline.discover(session, c, limit=5, now=NOW)
    assert list(got) == [A]
    assert got.vaults_checked is False  # M6
    cached = json.loads(session.get(Kv, pipeline.KV_LEADERBOARD).value)
    assert cached["vaults_checked"] is False
    assert any("vault" in r.getMessage() for r in caplog.records)


@respx.mock
async def test_discover_marks_existing_wallet_as_vault(session):
    V2 = "0x" + "ee" * 20  # a known wallet that is not on the leaderboard at all
    session.add_all([Wallet(address=V), Wallet(address=V2)])
    session.commit()
    respx.get(LB).mock(return_value=httpx.Response(
        200, json={"leaderboardRows": [lb_row(V, "5"), lb_row(A, "1")]}))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[vault_row(V), vault_row(V2)]))
    respx.post(INFO).mock(side_effect=sim_router())
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=5, now=NOW)
    assert list(got) == [A]
    assert session.get(Wallet, V).is_vault is True
    assert session.get(Wallet, V2).is_vault is True


# -- backfill (ruling P27: newest first, contiguous coverage) -----------------------

@respx.mock
async def test_backfill_rerun_inserts_zero(session):
    t0 = NOW_MS - 5 * DAY_MS
    api = FakeFillsAPI([fill(i, t0 + i * 1000) for i in range(10)])
    fund = [funding_row(t0 + 3600_000 * k) for k in range(3)]
    route = respx.post(INFO).mock(side_effect=sim_router({A: api}, {A: fund}))
    async with make_client() as c:
        r1 = await pipeline.backfill(session, c, A, days=30, now=NOW)
        n_calls = len(api.calls)
        r2 = await pipeline.backfill(session, c, A, days=30, now=NOW)
    assert (r1.new_fills, r1.new_funding, r1.truncated) == (10, 3, False)
    assert (r2.new_fills, r2.new_funding) == (0, 0)
    assert min(s for s, _ in api.calls[:n_calls]) == NOW_MS - 30 * DAY_MS
    assert all(s >= NOW_MS for s, _ in api.calls[n_calls:])  # rerun: only [cov_end, now]
    assert cov(session, A) == {"start": NOW_MS - 30 * DAY_MS, "end": NOW_MS, "capped": False}
    bodies = [json.loads(call.request.content) for call in route.calls]
    fund_bodies = [b for b in bodies if b["type"] == "userFunding"]
    assert fund_bodies[0]["startTime"] == NOW_MS - 30 * DAY_MS  # P31: cov_start
    assert fund_bodies[1]["startTime"] == NOW_MS  # rerun: the segment [cov_end, now]
    assert n_fills(session) == 10
    assert session.scalar(select(func.count()).select_from(Funding)) == 3
    assert session.get(Wallet, A).stats_json["backfill"]["history_partial"] is False


@respx.mock
async def test_backfill_capped_wallet_stores_newest_10k(session):
    fills = [fill(i, NOW_MS - 15_000 * 10_000 + i * 10_000) for i in range(15_000)]
    respx.post(INFO).mock(side_effect=sim_router({A: FakeFillsAPI(fills)}))
    async with make_client() as c:
        r = await pipeline.backfill(session, c, A, days=30, now=NOW)
    assert r.truncated is True and r.new_fills == 10_000
    stored = set(session.scalars(select(Fill.tid).where(Fill.address == A)))
    assert stored == set(range(5_000, 15_000))  # the newest 10k, not the oldest
    assert cov(session, A) == {"start": fills[5_000]["time"], "end": NOW_MS, "capped": True}
    assert session.get(Wallet, A).stats_json["backfill"]["history_partial"] is True


@respx.mock
async def test_backfill_rerun_after_new_fills_extends_cov_end(session):
    t0 = NOW_MS - 2 * DAY_MS
    api = FakeFillsAPI([fill(i, t0 + i * 60_000) for i in range(100)])
    respx.post(INFO).mock(side_effect=sim_router({A: api}))
    later = NOW + timedelta(hours=1)
    async with make_client() as c:
        await pipeline.backfill(session, c, A, days=30, now=NOW)
        n_calls = len(api.calls)
        api.fills += [fill(1000 + i, NOW_MS + 1 + i * 60_000) for i in range(50)]
        r2 = await pipeline.backfill(session, c, A, days=30, now=later)
    assert r2.new_fills == 50 and r2.truncated is False
    assert all(s >= NOW_MS for s, _ in api.calls[n_calls:])
    assert cov(session, A) == {"start": NOW_MS - 30 * DAY_MS,
                               "end": NOW_MS + 3_600_000, "capped": False}


@respx.mock
async def test_backfill_rerun_hitting_cap_resets_cov_start(session):
    api = FakeFillsAPI([fill(i, NOW_MS - DAY_MS + i * 1000) for i in range(100)])
    respx.post(INFO).mock(side_effect=sim_router({A: api}))
    later = NOW + timedelta(days=1)
    async with make_client() as c:
        await pipeline.backfill(session, c, A, days=30, now=NOW)
        assert cov(session, A)["capped"] is False
        new = [fill(10_000 + i, NOW_MS + 1 + i * 5) for i in range(12_000)]
        api.fills += new
        r2 = await pipeline.backfill(session, c, A, days=30, now=later)
        assert r2.truncated is True and r2.new_fills == 10_000
        assert cov(session, A) == {"start": new[2_000]["time"],
                                   "end": NOW_MS + DAY_MS, "capped": True}
        assert session.get(Wallet, A).stats_json["backfill"]["history_partial"] is True
        # an uncapped rerun extends the end but keeps the capped start (still partial)
        api.fills.append(fill(99_999, NOW_MS + DAY_MS + 5))
        r3 = await pipeline.backfill(session, c, A, days=30, now=later + timedelta(hours=1))
    assert r3.new_fills == 1
    assert cov(session, A)["start"] == new[2_000]["time"]
    assert session.get(Wallet, A).stats_json["backfill"]["history_partial"] is True


@respx.mock
async def test_backfill_stale_coverage_resets_to_days_floor(session):
    api = FakeFillsAPI([fill(0, NOW_MS - DAY_MS)])
    respx.post(INFO).mock(side_effect=sim_router({A: api}))
    much_later = NOW + timedelta(days=40)
    async with make_client() as c:
        await pipeline.backfill(session, c, A, days=30, now=NOW)
        await pipeline.backfill(session, c, A, days=30, now=much_later)
    end = NOW_MS + 40 * DAY_MS
    assert cov(session, A) == {"start": end - 30 * DAY_MS, "end": end, "capped": False}


@respx.mock
async def test_backfill_more_days_extends_cov_start(session):
    old = fill(0, NOW_MS - 20 * DAY_MS)
    api = FakeFillsAPI([old, fill(2, NOW_MS - DAY_MS)])
    respx.post(INFO).mock(side_effect=sim_router({A: api}))
    async with make_client() as c:
        r1 = await pipeline.backfill(session, c, A, days=7, now=NOW)
        r2 = await pipeline.backfill(session, c, A, days=30, now=NOW)
    assert (r1.new_fills, r2.new_fills) == (1, 1)
    assert cov(session, A)["start"] == NOW_MS - 30 * DAY_MS


@respx.mock
async def test_429_mid_backfill_keeps_earlier_pages(engine):
    # newest window (1 h) holds 1500 fills; the next request is rate limited
    fills = [fill(i, NOW_MS - 3_000_000 + i * 1000) for i in range(1500)]
    fills += [fill(5000 + i, NOW_MS - 10 * DAY_MS + i) for i in range(10)]
    respx.post(INFO).mock(side_effect=sim_router({A: FakeFillsAPI(fills)}, fail_after=1))
    with Session(engine) as s:
        async with make_client(max_attempts=2) as c:
            with pytest.raises(HLRateLimited):
                await pipeline.backfill(s, c, A, days=30, now=NOW)
    with Session(engine) as fresh:  # committed, not just flushed
        assert n_fills(fresh) == 1500
        w = fresh.get(Wallet, A)
        assert w is not None
        assert w.stats_json["backfill"]["last_error"] == "rate_limited"  # M12
        assert cov(fresh, A) is None  # incomplete segment: no coverage recorded


# -- sz decimals / rescore ----------------------------------------------------------

@respx.mock
async def test_load_sz_decimals_cached_24h(session):
    meta = {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "ETH", "szDecimals": 4}]}
    route = respx.post(INFO).mock(side_effect=sim_router(meta=meta))
    async with make_client() as c:
        assert await pipeline.load_sz_decimals(session, c, NOW) == {"BTC": 5, "ETH": 4}
        await pipeline.load_sz_decimals(session, c, NOW + timedelta(hours=23))
        assert route.call_count == 1
        await pipeline.load_sz_decimals(session, c, NOW + timedelta(hours=25))
        assert route.call_count == 2


def round_trips(n: int, t0: int, step_ms: int, *, win: bool = True) -> list[dict]:
    out = []
    for k in range(n):
        t = t0 + k * step_ms
        out.append(mk("BTC", "B", "1", "100", "0", t))
        out.append(mk("BTC", "A", "1", "110" if win else "90", "1", t + 3_600_000,
                      closed_pnl="10" if win else "-10"))
    return out


def test_rescore_persists_scores_reasons_and_passes_sz_decimals(session, monkeypatch):
    t0 = NOW_MS - 20 * DAY_MS
    session.add_all([Wallet(address=A), Wallet(address=B), Wallet(address=C),
                     Wallet(address=V, is_vault=True)])
    insert_fills(session, A, round_trips(35, t0, DAY_MS // 2))
    insert_fills(session, B, round_trips(35, t0, DAY_MS // 2, win=False))
    insert_fills(session, C, round_trips(3, t0, DAY_MS))
    session.add(Funding(address=A, coin="BTC", time=t0 + 1000, usdc="-1.5"))
    session.commit()

    seen = {}
    real = pipeline.wallet_stats

    def spy(addr, fills, funding, now, window_days=30, **kw):
        seen[addr] = kw
        return real(addr, fills, funding, now, window_days, **kw)

    monkeypatch.setattr(pipeline, "wallet_stats", spy)
    res = pipeline.rescore(session, NOW, sz_decimals={"BTC": 5})
    session.commit()
    assert seen[A]["sz_decimals"] == {"BTC": 5}
    assert seen[A]["oldest_fill_time"] == datetime.fromtimestamp(t0 / 1000, tz=timezone.utc)
    w = {x.address: x for x in session.scalars(select(Wallet))}
    assert w[A].score is not None and w[B].score is not None
    assert w[A].score > w[B].score
    assert w[C].score is None and w[C].stats_json["score_reason"] == "n<30"
    assert w[V].score is None and w[V].stats_json["score_reason"] == "bot"
    assert w[V].is_bot is True  # M5: is_bot(st, w.is_vault)
    assert w[A].stats_json["funding_usd"] == pytest.approx(-1.5)
    assert w[A].scored_at is not None
    assert res.scored == 2 and res.unscored == 2
    assert res.reasons == {"n<30": 1, "bot": 1}


def test_rescore_partial_history_under_7_days_unscored(session):
    t0 = NOW_MS - 3 * DAY_MS
    session.add(Wallet(address=A, stats_json={"backfill": {"history_partial": True}}))
    insert_fills(session, A, round_trips(40, t0, DAY_MS // 20))
    session.commit()
    pipeline.rescore(session, NOW, sz_decimals={})
    w = session.get(Wallet, A)
    assert w.score is None
    assert w.stats_json["score_reason"] == "history too short (hyperactive)"
    assert w.stats_json["history_partial"] is True
    assert w.stats_json["window_days"] == pytest.approx(3.0)
    assert w.stats_json["backfill"]["history_partial"] is True  # metadata kept


def test_stored_fill_roundtrip_keeps_liquidation_victim_and_builder_fee(session):
    f = mk("BTC", "A", "1", "90", "1", NOW_MS, liquidation={"liquidatedUser": B, "markPx": "90",
                                                            "method": "market"})
    f["builderFee"] = "0.25"
    insert_fills(session, A, [f])
    session.commit()
    (got,) = pipeline.stored_fills(session, A)
    assert got["liquidation"]["liquidatedUser"] == B
    assert got["builderFee"] == "0.25"
    assert got["startPosition"] == "1" or got["startPosition"] == "1.0"


def test_rescore_uses_coverage_window(session, monkeypatch):
    """P17 with P27: oldest_fill_time = cov_start; only fills in [cov_start, cov_end]."""
    cov_start = NOW_MS - 10 * DAY_MS
    session.add(Wallet(address=A, stats_json={"backfill": {"history_partial": True}}))
    insert_fills(session, A, round_trips(40, NOW_MS - 9 * DAY_MS, DAY_MS // 5))
    # a stored fill outside the coverage (e.g. from an older, superseded segment)
    insert_fills(session, A, [mk("ETH", "B", "1", "100", "0", NOW_MS - 20 * DAY_MS)])
    pipeline.kv_set_json(session, f"hl:cov:{A}",
                         {"start": cov_start, "end": NOW_MS, "capped": True})
    session.commit()
    seen = {}
    real = pipeline.wallet_stats

    def spy(addr, fills, funding, now, window_days=30, **kw):
        seen[addr] = kw
        return real(addr, fills, funding, now, window_days, **kw)

    monkeypatch.setattr(pipeline, "wallet_stats", spy)
    pipeline.rescore(session, NOW, sz_decimals={})
    w = session.get(Wallet, A)
    assert seen[A]["oldest_fill_time"] == datetime.fromtimestamp(cov_start / 1000, tz=timezone.utc)
    assert w.stats_json["window_days"] == pytest.approx(10.0)  # now - cov_start
    assert w.stats_json["trades"] == 80  # the ETH fill outside the coverage is ignored
    assert w.stats_json["history_partial"] is True


# -- round 2 / P30: activity probe ---------------------------------------------------

def lb_route(*rows):
    respx.get(LB).mock(return_value=httpx.Response(200, json={"leaderboardRows": list(rows)}))
    respx.get(VAULTS).mock(return_value=httpx.Response(200, json=[]))


def hfmt(i: int) -> str:
    return "0x" + f"{i:02x}" * 20


def bot_hft_sample() -> list[dict]:  # 2000 fills in the last ~33 min
    return [fill(i, NOW_MS - 2_000_000 + i * 1000) for i in range(2000)]


def bot_maker_sample() -> list[dict]:  # human pace and holds, but all maker fills
    out: list[dict] = []
    for d in range(20):
        out += round_trip("BTC", NOW_MS - (d + 1) * DAY_MS, 3_600_000, "5", crossed=False)
    return out


def bot_hold_sample() -> list[dict]:  # human pace, takers, 10 s holds
    out: list[dict] = []
    for d in range(20):
        out += round_trip("BTC", NOW_MS - (d + 1) * DAY_MS, 10_000, "5")
    return out


@respx.mock
async def test_discover_probe_rejects_bot_shapes_accepts_human(session):
    hft, maker, hold, human = hfmt(0x31), hfmt(0x32), hfmt(0x33), hfmt(0x34)
    lb_route(lb_row(hft, "400"), lb_row(maker, "300"), lb_row(hold, "200"),
             lb_row(human, "100"))
    calls: list[str] = []
    respx.post(INFO).mock(side_effect=sim_router(probes={
        hft: bot_hft_sample(), maker: bot_maker_sample(), hold: bot_hold_sample(),
        human: human_sample()}, probe_calls=calls))
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=5, now=NOW)
    assert list(got) == [human]
    assert calls == [hft, maker, hold, human]  # ranked order
    assert got.probe["probed"] == 4 and got.probe["accepted"] == 1
    assert got.probe["rejected"] == {"fills_per_day": 1, "maker_share": 1, "hold": 1}
    assert set(session.scalars(select(Wallet.address))) == {human}  # rejects not stored
    p = pipeline.kv_get_json(session, f"hl:probe:{hft}")
    assert p["verdict"] == "fills_per_day" and p["fills_per_day"] > 300
    p = pipeline.kv_get_json(session, f"hl:probe:{human}")
    assert p["verdict"] == "ok" and p["median_hold_s"] == 3600.0 and p["maker_share"] == 0.0


@respx.mock
async def test_discover_probe_rejects_inactive(session):
    stale, empty, human = hfmt(0x41), hfmt(0x42), hfmt(0x43)
    lb_route(lb_row(stale, "300"), lb_row(empty, "200"), lb_row(human, "100"))
    respx.post(INFO).mock(side_effect=sim_router(probes={
        stale: human_sample(NOW_MS - 31 * DAY_MS), empty: []}))
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=5, now=NOW)
    assert list(got) == [human]
    assert got.probe["rejected"] == {"inactive": 2}


@respx.mock
async def test_discover_probe_budget_is_10x_limit(session):  # P33
    bots = [hfmt(0x50 + i) for i in range(12)]
    lb_route(*(lb_row(a, str(100 - i)) for i, a in enumerate(bots)))
    calls: list[str] = []
    respx.post(INFO).mock(side_effect=sim_router(
        probes={a: bot_hft_sample() for a in bots}, probe_calls=calls))
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=1, now=NOW)
    assert list(got) == []
    assert calls == bots[:10]
    assert got.probe["probed"] == 10 and got.probe["budget"] == 10
    assert got.probe["budget_exhausted"] is True


def bursty_sample() -> list[dict]:
    """3 very busy days (400 fills each, 2-min holds) + 1 fill 29 days ago: the mean over
    the span is ~41/day but the median over active days is 400 (P32)."""
    out: list[dict] = [fill(0, NOW_MS - 29 * DAY_MS)]
    tid = 10
    for d in range(3):
        base = NOW_MS - (d + 1) * DAY_MS
        for k in range(200):
            t = base + k * 200_000
            out += [fill(tid, t), fill(tid + 1, t + 120_000)]
            tid += 2
    return out


def test_probe_verdict_uses_max_of_mean_and_median_fills_per_day():  # P32
    v = pipeline.probe_verdict(A, bursty_sample(), NOW)
    assert v["fills_per_day"] < 300 and v["median_fills_per_day"] > 300
    assert v["verdict"] == "fills_per_day"
    calm = human_sample()
    assert pipeline.probe_verdict(A, calm, NOW)["verdict"] == "ok"


@respx.mock
async def test_discover_probe_stops_once_limit_accepted(session):
    addrs = [hfmt(0x60 + i) for i in range(4)]
    lb_route(*(lb_row(a, str(100 - i)) for i, a in enumerate(addrs)))
    calls: list[str] = []
    respx.post(INFO).mock(side_effect=sim_router(probe_calls=calls))
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=2, now=NOW)
    assert list(got) == addrs[:2] and calls == addrs[:2]


@respx.mock
async def test_discover_probe_cached_24h(session):
    a, b = hfmt(0x71), hfmt(0x72)
    lb_route(lb_row(a, "200"), lb_row(b, "100"))
    calls: list[str] = []
    respx.post(INFO).mock(side_effect=sim_router(probes={b: bot_hft_sample()},
                                                 probe_calls=calls))
    async with make_client() as c:
        g1 = await pipeline.discover(session, c, limit=5, now=NOW)
        g2 = await pipeline.discover(session, c, limit=5, now=NOW + timedelta(hours=23))
        assert len(calls) == 2  # second run: both verdicts from kv
        assert list(g1) == list(g2) == [a]
        assert g2.probe["cached"] == 2 and g2.probe["rejected"] == {"fills_per_day": 1}
        await pipeline.discover(session, c, limit=5, now=NOW + timedelta(hours=25))
    assert len(calls) == 4


# -- round 2 / P31: funding over the fills coverage only ----------------------------

@respx.mock
async def test_backfill_funding_only_over_coverage(session):
    fills = [fill(i, NOW_MS - 15_000 * 10_000 + i * 10_000) for i in range(15_000)]
    fund = [funding_row(NOW_MS - 30 * DAY_MS + k * 3_600_000) for k in range(30 * 24)]
    route = respx.post(INFO).mock(side_effect=sim_router({A: FakeFillsAPI(fills)}, {A: fund}))
    async with make_client() as c:
        await pipeline.backfill(session, c, A, days=30, now=NOW)
    c0 = cov(session, A)
    assert c0["capped"] is True
    fb = [json.loads(x.request.content) for x in route.calls]
    fb = [b for b in fb if b["type"] == "userFunding"]
    assert fb and all(b["startTime"] >= c0["start"] for b in fb)
    assert all(b["endTime"] == c0["end"] for b in fb)
    times = list(session.scalars(select(Funding.time).where(Funding.address == A)))
    assert times and min(times) >= c0["start"]
    assert len(times) == sum(1 for r in fund if c0["start"] <= r["time"] <= NOW_MS)


@respx.mock
async def test_backfill_no_funding_call_when_coverage_empty(session, monkeypatch):
    from hades_hl import client as client_mod
    monkeypatch.setattr(client_mod, "MAX_BACKWARD_REQUESTS", 0)
    route = respx.post(INFO).mock(side_effect=sim_router(
        {A: FakeFillsAPI([fill(0, NOW_MS - DAY_MS)])}, {A: [funding_row(NOW_MS - DAY_MS)]}))
    async with make_client() as c:
        r = await pipeline.backfill(session, c, A, days=30, now=NOW)
    types = [json.loads(x.request.content)["type"] for x in route.calls]
    assert "userFunding" not in types
    c0 = cov(session, A)
    assert c0["start"] > c0["end"]  # empty
    assert r.new_funding == 0 and r.truncated is True
    assert session.get(Wallet, A).stats_json["backfill"]["history_partial"] is True


@respx.mock
async def test_backfill_early_exit_records_only_fetched_coverage(session, monkeypatch):
    from hades_hl import client as client_mod
    monkeypatch.setattr(client_mod, "MAX_BACKWARD_REQUESTS", 2)
    api = FakeFillsAPI([fill(0, NOW_MS - 10 * DAY_MS), fill(2, NOW_MS - 600_000)])
    respx.post(INFO).mock(side_effect=sim_router({A: api}))
    async with make_client() as c:
        r = await pipeline.backfill(session, c, A, days=30, now=NOW)
    assert r.new_fills == 1 and r.truncated is True
    assert cov(session, A)["start"] == NOW_MS - 3 * 3_600_000 + 1
    assert session.get(Wallet, A).stats_json["backfill"]["history_partial"] is True


@respx.mock
async def test_backfill_429_during_funding_is_refetched_next_run(session):
    t0 = NOW_MS - 5 * DAY_MS
    fund = [funding_row(t0 + 3_600_000 * k) for k in range(3)]
    inner = sim_router({A: FakeFillsAPI([fill(0, t0)])}, {A: fund})
    fail = [True]

    def handler(request):
        if fail[0] and json.loads(request.content)["type"] == "userFunding":
            return httpx.Response(429)
        return inner(request)

    route = respx.post(INFO).mock(side_effect=handler)
    later = NOW + timedelta(hours=1)
    async with make_client(max_attempts=1) as c:
        with pytest.raises(HLRateLimited):
            await pipeline.backfill(session, c, A, days=30, now=NOW)
        fail[0] = False
        r = await pipeline.backfill(session, c, A, days=30, now=later)
    assert r.new_funding == 3
    fb = [json.loads(x.request.content) for x in route.calls]
    fb = [b for b in fb if b["type"] == "userFunding"]
    assert fb[-1]["startTime"] == NOW_MS - 30 * DAY_MS
    assert session.get(Wallet, A).stats_json["backfill"]["funding_end"] == NOW_MS + 3_600_000


@respx.mock
async def test_discover_probe_loads_sz_decimals_once(session, monkeypatch):
    a, b = hfmt(0x81), hfmt(0x82)
    lb_route(lb_row(a, "200"), lb_row(b, "100"))
    meta = {"universe": [{"name": "BTC", "szDecimals": 5}]}
    route = respx.post(INFO).mock(side_effect=sim_router(meta=meta))
    seen: list = []
    real = pipeline.probe_verdict
    monkeypatch.setattr(pipeline, "probe_verdict",
                        lambda addr, sample, now, szd=None: seen.append(szd) or real(
                            addr, sample, now, szd))
    async with make_client() as c:
        await pipeline.discover(session, c, limit=5, now=NOW)
    types = [json.loads(x.request.content)["type"] for x in route.calls]
    assert types.count("meta") == 1
    assert seen == [{"BTC": 5}, {"BTC": 5}]  # the P28 chain tolerance reaches the probe


@respx.mock
async def test_load_sz_decimals_bad_meta_not_cached(session):
    respx.post(INFO).mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    async with make_client() as c:
        assert await pipeline.load_sz_decimals(session, c, NOW) == {}
    assert pipeline.kv_get_json(session, "hl:sz_decimals") is None


# -- Task 13 fix round 1 / P62: the probe rejects wallets rescore cannot score ---------

def scaling_in_sample(n: int, span_days: float) -> list[dict]:
    """``n`` taker fills evenly spread over the last ``span_days`` that only ever add to
    one BTC long: human pace, no closed position at all."""
    step = int(span_days * DAY_MS) // n
    return [mk("BTC", "B", "1", "100", str(i), NOW_MS - (n - i) * step, tid=50_000 + i)
            for i in range(n)]


def test_probe_verdict_rejects_fewer_than_30_closed_in_30d():  # P62
    v = pipeline.probe_verdict(A, human_sample(days=10, per_day=1), NOW)
    assert v["verdict"] == "n<30" == pipeline.REASON_FEW
    assert v["closed_30d"] == 10 and v["closed_window_complete"] is True
    v = pipeline.probe_verdict(A, human_sample(days=29, per_day=1), NOW)
    assert v["verdict"] == "n<30" and v["closed_30d"] == 29


def test_probe_verdict_accepts_30_closed_in_30d():  # P62: the scoring threshold itself
    v = pipeline.probe_verdict(A, human_sample(days=15, per_day=2), NOW)
    assert v["verdict"] == "ok" and v["closed_30d"] == 30 == pipeline.MIN_CLOSED_POSITIONS


def test_probe_verdict_counts_only_closes_inside_the_30d_window():  # P62
    old = human_sample(NOW_MS - 31 * DAY_MS, days=20, per_day=2)  # 40 closed, 32..51 d ago
    v = pipeline.probe_verdict(A, old + human_sample(days=5, per_day=1), NOW)
    assert v["verdict"] == "n<30" and v["closed_30d"] == 5


def test_probe_verdict_closed_count_matches_the_scoring_stat():  # P62: same definition
    sample = human_sample(days=12, per_day=2)
    st = pipeline.wallet_stats(A, sample, [], NOW, pipeline.SCORE_WINDOW_DAYS)
    assert pipeline.probe_verdict(A, sample, NOW)["closed_30d"] == st["n"] == 24


def test_probe_verdict_full_sample_shorter_than_30d_is_not_decisive():  # P62 careful rule
    # 2000 fills (the userFills cap) spanning only 20 days: older fills of the 30-day
    # window are unseen, so < 30 closed here proves nothing -> not rejected
    v = pipeline.probe_verdict(A, scaling_in_sample(2000, 20), NOW)
    assert v["verdict"] == "ok"
    assert v["closed_30d"] == 0 and v["closed_window_complete"] is False
    # the same capped sample reaching back >= 30 days covers the window: rejected
    v = pipeline.probe_verdict(A, scaling_in_sample(2000, 40), NOW)
    assert v["verdict"] == "n<30" and v["closed_window_complete"] is True
    # an uncapped sample is the wallet's whole history, however short: rejected
    v = pipeline.probe_verdict(A, scaling_in_sample(500, 20), NOW)
    assert v["verdict"] == "n<30" and v["closed_window_complete"] is True


@respx.mock
async def test_discover_skips_unscoreable_and_continues_down_the_ranking(session):  # P62
    few1, few2, ok1, ok2 = hfmt(0x81), hfmt(0x82), hfmt(0x83), hfmt(0x84)
    lb_route(lb_row(few1, "400"), lb_row(ok1, "300"), lb_row(few2, "200"),
             lb_row(ok2, "100"))
    calls: list[str] = []
    respx.post(INFO).mock(side_effect=sim_router(probes={
        few1: human_sample(days=10, per_day=1), few2: human_sample(days=29, per_day=1),
        ok1: human_sample(days=15, per_day=2)}, probe_calls=calls))
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=2, now=NOW)
    assert list(got) == [ok1, ok2]  # limit reached by going further down the ranking
    assert calls == [few1, ok1, few2, ok2]
    assert got.probe["probed"] == 4 and got.probe["accepted"] == 2
    assert got.probe["rejected"] == {"n<30": 2}
    assert set(session.scalars(select(Wallet.address))) == {ok1, ok2}
    assert pipeline.kv_get_json(session, f"hl:probe:{few1}")["verdict"] == "n<30"


@respx.mock
async def test_discover_reprobes_cached_verdicts_from_before_p62(session):
    a = hfmt(0x91)
    lb_route(lb_row(a, "100"))
    # an 'ok' verdict cached by the pre-P62 probe (no version): must not be trusted
    pipeline.kv_set_json(session, f"hl:probe:{a}", {"fetched_at": NOW_MS, "verdict": "ok"})
    session.commit()
    calls: list[str] = []
    respx.post(INFO).mock(side_effect=sim_router(
        probes={a: human_sample(days=10, per_day=1)}, probe_calls=calls))
    async with make_client() as c:
        got = await pipeline.discover(session, c, limit=5, now=NOW)
    assert calls == [a] and list(got) == []
    assert got.probe["cached"] == 0 and got.probe["rejected"] == {"n<30": 1}
