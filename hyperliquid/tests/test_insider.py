"""Task 7: insider tags with on-chain evidence (pre_listing, pre_move, fresh_whale)."""

import itertools
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_hl import insider, pipeline
from hades_hl.client import HLClient, HLError
from hades_hl.db import Tag as TagRow
from hades_hl.db import Wallet, init_db, insert_fills, make_engine
from hades_hl.insider import (
    Tag,
    open_legs,
    persist_tags,
    tag_fresh_whale,
    tag_pre_listing,
    tag_pre_move,
)
from hlfills import mk

INFO = "https://api.hyperliquid.xyz/info"
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
H = 3_600_000
MIN = 60_000
DAY = 86_400_000
A = "0x" + "a1" * 20
B = "0x" + "b2" * 20


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
def session(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'hl.db'}")
    init_db(eng)
    with Session(eng) as s:
        yield s


_TIDS = itertools.count(50_000_000)  # auto tids; explicit test tids stay < 1000


def rt(coin, t, sz, px, *, long=True, tid=None, hold=H, close_px=None):
    """Open from flat at ``t`` then close after ``hold``."""
    o, c = ("B", "A") if long else ("A", "B")
    start = sz if long else f"-{sz}"
    return [mk(coin, o, sz, px, "0", t, tid=next(_TIDS) if tid is None else tid),
            mk(coin, c, sz, close_px or px, start, t + hold, tid=next(_TIDS))]


def baseline(coin="BTC", n=5, t0=NOW_MS - 20 * DAY, notional_px="1000"):
    """``n`` round trips of notional 1000 (sz 1 @ 1000) on ``coin``: the usual size."""
    out = []
    for i in range(n):
        out += rt(coin, t0 + i * DAY, "1", notional_px)
    return out


# -- open legs --------------------------------------------------------------------

def test_open_legs_include_flip_open_leg():
    fills = [mk("ETH", "B", "1", "100", "0", NOW_MS, tid=1),
             mk("ETH", "A", "3", "100", "1", NOW_MS + MIN, tid=2)]  # flip to -2
    legs = open_legs(A, fills)
    assert [(lg.fill["tid"], lg.direction, lg.size) for lg in legs] == [
        (1, "long", 1), (2, "short", 2)]


# -- pre_listing --------------------------------------------------------------------

LIST_MS = NOW_MS - 10 * DAY


def test_pre_listing_fires_with_evidence_at_5x_boundary():
    fills = baseline() + rt("NEW", LIST_MS + H, "50", "100", tid=777)  # 5000 = 5 x 1000
    tags = tag_pre_listing(fills, {"NEW": datetime.fromtimestamp(LIST_MS / 1000, timezone.utc)},
                           None, address=A)
    assert len(tags) == 1
    t = tags[0]
    assert t.address == A and t.tag == "pre_listing"
    ev = t.evidence[0]
    assert ev["tid"] == 777 and ev["hash"] == "0xh777" and ev["coin"] == "NEW"
    assert ev["time"] == LIST_MS + H
    assert ev["detail"]["notional_usd"] == 5000.0
    assert ev["detail"]["hours_after_listing"] == pytest.approx(1.0)
    assert ev["detail"]["usual_notional_usd"] == 1000.0


def test_pre_listing_negative_below_5x_and_after_24h():
    small = baseline() + rt("NEW", LIST_MS + H, "49.99", "100")  # 4999 < 5000
    assert tag_pre_listing(small, {"NEW": LIST_MS}, 1000, address=A) == []
    late = baseline() + rt("NEW", LIST_MS + 24 * H + 1, "100", "100")
    assert tag_pre_listing(late, {"NEW": LIST_MS}, 1000, address=A) == []
    unknown = baseline() + rt("NEW", LIST_MS + H, "100", "100")
    assert tag_pre_listing(unknown, {}, 1000, address=A) == []


def test_pre_listing_boundary_exactly_24h_fires():
    fills = baseline() + rt("NEW", LIST_MS + 24 * H, "100", "100", tid=901)
    tags = tag_pre_listing(fills, {"NEW": LIST_MS}, 1000, address=A)
    assert [e["tid"] for e in tags[0].evidence] == [901]


def test_pre_listing_ignores_increase_legs():
    fills = baseline() + [mk("NEW", "B", "1", "100", "0", LIST_MS + H, tid=1),
                          mk("NEW", "B", "100", "100", "1", LIST_MS + 2 * H, tid=2)]
    assert tag_pre_listing(fills, {"NEW": LIST_MS}, 1000, address=A) == []


