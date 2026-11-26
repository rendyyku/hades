from datetime import timedelta

import httpx
import pytest
import respx

from conftest import NOW
from hades.config import HOT_LABELS
from hades.http import Http
from hades.runner import Runner
from hades.watchers.ct import CtWatcher

CRT = "https://crt.sh/"


async def _nosleep(_):
    return None


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield Http(c, rates={"crt.sh": 10_000}, sleep=_nosleep)


def _cert(cid, *names, not_after="2027-06-01T00:00:00"):
    return {"id": cid, "name_value": "\n".join(names), "not_after": not_after}


def _route(domain, *certs, status=200, text=None):
    resp = httpx.Response(status, text=text) if text is not None else httpx.Response(status, json=list(certs))
    return respx.get(CRT, params={"q": f"%.{domain}", "output": "json"}).mock(return_value=resp)


class Sink:
    def __init__(self):
        self.got = []

    async def handle(self, sig):
        self.got.append(sig)


def _runner(store, http, sink, clock=lambda: NOW):
    w = CtWatcher(store, HOT_LABELS, per_domain_interval_s=21600, clock=clock)
    return w, Runner(store, http, [w], sink, clock=clock, sleep=_nosleep)


@respx.mock
async def test_first_seen_domain_is_baseline(store, http):
    store.add_domain("cat.xyz", "manual")
    _route("cat.xyz", _cert(1, "www.cat.xyz"), _cert(2, "claim.cat.xyz"))
    sink = Sink()
    w, r = _runner(store, http, sink)
    assert await r.run_once(w) is True
    assert sink.got == []
    assert sorted(store.get_state("ct")["domains"]["cat.xyz"]["names"]) == ["claim.cat.xyz", "www.cat.xyz"]


async def _baseline_then(store, http, first, second, later=timedelta(hours=7)):
    store.add_domain("cat.xyz", "manual")
    sink = Sink()
    route = _route("cat.xyz", *first)
    w, r = _runner(store, http, sink)
    await r.run_once(w)
    route.mock(return_value=second if isinstance(second, httpx.Response) else httpx.Response(200, json=list(second)))
    r.clock = w.clock = lambda: NOW + later
    ok = await r.run_once(w)
    return ok, sink


@respx.mock
async def test_new_hot_subdomain_high(store, http):
    ok, sink = await _baseline_then(store, http, [_cert(1, "www.cat.xyz")],
                                    [_cert(1, "www.cat.xyz"), _cert(77, "claim.cat.xyz")])
    assert ok
    [s] = sink.got
    assert (s.kind, s.severity, s.source) == ("ct_high", "high", "ct")
    assert s.evidence_url == "https://crt.sh/?id=77"
    assert s.name == "claim.cat.xyz"
    assert s.project_key == "dom:cat.xyz"
    assert s.dedupe_key == "ct:claim.cat.xyz"


@respx.mock
async def test_non_hot_low(store, http):
    _, sink = await _baseline_then(store, http, [_cert(1, "www.cat.xyz")],
                                   [_cert(1, "www.cat.xyz"), _cert(5, "docs.cat.xyz")])
    assert [(s.kind, s.severity) for s in sink.got] == [("ct_low", "low")]


@respx.mock
async def test_wildcard_and_expired_ignored(store, http):
    _, sink = await _baseline_then(store, http, [_cert(1, "www.cat.xyz")], [
        _cert(1, "www.cat.xyz"), _cert(2, "*.cat.xyz"), _cert(3, "claim.cat.xyz", not_after="2026-01-01T00:00:00"),
        _cert(4, "claim.evilcat.xyz"),
    ])
    assert sink.got == []


@respx.mock
async def test_html_error_page_is_failure(store, http):
    store.add_domain("cat.xyz", "manual")
    _route("cat.xyz", text="<html>502 Bad Gateway</html>")
    w, r = _runner(store, http, Sink())
    assert await r.run_once(w) is False
    assert store.get_state("ct") is None


@respx.mock
async def test_empty_list_after_names_is_failure(store, http):
    ok, sink = await _baseline_then(store, http, [_cert(1, "www.cat.xyz")], httpx.Response(200, json=[]))
    assert ok is False
    assert sink.got == []
    assert store.get_state("ct")["domains"]["cat.xyz"]["names"] == ["www.cat.xyz"]


@respx.mock
async def test_one_domain_failing_keeps_others(store, http):
    store.add_domain("cat.xyz", "manual")
    store.add_domain("dog.xyz", "manual")
    _route("cat.xyz", _cert(1, "www.cat.xyz"))
    _route("dog.xyz", text="<html>")
    w, r = _runner(store, http, Sink())
    assert await r.run_once(w) is True
    doms = store.get_state("ct")["domains"]
    assert "cat.xyz" in doms and "dog.xyz" not in doms


@respx.mock
async def test_domain_not_due_not_fetched(store, http):
    ok, _ = await _baseline_then(store, http, [_cert(1, "www.cat.xyz")], [_cert(1, "www.cat.xyz")],
                                 later=timedelta(hours=1))
    assert ok is True
    assert respx.calls.call_count == 1
