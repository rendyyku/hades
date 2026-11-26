from typer.testing import CliRunner

from hades.cli import app


def _env(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("chains: {}\nground_truth: {exchanges: [binance]}\n", encoding="utf-8")
    monkeypatch.setenv("HADES_CONFIG", str(cfg))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'h.db').as_posix()}")
    from hades.config import get_settings
    get_settings.cache_clear()


def test_cli_once_unknown_watcher_exits_1(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    res = CliRunner().invoke(app, ["once", "nope"])
    assert res.exit_code == 1
    assert "ground_truth:binance" in res.output


def test_cli_evaluate_writes_report(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    out = tmp_path / "reports"
    res = CliRunner().invoke(app, ["evaluate", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert list(out.glob("eval-*.md"))
