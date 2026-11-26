from conftest import NOW, make_signal
from hades.config import AppConfig, ChainCfg
from hades.models import Enrichment
from hades.score import score

CFG = AppConfig(chains={"monad": ChainCfg(priority=True), "ethereum": ChainCfg()})


def _enr(buyable, liq=None):
    return Enrichment(provider="gt", checked_at=NOW, buyable=buyable, liquidity_usd=liq)


def test_score_example_from_spec():
    total, d = score(make_signal(kind="pr_opened"), _enr(False), {"repo_diff", "ct"}, CFG)
    assert d == {"base": 40, "priority_chain": 15, "earliness": 15, "fusion": 20, "total": 90}
    assert total == 90


def test_score_low_liquidity_is_early():
    _, d = score(make_signal(chain="ethereum"), _enr(True, 20_000), {"repo_diff"}, CFG)
    assert d == {"base": 30, "priority_chain": 0, "earliness": 15, "fusion": 0, "total": 45}


def test_score_deep_liquidity_not_early():
    _, d = score(make_signal(chain="ethereum"), _enr(True, 900_000), set(), CFG)
    assert d["earliness"] == 0


def test_score_cap_100():
    total, d = score(make_signal(kind="pr_opened"), _enr(False), {"repo_diff", "ct", "a", "b", "c"}, CFG)
    assert d["fusion"] == 40
    assert total == 100 and d["total"] == 100


def test_score_no_earliness_when_enrich_failed():
    _, d = score(make_signal(), _enr(None), set(), CFG)
    assert d["earliness"] == 0
    _, d = score(make_signal(), None, set(), CFG)
    assert d["earliness"] == 0


def test_unknown_kind_base_zero():
    _, d = score(make_signal(kind="weird", chain=None), None, set(), CFG)
    assert d["base"] == 0 and d["total"] == 0