# -- pre_move -------------------------------------------------------------------------

def candle(T, c):
    return {"t": T - MIN + 1, "T": T, "c": str(c)}


def move_case(n, pct, *, long=True, at=10 * MIN, coin="ETH", sz="10", tid0=500):
    """``n`` opens at px 100, each followed by a 1m candle closing at ``100*(1±pct)``
    ``at`` ms after the open (T = open + at)."""
    fills, candles = [], []
    for i in range(n):
        t = NOW_MS - (i + 1) * DAY
        fills += rt(coin, t, sz, "100", long=long, tid=tid0 + i, hold=2 * H)
        px = 100 * (1 + pct) if long else 100 * (1 - pct)
        candles += [candle(t + MIN - 1, 100), candle(t + at, px)]
    return fills, {coin: candles}


def test_pre_move_fires_on_three_moves_at_5pct_boundary():
    fills, prices = move_case(3, 0.05)
    tags = tag_pre_move(fills, prices, address=A)
    assert len(tags) == 1 and tags[0].tag == "pre_move"
    ev = tags[0].evidence
    assert sorted(e["tid"] for e in ev) == [500, 501, 502]
    assert all(e["hash"] == f"0xh{e['tid']}" for e in ev)
    assert ev[0]["detail"]["move_pct"] == pytest.approx(0.05)
    assert ev[0]["detail"]["direction"] == "long"


def test_pre_move_negative_two_occurrences_or_small_move():
    assert tag_pre_move(*move_case(2, 0.10), address=A) == []
    assert tag_pre_move(*move_case(3, 0.0499), address=A) == []


def test_pre_move_window_boundary():
    # the close must be inside [open, open + 30 min): T = open + 30 min - 1 counts
    assert tag_pre_move(*move_case(3, 0.06, at=30 * MIN - 1), address=A) != []
    assert tag_pre_move(*move_case(3, 0.06, at=30 * MIN), address=A) == []


def test_pre_move_direction_short_and_adverse():
    assert tag_pre_move(*move_case(3, 0.05, long=False), address=A) != []
    fills, prices = move_case(3, 0.05, long=True)
    # a long followed by a 5% DROP is not favorable
    for c in prices["ETH"]:
        if c["c"] != "100":
            c["c"] = "95.0"
    assert tag_pre_move(fills, prices, address=A) == []


def test_pre_move_candles_before_open_ignored():
    fills, prices = move_case(3, 0.10, at=-MIN)  # the +10% close is BEFORE the open
    assert tag_pre_move(fills, prices, address=A) == []


def test_pre_move_caps_to_largest_opens():
    fills, prices = move_case(3, 0.10, sz="1")  # three small movers (notional 100)
    big, _ = move_case(2, 0.0, sz="1000", coin="BTC", tid0=600)  # two big non-movers
    assert tag_pre_move(fills + big, prices, address=A) != []
    assert tag_pre_move(fills + big, prices, address=A, max_opens=4) == []


# -- fresh_whale ----------------------------------------------------------------------

def whale_fills(big_at: int = 2, big_sz="500"):
    fills = []
    for i in range(7):
        sz = big_sz if i == big_at else "1"
        fills += rt("SOL", NOW_MS - 5 * DAY + i * H, sz, "100", tid=900 + i, hold=MIN)
    return fills


def test_fresh_whale_fires_at_50k_boundary():
    tags = tag_fresh_whale(NOW - timedelta(days=6), whale_fills(), address=A, now=NOW)
    assert len(tags) == 1 and tags[0].tag == "fresh_whale"
    ev = tags[0].evidence
    assert [e["tid"] for e in ev] == [902]
    assert ev[0]["hash"] == "0xh902" and ev[0]["detail"]["notional_usd"] == 50000.0
    assert ev[0]["detail"]["open_index"] == 3


def test_fresh_whale_negative_cases():
    young = NOW - timedelta(days=6)
    assert tag_fresh_whale(young, whale_fills(big_sz="499.99"), address=A, now=NOW) == []
    assert tag_fresh_whale(young, whale_fills(big_at=5), address=A, now=NOW) == []  # 6th open
    assert tag_fresh_whale(NOW - timedelta(days=7), whale_fills(), address=A, now=NOW) == []
    assert tag_fresh_whale(None, whale_fills(), address=A, now=NOW) == []
    just = NOW - timedelta(days=7) + timedelta(milliseconds=1)
    assert tag_fresh_whale(just, whale_fills(), address=A, now=NOW) != []


