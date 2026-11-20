"""``hades-hl`` command line.

``init-db | discover | backfill | rescore`` (Task 6) and ``tag`` (Task 7) are
implemented here; ``serve | follow-sync | paper-run`` are stubs filled in by later tasks.
Nothing here ever prints ``HL_API_KEY`` (Settings hides it from repr; tracebacks
never show locals).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Any, Optional

import typer
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from hades_hl import __version__, insider, pipeline
from hades_hl.client import HLClient, HLError, HLRateLimited
from hades_hl.config import Settings, get_settings
from hades_hl.db import Wallet, init_db, make_engine

# pretty_exceptions_show_locals=False: a traceback must never print locals (settings carry keys).
app = typer.Typer(
    help="HADES Hyperliquid head", no_args_is_help=True, add_completion=False,
    pretty_exceptions_show_locals=False,
)
log = logging.getLogger("hades_hl.cli")
NOT_IMPLEMENTED = "not implemented in Task 6"


# -- wiring (patched in tests) -------------------------------------------------------

def make_client(settings: Settings) -> HLClient:
    return HLClient.from_settings(settings)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("hades_hl").setLevel(logging.INFO)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _session(settings: Settings) -> Session:
    engine = make_engine(settings.DATABASE_URL)
    init_db(engine)
    return Session(engine)


async def _with_client(settings: Settings, fn: Any) -> Any:
    client = make_client(settings)
    try:
        return await fn(client)
    finally:
        await client.aclose()


def masked_url(url: str) -> str:
    """The database URL with any password masked (M11)."""
    try:
        u = make_url(url)
    except Exception:  # unparseable: print nothing of it
        return "<database url>"
    return u.render_as_string(hide_password=True) if u.password else url


def _stub(name: str) -> None:
    typer.echo(f"{name}: {NOT_IMPLEMENTED}")
    raise typer.Exit(2)


# -- commands ------------------------------------------------------------------------

@app.command()
def version() -> None:
    """Print the hades-hl version."""
    typer.echo(__version__)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create or migrate the HL SQLite database at HL_DATABASE_URL."""
    url = get_settings().DATABASE_URL
    init_db(make_engine(url))
    typer.echo(f"initialised {masked_url(url)}")


@app.command()
def discover(limit: int = typer.Option(200, "--limit", min=1, help="max candidates")) -> None:
    """Pick candidate wallets from the HL leaderboard (cached 24 h), vaults excluded;
    each must pass a one-request activity probe (the bot rule on its latest fills)."""
    _setup_logging()
    settings = get_settings()
    with _session(settings) as session:
        got = asyncio.run(_with_client(
            settings, lambda c: pipeline.discover(session, c, limit=limit)))
        total = len(session.scalars(select(Wallet.address)).all())
    checked = "vaults checked: " + ("yes" if got.vaults_checked else
                                    "no (vault list unavailable; vaults may be included)")
    if got.error:
        typer.echo(f"{got.error}. Discovered 0 wallets (the trades-WS fallback is not "
                   f"implemented yet). Wallets in DB: {total}; {checked}")
        return
    typer.echo(f"discovered {len(got)} candidate wallets (limit {limit}); wallets in DB: {total}")
    pf = got.prefilter or {}
    if pf:
        typer.echo(f"prefilter: {pf.get('before')} -> {pf.get('after')} candidates (excluded: "
                   f"account value<10k {pf.get('account_value')}, no volume {pf.get('volume')}, "
                   f"volume>30x AV {pf.get('churn')}, month pnl<=0 {pf.get('pnl')}, "
                   f"vault {pf.get('vault')}); {checked}")
    else:
        typer.echo(f"prefilter: n/a (cached before the prefilter); {checked}")
    pr = got.probe or {}
    if pr:
        rej = pr.get("rejected") or {}
        why = (" (" + ", ".join(f"{k} {v}" for k, v in sorted(rej.items())) + ")") if rej else ""
        extra = []
        if pr.get("budget_exhausted"):
            extra.append(f"probe budget {pr.get('budget')} exhausted")
        if pr.get("rate_limited"):
            extra.append("stopped: rate limited")
        typer.echo(f"probe: {pr.get('probed')} probed ({pr.get('cached')} cached), "
                   f"{sum(rej.values())} rejected{why}, {pr.get('accepted')} accepted"
                   + ("; " + "; ".join(extra) if extra else ""))


