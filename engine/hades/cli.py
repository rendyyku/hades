"""HADES command line: `hades init-db | seed | discover | backfill | tag | rescore | serve | webhook-sync`."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import typer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hades import pipeline
from hades.config import Settings, get_settings
from hades.db import Swap, Token, Wallet, WalletStats, init_db, make_engine
from hades.insider import TOKEN_TAGS, Tag, tag_token
from hades.sources.dexscreener import DexScreener
from hades.sources.helius import HeliusClient, HeliusError

app = typer.Typer(help="HADES smart-money engine", no_args_is_help=True, add_completion=False)
log = logging.getLogger("hades.cli")


# -- wiring (patched in tests) -------------------------------------------------


def make_helius(settings: Settings) -> Any:
    if not settings.HELIUS_API_KEY:
        raise typer.BadParameter("HELIUS_API_KEY is not set (.env)")
    return HeliusClient(settings.HELIUS_API_KEY, rps=settings.RATE_LIMIT_RPS)


def make_dex(settings: Settings) -> Any:
    return DexScreener()


def _setup_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("hades").setLevel(logging.INFO)
    for name in ("httpx", "httpcore"):  # request URLs carry the api key
        logging.getLogger(name).setLevel(logging.WARNING)


@asynccontextmanager
async def _closing(client: Any) -> AsyncIterator[Any]:
    try:
        yield client
    finally:
        await client.aclose()


def _session(settings: Settings) -> Session:
    engine = make_engine(settings.DATABASE_URL)
    init_db(engine)
    return Session(engine)


def _swap_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Swap)) or 0


def _hub_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Wallet).where(Wallet.label == "hub")) or 0


async def _tag(session: Session, helius: Any, tok: Token) -> tuple[list[Tag], int] | None:
    """Run tag_token; on HeliusError roll back, log the error type, return None."""
    mint = tok.mint
    try:
        tags = await tag_token(session, helius, tok)
    except HeliusError as e:  # never log the message (may carry a URL/key)
        session.rollback()
        log.error("tagging %s failed: %s", mint, type(e).__name__)
        return None
    return tags, _hub_count(session)


def _echo_tags(result: tuple[list[Tag], int]) -> None:
    tags, hubs = result
    counts = {k: 0 for k in TOKEN_TAGS}
    for t in tags:
        counts[t.tag] = counts.get(t.tag, 0) + 1
    typer.echo("tags: " + "  ".join(f"{k}={v}" for k, v in counts.items()) + f"  (hubs labelled: {hubs})")


# -- commands ------------------------------------------------------------------


@app.callback()
def _main() -> None:
    _setup_logging()


@app.command("init-db")
def init_db_cmd() -> None:
    """Create all tables."""
    settings = get_settings()
    init_db(make_engine(settings.DATABASE_URL))
    typer.echo("database initialised")


@app.command()
def seed(mint: str) -> None:
    """Seed one token (pool, deployer, pool creation) from DexScreener + Helius."""
    settings = get_settings()

    async def run() -> Token:
        async with _closing(make_helius(settings)) as helius, _closing(make_dex(settings)) as dex:
            with _session(settings) as session:
                tok = await pipeline.seed_token(session, helius, dex, mint, settings)
                session.refresh(tok)  # commit expired it; load before detaching
                session.expunge(tok)
                return tok

    tok = asyncio.run(run())
    typer.echo(
        f"seeded {tok.symbol} {tok.mint}: pool={tok.pool_address} deployer={tok.deployer} "
        f"pool_created_slot={tok.pool_created_slot} runner={tok.is_runner} fdv={tok.peak_mcap_usd}"
    )


@app.command()
def discover(
    limit: int = typer.Option(0, "--limit", min=0, help="Max NEW runners to seed this run (0 = unlimited)"),
) -> None:
    """Seed new runners from DexScreener boosted tokens."""
    settings = get_settings()

    async def run() -> list[tuple[str, str | None]]:
        async with _closing(make_helius(settings)) as helius, _closing(make_dex(settings)) as dex:
            with _session(settings) as session:
                toks = await pipeline.discover(session, helius, dex, settings, limit=limit)
                return [(t.mint, t.symbol) for t in toks]

    new = asyncio.run(run())
    typer.echo(f"discovered {len(new)} new runner(s)")
    for mint, sym in new:
        typer.echo(f"  {sym or '?'} {mint}")


@app.command()
def backfill(
    wallet: Optional[str] = typer.Option(None, "--wallet", help="Backfill one wallet"),
    token: Optional[str] = typer.Option(None, "--token", help="Backfill the early buyers of a seeded token"),
    force: bool = typer.Option(False, "--force", help="Ignore the 6h freshness window"),
) -> None:
    """Backfill swap history for a wallet, or for a token's early buyers."""
    if (wallet is None) == (token is None):
        raise typer.BadParameter("pass exactly one of --wallet or --token")
    settings = get_settings()

    async def run() -> tuple[int, int, int, int, int, tuple[list[Tag], int] | None]:
        async with _closing(make_helius(settings)) as helius:
            with _session(settings) as session:
                if wallet is not None:
                    addresses = [wallet]
                    n_buyers = 0
                else:
                    tok = session.get(Token, token)
                    if tok is None:
                        raise typer.BadParameter(f"token {token} not seeded; run `hades seed {token}`")
                    addresses = await pipeline.early_buyers(
                        session, helius, tok, settings.EARLY_BUYERS_N, sig_max_pages=settings.SIG_SCAN_MAX_PAGES
                    )
                    n_buyers = len(addresses)
                    typer.echo(f"early buyers: {n_buyers}")
                saved = failed = 0
                for addr in addresses:
                    try:
                        saved += await pipeline.backfill_wallet(session, helius, addr, settings, force=force)
                    except HeliusError as e:
                        failed += 1
                        log.warning("backfill %s failed: %s", addr, e)
                    except Exception as e:  # keep going; never log the message (may carry a URL/key)
                        session.rollback()
                        failed += 1
                        log.warning("backfill %s failed: %s", addr, type(e).__name__)
                total = _swap_count(session)
                tagged = None
                if token is not None:
                    tagged = await _tag(session, helius, tok)
                return n_buyers, len(addresses), saved, failed, total, tagged

    n_buyers, n_wallets, saved, failed, total, tagged = asyncio.run(run())
    typer.echo(f"wallets: {n_wallets}  swaps saved: {saved}  failed: {failed}  swaps in db: {total}")
    if token is not None:
        if tagged is None:
            typer.echo("insider tagging failed (see log)", err=True)
            raise typer.Exit(1)
        _echo_tags(tagged)