# -- persist ----------------------------------------------------------------------------

EV = {"tid": 1, "hash": "0xh1", "coin": "BTC", "time": NOW_MS, "detail": {"interval": "1m"}}


def test_persist_rejects_empty_or_incomplete_evidence(session):
    with pytest.raises(ValueError):
        persist_tags(session, A, [Tag(A, "pre_move", [])])
    with pytest.raises(ValueError):
        persist_tags(session, A, [Tag(A, "pre_move", [{**EV, "hash": None}])])
    with pytest.raises(ValueError):
        persist_tags(session, A, [Tag(A, "pre_move", [{k: v for k, v in EV.items()
                                                       if k != "tid"}])])
    with pytest.raises(ValueError):
        persist_tags(session, A, [Tag(B, "pre_move", [EV])])  # other wallet's tag
    assert session.scalars(select(TagRow)).all() == []


def test_persist_is_idempotent_and_removes_stale(session):
    persist_tags(session, A, [Tag(A, "pre_move", [EV]), Tag(A, "fresh_whale", [EV])])
    persist_tags(session, B, [Tag(B, "pre_move", [EV])])
    persist_tags(session, A, [Tag(A, "pre_move", [{**EV, "tid": 2, "hash": "0xh2"}])])
    session.commit()
    rows = {(r.address, r.tag): r.evidence_json for r in session.scalars(select(TagRow))}
    assert set(rows) == {(A, "pre_move"), (B, "pre_move")}  # stale fresh_whale removed
    assert [e["tid"] for e in rows[(A, "pre_move")]] == [2]  # replaced, not appended


# -- pipeline: tag_wallet over the stored coverage --------------------------------------

def router(listing: dict[str, int], price_fn, calls: list, pre_cov: dict | None = None):
    """candleSnapshot: 1d from 0 -> one empty pre-listing candle then the listing day;
    finer intervals -> candles over the range with close ``price_fn(coin, T)``.
    userFillsByTime -> ``pre_cov[user]`` (the fills before the coverage)."""
    ivs = {"1m": MIN, "5m": 5 * MIN, "15m": 15 * MIN, "1h": H, "1d": DAY}

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["type"] == "candleSnapshot":
            r = body["req"]
            iv = ivs[r["interval"]]
            if r["interval"] == "1d":
                t0 = listing.get(r["coin"])
                if t0 is None:
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[
                    {"t": t0 - DAY, "T": t0 - 1, "c": "1", "n": 0, "v": "0.0"},
                    {"t": t0, "T": t0 + DAY - 1, "c": "1", "n": 5, "v": "10"}])
            t = r["startTime"] // iv * iv
            out = []
            while t <= r["endTime"]:
                out.append({"t": t, "T": t + iv - 1, "c": str(price_fn(r["coin"], t + iv - 1)),
                            "i": r["interval"], "n": 1})
                t += iv
            return httpx.Response(200, json=out)
        if body["type"] == "userFillsByTime":
            return httpx.Response(200, json=(pre_cov or {}).get(body["user"], []))
        raise AssertionError(body)  # pragma: no cover

    return handler


def store(session, addr, fills, *, cov_start, capped=False):
    pipeline._ensure_wallet(session, addr)
    insert_fills(session, addr, fills)
    pipeline.kv_set_json(session, f"hl:cov:{addr}",
                         {"start": cov_start, "end": NOW_MS, "capped": capped})
    w = session.get(Wallet, addr)
    w.stats_json = {"backfill": {"history_partial": capped}}
    session.commit()


def pumpy(coin, T):
    return 100  # flat price: no pre_move