@app.command()
def backfill(
    address: Optional[str] = typer.Option(None, "--address", help="one wallet"),
    all_: bool = typer.Option(False, "--all", help="every non-vault wallet in the DB"),
    days: int = typer.Option(90, "--days", min=1,
                             help="history window, honoured on every run"),
) -> None:
    """Import fills (newest first, coverage tracked) + funding. Idempotent.

    With --all, the exit code is 0 even when some wallets failed (rate limited or HL
    errors): check the per-wallet lines."""
    if bool(address) == bool(all_):
        raise typer.BadParameter("pass exactly one of --address or --all")
    _setup_logging()
    settings = get_settings()
    with _session(settings) as session:
        if address:
            addrs = [address.lower()]
        else:
            addrs = list(session.scalars(
                select(Wallet.address).where(Wallet.is_vault.is_(False)).order_by(Wallet.address)))

        async def run(client: HLClient) -> tuple[int, int, int, int]:
            ok = fills = funding = failed = 0
            for i, a in enumerate(addrs, 1):
                try:
                    r = await pipeline.backfill(session, client, a, days=days)
                except HLRateLimited:
                    failed += 1
                    typer.echo(f"[{i}/{len(addrs)}] {a}: rate limited, partial progress kept; "
                               "continuing")
                    continue
                except HLError as e:
                    session.rollback()
                    failed += 1
                    typer.echo(f"[{i}/{len(addrs)}] {a}: HL error {type(e).__name__} "
                               f"(status {e.status}); continuing")
                    continue
                ok += 1
                fills += r.new_fills
                funding += r.new_funding
                flag = " (history partial)" if r.truncated else ""
                typer.echo(f"[{i}/{len(addrs)}] {a}: +{r.new_fills} fills, "
                           f"+{r.new_funding} funding{flag}")
            return ok, fills, funding, failed

        ok, fills, funding, failed = asyncio.run(_with_client(settings, run))
    typer.echo(f"backfill done: {ok} ok, {failed} failed, +{fills} fills, +{funding} funding rows")
    if address and failed:
        raise typer.Exit(1)


@app.command()
def rescore(
    window_days: float = typer.Option(30, "--window-days", help="stats window"),
    offline: bool = typer.Option(False, "--offline", help="do not fetch meta (use kv cache)"),
) -> None:
    """Recompute stats + scores for every wallet from the stored fills."""
    _setup_logging()
    settings = get_settings()
    with _session(settings) as session:
        now = pipeline.utcnow()
        szd = None
        if not offline:
            szd = asyncio.run(_with_client(
                settings, lambda c: pipeline.load_sz_decimals(session, c, now)))
        res = pipeline.rescore(session, now, sz_decimals=szd, window_days=window_days)
        session.commit()
        top = []
        for a, s in res.top[:3]:
            st = (session.get(Wallet, a).stats_json or {})
            top.append((a, s, st))
    reasons = ", ".join(f"{k}: {v}" for k, v in res.reasons.most_common()) or "-"
    typer.echo(f"rescored: {res.scored} scored, {res.unscored} unscored ({reasons})")
    for a, s, st in top:
        wr = st.get("winrate")
        typer.echo(f"  {a}  score {s:.2f}  net ${st.get('net_pnl_usd', 0):,.0f}  "
                   f"n {st.get('n')}  winrate {'-' if wr is None else f'{wr:.0%}'}  "
                   f"window {st.get('window_days', 0):.1f}d  "
                   f"partial {bool(st.get('history_partial'))}  "
                   f"chain_gaps {st.get('chain_gaps')}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8010, "--port"),
) -> None:
    """Serve the head API (later task)."""
    _stub("serve")


@app.command()
def tag(
    address: Optional[str] = typer.Option(None, "--address", help="one wallet"),
    all_: bool = typer.Option(False, "--all", help="every non-vault wallet in the DB"),
) -> None:
    """Insider tags with evidence (pre_listing, pre_move, fresh_whale) over the stored
    fills. Idempotent: re-tagging replaces a wallet's tags and drops stale ones.

    With --all, the exit code is 0 even when some wallets failed (rate limited or HL
    errors): check the per-wallet lines."""
    if bool(address) == bool(all_):
        raise typer.BadParameter("pass exactly one of --address or --all")
    _setup_logging()
    settings = get_settings()
    with _session(settings) as session:
        if address:
            addrs = [address.lower()]
        else:
            addrs = list(session.scalars(
                select(Wallet.address).where(Wallet.is_vault.is_(False)).order_by(Wallet.address)))

        async def run(client: HLClient) -> tuple[int, int, Counter]:
            szd = await pipeline.load_sz_decimals(session, client)
            ok = failed = 0
            counts: Counter = Counter()
            for i, a in enumerate(addrs, 1):
                try:
                    got = await insider.tag_wallet(session, client, a, sz_decimals=szd)
                except HLRateLimited:
                    session.rollback()
                    failed += 1
                    typer.echo(f"[{i}/{len(addrs)}] {a}: rate limited; tags unchanged; "
                               "continuing")
                    continue
                except HLError as e:
                    session.rollback()
                    failed += 1
                    typer.echo(f"[{i}/{len(addrs)}] {a}: HL error {type(e).__name__} "
                               f"(status {e.status}); tags unchanged; continuing")
                    continue
                ok += 1
                counts.update(got.keys())
                desc = ", ".join(f"{k} ({len(t.evidence)} evidence)"
                                 for k, t in sorted(got.items())) or "no tags"
                typer.echo(f"[{i}/{len(addrs)}] {a}: {desc}")
            return ok, failed, counts

        ok, failed, counts = asyncio.run(_with_client(settings, run))
    per = ", ".join(f"{k} {counts.get(k, 0)}" for k in insider.TAG_KINDS)
    typer.echo(f"tag done: {ok} ok, {failed} failed; wallets tagged: {per}")
    if address and failed:
        raise typer.Exit(1)


@app.command("follow-sync")
def follow_sync() -> None:
    """Sync auto-follows (later task)."""
    _stub("follow-sync")


@app.command("paper-run")
def paper_run() -> None:
    """Run the paper-copy loop (later task)."""
    _stub("paper-run")
