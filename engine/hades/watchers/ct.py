"""Certificate Transparency watcher (spec 6.2): new subdomains of watched project domains, via crt.sh.

The runner calls it often (RUN_EVERY_S); each domain is only re-polled after per_domain_interval_s.
crt.sh is slow and flaky, so a bad answer for a domain (HTML, non-list, or an empty list where we
previously saw names) is a failure for that domain: its state is kept and it is retried next run.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from hades.http import Http, SourceError
from hades.identity import dedupe_key, project_key
from hades.models import Signal, WatchResult
from hades.store import Store
from hades.watchers.base import Watcher

log = logging.getLogger(__name__)

CRT_URL = "https://crt.sh/"
RUN_EVERY_S = 600
MAX_DOMAINS_PER_RUN = 20
TIMEOUT_S = 60.0
BACKOFF_BASE_S = 1200  # a failing domain waits 20 min, 40, 80 … capped at the normal interval


def _not_after(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class CtWatcher(Watcher):
    name = "ct"
    interval_s = RUN_EVERY_S

    def __init__(self, store: Store, hot_labels: list[str], per_domain_interval_s: int = 21600,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 max_per_run: int = MAX_DOMAINS_PER_RUN) -> None:
        self.store = store
        self.hot_labels = {h.lower() for h in hot_labels}
        self.per_domain = timedelta(seconds=per_domain_interval_s)
        self.clock = clock
        self.max_per_run = max_per_run
        # in-memory per-domain backoff: domain -> (consecutive failures, retry not before)
        self._backoff: dict[str, tuple[int, datetime]] = {}

    def _fail(self, domain: str, now: datetime) -> None:
        n = self._backoff.get(domain, (0, now))[0] + 1
        wait = min(self.per_domain, timedelta(seconds=BACKOFF_BASE_S * 2 ** (n - 1)))
        self._backoff[domain] = (n, now + wait)

    def _due(self, state: dict | None) -> list[str]:
        """Due domains, never-checked first, then the longest-unchecked; failing ones wait out their backoff."""
        now = self.clock()
        known = (state or {}).get("domains", {})
        due: list[tuple[str, str]] = []
        for row in self.store.domains():
            d = row["domain"]
            if d in self._backoff and self._backoff[d][1] > now:
                continue
            checked = known.get(d, {}).get("checked")
            if checked is None or now - datetime.fromisoformat(checked) >= self.per_domain:
                due.append((checked or "", d))
        return [d for _, d in sorted(due)][: self.max_per_run]

    async def fetch(self, http: Http, state: dict | None) -> dict:
        due = self._due(state)
        now = self.clock()
        results: dict[str, Any] = {}
        for d in due:
            try:
                data = await http.get_json(CRT_URL, params={"q": f"%.{d}", "output": "json"}, timeout=TIMEOUT_S,
                                           max_retries=1)
                results[d] = data if isinstance(data, list) else SourceError("crt.sh answer is not a list")
            except SourceError as e:
                results[d] = e
            if isinstance(results[d], Exception):
                self._fail(d, now)
            else:
                self._backoff.pop(d, None)
        if due and all(isinstance(v, Exception) for v in results.values()):
            raise SourceError(f"crt.sh failed for all {len(due)} due domains: {next(iter(results.values()))}")
        return results

    def parse(self, raw: dict) -> dict:
        out: dict[str, Any] = {}
        for d, certs in raw.items():
            if isinstance(certs, Exception):
                out[d] = certs
                continue
            names: dict[str, dict] = {}
            for c in certs:
                if not isinstance(c, dict):
                    continue
                for n in str(c.get("name_value", "")).splitlines():
                    n = n.strip().lower().rstrip(".")
                    if not n or n.startswith("*.") or n == d or not n.endswith("." + d):
                        continue
                    prev = names.get(n)
                    if prev is None or c.get("id", 0) < prev["id"]:
                        names[n] = {"id": c.get("id"), "not_after": _not_after(c.get("not_after"))}
            out[d] = names
        return out

    def diff(self, state: dict | None, parsed: dict, now: datetime) -> WatchResult:
        known: dict[str, dict] = dict((state or {}).get("domains", {}))
        signals: list[Signal] = []
        failed: list[str] = []
        for d, names in parsed.items():
            if isinstance(names, Exception):
                log.warning("ct %s failed: %s", d, names)
                failed.append(d)
                continue
            live = {n: v for n, v in names.items() if v["not_after"] is None or v["not_after"] >= now}
            prev = known.get(d)
            if prev is not None and not names and prev["names"]:
                log.warning("ct %s: empty answer after %d names, treating as outage", d, len(prev["names"]))
                self._fail(d, now)
                failed.append(d)
                continue
            if prev is not None and state is not None:
                for n in sorted(set(live) - set(prev["names"])):
                    label = n.split(".", 1)[0]
                    hot = label in self.hot_labels
                    signals.append(Signal(
                        source="ct", kind="ct_high" if hot else "ct_low", severity="high" if hot else "low",
                        project_key=project_key(domain=d), evidence_url=f"https://crt.sh/?id={live[n]['id']}",
                        observed_at=now, dedupe_key=dedupe_key("ct", n), name=n,
                        raw={"domain": d, "cert_id": live[n]["id"]},
                    ))
            merged = sorted(set(prev["names"] if prev else []) | set(live))
            known[d] = {"checked": now.isoformat(timespec="seconds"), "names": merged}
        if parsed and len(failed) == len(parsed):
            raise SourceError(f"crt.sh unusable for {', '.join(failed)}")
        return WatchResult(signals=signals, new_state={"domains": known})