@respx.mock
async def test_tag_wallet_pre_listing_and_listing_cache(session):
    new_list = (NOW_MS - 2 * DAY) // DAY * DAY
    fills = baseline(t0=NOW_MS - 20 * DAY) + rt("NEW", new_list + H, "60", "100", tid=424)
    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    calls: list = []
    respx.post(INFO).mock(side_effect=router({"BTC": NOW_MS - 900 * DAY, "NEW": new_list},
                                             pumpy, calls))
    async with make_client() as c:
        res = await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
        listing_calls = [b for b in calls if b["type"] == "candleSnapshot"
                         and b["req"]["interval"] == "1d"]
        assert sorted(b["req"]["coin"] for b in listing_calls) == ["BTC", "NEW"]
        assert all(b["req"]["startTime"] == 0 for b in listing_calls)
        assert pipeline.kv_get_json(session, "hl:listing:NEW")["listing_ms"] == new_list
        n = len(calls)
        res2 = await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
        assert [b for b in calls[n:] if b["type"] == "candleSnapshot"] == []  # all cached
    assert set(res) == set(res2) == {"pre_listing"}
    row = session.get(TagRow, (A, "pre_listing"))
    assert [e["tid"] for e in row.evidence_json] == [424]
    assert row.evidence_json[0]["hash"] == "0xh424"


def test_listing_uses_first_traded_candle():
    rows = [{"t": 1, "T": 2, "n": 0, "v": "0.0"}, {"t": 10, "T": 11, "n": 3, "v": "5"}]
    assert insider.listing_from_candles(rows) == 10
    assert insider.listing_from_candles([{"t": 7, "T": 8}]) == 7  # no n/v: first candle
    assert insider.listing_from_candles([]) is None


@respx.mock
async def test_tag_wallet_pre_move_fetches_once_per_open_and_caches(session):
    opens = [NOW_MS - 3 * H - i * 6 * H for i in range(3)]  # recent: 1m candles
    fills = []
    for i, t in enumerate(opens):
        fills += rt("ETH", t, "10", "100", tid=600 + i, hold=2 * H)

    def price(coin, T):
        return 106 if any(o + 5 * MIN <= T < o + 20 * MIN for o in opens) else 100

    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    calls: list = []
    respx.post(INFO).mock(side_effect=router({"ETH": NOW_MS - 900 * DAY}, price, calls))
    async with make_client() as c:
        res = await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={"ETH": 4})
        fine = [b for b in calls if b["type"] == "candleSnapshot"
                and b["req"]["interval"] != "1d"]
        assert 1 <= len(fine) <= 3 and all(b["req"]["interval"] == "1m" for b in fine)
        n = len(calls)
        await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={"ETH": 4})
        assert [b for b in calls[n:] if b["type"] == "candleSnapshot"] == []
    assert "pre_move" in res
    ev = session.get(TagRow, (A, "pre_move")).evidence_json
    assert sorted(e["tid"] for e in ev) == [600, 601, 602]
    assert all(e["detail"]["interval"] == "1m" for e in ev)


