"""Final-review fix wave: C1 truncation-safe anchors, I1 real-buy filter, I2 window,
I3 top-score webhook sync, I4 funder on busy wallets, I6 unpriced cost, minors."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from hades import cli, insider, pipeline, realtime
from hades.api.main import create_app
from hades.config import Settings, get_settings
from hades.db import Swap, Token, Wallet, WalletStats, WalletTag, Watchlist, init_db, make_engine
from hades.scoring import wallet_stats
from hades.sources.helius import SIG_PAGE_LIMIT, HeliusClient, SigList, sigs_truncated

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())
MINT = "MeMeMint1111111111111111111111111111111pump"
POOL = "PooLBest11111111111111111111111111111111111"
DEPLOYER = "DePLoyer1111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
KEY = "k"
H = {"X-Hades-Key": KEY}
SECRET = "whsec-test"
API = "https://api.helius.xyz"
RPC = "https://mainnet.helius-rpc.com/"
ADDR = "A" * 32 + "a1"
SYSTEM = "11111111111111111111111111111111"


def _settings(**kw) -> Settings:
    base = dict(HELIUS_API_KEY="test", DATABASE_URL="sqlite://", HADES_API_KEY=KEY,
                HELIUS_WEBHOOK_SECRET=SECRET, HELIUS_WEBHOOK_ID="", PUBLIC_WEBHOOK_URL="",
                SIG_SCAN_MAX_PAGES=2, _env_file=None)
    base.update(kw)
    return Settings(**base)


def swap_tx(sig, slot, wallet, side="buy", *, mint=MINT, sol=1.0, tokens=1000.0):
    lamports = int(sol * 1_000_000_000)
    if side == "buy":
        native = [{"fromUserAccount": wallet, "toUserAccount": POOL, "amount": lamports}]
        tt = [{"fromUserAccount": POOL, "toUserAccount": wallet, "tokenAmount": tokens, "mint": mint}]
        change = -lamports - 5000
    else:
        native = [{"fromUserAccount": POOL, "toUserAccount": wallet, "amount": lamports}]
        tt = [{"fromUserAccount": wallet, "toUserAccount": POOL, "tokenAmount": tokens, "mint": mint}]
        change = lamports - 5000
    return {
        "signature": sig, "slot": slot, "timestamp": NOW_TS - 3600 + slot % 1000, "type": "SWAP",
        "source": "RAYDIUM", "fee": 5000, "feePayer": wallet, "transactionError": None,
        "nativeTransfers": native, "tokenTransfers": tt,
        "accountData": [{"account": wallet, "nativeBalanceChange": change, "tokenBalanceChanges": []}],
    }


def plain_tx(sig, slot, fee_payer):
    return {"signature": sig, "slot": slot, "timestamp": NOW_TS - 7000, "type": "UNKNOWN",
            "fee": 5000, "feePayer": fee_payer, "transactionError": None,
            "nativeTransfers": [], "tokenTransfers": [], "accountData": []}


class FakeHelius:
    """sigs: address -> SigList/list (oldest first); txs: signature -> tx."""

    def __init__(self, sigs=None, txs=None, funders=None):
        self.sigs = sigs or {}
        self.txs = txs or {}
        self.funders = funders or {}

    async def signatures_oldest(self, address, max_pages):
        return self.sigs.get(address, SigList())

    async def parse_transactions(self, signatures):
        return [self.txs[s] for s in signatures if s in self.txs]

    async def first_incoming_sol(self, address, max_pages=5):
        return self.funders.get(address)


class FakeDex:
    def __init__(self, pairs):
        self.pairs = pairs

    async def token_pairs(self, mints):
        return [p for p in self.pairs if p["baseToken"]["address"] in set(mints)]


def pair():
    return {"chainId": "solana", "pairAddress": POOL,
            "baseToken": {"address": MINT, "name": "Meme", "symbol": "MEME"},
            "quoteToken": {"address": WSOL, "symbol": "SOL"}, "priceNative": "0.000002",
            "liquidity": {"usd": 50_000}, "fdv": 2_000_000, "priceChange": {"h24": 500.0},
            "pairCreatedAt": NOW_TS * 1000 - 86_400_000}


# -- C1: truncation signal -----------------------------------------------------


def rpc_pages(pages):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        sigs = pages[len(calls) - 1] if len(calls) <= len(pages) else []
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                         "result": [{"signature": s, "slot": 1} for s in sigs]})
    return handler


@respx.mock
async def test_signatures_oldest_reports_truncation():
    full = [[f"p{j}-{i}" for i in range(SIG_PAGE_LIMIT)] for j in range(3)]
    respx.post(RPC).mock(side_effect=rpc_pages(full))
    async with HeliusClient("key", rps=1000) as c:
        sigs = await c.signatures_oldest(ADDR, max_pages=2)
    assert sigs.truncated is True and len(sigs) == 2000

    respx.post(RPC).mock(side_effect=rpc_pages([full[0], ["short"]]))
    async with HeliusClient("key", rps=1000) as c:
        sigs = await c.signatures_oldest(ADDR, max_pages=2)
    assert sigs.truncated is False and sigs[0] == "short"


def test_sigs_truncated_fallback_for_plain_lists():
    assert sigs_truncated(SigList(["a"], truncated=True), 30) is True
    assert sigs_truncated(SigList(["a"] * 2000, truncated=False), 2) is False
    assert sigs_truncated(["a"] * 2000, 2) is True  # plain list: length heuristic
    assert sigs_truncated(["a"] * 1999, 2) is False


async def test_seed_truncated_mint_scan_nulls_deployer(session):
    helius = FakeHelius(
        sigs={MINT: SigList(["m-oldest-seen"], truncated=True), POOL: SigList(["p-init"])},
        txs={"m-oldest-seen": plain_tx("m-oldest-seen", 900, "NotTheDeployer"),
             "p-init": plain_tx("p-init", 1005, DEPLOYER)},
    )
    session.add(Token(mint=MINT, deployer="StaleDeployer"))
    session.commit()
    tok = await pipeline.seed_token(session, helius, FakeDex([pair()]), MINT, _settings(), now=NOW)
    assert tok.deployer is None
    assert tok.pool_created_slot == 1005
    assert tok.history_truncated is True


async def test_seed_truncated_pool_scan_nulls_anchor(session):
    helius = FakeHelius(
        sigs={MINT: SigList(["m-create"]), POOL: SigList(["p-mid"], truncated=True)},
        txs={"m-create": plain_tx("m-create", 1000, DEPLOYER), "p-mid": plain_tx("p-mid", 5000, "X")},
    )
    session.add(Token(mint=MINT, pool_created_slot=4242, pool_created_at=NOW))
    session.commit()
    tok = await pipeline.seed_token(session, helius, FakeDex([pair()]), MINT, _settings(), now=NOW)
    assert tok.deployer == DEPLOYER
    assert tok.pool_created_slot is None and tok.pool_created_at is None
    assert tok.history_truncated is True


async def test_seed_untruncated_clears_flag(session):
    helius = FakeHelius(
        sigs={MINT: SigList(["m-create"]), POOL: SigList(["p-init"])},
        txs={"m-create": plain_tx("m-create", 1000, DEPLOYER), "p-init": plain_tx("p-init", 1005, DEPLOYER)},
    )
    session.add(Token(mint=MINT, history_truncated=True))
    session.commit()
    tok = await pipeline.seed_token(session, helius, FakeDex([pair()]), MINT, _settings(), now=NOW)
    assert tok.history_truncated is False and tok.deployer == DEPLOYER and tok.pool_created_slot == 1005


async def test_early_buyers_reports_truncation_and_flags_token(session):
    tok = Token(mint=MINT, pool_address=POOL, pool_created_slot=100)
    session.add(tok)
    session.commit()
    txs = [swap_tx("s1", 5001, "WalletA"), swap_tx("s2", 5002, "WalletB")]
    helius = FakeHelius(sigs={POOL: SigList([t["signature"] for t in txs], truncated=True)},
                        txs={t["signature"]: t for t in txs})
    buyers = await pipeline.early_buyers(session, helius, tok, 10, sig_max_pages=1)
    assert list(buyers) == ["WalletA", "WalletB"]
    assert buyers.truncated is True
    assert session.get(Token, MINT).history_truncated is True

    helius.sigs[POOL] = SigList([t["signature"] for t in txs])
    buyers = await pipeline.early_buyers(session, helius, tok, 10, sig_max_pages=1)
    assert buyers.truncated is False


def _buy(sig, wallet, slot, sol=1.0, mint=MINT):
    return Swap(signature=sig, wallet=wallet, mint=mint, slot=slot, block_time=NOW, side="buy",
                token_amount=1000.0, sol_amount=sol, source="backfill")


async def test_tag_token_skips_sniper_and_deployer_link_with_null_anchors(session):
    tok = Token(mint=MINT, deployer=None, pool_created_slot=None, history_truncated=True)
    session.add(tok)
    session.add_all([_buy("b1", "W1", 1000), _buy("b2", "W2", 1000), _buy("b3", "W3", 1000)])
    # a stale sniper row from an earlier (untruncated-looking) run must go away
    session.add(WalletTag(address="W1", tag="sniper", mint=MINT,
                          evidence_json={"signatures": ["b1"], "slots": [1000]}))
    session.commit()
    funders = {w: ("F", f"f-{w}", 10) for w in ("W1", "W2", "W3")}
    helius = FakeHelius(funders=funders)
    tags = await insider.tag_token(session, helius, tok)
    kinds = {t.tag for t in tags}
    assert "sniper" not in kinds and "deployer_link" not in kinds
    assert kinds == {"bundle"}  # bundle still runs on the earliest observed buyers
    rows = session.scalars(select(WalletTag.tag).where(WalletTag.mint == MINT)).all()
    assert set(rows) == {"bundle"}


async def test_tag_token_truncated_flag_skips_even_with_stale_anchor(session):
    tok = Token(mint=MINT, deployer="W1", pool_created_slot=1000, history_truncated=True)
    session.add(tok)
    session.add(_buy("b1", "W1", 1000))
    session.commit()
    tags = await insider.tag_token(session, FakeHelius(), tok)
    assert tags == []


# -- I1: one definition of "buy" -----------------------------------------------


def _seed_buy_rows(session):
    session.add(Token(mint=MINT, deployer="W_RENT", pool_created_slot=1000, is_runner=True))
    session.add_all([
        _buy("real", "W_REAL", 1000, sol=1.0),
        _buy("rent", "W_RENT", 1000, sol=None),   # rent-only receipt: not SOL-quoted
        _buy("zero", "W_ZERO", 1001, sol=0.0),     # zero SOL: not a real buy
    ])
    session.commit()


async def test_rent_only_and_zero_sol_buys_never_tagged(session):
    _seed_buy_rows(session)
    tok = session.get(Token, MINT)
    tags = await insider.tag_token(session, FakeHelius(), tok)
    tagged = {t.address for t in tags}
    assert "W_REAL" in tagged  # sniper
    assert not tagged & {"W_RENT", "W_ZERO"}


def test_repeat_early_ignores_non_real_buys(session):
    mints = [f"Run{i}" + "1" * 30 for i in range(3)]
    for i, m in enumerate(mints):
        session.add(Token(mint=m, is_runner=True))
        session.add(_buy(f"r{i}", "W_RENT", 10, sol=None, mint=m))
        session.add(_buy(f"z{i}", "W_ZERO", 10, sol=0.0, mint=m))
        session.add(_buy(f"g{i}", "W_GOOD", 11, sol=0.5, mint=m))
    session.commit()
    assert [t.address for t in insider.tag_repeat_early(session)] == ["W_GOOD"]


def test_api_early_buyers_exclude_non_real_buys():
    app = create_app(_settings())
    with Session(app.state.engine) as s:
        _seed_buy_rows(s)
    r = TestClient(app).get(f"/api/tokens/{MINT}", headers=H)
    assert r.status_code == 200
    assert [b["address"] for b in r.json()["early_buyers"]] == ["W_REAL"]
    app.state.engine.dispose()


async def test_pipeline_early_buyers_exclude_zero_sol(session):
    tok = Token(mint=MINT, pool_address=POOL)
    session.add(tok)
    session.commit()
    t0 = swap_tx("s0", 100, "W_ZERO", sol=0.0)
    t1 = swap_tx("s1", 101, "W_REAL")
    helius = FakeHelius(sigs={POOL: SigList(["s0", "s1"])}, txs={"s0": t0, "s1": t1})
    assert list(await pipeline.early_buyers(session, helius, tok, 5, sig_max_pages=1)) == ["W_REAL"]


# -- C1 API + I2 + I6 payloads ---------------------------------------------------


def test_token_detail_exposes_history_truncated():
    app = create_app(_settings())
    with Session(app.state.engine) as s:
        s.add(Token(mint=MINT, history_truncated=True))
        s.commit()
    body = TestClient(app).get(f"/api/tokens/{MINT}", headers=H).json()
    assert body["token"]["history_truncated"] is True
    app.state.engine.dispose()


def test_wallet_detail_window_bot_and_unpriced():
    app = create_app(_settings())
    with Session(app.state.engine) as s:
        s.add(Wallet(address=ADDR, is_bot=True))
        s.add(WalletStats(address=ADDR, window_days=30, score=None, tokens_closed=1,
                          unpriced_cost_sol=1.5))
        s.add(_buy("o1", ADDR, 10, sol=1.5))  # open position, never sold
        s.commit()
    body = TestClient(app).get(f"/api/wallets/{ADDR}", headers=H).json()
    assert body["window_days"] == 30
    assert body["is_bot"] is True
    assert body["min_tokens_closed"] == 5
    assert body["stats"]["unpriced_cost_sol"] == 1.5
    assert body["positions"][0]["unpriced_cost_sol"] == 1.5
    app.state.engine.dispose()


def test_wallet_detail_window_default_without_stats():
    app = create_app(_settings())
    with Session(app.state.engine) as s:
        s.add(Wallet(address=ADDR))
        s.commit()
    body = TestClient(app).get(f"/api/wallets/{ADDR}", headers=H).json()
    assert body["window_days"] == 30 and body["stats"] is None and body["is_bot"] is False
    app.state.engine.dispose()


def test_wallet_stats_unpriced_cost():
    swaps = [_buy("a", ADDR, 1, sol=2.0, mint="PRICED"), _buy("b", ADDR, 2, sol=3.0, mint="RUGGED")]
    st = wallet_stats(ADDR, swaps, {"PRICED": 0.001}, NOW + timedelta(minutes=1))
    assert st.unpriced_cost_sol == 3.0


def test_init_db_migrates_token_and_stats_columns():
    eng = make_engine("sqlite:///:memory:")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE tokens (mint VARCHAR PRIMARY KEY, symbol VARCHAR)"))
        c.execute(text("INSERT INTO tokens (mint) VALUES ('M')"))
        c.execute(text("CREATE TABLE wallet_stats (address VARCHAR PRIMARY KEY, score FLOAT)"))
        c.execute(text("INSERT INTO wallet_stats (address) VALUES ('A')"))
    init_db(eng)
    init_db(eng)
    with eng.connect() as c:
        assert c.execute(text("SELECT history_truncated FROM tokens WHERE mint='M'")).scalar() in (0, False)
        assert c.execute(text("SELECT unpriced_cost_sol FROM wallet_stats WHERE address='A'")).scalar() == 0
        c.execute(text("INSERT INTO tokens (mint) VALUES ('N')"))  # NOT NULL default applies
        assert c.execute(text("SELECT history_truncated FROM tokens WHERE mint='N'")).scalar() in (0, False)
    eng.dispose()


# -- I3: top-score wallets subscribed ---------------------------------------------


def _top_app(**kw):
    app = create_app(_settings(PUBLIC_WEBHOOK_URL="https://x.example", **kw))
    app.state.sync_lock = asyncio.Lock()
    return app


def _capture_upsert(monkeypatch):
    calls = []

    async def fake(self, addresses, url, secret, webhook_id=None):
        calls.append(list(addresses))
        return "wh-1"

    monkeypatch.setattr(HeliusClient, "upsert_webhook", fake)
    return calls


def test_sync_union_watchlist_and_top_scores_capped(monkeypatch):
    calls = _capture_upsert(monkeypatch)
    app = _top_app(WEBHOOK_TOP_SCORE_MAX=2)
    W = lambda c: c * 32 + "zz"  # noqa: E731
    with Session(app.state.engine) as s:
        s.add(Watchlist(address=W("w")))
        s.add_all([
            WalletStats(address=W("a"), score=95.0), WalletStats(address=W("b"), score=80.0),
            WalletStats(address=W("c"), score=75.0),  # cut by the cap
            WalletStats(address=W("d"), score=50.0),  # below ALERT_MIN_SCORE
            WalletStats(address=W("e"), score=99.0), Wallet(address=W("e"), is_bot=True),  # bot
        ])
        s.commit()
    assert asyncio.run(realtime.sync_webhook(app)) == "ok"
    assert calls == [sorted([W("w"), W("a"), W("b")])]


def test_sync_top_scores_only_when_watchlist_empty(monkeypatch):
    calls = _capture_upsert(monkeypatch)
    app = _top_app()
    with Session(app.state.engine) as s:
        s.add(WalletStats(address=ADDR, score=90.0))
        s.commit()
    assert asyncio.run(realtime.sync_webhook(app)) == "ok"
    assert calls == [[ADDR]]


def test_sync_empty_union_deletes(monkeypatch):
    calls = _capture_upsert(monkeypatch)
    deleted = []

    async def fake_delete(self, wid):
        deleted.append(wid)

    async def fake_find(self, url):
        return "wh-orphan"

    monkeypatch.setattr(HeliusClient, "delete_webhook", fake_delete)
    monkeypatch.setattr(HeliusClient, "find_webhook", fake_find)
    app = _top_app()
    with Session(app.state.engine) as s:
        s.add(WalletStats(address=ADDR, score=60.0))  # below threshold: not in the union
        s.commit()
    assert asyncio.run(realtime.sync_webhook(app)) == "ok"
    assert calls == [] and deleted == ["wh-orphan"]


runner = CliRunner()


def _cli_db(tmp_path, monkeypatch, public_url):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("PUBLIC_WEBHOOK_URL", public_url)
    get_settings.cache_clear()

    class Dex:
        async def token_pairs(self, mints):
            return []

        async def aclose(self):
            pass

    monkeypatch.setattr(cli, "make_dex", lambda s: Dex())
    init_db(make_engine(url))


def test_rescore_triggers_sync_when_public_url_set(tmp_path, monkeypatch):
    _cli_db(tmp_path, monkeypatch, "https://x.example")
    calls = []

    async def fake_sync(app):
        calls.append(app.state.settings.PUBLIC_WEBHOOK_URL)
        return "ok"

    monkeypatch.setattr(realtime, "sync_webhook", fake_sync)
    res = runner.invoke(cli.app, ["rescore"])
    get_settings.cache_clear()
    assert res.exit_code == 0, res.output
    assert calls == ["https://x.example"]
    assert "webhook sync: ok" in res.output


def test_rescore_skips_sync_without_public_url(tmp_path, monkeypatch):
    _cli_db(tmp_path, monkeypatch, "")

    async def boom(app):
        raise AssertionError("must not sync")

    monkeypatch.setattr(realtime, "sync_webhook", boom)
    res = runner.invoke(cli.app, ["rescore"])
    get_settings.cache_clear()
    assert res.exit_code == 0, res.output
    assert "webhook sync" not in res.output


# -- I4: funder on busy wallets ----------------------------------------------------


async def _first_funder(sigs, txs):
    client = HeliusClient("key", rps=1000)

    async def fake_sigs(address, max_pages):
        return sigs

    async def fake_parse(signatures):
        return [txs[s] for s in signatures if s in txs]

    client.signatures_oldest = fake_sigs
    client.parse_transactions = fake_parse
    try:
        return await client.first_incoming_sol(ADDR, max_pages=3)
    finally:
        await client.aclose()


def _transfer(sig, slot, frm, *, instructions=None, **extra):
    tx = {"signature": sig, "slot": slot, "transactionError": None,
          "nativeTransfers": [{"fromUserAccount": frm, "toUserAccount": ADDR, "amount": 10**9}]}
    if instructions is not None:
        tx["instructions"] = instructions
    tx.update(extra)
    return tx


async def test_first_incoming_sol_none_when_truncated():
    txs = {"t1": _transfer("t1", 1, "FUNDER")}
    assert await _first_funder(SigList(["t1"], truncated=True), txs) is None
    assert await _first_funder(SigList(["t1"]), txs) == ("FUNDER", "t1", 1)


async def test_first_incoming_sol_skips_swap_proceeds():
    sell = _transfer("sell", 1, "PoolVault", type="SWAP",
                     tokenTransfers=[{"fromUserAccount": ADDR, "toUserAccount": "PoolVault",
                                      "tokenAmount": 5, "mint": MINT}],
                     instructions=[{"programId": "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"}])
    unwrap = _transfer("unwrap", 2, "WsolAta",
                       instructions=[{"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"}])
    fund = _transfer("fund", 3, "REALFUNDER", instructions=[{"programId": SYSTEM}])
    txs = {t["signature"]: t for t in (sell, unwrap, fund)}
    assert await _first_funder(SigList(["sell", "unwrap", "fund"]), txs) == ("REALFUNDER", "fund", 3)


# -- minors ------------------------------------------------------------------------


def test_webhook_rejects_oversized_body_413():
    app = create_app(_settings())
    c = TestClient(app)
    big = b"[" + b" " * (5 * 1024 * 1024 + 10) + b"]"
    r = c.post("/webhook/helius", content=big,
               headers={"Authorization": SECRET, "Content-Type": "application/json"})
    assert r.status_code == 413
    app.state.engine.dispose()


def test_cli_does_not_show_locals_in_tracebacks():
    assert cli.app.pretty_exceptions_show_locals is False
