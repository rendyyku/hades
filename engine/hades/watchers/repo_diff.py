"""repo_diff watcher (spec 6.1): new tokens in token lists / asset registries of chosen repos.

fetch() does all GitHub I/O and returns a structured snapshot; diff() turns it into signals.
Open PRs touching the path count too (kind pr_opened, the earliest trace); a later merge of the
same token has the same dedupe key, so the pipeline upgrades it instead of alerting again.
"""

import json
import logging
import re
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from hades.config import DOMAIN_DENYLIST, ChainCfg, RepoTarget
from hades.github import GitHub
from hades.http import SourceError
from hades.identity import dedupe_key, normalize_address, project_key, registered_domain
from hades.models import Signal, WatchResult
from hades.watchers.base import Watcher

log = logging.getLogger(__name__)

MAX_COMMITS_PER_RUN = 10
MAX_PRS_PER_RUN = 20
# full-mode snapshot below this share of the known keys = broken/refactored file, not a mass delisting
SHRINK_RATIO = 0.5
# more additions than this in one commit = a bulk onboarding batch: still recorded, but as low severity (digest)
BATCH_LOW_AFTER = 25


def build_repo_watchers(target: RepoTarget, gh: GitHub, interval_s: int, chains: dict[str, ChainCfg],
                        domain_denylist: list[str] | None = None) -> list["RepoDiffWatcher"]:
    """One watcher per expanded path: `{chain}` in the path becomes each folder in target.chains."""
    deny = DOMAIN_DENYLIST if domain_denylist is None else domain_denylist
    if "{chain}" not in target.path:
        return [RepoDiffWatcher(target, gh, interval_s, chains, target.path, None, deny)]
    return [RepoDiffWatcher(target, gh, interval_s, chains, target.path.replace("{chain}", f), f, deny)
            for f in target.chains]


def _website_domain(url: Any) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    host = urlsplit(url if "//" in url else f"//{url}").hostname
    return registered_domain(host) if host else None