def test_candle_interval_follows_hl_horizon():
    # 1m candles reach back ~3.4 days, 5m ~17 days, 15m ~51 days, 1h ~204 days
    assert insider.candle_interval(NOW_MS - 2 * DAY, NOW_MS)[0] == "1m"
    assert insider.candle_interval(NOW_MS - 10 * DAY, NOW_MS)[0] == "5m"
    assert insider.candle_interval(NOW_MS - 40 * DAY, NOW_MS)[0] == "15m"
    # 1h: only measurable if an hourly close falls inside the 30-min window
    assert insider.candle_interval(NOW_MS - 100 * DAY // H * H + 40 * MIN, NOW_MS)[0] == "1h"
    assert insider.candle_interval(NOW_MS - 100 * DAY // H * H + 10 * MIN, NOW_MS) is None
    assert insider.candle_interval(NOW_MS - 300 * DAY, NOW_MS) is None
    assert insider.candle_interval(NOW_MS - 10 * MIN, NOW_MS) is None  # window not over


@respx.mock
async def test_tag_wallet_fresh_whale_first_seen(session):
    fills = whale_fills()
    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    store(session, B, fills, cov_start=NOW_MS - 30 * DAY, capped=True)
    calls: list = []
    respx.post(INFO).mock(side_effect=router({"SOL": NOW_MS - 900 * DAY}, pumpy, calls))
    async with make_client() as c:
        ra = await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
        rb = await insider.tag_wallet(session, c, B, now=NOW, sz_decimals={})
    oldest = min(f["time"] for f in fills)
    assert session.get(Wallet, A).first_seen == datetime.fromtimestamp(oldest / 1000,
                                                                        timezone.utc)
    assert session.get(Wallet, B).first_seen is None  # capped: unknown, tag skipped
    assert "fresh_whale" in ra and "fresh_whale" not in rb
    # the pre-coverage check: HL had no fill of A before the coverage
    pre = [b for b in calls if b["type"] == "userFillsByTime"]
    assert len(pre) == 1 and pre[0]["user"] == A and pre[0]["startTime"] == 0


@respx.mock
async def test_tag_wallet_fresh_whale_older_fill_before_coverage(session):
    fills = whale_fills()
    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    older = mk("SOL", "B", "1", "100", "0", NOW_MS - 200 * DAY, tid=1)
    respx.post(INFO).mock(side_effect=router({"SOL": NOW_MS - 900 * DAY}, pumpy, [],
                                             pre_cov={A: [older]}))
    async with make_client() as c:
        res = await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
    assert "fresh_whale" not in res
    assert session.get(Wallet, A).first_seen == datetime.fromtimestamp(
        (NOW_MS - 200 * DAY) / 1000, timezone.utc)


@respx.mock
async def test_tag_wallet_removes_stale_tag(session):
    store(session, A, baseline(), cov_start=NOW_MS - 30 * DAY)
    persist_tags(session, A, [Tag(A, "pre_listing", [EV])])
    session.commit()
    respx.post(INFO).mock(side_effect=router({"BTC": NOW_MS - 900 * DAY}, pumpy, []))
    async with make_client() as c:
        res = await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
    assert res == {}
    assert session.scalars(select(TagRow)).all() == []


def test_meta_non_dict_raises():
    async def go():
        with respx.mock:
            respx.post(INFO).mock(return_value=httpx.Response(200, json=[1, 2]))
            async with make_client() as c:
                await c.meta()
    import asyncio
    with pytest.raises(HLError):
        asyncio.run(go())


# -- fix round 1 ------------------------------------------------------------------------

def failing_router(base, *, what: str, mode: str):
    """Wrap ``base``: requests matching ``what`` fail with ``mode`` (an HTTP status as
    str, or "net" for a network error). ``what``: "candles" (non-1d candleSnapshot),
    "listing" (1d candleSnapshot) or "precov" (userFillsByTime)."""
    def handler(request):
        b = json.loads(request.content)
        hit = (
            (what == "candles" and b["type"] == "candleSnapshot"
             and b["req"]["interval"] != "1d")
            or (what == "listing" and b["type"] == "candleSnapshot"
                and b["req"]["interval"] == "1d")
            or (what == "precov" and b["type"] == "userFillsByTime"))
        if hit:
            if mode == "net":
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(int(mode))
        return base(request)
    return handler


def _rows(session):
    session.expire_all()
    return {r.tag: r.evidence_json for r in session.scalars(select(TagRow))}


@pytest.mark.parametrize("mode", ["500", "net"])
@respx.mock
async def test_pre_move_tag_survives_candle_fetch_failure(session, mode, caplog):
    fills, _ = move_case(3, 0.10)
    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    persist_tags(session, A, [Tag(A, "pre_move", [EV]), Tag(A, "fresh_whale", [EV])])
    session.commit()
    respx.post(INFO).mock(side_effect=failing_router(
        router({"ETH": NOW_MS - 900 * DAY}, pumpy, []), what="candles", mode=mode))
    async with make_client(max_attempts=2) as c:
        res = await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
    rows = _rows(session)
    assert rows["pre_move"] == [EV]  # incomplete inputs: the tag is kept
    assert "fresh_whale" not in rows  # a complete rule still removes its stale tag
    assert "pre_move" not in res
    assert any("pre_move" in r.getMessage() and A in r.getMessage()
               and r.levelname == "WARNING" for r in caplog.records)


@pytest.mark.parametrize("mode", ["500", "net"])
@respx.mock
async def test_pre_listing_tag_survives_listing_fetch_failure(session, mode):
    store(session, A, baseline(), cov_start=NOW_MS - 30 * DAY)
    persist_tags(session, A, [Tag(A, "pre_listing", [EV])])
    session.commit()
    respx.post(INFO).mock(side_effect=failing_router(
        router({"BTC": NOW_MS - 900 * DAY}, pumpy, []), what="listing", mode=mode))
    async with make_client(max_attempts=2) as c:
        await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
    assert _rows(session) == {"pre_listing": [EV]}
    assert pipeline.kv_get_json(session, "hl:listing:BTC") is None  # failure not cached


@pytest.mark.parametrize("mode", ["500", "net"])
@respx.mock
async def test_fresh_whale_survives_precoverage_failure(session, mode, caplog):
    fills = whale_fills(big_sz="1")  # would no longer fire
    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    known = datetime(2026, 11, 14, 1, 2, 3, tzinfo=timezone.utc)
    session.get(Wallet, A).first_seen = known
    persist_tags(session, A, [Tag(A, "fresh_whale", [EV])])
    session.commit()
    respx.post(INFO).mock(side_effect=failing_router(
        router({"SOL": NOW_MS - 900 * DAY}, pumpy, []), what="precov", mode=mode))
    async with make_client(max_attempts=2) as c:
        await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
    assert _rows(session) == {"fresh_whale": [EV]}
    assert session.get(Wallet, A).first_seen == known  # not overwritten
    assert any("fresh_whale" in r.getMessage() and r.levelname == "WARNING"
               for r in caplog.records)


@respx.mock
async def test_rate_limit_midway_leaves_every_tag_unchanged(session):
    from hades_hl.client import HLRateLimited
    fills, _ = move_case(3, 0.10)
    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    known = datetime(2026, 11, 14, tzinfo=timezone.utc)
    session.get(Wallet, A).first_seen = known
    persist_tags(session, A, [Tag(A, k, [EV]) for k in insider.TAG_KINDS])
    session.commit()
    respx.post(INFO).mock(side_effect=failing_router(
        router({"ETH": NOW_MS - 900 * DAY}, pumpy, []), what="candles", mode="429"))
    async with make_client(max_attempts=1) as c:
        with pytest.raises(HLRateLimited):
            await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
    session.rollback()
    assert _rows(session) == {k: [EV] for k in insider.TAG_KINDS}
    assert session.get(Wallet, A).first_seen == known


# P38: pre_move only considers opens with notional >= the median open notional

def test_pre_move_open_at_median_counts():
    movers, prices = move_case(3, 0.10, sz="10")  # notional 1000
    flat, _ = move_case(4, 0.0, sz="10", coin="BTC", tid0=600)  # 1000, no prices
    tags = tag_pre_move(movers + flat, prices, address=A)
    assert len(tags) == 1 and len(tags[0].evidence) == 3
    assert tags[0].evidence[0]["detail"]["median_open_notional_usd"] == 1000.0


def test_pre_move_open_below_median_ignored():
    movers, prices = move_case(3, 0.10, sz="9.99")  # 999 < median 1000
    flat, _ = move_case(4, 0.0, sz="10", coin="BTC", tid0=600)
    assert tag_pre_move(movers + flat, prices, address=A) == []


def test_pre_move_floor_applies_before_cap():
    movers, _ = move_case(3, 0.10, sz="10")  # 1000 each
    small, _ = move_case(3, 0.0, sz="5", coin="BTC", tid0=600)  # 500 each
    legs = open_legs(A, movers + small)  # median 750
    sel = insider.select_pre_move_opens(legs, max_opens=200)
    assert sorted(lg.fill["tid"] for lg in sel) == [500, 501, 502]
    assert len(insider.select_pre_move_opens(legs, max_opens=2)) == 2


# P40: distinct move events

def pump_case(starts, *, step=5 * MIN, n=3, tid0=700):
    """For each start, ``n`` quick opens ``step`` apart (each closed after 1 min), and
    one +10% close 20 min after the start."""
    fills, candles = [], []
    for k, s in enumerate(starts):
        for j in range(n):
            t = s + j * step
            fills += rt("ETH", t, "10", "100", tid=tid0 + 10 * k + j, hold=MIN)
        candles += [candle(s + MIN - 1, 100), candle(s + 20 * MIN + 1, 110)]
    return fills, {"ETH": candles}


def test_pre_move_three_opens_in_one_pump_do_not_fire():
    fills, prices = pump_case([NOW_MS - DAY])
    assert tag_pre_move(fills, prices, address=A) == []


def test_pre_move_distinct_events_fire_and_mark_event_index():
    fills, prices = pump_case([NOW_MS - 3 * DAY, NOW_MS - 2 * DAY, NOW_MS - DAY], n=2)
    tags = tag_pre_move(fills, prices, address=A)
    assert len(tags) == 1
    ev = tags[0].evidence
    assert len(ev) == 6  # every row kept
    assert [e["detail"]["event_index"] for e in ev] == [1, 1, 2, 2, 3, 3]


def test_pre_move_event_window_boundary():
    t0 = NOW_MS - DAY
    # exactly 30 min after the previous qualifying open: a new event
    fills, candles = [], []
    for j in range(3):
        t = t0 + j * 30 * MIN
        fills += rt("ETH", t, "10", "100", tid=800 + j, hold=MIN)
        candles += [candle(t + 10 * MIN, 110)]
    tags = tag_pre_move(fills, {"ETH": candles}, address=A)
    assert [e["detail"]["event_index"] for e in tags[0].evidence] == [1, 2, 3]
    # 30 min - 1 ms apart: one chained event
    fills, candles = [], []
    for j in range(3):
        t = t0 + j * (30 * MIN - 1)
        fills += rt("ETH", t, "10", "100", tid=810 + j, hold=MIN)
        candles += [candle(t + 10 * MIN, 110)]
    assert tag_pre_move(fills, {"ETH": candles}, address=A) == []


# M1

def test_pre_listing_evidence_has_listing_resolution():
    fills = baseline() + rt("NEW", LIST_MS + H, "50", "100", tid=777)
    ev = tag_pre_listing(fills, {"NEW": LIST_MS}, None, address=A)[0].evidence[0]
    assert ev["detail"]["listing_resolution"] == "1d"


# M4

@respx.mock
async def test_empty_candle_slices_not_cached(session):
    from hades_hl.db import Kv
    fills, _ = move_case(3, 0.10)
    store(session, A, fills, cov_start=NOW_MS - 30 * DAY)
    base = router({"ETH": NOW_MS - 900 * DAY}, pumpy, [])

    def handler(request):
        b = json.loads(request.content)
        if b["type"] == "candleSnapshot" and b["req"]["interval"] != "1d":
            return httpx.Response(200, json=[])
        return base(request)

    respx.post(INFO).mock(side_effect=handler)
    async with make_client() as c:
        await insider.tag_wallet(session, c, A, now=NOW, sz_decimals={})
    keys = [k for k in session.scalars(select(Kv.key)) if k.startswith("hl:candles:")]
    assert keys == []


# M5

def test_persist_requires_interval_for_pre_move(session):
    with pytest.raises(ValueError):
        persist_tags(session, A, [Tag(A, "pre_move", [{**EV, "detail": {}}])])
    with pytest.raises(ValueError):
        persist_tags(session, A, [Tag(A, "pre_move", [{**EV, "detail": {"interval": None}}])])
    persist_tags(session, A, [Tag(A, "pre_listing", [{**EV, "detail": {}}])])  # not needed


# M6: zero-hash fills are not evidence

ZERO = "0x" + "0" * 64


def test_persist_rejects_zero_hash(session):
    with pytest.raises(ValueError):
        persist_tags(session, A, [Tag(A, "pre_listing", [{**EV, "hash": ZERO}])])


def _zero(fills, tid):
    for f in fills:
        if f["tid"] == tid:
            f["hash"] = ZERO
    return fills


def test_zero_hash_open_drops_out_of_pre_move():
    fills, prices = move_case(3, 0.10)
    assert tag_pre_move(fills, prices, address=A) != []
    assert tag_pre_move(_zero(fills, 501), prices, address=A) == []


def test_zero_hash_open_drops_out_of_fresh_whale_and_pre_listing():
    young = NOW - timedelta(days=6)
    assert tag_fresh_whale(young, _zero(whale_fills(), 902), address=A, now=NOW) == []
    fills = _zero(baseline() + rt("NEW", LIST_MS + H, "50", "100", tid=777), 777)
    assert tag_pre_listing(fills, {"NEW": LIST_MS}, None, address=A) == []


@respx.mock
async def test_cached_empty_candle_slice_is_a_cache_miss(session):
    t = NOW_MS - 3 * H  # recent: 1m candles
    legs = open_legs(A, rt("ETH", t, "10", "100", tid=990, hold=2 * H))
    lo, hi = t // MIN * MIN, t + 30 * MIN
    # an empty slice in kv (e.g. written by an older build) must not count as cached
    pipeline.kv_set_json(session, insider.KV_CANDLES.format("ETH", "1m", lo, hi), [])
    calls: list = []
    respx.post(INFO).mock(side_effect=router({}, pumpy, calls))
    async with make_client() as c:
        load = await insider.load_pre_move_candles(session, c, legs, NOW_MS)
    assert load.cached == 0 and load.fetched == 1
    assert [b["type"] for b in calls] == ["candleSnapshot"]
    assert load.prices["ETH"]
