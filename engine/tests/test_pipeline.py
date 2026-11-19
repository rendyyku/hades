"""Pipeline tests with in-memory fake Helius / DexScreener clients."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from hades import pipeline
from hades.config import Settings
from hades.db import Swap, Token, Wallet, WalletStats
from hades.sources.helius import HeliusError, HeliusRateLimited

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())
NOW_MS = NOW_TS * 1000

MINT = "MeMeMint1111111111111111111111111111111pump"
POOL = "PooLBest11111111111111111111111111111111111"
POOL2 = "PooLSmall1111111111111111111111111111111111"
DEPLOYER = "DePLoyer1111111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"


def settings(**kw) -> Settings:
    base = dict(
        HELIUS_API_KEY="test",
        DATABASE_URL="sqlite://",
        EARLY_BUYERS_N=100,
        HELIUS_MAX_PAGES_PER_ADDRESS=20,
        SIG_SCAN_MAX_PAGES=30,
        RATE_LIMIT_RPS=8,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


# -- tx builders ---------------------------------------------------------------


def swap_tx(sig, slot, wallet, side="buy", *, mint=MINT, sol=1.0, tokens=1000.0, ts=None, pool=POOL):
    lamports = int(sol * 1_000_000_000)
    if side == "buy":
        native = [{"fromUserAccount": wallet, "toUserAccount": pool, "amount": lamports}]
        tt = [{"fromUserAccount": pool, "toUserAccount": wallet, "tokenAmount": tokens, "mint": mint}]
        change = -lamports - 5000
    else:
        native = [{"fromUserAccount": pool, "toUserAccount": wallet, "amount": lamports}]
        tt = [{"fromUserAccount": wallet, "toUserAccount": pool, "tokenAmount": tokens, "mint": mint}]
        change = lamports - 5000
    return {
        "signature": sig,
        "slot": slot,
        "timestamp": ts if ts is not None else NOW_TS - 3600 + slot % 1000,
        "type": "SWAP",
        "source": "RAYDIUM",
        "fee": 5000,
        "feePayer": wallet,
        "transactionError": None,
        "nativeTransfers": native,
        "tokenTransfers": tt,
        "accountData": [{"account": wallet, "nativeBalanceChange": change, "tokenBalanceChanges": []}],
    }


def plain_tx(sig, slot, fee_payer, ts):
    return {
        "signature": sig, "slot": slot, "timestamp": ts, "type": "UNKNOWN", "source": "SYSTEM_PROGRAM",
        "fee": 5000, "feePayer": fee_payer, "transactionError": None,
        "nativeTransfers": [], "tokenTransfers": [], "accountData": [],
    }


def pair(mint=MINT, address=POOL, liq=50_000, h24=500.0, fdv=2_000_000, price="0.000002", symbol="MEME"):
    return {
        "chainId": "solana",
        "pairAddress": address,
        "baseToken": {"address": mint, "name": f"{symbol} Coin", "symbol": symbol},
        "quoteToken": {"address": WSOL, "symbol": "SOL"},
        "priceNative": price,
        "liquidity": {"usd": liq},
        "fdv": fdv,
        "priceChange": {"h24": h24},
        "pairCreatedAt": NOW_MS - 86_400_000,
    }


# -- fakes ---------------------------------------------------------------------


class FakeHelius:
    def __init__(self, *, sigs=None, txs=None, address_txs=None, error=None):
        self.sigs = sigs or {}  # address -> oldest-first signatures
        self.txs = txs or {}  # signature -> tx
        self.address_txs = address_txs or {}  # address -> newest-first txs
        self.error = error  # HeliusError raised by address_transactions
        self.calls: list[tuple] = []

    async def signatures_oldest(self, address, max_pages):
        self.calls.append(("signatures_oldest", address, max_pages))
        return list(self.sigs.get(address, []))

    async def parse_transactions(self, signatures):
        self.calls.append(("parse_transactions", tuple(signatures)))
        return [self.txs[s] for s in signatures if s in self.txs]

    async def address_transactions(self, address, *, tx_type="SWAP", before=None, max_pages):
        self.calls.append(("address_transactions", address, tx_type, max_pages))
        if self.error is not None:
            raise self.error
        return list(self.address_txs.get(address, []))[: max_pages * 100]

    def count(self, name):
        return sum(1 for c in self.calls if c[0] == name)


class FakeDex:
    def __init__(self, pairs=None, boosted=None):
        self.pairs = pairs or []
        self.boosted = boosted or []
        self.calls: list[tuple] = []

    async def token_pairs(self, mints):
        self.calls.append(("token_pairs", tuple(mints)))
        want = set(mints)
        return [p for p in self.pairs if p["baseToken"]["address"] in want or p["quoteToken"]["address"] in want]

    async def boosted_tokens(self):
        self.calls.append(("boosted_tokens",))
        return list(self.boosted)


def seed_fakes(mint=MINT, pool=POOL, deployer=DEPLOYER):
    helius = FakeHelius(
        sigs={mint: [f"{mint}-create", f"{mint}-later"], pool: [f"{pool}-init", f"{pool}-swap1"]},
        txs={
            f"{mint}-create": plain_tx(f"{mint}-create", 1000, deployer, NOW_TS - 7200),
            f"{mint}-later": plain_tx(f"{mint}-later", 1100, "SomeoneElse", NOW_TS - 7000),
            f"{pool}-init": plain_tx(f"{pool}-init", 1005, deployer, NOW_TS - 7100),
        },
    )
    return helius


# -- seed_token ----------------------------------------------------------------


async def test_seed_sets_pool_and_deployer(session):
    helius = seed_fakes()
    dex = FakeDex(pairs=[
        pair(address=POOL2, liq=10_000),
        pair(address=POOL, liq=50_000),
        # inverted pair: MINT is the quote, must be ignored even though it is deeper
        {**pair(mint=WSOL, address="PooLInverted", liq=900_000), "quoteToken": {"address": MINT}},
    ])
    tok = await pipeline.seed_token(session, helius, dex, MINT, settings(SIG_SCAN_MAX_PAGES=7), now=NOW)

    assert tok.mint == MINT
    assert tok.symbol == "MEME" and tok.name == "MEME Coin"
    assert tok.pool_address == POOL
    assert tok.deployer == DEPLOYER
    assert tok.pool_created_slot == 1005
    assert tok.pool_created_at == datetime.fromtimestamp(NOW_TS - 7100, timezone.utc)
    assert tok.is_runner is True
    assert tok.peak_mcap_usd == 2_000_000
    assert ("signatures_oldest", MINT, 7) in helius.calls
    assert ("signatures_oldest", POOL, 7) in helius.calls
    assert session.get(Token, MINT).pool_address == POOL


async def test_seed_upserts_existing(session):
    session.add(Token(mint=MINT, symbol="OLD", is_runner=False, peak_mcap_usd=1.0))
    session.commit()
    tok = await pipeline.seed_token(session, seed_fakes(), FakeDex(pairs=[pair()]), MINT, settings(), now=NOW)
    assert tok.symbol == "MEME" and tok.is_runner is True
    assert session.scalar(select(func.count()).select_from(Token)) == 1


async def test_seed_no_pairs_raises(session):
    dex = FakeDex(pairs=[{**pair(mint=WSOL), "quoteToken": {"address": MINT}}])
    with pytest.raises(ValueError):
        await pipeline.seed_token(session, seed_fakes(), dex, MINT, settings(), now=NOW)


# -- early_buyers --------------------------------------------------------------


def make_token(session):
    tok = Token(mint=MINT, pool_address=POOL, pool_created_slot=100)
    session.add(tok)
    session.commit()
    return tok


async def test_early_buyers_unique_and_ordered(session):
    tok = make_token(session)
    txs = [
        plain_tx("s00", 100, DEPLOYER, NOW_TS - 5000),  # pool init, no swap
        swap_tx("s01", 103, "WalletC"),
        swap_tx("s02", 101, "WalletA"),  # earlier slot than s01 despite sig order
        swap_tx("s03", 102, "WalletB"),
        swap_tx("s04", 104, "WalletA"),  # duplicate buyer
        swap_tx("s05", 104, "WalletS", side="sell"),  # seller, not a buyer
        swap_tx("s06", 105, "WalletX", mint="OtherMint"),  # other mint
        swap_tx("s07", 106, "WalletD"),
        swap_tx("s08", 107, "WalletE"),
    ]
    helius = FakeHelius(sigs={POOL: [t["signature"] for t in txs]}, txs={t["signature"]: t for t in txs})

    buyers = await pipeline.early_buyers(session, helius, tok, 4, sig_max_pages=9)

    assert buyers == ["WalletA", "WalletB", "WalletC", "WalletD"]
    assert ("signatures_oldest", POOL, 9) in helius.calls
    for w in buyers:
        assert session.get(Wallet, w) is not None
    saved = session.scalars(select(Swap)).all()
    assert {s.source for s in saved} == {"backfill"}
    assert all(s.mint == MINT for s in saved)
    assert "s04" in {s.signature for s in saved}  # duplicate buyer's swap still saved


async def test_early_buyers_stops_parsing_once_n_reached(session):
    tok = make_token(session)
    txs = [swap_tx(f"s{i:04d}", 200 + i, f"W{i:04d}") for i in range(250)]
    helius = FakeHelius(sigs={POOL: [t["signature"] for t in txs]}, txs={t["signature"]: t for t in txs})

    buyers = await pipeline.early_buyers(session, helius, tok, 10, sig_max_pages=1)

    assert buyers == [f"W{i:04d}" for i in range(10)]
    parse_calls = [c for c in helius.calls if c[0] == "parse_transactions"]
    assert len(parse_calls) == 1 and len(parse_calls[0][1]) == 100
    assert parse_calls[0][1][0] == "s0000"


async def test_early_buyers_skips_non_sol_quoted(session):
    tok = make_token(session)
    t = swap_tx("s01", 101, "WalletA")
    t["nativeTransfers"], t["accountData"], t["fee"] = [], [], 0  # no SOL moved -> sol_amount None
    helius = FakeHelius(sigs={POOL: ["s01"]}, txs={"s01": t})
    assert await pipeline.early_buyers(session, helius, tok, 5, sig_max_pages=1) == []
    assert session.scalar(select(func.count()).select_from(Swap)) == 0


# -- backfill_wallet -----------------------------------------------------------


async def test_backfill_skips_recent(session):
    session.add(Wallet(address="WalletA", last_backfilled_at=NOW - timedelta(hours=1)))
    session.commit()
    helius = FakeHelius(address_txs={"WalletA": [swap_tx("s1", 1, "WalletA")]})
    assert await pipeline.backfill_wallet(session, helius, "WalletA", settings(), now=NOW) == 0
    assert helius.calls == []

    # force overrides the freshness check
    assert await pipeline.backfill_wallet(session, helius, "WalletA", settings(), force=True, now=NOW) == 1


async def test_backfill_runs_when_stale(session):
    session.add(Wallet(address="WalletA", last_backfilled_at=NOW - timedelta(hours=7)))
    session.commit()
    helius = FakeHelius(address_txs={"WalletA": [swap_tx("s1", 1, "WalletA")]})
    assert await pipeline.backfill_wallet(session, helius, "WalletA", settings(), now=NOW) == 1


async def test_backfill_idempotent(session):
    txs = [
        swap_tx("s3", 3, "WalletA", side="sell"),
        swap_tx("s2", 2, "WalletA"),
        swap_tx("s1", 1, "OtherWallet"),  # feePayer is someone else -> not this wallet's swap
    ]
    helius = FakeHelius(address_txs={"WalletA": txs})
    s = settings(HELIUS_MAX_PAGES_PER_ADDRESS=4)

    n1 = await pipeline.backfill_wallet(session, helius, "WalletA", s, now=NOW)
    n2 = await pipeline.backfill_wallet(session, helius, "WalletA", s, force=True, now=NOW)

    assert n1 == n2 == 2
    assert session.scalar(select(func.count()).select_from(Swap)) == 2
    assert ("address_transactions", "WalletA", "SWAP", 4) in helius.calls
    w = session.get(Wallet, "WalletA")
    assert w.last_backfilled_at == NOW
    assert {s.source for s in session.scalars(select(Swap))} == {"backfill"}


async def test_backfill_warns_when_truncated(session, caplog):
    txs = [swap_tx(f"s{i}", i, "WalletA") for i in range(100)]
    helius = FakeHelius(address_txs={"WalletA": txs})
    caplog.set_level("WARNING")
    await pipeline.backfill_wallet(session, helius, "WalletA", settings(HELIUS_MAX_PAGES_PER_ADDRESS=1), now=NOW)
    assert "truncated" in caplog.text


async def test_backfill_saves_partial_on_error(session):
    err = HeliusRateLimited("boom", partial=[swap_tx("s1", 1, "WalletA"), swap_tx("s2", 2, "WalletA")], cursor="s2")
    helius = FakeHelius(error=err)
    with pytest.raises(HeliusError):
        await pipeline.backfill_wallet(session, helius, "WalletA", settings(), now=NOW)

    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Swap)) == 2
    w = session.get(Wallet, "WalletA")
    assert w is None or w.last_backfilled_at is None


# -- discover ------------------------------------------------------------------


async def test_discover_seeds_new_runners_only(session):
    a, b, c = "MintA", "MintB", "MintC"
    session.add(Token(mint=c, symbol="C"))
    session.commit()
    dex = FakeDex(
        boosted=[a, b, c],
        pairs=[
            pair(mint=a, address="PoolA", liq=60_000, h24=400),
            pair(mint=a, address="PoolA2", liq=5_000, h24=0),
            pair(mint=b, address="PoolB", liq=60_000, h24=10, fdv=10),  # not a runner
            pair(mint=c, address="PoolC", liq=60_000, h24=400),  # runner but already known
        ],
    )
    helius = seed_fakes(mint=a, pool="PoolA")
    new = await pipeline.discover(session, helius, dex, settings(), now=NOW)

    assert [t.mint for t in new] == [a]
    assert new[0].pool_address == "PoolA"
    assert session.get(Token, b) is None
    assert dex.calls[0] == ("boosted_tokens",)
    assert dex.calls[1] == ("token_pairs", (a, b, c))


# -- rescore -------------------------------------------------------------------


def add_swap(session, sig, wallet, mint, side, tokens, sol, when):
    session.add(Swap(signature=sig, wallet=wallet, mint=mint, slot=0, block_time=when, side=side,
                     token_amount=tokens, sol_amount=sol, source="backfill", dex="RAYDIUM"))


async def test_rescore_scores_all_wallets_with_one_price_call(session):
    t0 = NOW - timedelta(days=2)
    # WalletA: closed MintX, open MintY
    add_swap(session, "a1", "WalletA", "MintX", "buy", 100, 1.0, t0)
    add_swap(session, "a2", "WalletA", "MintX", "sell", 100, 2.0, t0 + timedelta(minutes=5))
    add_swap(session, "a3", "WalletA", "MintY", "buy", 100, 1.0, t0)
    # WalletB: open MintZ
    add_swap(session, "b1", "WalletB", "MintZ", "buy", 50, 1.0, t0)
    session.commit()
    dex = FakeDex(pairs=[pair(mint="MintY", address="PY", price="0.02"), pair(mint="MintZ", address="PZ", price="0.04")])

    n = await pipeline.rescore(session, dex, now=NOW)

    assert n == 2
    assert len(dex.calls) == 1
    assert dex.calls[0][0] == "token_pairs" and set(dex.calls[0][1]) == {"MintY", "MintZ"}
    a = session.get(WalletStats, "WalletA")
    b = session.get(WalletStats, "WalletB")
    assert a.realized_pnl_sol == pytest.approx(1.0)
    assert a.unrealized_pnl_sol == pytest.approx(100 * 0.02 - 1.0)
    assert b.unrealized_pnl_sol == pytest.approx(50 * 0.04 - 1.0)


async def test_rescore_skips_valueerror_wallet(session):
    t0 = NOW - timedelta(days=1)
    add_swap(session, "g1", "Good", "MintX", "buy", 100, 1.0, t0)
    add_swap(session, "x1", "Bad", "MintX", "bogus", 100, 1.0, t0)
    session.commit()
    n = await pipeline.rescore(session, FakeDex(), now=NOW)
    assert n == 1
    assert session.get(WalletStats, "Good") is not None
    assert session.get(WalletStats, "Bad") is None


async def test_rescore_no_open_mints_no_price_call(session):
    t0 = NOW - timedelta(days=1)
    add_swap(session, "g1", "Good", "MintX", "buy", 100, 1.0, t0)
    add_swap(session, "g2", "Good", "MintX", "sell", 100, 1.5, t0 + timedelta(minutes=1))
    session.commit()
    dex = FakeDex()
    assert await pipeline.rescore(session, dex, now=NOW) == 1
    assert dex.calls == []


async def test_rescore_prices_only_from_sol_quoted_pairs(session):
    # Real-data bug: priceNative is in quote-token units, so a USDC/meme-quoted
    # pair with more liquidity must not be used as a SOL price.
    t0 = NOW - timedelta(days=1)
    add_swap(session, "a1", "WalletA", "MintY", "buy", 100, 1.0, t0)
    add_swap(session, "a2", "WalletA", "MintZ", "buy", 100, 1.0, t0)
    session.commit()
    usdc_y = {**pair(mint="MintY", address="PYusdc", liq=900_000, price="5.0"),
              "quoteToken": {"address": "USDCmint", "symbol": "USDC"}}
    usdc_z = {**pair(mint="MintZ", address="PZusdc", price="7.0"),
              "quoteToken": {"address": "USDCmint", "symbol": "USDC"}}
    dex = FakeDex(pairs=[usdc_y, pair(mint="MintY", address="PY", liq=10_000, price="0.02"), usdc_z])

    await pipeline.rescore(session, dex, now=NOW)

    a = session.get(WalletStats, "WalletA")
    assert a.unrealized_pnl_sol == pytest.approx(100 * 0.02 - 1.0)  # MintZ has no SOL price -> 0


async def test_rescore_ignores_price_from_illiquid_pair(session):
    # R13: an open position marked at an illiquid pair's price must not
    # create unrealized PnL (real data: +434 SOL phantom from such pairs).
    t0 = NOW - timedelta(days=1)
    add_swap(session, "a1", "WalletA", "MintY", "buy", 100, 1.0, t0)
    session.commit()
    dex = FakeDex(pairs=[pair(mint="MintY", address="PY", liq=9_999, price="50.0")])

    await pipeline.rescore(session, dex, now=NOW)

    assert session.get(WalletStats, "WalletA").unrealized_pnl_sol == pytest.approx(0.0)


async def test_early_buyers_saves_only_selected_buyers_swaps(session):
    # R15: swaps of wallets that are not among the selected early buyers
    # (sellers, buyers past n) are not saved; their histories would be partial.
    tok = make_token(session)
    txs = [
        swap_tx("s01", 101, "WalletA"),
        swap_tx("s02", 102, "WalletS", side="sell"),  # seller, never a buyer
        swap_tx("s03", 103, "WalletB"),
        swap_tx("s04", 104, "WalletA", side="sell"),  # selected buyer's later sell
        swap_tx("s05", 105, "WalletLate"),  # buyer past n
    ]
    helius = FakeHelius(sigs={POOL: [t["signature"] for t in txs]}, txs={t["signature"]: t for t in txs})

    buyers = await pipeline.early_buyers(session, helius, tok, 2, sig_max_pages=1)

    assert buyers == ["WalletA", "WalletB"]
    saved = {(s.signature, s.wallet) for s in session.scalars(select(Swap))}
    assert saved == {("s01", "WalletA"), ("s03", "WalletB"), ("s04", "WalletA")}
