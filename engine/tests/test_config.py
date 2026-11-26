from pathlib import Path

import pytest

from hades.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[2]


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_config_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, "chains: {}\n"))
    assert cfg.alerts.min_score == 40
    assert cfg.enrich.min_liquidity_usd == 1000
    assert cfg.ct.interval_s == 21600
    assert cfg.scoring.base["pr_opened"] == 40


def test_load_config_bad_mode(tmp_path):
    text = "repo_diff:\n  targets:\n    - repo: a/b\n      path: x\n      mode: foo\n"
    with pytest.raises(ConfigError) as e:
        load_config(_write(tmp_path, text))
    assert "repo_diff.targets.0.mode" in str(e.value)


def test_config_example_loads():
    cfg = load_config(ROOT / "config.example.yaml")
    assert cfg.chains["monad"].priority is True
    assert cfg.chains["solana"].evm is False
