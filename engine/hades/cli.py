import typer

from hades import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def version() -> None:
    """Print the HADES version."""
    typer.echo(f"hades {__version__}")


@app.callback()
def main() -> None:
    """HADES v2: early signals from public data."""
