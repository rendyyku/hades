import asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import typer

from hades import __version__
from hades.config import ConfigError, get_settings, load_config

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _services():
    from hades.app import build, setup_logging

    setup_logging()
    settings = get_settings()
    try:
        cfg = load_config(settings.HADES_CONFIG)
    except ConfigError as e:
        typer.echo(f"config error: {e}", err=True)
        raise typer.Exit(2) from e
    return build(settings, cfg)


@app.callback()
def main() -> None:
    """HADES v2: early signals from public data."""


@app.command()
def version() -> None:
    """Print the HADES version."""
    typer.echo(f"hades {__version__}")


@app.command()
def run(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run watchers, alerts, bot and the API in one process."""
    from hades.app import serve

    asyncio.run(serve(_services(), host, port))


@app.command()
def once(name: str) -> None:
    """Dry-run one watcher: fetch + diff against stored state and print what it would emit (saves nothing)."""
    s = _services()
    w = next((w for w in s.watchers if w.name == name), None)
    if w is None:
        typer.echo(f"unknown watcher {name!r}. Known:\n  " + "\n  ".join(x.name for x in s.watchers))
        raise typer.Exit(1)

    async def go():
        try:
            state = s.store.get_state(w.name)
            res = w.diff(state, w.parse(await w.fetch(s.http, state)), datetime.now(timezone.utc))
        finally:
            await s.client.aclose()
        if state is None:
            typer.echo("baseline (first run): no signals")
        for sig in res.signals:
            typer.echo(f"{sig.kind:10} {sig.chain or '-':10} {sig.symbol or sig.name or '-':20} {sig.evidence_url}")
        for ev in res.listings:
            typer.echo(f"listing    {ev.exchange:10} {ev.symbol}")
        typer.echo(f"{len(res.signals)} signals, {len(res.listings)} listings, {len(res.domains)} domains")

    asyncio.run(go())


@app.command()
def evaluate(out: Path = Path("reports"), prices: bool = False) -> None:
    """Write the evaluation report (Markdown + CSV)."""
    from hades.evaluate import evaluate as run_eval
    from hades.evaluate import price_move, write_report

    s = _services()
    report = run_eval(s.store, datetime.now(timezone.utc))

    async def add_prices():
        try:
            for h in report["hits"]:
                enrs = s.store.enrichments(h["signal_id"])
                url = next((e["pool_url"] for e in reversed(enrs)
                            if e["pool_url"] and "geckoterminal.com" in e["pool_url"]), None)
                if not url:
                    continue
                net, _, pool = urlsplit(url).path.strip("/").split("/")
                sig = s.store.get_signal(h["signal_id"])
                listing = next(x for x in s.store.all_listings() if x["id"] == h["listing_id"])
                h["price_move_pct"] = await price_move(s.http, net, pool, sig["observed_at"], listing["first_seen_at"])
        finally:
            await s.client.aclose()

    if prices:
        asyncio.run(add_prices())
    md, csv_path = write_report(report, out)
    typer.echo(f"wrote {md} and {csv_path}")
