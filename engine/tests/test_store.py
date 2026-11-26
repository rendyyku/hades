from datetime import timedelta

from conftest import NOW, make_signal
from hades.models import Enrichment, ListingEvent


def _enr(at):
    return Enrichment(provider="geckoterminal", checked_at=at, buyable=False, pool_count=0)


def test_insert_signal_dedupes(store):
    assert store.insert_signal(make_signal()) is not None
    assert store.insert_signal(make_signal()) is None
    assert len(store.list_signals()) == 1


def test_get_signal_roundtrip(store):
    sid = store.insert_signal(make_signal(raw={"x": 1}))
    row = store.get_signal(sid)
    assert row["symbol"] == "CAT"
    assert row["raw"] == {"x": 1}
    assert row["observed_at"] == NOW
    assert store.get_signal(999) is None


def test_upgrade_signal(store):
    store.insert_signal(make_signal(kind="pr_opened", evidence_url="https://github.com/a/b/pull/1"))
    sid = store.upgrade_signal("repo_diff:a/b:monad:0xabc", "merged", "https://github.com/a/b/commit/2")
    assert sid is not None
    row = store.get_signal(sid)
    assert row["kind"] == "merged"
    assert row["evidence_url"] == "https://github.com/a/b/commit/2"
    assert store.upgrade_signal("repo_diff:a/b:monad:0xabc", "merged", "https://github.com/a/b/commit/2") is None


def test_due_rechecks(store):
    a = store.insert_signal(make_signal(dedupe_key="a"))
    b = store.insert_signal(make_signal(dedupe_key="b"))
    c = store.insert_signal(make_signal(dedupe_key="c"))
    store.add_enrichment(a, _enr(NOW - timedelta(hours=25)))
    store.add_enrichment(b, _enr(NOW - timedelta(hours=1)))
    store.add_enrichment(c, _enr(NOW - timedelta(hours=30)))
    store.add_enrichment(c, _enr(NOW - timedelta(hours=2)))
    assert store.due_rechecks(NOW, 86400) == [a]


def test_enrichments_roundtrip(store):
    sid = store.insert_signal(make_signal())
    store.add_enrichment(sid, Enrichment(provider="gt", checked_at=NOW, buyable=True, liquidity_usd=12500.0,
                                         candidates=[{"pool": "x"}]))
    rows = store.enrichments(sid)
    assert rows[0]["buyable"] is True
    assert rows[0]["liquidity_usd"] == 12500.0
    assert rows[0]["candidates"] == [{"pool": "x"}]


def test_sources_for_project_uses_alias(store):
    store.insert_signal(make_signal())
    store.insert_signal(make_signal(source="ct", kind="ct_high", project_key="dom:cat.xyz", dedupe_key="ct:claim.cat.xyz"))
    store.add_alias("monad:0xabc", "dom:cat.xyz")
    since = NOW - timedelta(days=7)
    assert store.sources_for_project("monad:0xabc", since) == {"repo_diff", "ct"}
    assert store.resolve("dom:cat.xyz") == "monad:0xabc"
    assert store.resolve("dom:other.xyz") == "dom:other.xyz"


def test_mutes_expire(store):
    store.add_mute("monad", NOW - timedelta(hours=1))
    store.add_mute("ct", NOW + timedelta(hours=1))
    assert [m["target"] for m in store.active_mutes(NOW)] == ["ct"]
    assert store.remove_mute("ct") is True
    assert store.remove_mute("ct") is False


def test_list_signals_limit_capped(store):
    for i in range(205):
        store.insert_signal(make_signal(dedupe_key=f"k{i}"))
    assert len(store.list_signals(limit=1000)) == 200


def test_list_signals_filters(store):
    a = store.insert_signal(make_signal(dedupe_key="a"))
    store.insert_signal(make_signal(dedupe_key="b", source="ct"))
    store.set_score(a, 80, {"total": 80})
    assert [r["id"] for r in store.list_signals(min_score=50)] == [a]
    assert len(store.list_signals(source="ct")) == 1


def test_state_and_health(store):
    assert store.get_state("w") is None
    store.save_state("w", {"head": "abc"})
    assert store.get_state("w") == {"head": "abc"}
    store.record_error("w", "SourceError: boom", NOW)
    store.record_error("w", "SourceError: boom", NOW)
    store.record_error("w", "SourceError: boom", NOW)
    w = store.watchers()[0]
    assert w["fail_count"] == 3 and w["healthy"] is False
    store.record_ok("w", NOW)
    assert store.watchers()[0]["healthy"] is True


def test_listings_and_domains(store):
    ev = ListingEvent("coinbase", "CAT-USD", "CAT", NOW, {})
    assert store.insert_listing(ev) is True
    assert store.insert_listing(ev) is False
    assert store.list_listings()[0]["base_asset"] == "CAT"
    assert store.add_domain("cat.xyz", "auto") is True
    assert store.add_domain("cat.xyz", "manual") is False
    assert store.domains()[0]["origin"] == "auto"
    assert store.remove_domain("cat.xyz") is True


def test_counts_and_mark(store):
    sid = store.insert_signal(make_signal())
    store.mark_sent(sid, 42, NOW)
    store.mark_listed(sid, "coinbase", NOW)
    store.insert_listing(ListingEvent("kraken", "CAT/USD", "CAT", NOW, {}))
    assert store.counts_since(NOW - timedelta(days=1)) == {"signals": 1, "alerts": 1, "listings": 1}
    assert store.get_signal(sid)["listed_on"][0]["exchange"] == "coinbase"
