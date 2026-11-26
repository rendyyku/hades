from typer.testing import CliRunner

from hades.cli import app


def test_cli_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert "2.0" in result.output
