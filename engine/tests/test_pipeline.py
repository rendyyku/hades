from datetime import timedelta

import pytest

from conftest import NOW, make_signal
from hades.bus import Bus
from hades.config import AppConfig, ChainCfg
from hades.models import Enrichment, ListingEvent
from hades.pipeline import Pipeline

CFG = AppConfig(chains={"monad": ChainCfg(geckoterminal="monad", priority=True)})


class FakeNotifier:
    def __init__(self, muted=()):
        self.sent = []
        self.muted = set(muted)

    def is_muted(self, sig, now):
        get = sig.get if isinstance(sig, dict) else lambda k: getattr(sig, k)
        return bool(self.muted & {get("project_key"), get("source"), get("chain")})

    async def send_signal(self, signal_id, prefix=""):
        self.sent.append((signal_id, prefix))
        return 100 + len(self.sent)


class FakeEnrich:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, http, chain, address, symbol, **kw):
        self.calls.append((chain, address, symbol))
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return Enrichment(provider="gt", checked_at=kw["now"], buyable=r)


@pytest.fixture
def make(store, monkeypatch):
    def _make(*enrich, muted=()):
        fe = FakeEnrich(*(enrich or (False,)))
        monkeypatch.setattr("hades.pipeline.enrich_check", fe)
        n = FakeNotifier(muted)
        bus = Bus()
        q = bus.subscribe()
        p = Pipeline(store, http=None, cfg=CFG, notifier=n, bus=bus, clock=lambda: NOW)
        return p, n, q, fe
    return _make


async def test_pipeline_alerts_high_score(store, make):
    p, n, q, _ = make()
    await p.handle(make_signal(kind="pr_opened"))  # 40 + 15 + 15 = 70
    [(sid, prefix)] = n.sent
    row = store.get_signal(sid)
    assert row["score"] == 70 and row["score_detail"]["total"] == 70
    assert store.counts_since(NOW - timedelta(days=1))["alerts"] == 1
    event, data = q.get_nowait()
    assert event == "signal"
    assert data["enrichment"]["buyable"] is False
    assert "raw" not in data


async def test_pipeline_below_threshold_no_alert(store, make):
    p, n, _, _ = make(True)
    await p.handle(make_signal(chain="other", source="ct", kind="ct_low", severity="high"))
    assert n.sent == []


async def test_pipeline_no_alert_low_severity(store, make):
    p, n, _, _ = make()
    await p.handle(make_signal(kind="pr_opened", severity="low"))
    assert n.sent == []
    assert store.list_signals()[0]["score"] == 70


async def test_pipeline_muted_no_alert(store, make):
    p, n, _, _ = make(muted={"monad"})
    await p.handle(make_signal(kind="pr_opened"))
    assert n.sent == []


async def test_pipeline_duplicate_upgrades_without_alert(store, make):
    p, n, q, _ = make()
    await p.handle(make_signal(kind="pr_opened", evidence_url="https://github.com/a/b/pull/1"))
    await p.handle(make_signal(kind="merged", evidence_url="https://github.com/a/b/commit/2"))
    assert len(n.sent) == 1
    [row] = store.list_signals()
    assert row["kind"] == "merged"
    events = [q.get_nowait()[0] for _ in range(q.qsize())]
    assert events == ["signal", "signal_updated"]


async def test_pipeline_fusion_via_website_alias(store, make):
    p, _, _, fe = make()
    await p.handle(make_signal(website="cat.xyz"))
    await p.handle(make_signal(source="ct", kind="ct_high", project_key="dom:cat.xyz", dedupe_key="ct:claim.cat.xyz",
                               chain=None, address=None, symbol=None, name="claim.cat.xyz"))
    ct = store.list_signals()[0]
    assert ct["score_detail"]["fusion"] == 20
    # the CT signal is enriched with the token the domain belongs to
    assert fe.calls[-1][1:] == ("0xabc", None)


async def test_recheck_turns_buyable_notifies(store, make):
    p, n, _, _ = make(False, True)
    await p.handle(make_signal(kind="pr_opened"))
    later = NOW + timedelta(hours=25)
    p.clock = lambda: later
    await p.recheck_due(later)
    assert len(store.enrichments(1)) == 2
    assert n.sent[-1] == (1, "Sekarang bisa dibeli")
    await p.recheck_due(later)
    assert len(store.enrichments(1)) == 2


async def test_match_listing_marks_signal(store, make):
    p, _, _, _ = make()
    await p.handle(make_signal(symbol="Cat"))
    await p.match_listing(ListingEvent("coinbase", "CAT-USD", "CAT", NOW + timedelta(days=2), {}))
    assert store.get_signal(1)["listed_on"][0]["exchange"] == "coinbase"


async def test_match_listing_ignores_old_signals(store, make):
    p, _, _, _ = make()
    await p.handle(make_signal(symbol="CAT"))
    await p.match_listing(ListingEvent("coinbase", "CAT-USD", "CAT", NOW + timedelta(days=31), {}))
    assert store.get_signal(1)["listed_on"] == []
