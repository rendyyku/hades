# HADES v2 Early Signals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Solana smart-money engine with an early-signal engine (repo diff, Certificate Transparency, CEX ground truth, on-chain liquidity check, scoring, Telegram, web dashboard, evaluation).

**Architecture:** One async Python process (`python -m hades run`) runs plugin watchers on their own intervals. New items go through a pipeline: dedupe → project resolution → enrichment → score → Telegram/SSE. Everything lands in SQLite. The same process serves a FastAPI read/write API, which the existing Next.js dashboard reaches through its server-side proxy.

**Tech Stack:** Python 3.12, httpx, SQLAlchemy 2 (sync, SQLite WAL), pydantic / pydantic-settings, PyYAML, tldextract (offline PSL), FastAPI + sse-starlette, typer, pytest + pytest-asyncio + respx. Web: Next.js 15, Tailwind v4, vitest.

**Spec:** `docs/superpowers/specs/2026-11-27-hades-early-signals-design.md`

## Global Constraints

- Secrets only in `.env`: `GITHUB_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `COINGECKO_API_KEY` (optional), `HADES_API_KEY`. Everything else lives in `config.yaml`. No real values in `.env.example`, fixtures, or logs.
- No private keys, signing, auto-buy, or copy-trade code anywhere.
- Every alert carries a non-empty `evidence_url` (commit/PR URL or crt.sh certificate URL). A `Signal` without one is rejected at construction.
- Store times in UTC. Show the owner times in WIB (UTC+7) as `27 Nov 2026 14:05 WIB`.
- Only documented public endpoints; respect rate limits; send `User-Agent: hades-signals/2.0 (+github.com/rendyyku/hades)`.
- First successful snapshot of any source is a silent baseline: no signals, no listing events.
- One failing source never stops another watcher, and never raises out of the runner.
- Tests never touch the live network (respx + fixtures in `engine/tests/fixtures/`).
- API convention kept from v1: routes under `/api`, header `X-Hades-Key`, constant-time compare, empty key → 503 (fail closed), `/healthz` public.
- Defaults from spec: buyable ≥ $1,000 liquidity; low-liquidity earliness < $50,000; re-check at T+86,400 s; alert threshold 40; fusion window 7 days; digest 08:00 WIB; ct poll 21,600 s; ground_truth poll 600 s; repo_diff poll 900 s.
- Commits: conventional commits, ending with the session's attribution lines.
- Python commands run from `engine/`: `.venv/Scripts/python.exe -m pytest -q`. Web: `npm test`, `npm run build` from `web/`.

## Review Focus

1. **GitHub cursor SHA disappears** (force-push or history rewrite): the watcher must re-baseline silently, not re-alert every token. Tested in Task 5.
2. **crt.sh returns an HTML error page, a timeout, or `[]` during an outage**: this counts as a failure, not as "zero certs", and state is not overwritten. Tested in Task 6.
3. **A CEX endpoint returns a truncated list during an outage**: if the snapshot shrinks by more than 50% versus state, treat it as a failure so recovery does not create thousands of "new" listings. Tested in Task 7.
4. **EVM address case** (checksum vs lowercase in different lists): the same token must give the same `project_key` and dedupe key. Solana addresses stay case-sensitive. Tested in Task 9.
5. **Token names with `<`, `&`, `_`, `*`** break Telegram formatting: use HTML parse mode with escaping, and keep messages ≤ 4,096 chars. Commands from other chat IDs are ignored. Tested in Tasks 10–11.

---

### Task 1: Clean slate and skeleton

**Files:**
- Delete: `core/`, `hyperliquid/`, `scratch/`, `hades.db*`, `engine/uv.lock` (untracked), `engine/hades/{insider,pipeline,pnl,realtime,scoring}.py`, `engine/hades/{ingest,sources,api}/`, `engine/hades/db.py`, all of `engine/tests/*` except `conftest.py` (rewrite it), `scripts/dev.sh`
- Delete in web: `app/{arena,hyperliquid,token,wallet,wallets,live,watchlist}/`, `app/api/heads/`, and components/libs not in the keep-list below, plus their tests
- Keep in web: `app/layout.tsx`, `app/globals.css`, `app/page.tsx` (replaced by a stub), `components/{Nav,LiveBadge,ErrorCard,Section,StatCard,ScrollRegion,ThreadsOverlay,Sparkline}.tsx`, `lib/{format,originGuard,contrast,heroThreads,query,api,live,streamMerge,alertsProxy,watchlistProxy}.ts` (+ tests). `api.ts`, `live.ts`, `streamMerge.ts`, and the proxies are rewritten in Tasks 13–14. Here, cut them to compile: remove wallet/HL types and keep `apiGet`.
- Modify: `engine/pyproject.toml` (description, deps: add `pyyaml`, `tldextract`; script `hades = "hades.cli:app"` stays), `AGENTS.md`, `README.md` (stub: "HADES v2, see spec"), `docs/superpowers/specs/2026-11-19-hades-design.md` + `2026-11-20-...` (add line 2: `Status: superseded by 2026-11-27-hades-early-signals-design.md`)
- Create: `engine/hades/__main__.py` (calls `cli.app()`), `engine/hades/cli.py` (typer app with a placeholder `version` command), `engine/tests/test_smoke.py`

- [ ] **Step 1:** `git tag hades-solana-final` on the current HEAD (before any deletion). Verify with `git tag --list hades-solana-final`.
- [ ] **Step 2:** Delete and trim the files listed above. The web home page becomes a stub using `Section`, with the heading "HADES · Early Signals".
- [ ] **Step 3:** Write `AGENTS.md`. Keep the secrets / no-keys / conventional-commit rules. Replace the PnL line with "Every alert must carry an evidence URL (commit, PR, or crt.sh certificate)". Point the spec/plan lines at the 2026-11-27 files.
- [ ] **Step 4:** `test_smoke.py::test_cli_version` runs `typer.testing.CliRunner().invoke(app, ["version"])` and asserts exit code 0 and that `"2.0"` is in the output.
- [ ] **Step 5:** Run `.venv/Scripts/python.exe -m pip install -e ".[dev]"` then `-m pytest -q` → 1 passed. In `web/`: `npm test` and `npm run build` both pass.
- [ ] **Step 6:** Commit `chore: clear v1 engine and web pages for HADES v2`.

### Task 2: Config, models, HTTP client

**Files:**
- Create: `engine/hades/config.py`, `engine/hades/models.py`, `engine/hades/http.py`, `config.example.yaml`, `.env.example`
- Test: `engine/tests/test_config.py`, `engine/tests/test_models.py`, `engine/tests/test_http.py`

**Interfaces (Produces):**
- `Settings(BaseSettings)`: `GITHUB_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `COINGECKO_API_KEY`, `HADES_API_KEY` (all `str = ""`), `DATABASE_URL = "sqlite:///./hades.db"`, `HADES_CONFIG = "config.yaml"`; env files `(".env", "../.env")`; `get_settings()` with lru_cache.
- `ChainCfg(geckoterminal: str | None, dexscreener: str | None, evm: bool = True, priority: bool = False)`
- `RepoTarget(repo: str, path: str, mode: Literal["dir_added","json_tokenlist","regex_entries"], chains: list[str] = [], chain_map: dict[str,str] = {}, regex: str | None = None)`. `chain_map` maps the repo's folder or chainId to our chain key.
- `AppConfig` with sections `chains: dict[str, ChainCfg]`, `repo_diff(interval_s=900, targets)`, `ct(interval_s=21600, domains=[], hot_labels=[claim, airdrop, stake, staking, points, tge, token, bridge, rewards, app])`, `ground_truth(interval_s=600, exchanges=[coinbase, kraken, okx, binance, bybit])`, `enrich(min_liquidity_usd=1000, low_liquidity_usd=50000, recheck_after_s=86400)`, `scoring(base={pr_opened:40, merged:30, ct_high:30, ct_low:10}, priority_chain=15, earliness=15, fusion_per_source=20, fusion_max=40, fusion_window_days=7)`, `alerts(min_score=40, digest_hour_wib=8)`. `load_config(path: str | Path) -> AppConfig` raises `ConfigError` with the YAML path of the bad field.
- `models.Signal` (frozen dataclass): `source, kind, project_key, evidence_url, observed_at: datetime, dedupe_key, chain=None, address=None, symbol=None, name=None, website=None, severity: Literal["high","low"]="high", raw: dict = {}`. `__post_init__` raises `ValueError` if `evidence_url` is empty or `observed_at` is naive.
- `models.ListingEvent(exchange, symbol, base_asset, first_seen_at: datetime, raw: dict)`
- `models.WatchResult(signals: list[Signal] = [], listings: list[ListingEvent] = [], new_state: dict | None = None, domains: list[str] = [])`. `new_state=None` means "do not change state".
- `models.Enrichment(provider, checked_at, buyable: bool | None, liquidity_usd: float | None = None, dex=None, pool_url=None, volume_24h=None, pool_count: int = 0, verified: bool = True, candidates: list[dict] = [], error: str | None = None)`
- `http.SourceError(Exception)`, `http.TokenBucket` (copy from v1 `helius.py:125-147`), `http.Http(client: httpx.AsyncClient, rates: dict[str, float], sleep=asyncio.sleep)` with `async get_json(url, *, params=None, headers=None, max_retries=4) -> Any` and `async get_text(...) -> str`. Per-host bucket (default 2 rps). Retries on 429/5xx/network errors with delay `min(60, 2**attempt) * uniform(0.5, 1.5)`, honouring `Retry-After`. Raises `SourceError` on any other 4xx, on exhausted retries, or when the body is not valid JSON. The User-Agent is applied to every request.

- [ ] **Step 1: Failing tests**
  - `test_load_config_defaults`: minimal YAML `chains: {}` → `cfg.alerts.min_score == 40`, `cfg.enrich.min_liquidity_usd == 1000`, `cfg.ct.interval_s == 21600`.
  - `test_load_config_bad_mode`: target with `mode: foo` → `ConfigError`, and the message contains `repo_diff.targets.0.mode`.
  - `test_config_example_loads`: `load_config("../config.example.yaml")` succeeds.
  - `test_signal_requires_evidence`: `Signal(..., evidence_url="")` → `ValueError`. Same for naive `observed_at`.
  - `test_get_json_retries_then_ok`: respx returns 429 (with `Retry-After: 0`), then 200 `{"a":1}`. Result is `{"a":1}`, there are 2 calls, and the fake sleep was called once.
  - `test_get_json_html_body_raises`: 200 with `<html>` → `SourceError`.
  - `test_get_json_404_no_retry`: 404 → `SourceError`, 1 call.
- [ ] **Step 2:** Run `-m pytest tests/test_config.py tests/test_models.py tests/test_http.py -q` → fail (import errors).
- [ ] **Step 3:** Implement. Write `config.example.yaml` with the four priority chains (`robinhood`, `megaeth`, `monad`, `hyperevm`, `priority: true`) plus `ethereum`, `base`, `solana` (`evm: false` for solana). Network IDs are placeholders marked `# verify (Task 15)`. Seed the three targets from spec 6.1. `.env.example` lists the variables with empty values.
- [ ] **Step 4:** Run the tests → pass.
- [ ] **Step 5:** Commit `feat(engine): config, signal models, retrying http client`.

### Task 3: Store (SQLite)

**Files:**
- Create: `engine/hades/store.py`
- Test: `engine/tests/test_store.py`

**Interfaces (Produces):** `Store(url: str)`. It creates the tables in spec §10 on init, sets WAL + `busy_timeout=5000`, and adds `signals.score_detail` (JSON), `signals.website`, `signals.severity`, and `signals.listed_on` (JSON list). Methods (sync, called directly; they are fast):
- `get_state(name) -> dict | None`, `save_state(name, state: dict)`, `record_ok(name, at)`, `record_error(name, err: str, at)`, `watchers() -> list[dict]` (name, last_ok, last_err, fail_count, healthy = fail_count < 3)
- `insert_signal(sig: Signal) -> int | None` (None on duplicate `dedupe_key`), `upgrade_signal(dedupe_key, kind, evidence_url) -> int | None` (returns id if it changed), `set_score(id, score: int, detail: dict)`, `get_signal(id) -> dict | None`, `list_signals(*, source=None, chain=None, min_score=None, buyable=None, before_id=None, limit=50) -> list[dict]` (newest first, limit capped at 200)
- `add_enrichment(signal_id, e: Enrichment)`, `enrichments(signal_id) -> list[dict]`, `due_rechecks(now, after_s) -> list[int]` (signals with exactly one enrichment, older than `after_s`)
- `insert_listing(ev: ListingEvent) -> bool`, `list_listings(limit=100, exchange=None) -> list[dict]`, `mark_listed(signal_id, exchange, at)`
- `sources_for_project(project_key, since) -> set[str]` (distinct `source` of signals with that key, or with an alias of it, since `since`)
- `add_alias(project_key, alias)`, `resolve(key) -> str` (alias → canonical project_key, else key)
- `add_domain(domain, origin: Literal["manual","auto"]) -> bool`, `remove_domain(domain) -> bool`, `domains() -> list[dict]`
- `add_mute(target, until)`, `remove_mute(target) -> bool`, `active_mutes(now) -> list[dict]`
- `mark_sent(signal_id, message_id, at)`, `signals_since(since, severity=None) -> list[dict]`, `counts_since(since) -> dict` (signals, alerts, listings)

- [ ] **Step 1: Failing tests** (`Store("sqlite://")` in-memory, using `StaticPool`):
  - `test_insert_signal_dedupes`: second insert with the same `dedupe_key` → `None`; `list_signals()` has 1 row.
  - `test_upgrade_signal`: `pr_opened` → `upgrade_signal(key, "merged", url2)` returns the id; the row has kind `merged` and evidence `url2`. Upgrading again with the same values → `None`.
  - `test_due_rechecks`: a signal with one enrichment 25 h old is due; with one 1 h old, or with two enrichments, it is not.
  - `test_sources_for_project_uses_alias`: signal A (`repo_diff`, key `monad:0xabc`), signal B (`ct`, key `dom:cat.xyz`), and `add_alias("monad:0xabc","dom:cat.xyz")` → `sources_for_project("monad:0xabc", since) == {"repo_diff","ct"}`.
  - `test_mutes_expire`: a mute with `until` in the past is not in `active_mutes(now)`.
  - `test_list_signals_limit_capped`: `limit=1000` returns at most 200.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement with SQLAlchemy Core or ORM (implementer's choice). JSON columns go through `sqlalchemy.JSON`. Datetimes are stored as UTC-aware ISO strings or `DateTime(timezone=True)`, and are returned as aware datetimes.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): sqlite store for signals, state, listings, mutes`.

### Task 4: Watcher base and runner

**Files:**
- Create: `engine/hades/watchers/__init__.py`, `engine/hades/watchers/base.py`, `engine/hades/runner.py`
- Test: `engine/tests/test_runner.py`

**Interfaces:**
- Consumes: `Store`, `Http`, `WatchResult`, `Signal`.
- Produces: `class Watcher(ABC)` with `name: str`, `interval_s: int`, `async fetch(self, http: Http, state: dict | None) -> Any`, `parse(self, raw: Any) -> Any`, `diff(self, state: dict | None, parsed: Any, now: datetime) -> WatchResult`. Contract: when `state is None`, `diff` returns `signals=[]`, `listings=[]` and a populated `new_state` (baseline).
- `class SignalSink(Protocol)`: `async handle(self, sig: Signal) -> None`.
- `Runner(store: Store, http: Http, watchers: list[Watcher], sink: SignalSink, clock=lambda: datetime.now(UTC), sleep=asyncio.sleep)` with `async run_once(w: Watcher) -> bool` and `async run_forever(stop: asyncio.Event)`. `run_forever` starts one loop per watcher with a staggered start (`i * 5 s`).
- `run_once` order: `state = store.get_state(w.name)` → fetch → parse → diff. Then `sink.handle` each signal, `store.insert_listing` each listing, `store.add_domain(d, "auto")` each domain, and `save_state` if `new_state is not None`. Finally `record_ok`. Any exception (including in `diff`) → `record_error(name, f"{type}: {msg}")`, state not saved, return False. `sink.handle` exceptions are logged per signal and do not abort the batch.

- [ ] **Step 1: Failing tests** (`FakeWatcher` whose fetch returns a scripted list):
  - `test_baseline_emits_nothing`: first run with items {a, b} → the sink got 0 signals, and the state is saved.
  - `test_second_run_emits_new_only`: then {a, b, c} → the sink got exactly the signal for c.
  - `test_fetch_error_keeps_state`: fetch raises `SourceError` → `run_once` is False, `watchers()[0]["fail_count"] == 1`, and the state is unchanged.
  - `test_one_watcher_failure_does_not_stop_other`: two watchers, one always raising. After `run_forever` runs with a fake sleep for 3 cycles, then stop, the good watcher has 3 successful runs.
  - `test_sink_error_does_not_abort_batch`: a sink raising on the first signal still receives the second.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): watcher interface and fault-isolated runner`.

### Task 5: repo_diff watcher

**Files:**
- Create: `engine/hades/watchers/repo_diff.py`, `engine/hades/github.py`
- Test: `engine/tests/test_repo_diff.py`, fixtures `engine/tests/fixtures/github/*.json`

**Interfaces:**
- Consumes: `Watcher`, `Http`, `RepoTarget`, `Signal`, `project_key()` from Task 9. Until Task 9 lands, import from `hades.identity` and implement `project_key` there first. If running tasks in order, create `identity.py` here with `project_key` and `dedupe_key` exactly as specified in Task 9, and Task 9 adds the rest.
- `github.GitHub(http: Http, token: str)`: `async commits(repo, path, per_page=30) -> list[dict]` (`GET /repos/{repo}/commits?path=`), `async compare(repo, base, head) -> dict`, `async file(repo, path, ref) -> str | None` (raw contents via `Accept: application/vnd.github.raw`; 404 → None), `async open_pulls(repo, per_page=50) -> list[dict]`, `async pull_files(repo, number) -> list[dict]`, `async commit_pulls(repo, sha) -> list[dict]`. Base `https://api.github.com`, with an `Authorization: Bearer` header when the token is set.
- `RepoDiffWatcher(target: RepoTarget, gh: GitHub, interval_s: int)` with name `repo_diff:{repo}:{path}`. `path` may contain `{chain}`, which expands to one sub-path per entry in `target.chains`.
- Parsing by mode, each yielding `Entry(chain, address, symbol, name, website)` keyed by `(chain, address_or_symbol)`:
  - `dir_added`: a new direct child directory under the path. The address is the dir name. `info.json` in that dir, if present, gives symbol/name/website.
  - `json_tokenlist`: `tokens[]` with `chainId`, `address`, `symbol`, `name`. `chain_map` maps `str(chainId)` → chain key; unmapped chainIds are skipped.
  - `regex_entries`: `target.regex` with named groups `symbol` (required), `address`, and `chain` (optional, mapped via `chain_map`).
- State: `{"head": sha, "pr_seen": {number: [entry keys]}}` plus, for `json_tokenlist`/`regex_entries`, `"keys": [...]`.
- Diff rules: new commits since `head` (oldest first) → `kind="merged"`, evidence `https://github.com/{repo}/commit/{sha}`, or the PR URL from `commit_pulls` if there is one. Open PRs touching the path whose files add new entries → `kind="pr_opened"`, evidence the PR `html_url`. Dedupe key = `dedupe_key("repo_diff", repo, chain, address or symbol)`, the same key for both kinds, so `merged` after `pr_opened` goes through `upgrade_signal` (Task 9 pipeline) and no new alert is sent. `website` domains are returned in `WatchResult.domains`.
- If `head` is not found in the commits list (force-push) and no compare is possible → re-baseline: `new_state` with the current head, no signals, and a log warning.

- [ ] **Step 1: Failing tests** (respx + fixture JSON):
  - `test_dir_added_emits_one_signal_per_new_dir`: compare fixture with 2 added `blockchains/monad/assets/0xAbC.../info.json` files from 2 tokens → 2 signals, `kind == "merged"`, `chain == "monad"`, symbol from info.json, evidence is the commit URL, and the domain is in `domains`.
  - `test_tokenlist_diff_only_added`: old list {A, B}, new {B, C}; C has chainId mapped → 1 signal (C). A removed → no signal.
  - `test_unmapped_chainid_skipped`.
  - `test_open_pr_emits_pr_opened`: an open PR adding a dir → `kind == "pr_opened"`, evidence the PR URL. The same PR on the next run → no new signal.
  - `test_force_push_rebaselines`: state head `deadbeef` not in commits, compare returns 404 → 0 signals, `new_state["head"]` = newest sha.
  - `test_regex_entries`: a fixture TS line `{ symbol: 'CAT', contractAddress: '0xabc', network: 'monad' }` with the regex from config → 1 signal.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement. Keep the GitHub calls per run bounded: at most 10 commits and 20 PRs per target per run. Excess commits are processed on the next run (state advances to the last processed sha).
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): repo_diff watcher for token lists and asset registries`.

### Task 6: ct watcher

**Files:**
- Create: `engine/hades/watchers/ct.py`
- Test: `engine/tests/test_ct.py`, fixtures `engine/tests/fixtures/crtsh/*.json`

**Interfaces:**
- `CtWatcher(store: Store, hot_labels: list[str], interval_s: int)` with name `ct`. Each run polls only domains that are due, i.e. `last_checked` older than `interval_s` (tracked in state `{"domains": {domain: {"checked": iso, "names": [...]}}}`), and at most 20 domains per run. The runner interval is 600 s, so the per-domain 6 h interval is enforced from state.
- Fetch: `GET https://crt.sh/?q=%25.{domain}&output=json` via `http.get_json` (timeout 60 s). A non-list response or an exception for one domain → that domain stays unchanged and the error is recorded. If all due domains fail, raise `SourceError`.
- Parse: each cert has `name_value` (newline-separated names), `id`, `not_after`. Drop wildcards (`*.`), names not ending in `.{domain}` or equal to it, and certs whose `not_after` < now.
- Diff per domain: a domain seen for the first time → baseline (no signals). A new name → `Signal(source="ct", kind="ct_high"|"ct_low", project_key=project_key(domain=domain), severity="high" if left label in hot_labels else "low", evidence_url=f"https://crt.sh/?id={id}", name=subdomain)`. Dedupe key = `dedupe_key("ct", subdomain)`.
- An empty list `[]` for a domain that previously had names → treated as a failure for that domain (outage guard), state kept.

- [ ] **Step 1: Failing tests**:
  - `test_first_seen_domain_is_baseline`.
  - `test_new_hot_subdomain_high`: baseline {www}, then {www, claim} → 1 signal, `kind == "ct_high"`, `severity == "high"`, evidence `https://crt.sh/?id=<id>`.
  - `test_non_hot_low`: `docs.` → `ct_low`.
  - `test_wildcard_and_expired_ignored`.
  - `test_html_error_page_is_failure`: crt.sh returns `<html>` with 200 → state unchanged for that domain, no signals. With one domain, `run_once` returns False.
  - `test_empty_list_after_names_is_failure`.
  - `test_domain_not_due_not_fetched`: checked 1 h ago → no HTTP call (respx asserts not called).
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement. The domain list = `store.domains()` at fetch time.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): certificate transparency watcher`.

### Task 7: ground_truth watcher

**Files:**
- Create: `engine/hades/watchers/ground_truth.py`
- Test: `engine/tests/test_ground_truth.py`, fixtures `engine/tests/fixtures/cex/*.json` (small trimmed real responses)

**Interfaces:**
- `GroundTruthWatcher(exchange: str, interval_s: int)` with name `ground_truth:{exchange}`. One instance per configured exchange. The parser registry maps exchange → `(urls, parse_fn)`, and `parse_fn` returns `dict[symbol, base_asset]`:
  - coinbase: `https://api.exchange.coinbase.com/products` → `id` (e.g. `CAT-USD`), base `base_currency`, only `status == "online"`.
  - kraken: `https://api.kraken.com/0/public/AssetPairs` → `result` keys → `wsname` / `base`.
  - okx: `https://www.okx.com/api/v5/public/instruments?instType=SPOT` → `data[].instId`, `baseCcy`.
  - binance: `https://api.binance.com/api/v3/exchangeInfo` → `symbols[]` with `status == "TRADING"`, `baseAsset`.
  - bybit: `https://api.bybit.com/v5/market/instruments-info?category=spot` → `result.list[].symbol`, `baseCoin`.
- State: `{"symbols": {symbol: base}}`. New symbol → `ListingEvent(exchange, symbol, base_asset, first_seen_at=now, raw=item)`. It never emits a `Signal`.
- Outage guard: if the parsed count is less than 50% of the state count → raise `SourceError("snapshot shrank")`.
- Robinhood: not included unless Task 15 finds a documented public endpoint.

- [ ] **Step 1: Failing tests**: per exchange, `test_parse_<exchange>` asserts the expected symbol→base pairs from the fixture. Also `test_new_pair_emits_listing_event`, `test_baseline_no_events`, and `test_shrunken_snapshot_raises` (state 100 symbols, new 40 → `SourceError`, state unchanged via the runner).
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): CEX ground-truth listing logger`.

### Task 8: Enrichment (buyable + liquidity)

**Files:**
- Create: `engine/hades/enrich.py`
- Test: `engine/tests/test_enrich.py`, fixtures `engine/tests/fixtures/geckoterminal/*.json`, `fixtures/dexscreener/*.json`

**Interfaces:**
- `async check(http: Http, chain: ChainCfg | None, address: str | None, symbol: str | None, *, min_liq: float, priority_chains: dict[str, ChainCfg], now: datetime) -> Enrichment`
  - Address + chain with a `geckoterminal` id: `GET https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{addr}/pools`. Pools come from `data[].attributes` (`reserve_in_usd`, `volume_usd.h24`, `address`) and `relationships.dex.data.id`. The top pool is the one with max reserve. `buyable = top_reserve >= min_liq`. `pool_url = https://www.geckoterminal.com/{net}/pools/{pool_address}`. A 404 → `buyable=False, pool_count=0` (token not indexed = not tradable yet).
  - A GeckoTerminal `SourceError`, or no `geckoterminal` id but a `dexscreener` id → DexScreener `GET https://api.dexscreener.com/token-pairs/v1/{chainId}/{address}`, with `liquidity.usd`, `volume.h24`, `dexId`, and `url`. Provider `dexscreener`.
  - Symbol only: GeckoTerminal `GET /api/v2/search/pools?query={symbol}&network={net}` for each priority chain. Candidates (≤ 5) are pools whose base token symbol equals the symbol (case-insensitive). `verified=False`. `buyable` is True if any candidate ≥ `min_liq`, else False.
  - Both providers fail → `Enrichment(buyable=None, error=...)`. It never raises.
- GeckoTerminal rate: register host `api.geckoterminal.com` at 0.5 rps in `Http.rates`. Add the `x-cg-demo-api-key` header if `COINGECKO_API_KEY` is set.

- [ ] **Step 1: Failing tests**:
  - `test_gt_buyable_top_pool`: two pools, reserves 800 and 12,500 → `buyable True`, `liquidity_usd == 12500`, correct `pool_url`, `pool_count == 2`.
  - `test_gt_below_threshold_not_buyable`: top 800 → False.
  - `test_gt_404_not_buyable`.
  - `test_fallback_to_dexscreener_on_gt_500`.
  - `test_symbol_only_candidates_unverified`.
  - `test_both_fail_returns_error_not_raise`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): on-chain buyability and liquidity check`.

### Task 9: Identity, scoring, pipeline

**Files:**
- Create (or extend from Task 5): `engine/hades/identity.py`, `engine/hades/score.py`, `engine/hades/pipeline.py`, `engine/hades/bus.py`
- Test: `engine/tests/test_identity.py`, `engine/tests/test_score.py`, `engine/tests/test_pipeline.py`

**Interfaces:**
- `identity.normalize_address(chain_cfg: ChainCfg | None, address: str) -> str`: lowercase when `evm` is true or `address` starts with `0x`, otherwise unchanged.
- `identity.project_key(*, chain=None, address=None, symbol=None, domain=None, chains: dict[str, ChainCfg] = {}) -> str`: returns `f"{chain}:{norm_addr}"`, else `f"sym:{symbol.lower()}"`, else `f"dom:{registered_domain(domain)}"`. Raises `ValueError` if none is given.
- `identity.registered_domain(host: str) -> str` via `tldextract.TLDExtract(suffix_list_urls=())` (offline bundled PSL): `claim.cat.co.uk` → `cat.co.uk`.
- `identity.dedupe_key(source: str, *parts: str | None) -> str` = `source + ":" + ":".join(lowercased non-None parts)`.
- `score.score(sig: Signal, enr: Enrichment | None, fusion_sources: set[str], cfg: AppConfig) -> tuple[int, dict]`. The detail dict has keys `base, priority_chain, earliness, fusion, total`. `base` is looked up by `sig.kind` in `cfg.scoring.base` (0 if missing). Earliness applies when `enr.buyable is False` or `enr.liquidity_usd < low_liquidity_usd`, and not when `enr` is None or `buyable is None`. Fusion = `min(fusion_max, fusion_per_source * len(fusion_sources - {sig.source}))`. Total is capped at 100.
- `bus.Bus`: `subscribe() -> asyncio.Queue`, `unsubscribe(q)`, `publish(event: str, data: dict)` (non-blocking; drops for full queues of maxsize 100).
- `pipeline.Pipeline(store, http, cfg: AppConfig, notifier: Notifier, bus: Bus, clock)` implements `SignalSink.handle(sig)`:
  1. `id = store.insert_signal(sig)`. If None: `upgraded = store.upgrade_signal(sig.dedupe_key, sig.kind, sig.evidence_url)`. Publish `signal_updated` if upgraded. Stop (no alert).
  2. If `sig.website`: `store.add_alias(sig.project_key, project_key(domain=sig.website))`.
  3. `enr = await enrich.check(...)` → `store.add_enrichment`.
  4. `fusion = store.sources_for_project(store.resolve(sig.project_key), now - fusion_window)`.
  5. `score, detail = score.score(...)` → `store.set_score`.
  6. If `sig.severity == "high"`, `score >= cfg.alerts.min_score`, and not muted (`notifier.is_muted(sig, now)`): `notifier.send_signal(...)` → `store.mark_sent`.
  7. `bus.publish("signal", store.get_signal(id))`.
- `pipeline.recheck_due(now)`: for each id in `store.due_rechecks(now, recheck_after_s)`, run enrich again, `add_enrichment`, and publish `signal_updated`. If buyable changed from False to True, notify with the prefix "Sekarang bisa dibeli".
- `pipeline.match_listing(ev: ListingEvent)`: called by the runner after `insert_listing` returns True. It finds signals from the last 30 days with symbol == `ev.base_asset` (case-insensitive) and calls `store.mark_listed`. The runner gets an optional `on_listing` callback for this. Add it to `Runner.__init__` as `on_listing: Callable[[ListingEvent], Awaitable[None]] | None = None`.

- [ ] **Step 1: Failing tests**:
  - `test_evm_address_case_same_key`: `0xAbC` and `0xabc` on `monad` → equal keys. Solana `AbC` ≠ `abc`.
  - `test_registered_domain_multi_suffix`.
  - `test_score_example_from_spec`: `pr_opened` on a priority chain, not buyable, one CT sibling → 40 + 15 + 15 + 20 = 90.
  - `test_score_cap_100`, `test_score_no_earliness_when_enrich_failed`.
  - `test_pipeline_alerts_high_score` (fake notifier records the send).
  - `test_pipeline_no_alert_low_severity`, `test_pipeline_muted_no_alert`.
  - `test_pipeline_duplicate_upgrades_without_alert`: `pr_opened` then `merged` with the same dedupe key → 1 send total, row kind `merged`.
  - `test_pipeline_fusion_via_website_alias`: a repo signal with website `cat.xyz` arrives first; then a ct signal for `claim.cat.xyz` has a fusion bonus of 20 in its detail.
  - `test_recheck_turns_buyable_notifies`.
  - `test_match_listing_marks_signal`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement. Since `insert_signal` runs before scoring, the current signal counts in `sources_for_project`, hence the subtraction of `{sig.source}` in fusion.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): project identity, scoring and signal pipeline`.

### Task 10: Telegram notifier and digest

**Files:**
- Create: `engine/hades/notify.py`
- Test: `engine/tests/test_notify.py`

**Interfaces:**
- `to_wib(dt: datetime) -> str` → `"27 Nov 2026 14:05 WIB"`. Months in English short form, matching the spec example.
- `format_signal(sig: dict, enr: dict | None, detail: dict, fusion: list[dict], prefix: str = "") -> str`: Telegram HTML following the spec §9 template. Every dynamic field goes through `html.escape`. The address is shortened to `0xabc…def`. Buyability lines (Indonesian):
  - `Belum bisa dibeli onchain (N pool)`
  - `Bisa dibeli · likuiditas $12.5k · <dex> · <a href=pool_url>pool</a>`
  - `Kandidat (belum terverifikasi): …`
  - `Cek likuiditas gagal`

  The result is truncated to 4,096 chars.
- `format_digest(low_signals: list[dict], rechecks: list[dict], now) -> str | None`: returns None when both lists are empty.
- `class Notifier(http: Http, store: Store, token: str, chat_id: str, clock)`: `is_muted(sig: Signal | dict, now) -> bool` (a target equal to `project_key`, `source`, or `chain`), `async send_signal(signal_id, prefix="") -> int | None`, `async send_text(text) -> int | None`. Uses `POST https://api.telegram.org/bot{token}/sendMessage` with `parse_mode=HTML` and `disable_web_page_preview=true`. With an empty token or chat_id it logs and returns None (dev mode). Errors are logged and never raised. A `post_json` method is added to `Http` with the same retry rules.
- `async digest_loop(notifier, store, cfg, stop, sleep)`: sends at `digest_hour_wib:00` WIB daily, covering low-severity signals and rechecks from the last 24 h.

- [ ] **Step 1: Failing tests**:
  - `test_to_wib`: `2026-11-27T07:05Z` → `"27 Nov 2026 14:05 WIB"`.
  - `test_format_escapes_html`: name `<b>A&B_*` appears escaped.
  - `test_format_not_buyable_line`, `test_format_truncates_4096`.
  - `test_is_muted_by_chain`.
  - `test_send_without_token_returns_none` (no HTTP call).
  - `test_send_posts_html` (respx asserts the body has `parse_mode == "HTML"` and the chat_id).
  - `test_digest_none_when_empty`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): telegram alerts and daily digest`.

### Task 11: Telegram bot commands

**Files:**
- Create: `engine/hades/bot.py`
- Test: `engine/tests/test_bot.py`

**Interfaces:**
- `handle_command(text: str, chat_id: str, *, owner_chat_id: str, store: Store, now: datetime) -> str | None`. Pure apart from store calls. Returns None for a foreign chat_id or a non-command. Commands and replies (Indonesian):
  - `/status`: one line per watcher (`✅ name · ok 3m lalu` / `⚠️ name · gagal ×4: <err>`), plus `24 jam: N sinyal, M alert, K listing`.
  - `/watchlist`: domains with origin. `/watchlist add <domain>` validates the hostname, lowercases, and strips the scheme/path. `/watchlist rm <domain>`.
  - `/mute <target> [hours]`: default 24, max 720. `/mute list`. `/unmute <target>`.
  - Unknown → help text listing the commands.
- `async poll_loop(notifier/http, store, settings, stop)`: `getUpdates` long-poll (timeout 30), offset persisted in `watcher_state` under the name `telegram_bot`. Replies via `send_text`. Not started when the token is empty.

- [ ] **Step 1: Failing tests**: `test_foreign_chat_ignored`, `test_status_lists_watchers`, `test_watchlist_add_normalizes` (`https://Cat.XYZ/path` → `cat.xyz`, origin manual), `test_watchlist_add_rejects_garbage` (`not a domain` → error text, nothing stored), `test_mute_default_24h_and_cap`, `test_unknown_shows_help`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → pass.
- [ ] **Step 5:** Commit `feat(engine): telegram bot commands`.

### Task 12: API, evaluation, CLI wiring

**Files:**
- Create: `engine/hades/api.py`, `engine/hades/evaluate.py`, `engine/hades/app.py` (process wiring)
- Modify: `engine/hades/cli.py`
- Test: `engine/tests/test_api.py`, `engine/tests/test_evaluate.py`, `engine/tests/test_cli.py`

**Interfaces:**
- `evaluate.evaluate(store, now, window_days=30) -> dict`: returns `{"generated_at", "by_source": [{source, kind, signals, hits, false_positives, pending, median_lead_h, min_lead_h, small_sample: hits < 10}], "misses": [listing rows with no earlier signal], "hits": [{signal_id, listing_id, exchange, lead_h}]}`. A hit is a signal with `observed_at < listing.first_seen_at <= observed_at + window`, matched on symbol (weak) or on an address in `raw`. A false positive is a signal older than the window with no hit. Pending is one younger than the window with no hit.
- `evaluate.write_report(report, out_dir: Path) -> tuple[Path, Path]` writes `eval-YYYYMMDD.md` and `.csv`. The markdown states "sampel kecil, belum bisa disimpulkan" for small samples.
- `evaluate.price_move(http, net, pool_address, t0, t1) -> float | None`: GeckoTerminal `GET /api/v2/networks/{net}/pools/{pool}/ohlcv/hour?before_timestamp=...`, giving the close-to-close % change, or None. Used only by the CLI report, not the API.
- `api.create_app(store, bus, settings) -> FastAPI` with routes: `/healthz`; `/api/overview` (`counts_since(24h)`, `watchers()`, top 10 signals by score in 24 h); `/api/signals` (query params from `list_signals`); `/api/signals/{id}` (signal + enrichments + fusion siblings + listed_on; 404); `/api/listings`; `/api/watchers`; `GET/POST/DELETE /api/watchlist` (POST body `{domain}`, same validation as the bot, shared via `bot.normalize_domain`); `GET/POST/DELETE /api/mutes` (POST `{target, hours}`); `/api/eval` (`evaluate(store, now)`); `/api/stream` (SSE events `signal`, `signal_updated`, `watcher`, and `ping` every 15 s).
- `app.build(settings, cfg) -> Services` (store, http, bus, notifier, pipeline, watchers list, runner) and `async app.serve(services, host="127.0.0.1", port=8000)`. `serve` runs uvicorn and the runner + recheck loop (every 600 s) + digest loop + bot loop in one event loop.
- CLI: `hades run [--host --port]`, `hades once <watcher-name>` (run one watcher once and print its signals; for debugging), `hades evaluate [--out reports/] [--prices]`, `hades version`.

- [ ] **Step 1: Failing tests**:
  - `test_api_requires_key` (missing → 401, empty server key → 503).
  - `test_api_signals_filter_min_score`.
  - `test_api_signal_detail_404`.
  - `test_api_watchlist_post_validates`.
  - `test_api_stream_emits_signal` (publish on the bus → the SSE body contains `event: signal`).
  - `test_evaluate_hit_lead_time` (signal at T, listing at T+50 h → hit, lead 50).
  - `test_evaluate_false_positive_after_window`, `test_evaluate_pending_inside_window`, `test_evaluate_miss`.
  - `test_cli_once_unknown_watcher_exits_1`.
- [ ] **Step 2:** Run → fail.
- [ ] **Step 3:** Implement. Copy the v1 SSE pattern (`EventSourceResponse` + `request.is_disconnected`) from git history: `git show hades-solana-final:engine/hades/api/main.py`.
- [ ] **Step 4:** Run the full suite `-m pytest -q` → all pass.
- [ ] **Step 5:** Commit `feat(engine): API, SSE stream, evaluation and run command`.

### Task 13: Web — data layer, Overview, Signals feed

**Files:**
- Modify: `web/lib/api.ts` (types mirror the Task 12 JSON: `SignalRow`, `SignalDetail`, `EnrichmentRow`, `ListingRow`, `WatcherRow`, `Overview`, `EvalReport`), `web/lib/live.ts` + `web/lib/streamMerge.ts` (event `signal` / `signal_updated`, keyed by id), `web/app/api/stream/route.ts` (proxy `/api/stream`), `web/lib/alertsProxy.ts` → rename to `web/lib/signalsProxy.ts` (GET `/api/signals` backfill), `web/components/Nav.tsx` (links: Overview, Signals, Listings, Watchlist, Eval), `web/app/layout.tsx` (metadata: title "HADES — Early signals", description "Sinyal awal dari data publik")
- Create: `web/app/page.tsx` (Overview), `web/app/signals/page.tsx`, `web/components/SignalFeed.tsx` (client, SSE), `web/components/SignalTable.tsx`, `web/components/WatcherHealth.tsx`, `web/components/BuyableBadge.tsx`, `web/lib/signalView.ts`
- Test: `web/lib/signalView.test.ts`, `web/lib/live.test.ts`, `web/lib/streamMerge.test.ts`, `web/lib/signalsProxy.test.ts`, `web/components/SignalTable.test.tsx`, `web/components/Nav.test.tsx`

**Interfaces:**
- `signalView.buyableState(e: EnrichmentRow | null): { label: "Belum bisa dibeli" | "Bisa dibeli" | "Kandidat" | "Tidak diketahui"; tone: "ember" | "gain" | "muted" }`. "Bisa dibeli" never relies on colour only: it pairs with an icon (Lucide `CircleCheck` / `CircleDashed`).
- `signalView.formatLiquidity(usd: number | null): string` → `$12.5k`, `$1.2M`, `—`.
- `signalView.shortAddr(a)`, `signalView.scoreTone(score)` (≥ 70 ember, ≥ 40 primary, else muted).
- Overview: hero "HADES", then a `#1 WATCHERS` WatcherHealth grid (OK / gagal sejak …), a `#2 24 JAM` StatCards (signals, alerts, listings), and `#3 TOP SINYAL` (top 10).
- Signals: a filter form via `lib/query.ts` (source, chain, min_score, buyable) and a live feed with LiveBadge (LIVE only when connected and the last event is < 60 s old). On reconnect it backfills via the proxy. The table columns are time (WIB), score, source·kind, chain, token/subdomain, buyable + liquidity, and evidence link.

- [ ] **Step 1: Failing tests**: `buyableState` for the four cases, `formatLiquidity(12500) == "$12.5k"`, `streamMerge` dedupes by id and applies `signal_updated`, the proxy never leaks the key (adapt the existing `alertsProxy.test.ts`), the Nav renders 5 links, and `SignalTable` renders the evidence link with `rel="noopener noreferrer"`.
- [ ] **Step 2:** `npm test` → fail.
- [ ] **Step 3:** Implement with the existing design tokens. No raw hex in components.
- [ ] **Step 4:** `npm test` and `npm run build` → pass.
- [ ] **Step 5:** Commit `feat(web): overview and live signals feed`.

### Task 14: Web — Signal detail, Listings, Watchlist, Eval

**Files:**
- Create: `web/app/signal/[id]/page.tsx` + `not-found.tsx`, `web/app/listings/page.tsx`, `web/app/watchlist/page.tsx`, `web/app/eval/page.tsx`, `web/components/ScoreBreakdown.tsx`, `web/components/EnrichmentTimeline.tsx`, `web/components/WatchlistControls.tsx` (rewrite: domains + mutes), `web/app/api/watchlist/route.ts` + `web/app/api/mutes/route.ts`
- Modify: `web/lib/watchlistProxy.ts` (generalize into `web/lib/writeProxy.ts`: POST/DELETE to `/api/watchlist` and `/api/mutes`, keeping the origin guard and DNS-rebinding host check)
- Test: `web/lib/writeProxy.test.ts`, `web/components/ScoreBreakdown.test.tsx`, `web/lib/signalView.test.ts` (eval helpers)

**Interfaces:**
- Signal detail sections: `#1 BUKTI` (evidence link(s), raw excerpt as a `<pre>` limited to 2 KB), `#2 LIKUIDITAS` (EnrichmentTimeline: T0 and T+24h rows), `#3 SKOR` (ScoreBreakdown: base / chain prioritas / earliness / fusion / total), `#4 FUSION` (siblings with links), and `#5 LISTING` ("Listing di Coinbase · 27 Nov 2026 14:05 WIB · 50 jam setelah sinyal").
- Listings: a table per exchange with the earliest matching signal and the lead time, or "tidak ada sinyal sebelumnya".
- Watchlist: domains with origin badges (manual/auto) and add/remove; active mutes with an until time and remove.
- Eval: a table from `/api/eval` with a "sampel kecil" badge where `small_sample`.
- `signalView.leadText(hours: number): string` → `"50 jam setelah sinyal"` / `"3 hari setelah sinyal"` (≥ 72 h shows days).

- [ ] **Step 1: Failing tests**: the write proxy rejects a cross-origin POST (403) and never forwards the key to the browser; `ScoreBreakdown` renders all five rows with the totals from props; `leadText(50)` and `leadText(80)`.
- [ ] **Step 2:** `npm test` → fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `npm test` and `npm run build` → pass.
- [ ] **Step 5:** Commit `feat(web): signal detail, listings, watchlist and eval pages`.

### Task 15: Seeds verification, Docker, README

**Files:**
- Modify: `config.example.yaml` (verified values), `README.md`
- Create: `engine/Dockerfile`, `web/Dockerfile`, `docker-compose.yml`, `.dockerignore`, `docs/superpowers/notes/2026-11-27-seed-verification.md`

- [ ] **Step 1: Verify seeds live** (one-off, results written in the notes file with the date):
  - The Trust Wallet `blockchains/` folder names for robinhood/megaeth/monad/hyperevm (`GET /repos/trustwallet/assets/contents/blockchains`). Drop any chain without a folder, and record that.
  - The BitGoJS statics file(s) and a working `regex` against today's file.
  - Official token lists for the priority chains, if any exist (repo + path + schema).
  - The GeckoTerminal network IDs (`GET https://api.geckoterminal.com/api/v2/networks?page=N`) and DexScreener chain IDs for all configured chains.
  - Whether Robinhood documents a public asset/pair endpoint. If not, write "skipped: no documented public endpoint".
- [ ] **Step 2:** Update `config.example.yaml` with the verified values. `hades once repo_diff:trustwallet/assets:blockchains/{chain}/assets` runs against the live API and prints `baseline` without errors. Then run each ground_truth watcher once.
- [ ] **Step 3:** Dockerfiles: engine `python:3.12-slim`, non-root, `CMD ["python","-m","hades","run","--host","0.0.0.0"]`, SQLite in volume `/data`. Web: Node 20 multi-stage, `next start`. Compose: `engine` (port bound to the internal network only) and `web` (`HADES_API_URL=http://engine:8000`, port 3200 on `127.0.0.1`), with `env_file: .env`.
- [ ] **Step 4:** README in Indonesian, for beginners:
  - what HADES v2 does
  - creating a Telegram bot with @BotFather and getting a chat ID
  - creating a GitHub token (no scopes needed for public repos)
  - copying `.env.example` / `config.example.yaml`
  - running on Windows (`python -m hades run`, `npm run dev`) and via `docker compose up -d`
  - adding a repo target or a domain
  - reading `/status` and `hades evaluate`
  - a "Batasan" section: only public data, no auto-buy, alerts are not financial advice, and the evaluation must be read before trusting any watcher
- [ ] **Step 5:** `docker compose config` validates. Full `pytest -q`, `npm test`, and `npm run build` pass.
- [ ] **Step 6:** Commit `chore: verified seeds, docker, beginner README`.
