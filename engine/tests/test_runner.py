import asyncio

from conftest import NOW, make_signal
from hades.http import SourceError
from hades.models import ListingEvent, WatchResult
from hades.runner import Runner
from hades.watchers.base import Watcher


class FakeWatcher(Watcher):
    def __init__(self, name, scripted, interval_s=60):
        self.name = name
        self.interval_s = interval_s
        self.scripted = list(scripted)
        self.runs = 0

    async def fetch(self, http, state):
        self.runs += 1
        item = self.scripted.pop(0) if len(self.scripted) > 1 else self.scripted[0]
        if isinstance(item, Exception):
            raise item
        return item

    def parse(self, raw):
        return set(raw)

    def diff(self, state, parsed, now):
        new_state = {"items": sorted(parsed)}
        if state is None:
            return WatchResult(new_state=new_state)
        added = sorted(parsed - set(state["items"]))
        return WatchResult(
            signals=[make_signal(dedupe_key=f"k:{x}", symbol=x) for x in added],
            listings=[ListingEvent("cb", x, x, now, {}) for x in added],
            new_state=new_state,
            domains=[f"{x}.xyz" for x in added],
        )


class Sink:
    def __init__(self, fail_first=False):
        self.got = []
        self.fail_first = fail_first

    async def handle(self, sig):
        if self.fail_first and not self.got and not getattr(self, "_failed", False):
            self._failed = True
            raise RuntimeError("boom")
        self.got.append(sig)


async def _nosleep(_):
    await asyncio.sleep(0)


def _runner(store, watchers, sink, **kw):
    return Runner(store, http=None, watchers=watchers, sink=sink, clock=lambda: NOW, sleep=_nosleep, **kw)


async def test_baseline_emits_nothing(store):
    w, sink = FakeWatcher("w", [["a", "b"]]), Sink()
    assert await _runner(store, [w], sink).run_once(w) is True
    assert sink.got == []
    assert store.get_state("w") == {"items": ["a", "b"]}


async def test_second_run_emits_new_only(store):
    w, sink = FakeWatcher("w", [["a", "b"], ["a", "b", "c"]]), Sink()
    r = _runner(store, [w], sink)
    await r.run_once(w)
    await r.run_once(w)
    assert [s.symbol for s in sink.got] == ["c"]
    assert store.list_listings()[0]["symbol"] == "c"
    assert store.domains()[0]["domain"] == "c.xyz"


async def test_fetch_error_keeps_state(store):
    w, sink = FakeWatcher("w", [["a"], SourceError("down")]), Sink()
    r = _runner(store, [w], sink)
    await r.run_once(w)
    assert await r.run_once(w) is False
    assert store.watchers()[0]["fail_count"] == 1
    assert store.get_state("w") == {"items": ["a"]}


async def test_diff_error_is_contained(store):
    class Bad(FakeWatcher):
        def diff(self, state, parsed, now):
            raise KeyError("x")
    w = Bad("w", [["a"]])
    assert await _runner(store, [w], Sink()).run_once(w) is False
    assert "KeyError" in store.watchers()[0]["last_err"]


async def test_one_watcher_failure_does_not_stop_other(store):
    good = FakeWatcher("good", [["a"]])
    bad = FakeWatcher("bad", [SourceError("down")])
    r = _runner(store, [good, bad], Sink(), stagger_s=0)
    stop = asyncio.Event()
    task = asyncio.create_task(r.run_forever(stop))
    for _ in range(200):
        await asyncio.sleep(0)
        if good.runs >= 3:
            break
    stop.set()
    await asyncio.wait_for(task, 1)
    assert good.runs >= 3
    assert bad.runs >= 1


async def test_sink_error_does_not_abort_batch(store):
    w, sink = FakeWatcher("w", [["a"], ["a", "b", "c"]]), Sink(fail_first=True)
    r = _runner(store, [w], sink)
    await r.run_once(w)
    assert await r.run_once(w) is True
    assert [s.symbol for s in sink.got] == ["c"]


async def test_on_listing_called_for_new_listing(store):
    seen = []

    async def on_listing(ev):
        seen.append(ev.symbol)

    w = FakeWatcher("w", [["a"], ["a", "b"], ["a", "b"]])
    r = _runner(store, [w], Sink(), on_listing=on_listing)
    for _ in range(3):
        await r.run_once(w)
    assert seen == ["b"]
