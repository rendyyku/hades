"""``hades-hl`` command line (more commands arrive in later tasks)."""

import typer

from hades_hl import __version__
from hades_hl.config import get_settings
from hades_hl.db import init_db, make_engine

app = typer.Typer(help="HADES Hyperliquid head", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the hades-hl version."""
    typer.echo(__version__)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create or migrate the HL SQLite database at HL_DATABASE_URL."""
    url = get_settings().DATABASE_URL
    init_db(make_engine(url))
    typer.echo(f"initialised {url}")
