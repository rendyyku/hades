"""CLI wiring tests (fake clients injected via the cli client factories)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from hades import cli
from hades.config import get_settings
from hades.db import Swap, Token, init_db, make_engine
from hades.sources.helius import HeliusError

runner = CliRunner()
MINT = "MeMeMint1111111111111111111111111111111pump"
POOL = "PooL1111111111111111111111111111111111111111"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()


def buy_tx(sig, slot, wallet):
    return {
        "signature": sig, "slot": slot, "timestamp": 1_763_500_000 + slot, "type": "SWAP", "source": "RAYDIUM",
        "fee": 5000, "feePayer": wallet, "transactionError": None,
        "nativeTransfers": [{"fromUserAccount": wallet, "toUserAccount": POOL, "amount": 1_000_000_000}],
        "tokenTransfers": [{"fromUserAccount": POOL, "toUserAccount": wallet, "tokenAmount": 10.0, "mint": MINT}],
        "accountData": [{"account": wallet, "nativeBalanceChange": -1_000_005_000}],
    }


class FakeHelius:
    closed = False

    def __init__(self):
        self.txs = {"p1": buy_tx("p1", 10, "WalletA"), "p2": buy_tx("p2", 11, "WalletB")}

    async def signatures_oldest(self, address, max_pages):
        return ["p1", "p2"] if address == POOL else []

    async def parse_transactions(self, sigs):
        return [self.txs[s] for s in sigs if s in self.txs]

    async def address_transactions(self, address, *, tx_type="SWAP", before=None, max_pages):
        if address == "WalletA":
            raise HeliusError("down")
        return [buy_tx("w1", 50, address)]

    async def aclose(self):
        FakeHelius.closed = True


class FakeDex:
    closed = False

    async def token_pairs(self, mints):
        return []

    async def boosted_tokens(self):
        return []

    async def aclose(self):
        FakeDex.closed = True


def use_db(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("EARLY_BUYERS_N", "5")
    get_settings.cache_clear()
    monkeypatch.setattr(cli, "make_helius", lambda s: FakeHelius())
    monkeypatch.setattr(cli, "make_dex", lambda s: FakeDex())
    return url


def test_help_lists_commands():
    out = runner.invoke(cli.app, ["--help"]).output
    for cmd in ("init-db", "seed", "discover", "backfill", "rescore", "serve"):
        assert cmd in out


def test_init_db_creates_tables(tmp_path, monkeypatch):
    url = use_db(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["init-db"])
    assert res.exit_code == 0, res.output
    assert "swaps" in inspect(make_engine(url)).get_table_names()


def test_backfill_token_continues_on_helius_error(tmp_path, monkeypatch):
    url = use_db(tmp_path, monkeypatch)
    eng = make_engine(url)
    init_db(eng)
    with Session(eng) as s:
        s.add(Token(mint=MINT, pool_address=POOL, pool_created_at=datetime.now(timezone.utc)))
        s.commit()
    FakeHelius.closed = False

    res = runner.invoke(cli.app, ["backfill", "--token", MINT])

    assert res.exit_code == 0, res.output
    assert "early buyers: 2" in res.output
    assert "failed: 1" in res.output
    with Session(eng) as s:
        sigs = {(x.signature, x.wallet) for x in s.scalars(select(Swap))}
    assert ("w1", "WalletB") in sigs  # WalletB still backfilled after WalletA failed
    assert FakeHelius.closed


def test_backfill_requires_wallet_or_token(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    res = runner.invoke(cli.app, ["backfill"])
    assert res.exit_code != 0


def test_rescore_prints_summary(tmp_path, monkeypatch):
    url = use_db(tmp_path, monkeypatch)
    init_db(make_engine(url))
    res = runner.invoke(cli.app, ["rescore"])
    assert res.exit_code == 0, res.output
    assert "scored 0 wallets" in res.output


def test_seed_prints_summary(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)

    class SeedDex(FakeDex):
        async def token_pairs(self, mints):
            return [{"chainId": "solana", "pairAddress": POOL, "priceNative": "0.000001",
                     "baseToken": {"address": MINT, "symbol": "MEME", "name": "Meme"},
                     "quoteToken": {"address": "So11111111111111111111111111111111111111112"},
                     "liquidity": {"usd": 50_000}, "fdv": 2_000_000, "priceChange": {"h24": 500}}]

    monkeypatch.setattr(cli, "make_dex", lambda s: SeedDex())
    res = runner.invoke(cli.app, ["seed", MINT])
    assert res.exit_code == 0, res.output
    assert f"seeded MEME {MINT}: pool={POOL}" in res.output


def test_backfill_continues_on_generic_error_and_rolls_back(tmp_path, monkeypatch, caplog):
    # Finding 6: any per-wallet exception is caught, the session rolled back,
    # `wallet + error type` logged, and the loop continues.
    url = use_db(tmp_path, monkeypatch)
    eng = make_engine(url)
    init_db(eng)
    with Session(eng) as s:
        s.add(Token(mint=MINT, pool_address=POOL, pool_created_at=datetime.now(timezone.utc)))
        s.commit()

    from hades import pipeline

    real = pipeline.backfill_wallet
    rollbacks = []
    orig_rollback = Session.rollback

    def spy_rollback(self):
        rollbacks.append(True)
        return orig_rollback(self)

    async def flaky(session, helius, address, settings, force=False, now=None):
        if address == "WalletA":
            raise RuntimeError("secret-ish detail")
        return await real(session, helius, address, settings, force=force, now=now)

    monkeypatch.setattr(pipeline, "backfill_wallet", flaky)
    monkeypatch.setattr(Session, "rollback", spy_rollback)

    with caplog.at_level("WARNING", logger="hades.cli"):
        res = runner.invoke(cli.app, ["backfill", "--token", MINT])

    assert res.exit_code == 0, res.output
    assert "failed: 1" in res.output
    assert rollbacks
    msgs = [r.getMessage() for r in caplog.records if r.name == "hades.cli"]
    assert any("WalletA" in m and "RuntimeError" in m for m in msgs)
    assert not any("secret-ish detail" in m for m in msgs)
    with Session(eng) as s:
        sigs = {(x.signature, x.wallet) for x in s.scalars(select(Swap))}
    assert ("w1", "WalletB") in sigs