class RepoDiffWatcher(Watcher):
    def __init__(self, target: RepoTarget, gh: GitHub, interval_s: int, chains: dict[str, ChainCfg],
                 path: str, folder: str | None, domain_denylist: list[str] | None = None) -> None:
        self.target = target
        self.gh = gh
        self.interval_s = interval_s
        self.chains = chains
        self.path = path.rstrip("/")
        self.folder = folder
        self.name = f"repo_diff:{target.repo}:{self.path}"
        # full = each snapshot is the whole list, diffed against the keys in state;
        # dir_added snapshots already contain only the additions
        self.full = target.mode != "dir_added"
        self.denylist = [d.lower() for d in (DOMAIN_DENYLIST if domain_denylist is None else domain_denylist)]

    # -- entries ------------------------------------------------------------

    def _chain(self, raw: str | None) -> str | None:
        if raw is None:
            return self.target.chain_map.get("*")
        return self.target.chain_map.get(raw, raw if self.target.mode == "dir_added" else None)

    def _key(self, e: dict) -> str:
        if e.get("address"):
            return f"{e['chain']}:{normalize_address(self.chains.get(e['chain']), e['address'])}"
        return f"{e['chain']}:sym:{(e.get('symbol') or '').lower()}"

    def _parse_file(self, text: str) -> list[dict]:
        out: list[dict] = []
        if self.target.mode == "json_tokenlist":
            try:
                tokens = json.loads(text).get("tokens", [])
            except (json.JSONDecodeError, AttributeError):
                log.warning("%s: token list is not valid JSON", self.name)
                return []
            for t in tokens:
                if not isinstance(t, dict):
                    continue
                chain = self.target.chain_map.get(str(t.get("chainId")))
                if chain and t.get("address"):
                    out.append({"chain": chain, "address": t["address"], "symbol": t.get("symbol"),
                                "name": t.get("name"), "website": None})
        else:
            for m in re.finditer(self.target.regex or "(?!)", text):
                g = m.groupdict()
                chain = self._chain(g.get("chain"))
                if chain and g.get("symbol"):
                    out.append({"chain": chain, "address": g.get("address"), "symbol": g["symbol"],
                                "name": g.get("name"), "website": None})
        return out

    async def _dir_entries(self, files: list[dict], ref: str) -> list[dict]:
        """An added `<path>/<dir>/info.json` = one new token; info.json gives symbol/name/website."""
        out = []
        prefix = self.path + "/"
        for f in files:
            name = f.get("filename", "")
            if f.get("status") != "added" or not name.startswith(prefix):
                continue
            parts = name[len(prefix):].split("/")
            if len(parts) != 2 or parts[1] != "info.json":
                continue
            text = await self.gh.file(self.target.repo, name, ref)
            try:
                info = json.loads(text) if text else {}
            except json.JSONDecodeError:
                info = {}
            if not isinstance(info, dict):
                info = {}
            out.append({"chain": self._chain(self.folder), "address": parts[0], "symbol": info.get("symbol"),
                        "name": info.get("name"), "website": _website_domain(info.get("website"))})
        return out

    async def _file_entries(self, ref: str) -> list[dict] | None:
        text = await self.gh.file(self.target.repo, self.path, ref)
        return None if text is None else self._parse_file(text)

    # -- fetch --------------------------------------------------------------

    async def _baseline(self, kind: str, head: str, open_prs: dict[str, str]) -> dict:
        keys: list[str] = []
        if self.full:
            keys = sorted({self._key(e) for e in (await self._file_entries(head) or [])})
            if not keys:
                # an empty baseline would make every token in the next good commit "new"
                raise SourceError(f"{self.name}: file parses to 0 entries at {head[:7]}; check path/regex")
        return {"kind": kind, "head": head, "keys": keys, "open_prs": open_prs, "commits": [], "prs": []}

    async def fetch(self, http: Any, state: dict | None) -> dict:
        repo = self.target.repo
        commits = await self.gh.commits(repo, self.path)
        pulls = (await self.gh.open_pulls(repo))[:MAX_PRS_PER_RUN]
        open_prs = {str(p["number"]): p["head"]["sha"] for p in pulls}
        if not commits:
            raise SourceError(f"{self.name}: no commits for path")
        newest = commits[0]["sha"]
        if state is None:
            return await self._baseline("baseline", newest, open_prs)

        shas = [c["sha"] for c in commits]
        if state["head"] in shas:
            new = list(reversed(commits[: shas.index(state["head"])]))
        else:
            try:
                cmp = await self.gh.compare(repo, state["head"], newest)
            except SourceError as e:
                if e.status != 404:
                    raise
                cmp = {"status": "gone"}
            # GitHub keeps orphaned commits reachable: after a force-push compare answers 200 "diverged"
            if cmp.get("status") != "ahead":
                log.warning("%s: head %s is %s relative to %s (force-push?), re-baselining",
                            self.name, state["head"], cmp.get("status"), newest)
                return await self._baseline("rebaseline", newest, open_prs)
            log.warning("%s: more than %d new commits since %s, older ones skipped",
                        self.name, len(commits), state["head"])
            new = list(reversed(commits))
        new = new[:MAX_COMMITS_PER_RUN]

        out_commits = []
        for c in new:
            if self.full:
                entries = await self._file_entries(c["sha"])
                if entries is None:  # file missing at this commit: keep the running keys
                    continue
            else:
                detail = await self.gh.commit(repo, c["sha"])
                entries = await self._dir_entries(detail.get("files", []), c["sha"])
            url = c.get("html_url") or f"https://github.com/{repo}/commit/{c['sha']}"
            if entries:
                prs = await self.gh.commit_pulls(repo, c["sha"])
                if prs:
                    url = prs[0]["html_url"]
            out_commits.append({"sha": c["sha"], "url": url, "entries": entries})

        out_prs = []
        seen = state.get("prs", {})
        for p in pulls:
            num, sha = str(p["number"]), p["head"]["sha"]
            if seen.get(num) == sha:
                continue
            files = await self.gh.pull_files(repo, p["number"])
            if self.full:
                touched = any(f.get("filename") == self.path for f in files)
                entries = (await self._file_entries(sha) or []) if touched else []
            else:
                entries = await self._dir_entries(files, sha)
            out_prs.append({"number": num, "sha": sha, "url": p["html_url"], "entries": entries})

        head = new[-1]["sha"] if new else state["head"]
        return {"kind": "update", "head": head, "keys": state.get("keys", []), "open_prs": open_prs,
                "commits": out_commits, "prs": out_prs}

    def parse(self, raw: dict) -> dict:
        return raw

    # -- diff ---------------------------------------------------------------

    def _signal(self, e: dict, kind: str, url: str, ref: dict, now: datetime) -> Signal:
        return Signal(
            source="repo_diff", kind=kind,
            project_key=project_key(chain=e["chain"], address=e.get("address"), symbol=e.get("symbol"),
                                    chains=self.chains),
            evidence_url=url, observed_at=now,
            dedupe_key=dedupe_key("repo_diff", self.target.repo, e["chain"], e.get("address") or e.get("symbol")),
            chain=e["chain"], address=e.get("address"), symbol=e.get("symbol"), name=e.get("name"),
            website=e.get("website"), raw={"repo": self.target.repo, "path": self.path, **ref},
        )

    def _added(self, entries: list[dict], running: set[str]) -> tuple[list[dict], set[str]]:
        if not self.full:
            return entries, running
        cur = {self._key(e): e for e in entries}
        if running and len(cur) < SHRINK_RATIO * len(running):
            log.warning("%s: snapshot has %d entries vs %d known; skipped as broken", self.name, len(cur), len(running))
            return [], running
        return [cur[k] for k in sorted(set(cur) - running)], set(cur)

    def _watchable(self, domain: str | None) -> bool:
        return bool(domain) and not any(domain == d or domain.endswith("." + d) for d in self.denylist)

    def diff(self, state: dict | None, snap: dict, now: datetime) -> WatchResult:
        if snap["kind"] != "update":
            return WatchResult(new_state={"head": snap["head"], "prs": snap["open_prs"], "keys": snap["keys"]})
        signals: list[Signal] = []
        running = set(snap["keys"])
        for c in snap["commits"]:
            added, running = self._added(c["entries"], running)
            batch = [self._signal(e, "merged", c["url"], {"sha": c["sha"]}, now) for e in added]
            if len(batch) > BATCH_LOW_AFTER:
                log.info("%s: %d additions in %s, recorded as low severity", self.name, len(batch), c["sha"][:7])
                batch = [replace(s, severity="low") for s in batch]
            signals += batch
        for p in snap["prs"]:
            added, _ = self._added(p["entries"], running)
            signals += [self._signal(e, "pr_opened", p["url"], {"pr": int(p["number"]), "sha": p["sha"]}, now)
                        for e in added]
        new_state = {"head": snap["head"], "prs": snap["open_prs"], "keys": sorted(running)}
        # only merged entries feed the CT watchlist: anyone can open a PR with any "website"
        domains = sorted({s.website for s in signals if s.kind == "merged" and self._watchable(s.website)})
        return WatchResult(signals=signals, new_state=new_state, domains=domains)