@app.command()
def tag(mint: str) -> None:
    """Tag insiders (sniper, bundle, deployer_link) of an already-seeded token."""
    settings = get_settings()

    async def run() -> tuple[list[Tag], int] | None | bool:
        async with _closing(make_helius(settings)) as helius:
            with _session(settings) as session:
                tok = session.get(Token, mint)
                if tok is None:
                    return False
                return await _tag(session, helius, tok)

    res = asyncio.run(run())
    if res is False:
        typer.echo(f"error: token {mint} not seeded; run `hades seed {mint}`", err=True)
        raise typer.Exit(1)
    if res is None:
        typer.echo("insider tagging failed (see log)", err=True)
        raise typer.Exit(1)
    _echo_tags(res)


@app.command()
def rescore() -> None:
    """Recompute stats and scores for every wallet with swaps."""
    settings = get_settings()

    async def run() -> tuple[int, int, list[tuple[str, float | None, float, int]]]:
        async with _closing(make_dex(settings)) as dex:
            with _session(settings) as session:
                n = await pipeline.rescore(session, dex)
                top = session.execute(
                    select(WalletStats.address, WalletStats.score, WalletStats.total_pnl_sol,
                           WalletStats.tokens_closed)
                    .where(WalletStats.score.is_not(None))
                    .order_by(WalletStats.score.desc())
                    .limit(5)
                ).all()
                return n, _swap_count(session), [tuple(r) for r in top]

    n, swaps, top = asyncio.run(run())
    typer.echo(f"scored {n} wallets ({swaps} swaps in db)")
    if top:
        typer.echo("top 5 by score:")
        for addr, score, pnl, closed in top:
            typer.echo(f"  {score:6.2f}  {addr}  pnl={pnl:+.3f} SOL  closed={closed}")
    else:
        typer.echo("no wallet eligible for a score yet (needs >= 5 closed tokens, non-bot)")


@app.command("webhook-sync")
def webhook_sync() -> None:
    """Push the current watchlist to the Helius webhook once (create/update/delete)."""
    from types import SimpleNamespace

    from hades.realtime import effective_webhook_id, sync_webhook

    settings = get_settings()
    engine = make_engine(settings.DATABASE_URL)
    init_db(engine)

    async def run() -> str:
        state = SimpleNamespace(settings=settings, engine=engine, sync_lock=asyncio.Lock())
        return await sync_webhook(SimpleNamespace(state=state))

    try:
        status = asyncio.run(run())
        wid = effective_webhook_id(engine, settings)
    finally:
        engine.dispose()
    typer.echo(f"webhook sync: {status}  webhook id: {wid or '-'}")
    if status == "skipped":
        typer.echo("PUBLIC_WEBHOOK_URL is not set; nothing to sync", err=True)
    elif status == "error":
        typer.echo("webhook sync failed (see log)", err=True)
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
) -> None:
    """Run the API server (uvicorn hades.api.main:app)."""
    import uvicorn

    uvicorn.run("hades.api.main:app", host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    app()
