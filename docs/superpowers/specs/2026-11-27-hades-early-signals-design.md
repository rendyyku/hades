# HADES v2 — Early Signals from Public Data

Status: draft, awaiting owner approval. Date: 2026-11-27.
Replaces the Solana smart-money engine (specs 2026-11-19 and 2026-11-20). The old code is kept only in
git history under tag `hades-solana-final`.

## 1. Intent

Before an event that moves a token's price (a listing, a wallet integration, an airdrop), the parties
involved have to prepare infrastructure, and that preparation often leaves public traces. HADES v2
watches a few of those traces, checks right away whether the token can already be bought on-chain and
how deep the liquidity is, and pushes a short alert to the owner on Telegram.

It also logs when listings *actually* happen (ground truth), so the owner can measure which watchers
really lead announcements and which are noise. Trust is earned through that measurement, not assumed.

The owner trades on the alerts manually. Focus: **new chains/ecosystems with few watchers**
(initial priority: Robinhood Chain, MegaETH, Monad, HyperEVM; configurable).

## 2. Goals / Non-goals / Anti-goals

Goals (this phase)
1. Watcher **repo_diff**: diffs specific files/directories (token lists, asset registries) in GitHub
   repos the owner chooses, and emits one signal per newly added token.
2. Watcher **ct**: Certificate Transparency. Finds new subdomains (`claim.`, `airdrop.`, `stake.`, …)
   for watched project domains.
3. Logger **ground_truth**: snapshots the public asset/pair endpoints of major CEXs and records when a
   new asset or pair appears. Stored, not alerted.
4. **Enrichment** on every signal: is the token buyable on-chain, top pool liquidity (USD), DEX, pool
   link, 24h volume. Re-checked at T+24h.
5. A **simple score** per signal, including a fusion bonus when another source hits the same project.
6. **Telegram alerts** with commands `/status`, `/watchlist`, `/mute`.
7. An **evaluation script**: signal time vs ground-truth listing time and price move, per watcher.
8. Ops: `config.yaml` + `.env`, retry with backoff, one dead source never stops the others,
   Dockerfile, and a beginner-friendly README.
9. **Web dashboard** (existing `web/`, kept and repurposed): live signal feed, signal detail with
   evidence + liquidity, watcher health, ground-truth log, watchlist/mute management, evaluation report.

Non-goals
- Watchers #3, #4, #7–#11 and the rest from the research list. The plugin interface makes them easy to add later.
- Certstream firehose (CT option B). Revisit only if the watchlist approach proves valuable.
- Auto-buy, copy trading, private keys, signing. None anywhere.
- Multi-user / SaaS.

Anti-goals (failure even if it runs)
- An alert without a verifiable evidence link (commit/PR URL, crt.sh certificate URL).
- An alert flood. If the owner mutes everything within a week, the system failed.
- Claiming a watcher "works" without the evaluation numbers to back it.
- Hitting undocumented/private endpoints, bypassing logins, guessing URLs, or ignoring rate limits.

## 3. Constraints

- Dev on Windows 11, runs later in Docker on the owner's Ubuntu VPS. Python 3.12 (brief allows 3.11+).
- Async throughout (`asyncio`, `httpx`).
- Secrets only in `.env` (gitignored): `GITHUB_TOKEN`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  optional `COINGECKO_API_KEY`. Everything else lives in `config.yaml`.
- Free tiers only: GitHub REST (5,000 req/h with token), crt.sh (free, often slow/flaky),
  GeckoTerminal public API (~30 req/min), DexScreener public API, CEX public market endpoints.
- Times shown to the owner in WIB (UTC+7). Stored in UTC.

## 4. Approaches

**Rewrite scope** (owner first picked A, then asked to keep the dashboard → B)
- A. Full replacement, including `web/`. Superseded: the owner still wants the dashboard.
- B. Replace the engine, keep `web/` and repurpose it. **Picked.** Keep the design system, layout/Nav,
  server-side proxy pattern (API key never reaches the browser, origin guard), SSE live feed +
  LIVE/STALE badge, and shared components. Delete the wallet/PnL/token/Hyperliquid/arena pages and their libs.
- C. Keep Hyperliquid next to the new system. Rejected: two unrelated projects in one repo.

**CT domain selection** (owner accepted the recommendation by default)
- A. Watchlist of domains polled on crt.sh, auto-filled from domains found by repo_diff. **Picked.**
- B. Certstream firehose with keyword filters. Deferred: heavy traffic, lots of phishing look-alikes, needs 24/7.

**Liquidity source** (agent's call)
- GeckoTerminal as primary: 200+ networks, including Monad and HyperEVM, no key needed.
  **Picked.** DexScreener as fallback.
- Direct RPC reads of DEX factories. Rejected: needs per-chain, per-DEX code.

## 5. Architecture

```
config.yaml ─┐
             ▼
  runner (asyncio) ── schedules each watcher at its own interval, isolates failures
     │
     ├── watchers/repo_diff.py ─┐
     ├── watchers/ct.py ────────┼──► Signal ──► store (dedupe) ──► enrich (liquidity) ──► score ──► notifier (Telegram)
     └── watchers/ground_truth.py ──► ListingEvent ──► store (no alert)
                                                                   │
  bot (Telegram long-poll: /status /watchlist /mute) ◄─────────────┤
  api (FastAPI, API key, SSE /stream) ◄── web/ (Next.js proxy) ◄───┘
  scripts/evaluate.py  (reads store, writes report; also served by the api)
```
Engine and API run in one process (`python -m hades run`). The API reads the same SQLite DB,
and signals are pushed to SSE subscribers from the notify step.

**Watcher interface** (every source implements it):
```python
class Watcher(Protocol):
    name: str
    interval_s: int
    async def fetch(self, ctx) -> RawSnapshot          # network I/O only
    def parse(self, raw) -> list[Item]                 # pure
    def diff(self, prev: State, items) -> list[Signal] # pure, emits only NEW things
```
The runner owns state load/save, retry with exponential backoff and jitter, and health tracking
(last success, last error, consecutive failures). A watcher that keeps failing is marked unhealthy
in `/status` and keeps retrying. It never raises into the runner.

**First run = baseline.** The first successful snapshot of any source is stored silently. Only
additions after that emit signals. This avoids alerting on thousands of existing tokens.

**Signal** (common shape): `source`, `kind`, `project_key`, `chain`, `address?`, `symbol?`,
`name?`, `evidence_url` (required), `observed_at`, `raw` (JSON), `dedupe_key`.

## 6. Watchers

### 6.1 repo_diff
Config per target:
```yaml
- repo: trustwallet/assets
  path: blockchains/{chain}/assets      # directory mode: one added subdir = one token
  mode: dir_added
  chains: [robinhood, megaeth, monad, hyperevm]   # repo-specific chain folder names
- repo: <owner>/<repo>
  path: path/to/tokenlist.json
  mode: json_tokenlist                  # Uniswap token-list schema: tokens[].{chainId,address,symbol,name}
- repo: BitGo/BitGoJS
  path: <statics coin file(s)>
  mode: regex_entries                   # regex per entry → symbol/address/chain
```
Flow: list commits touching `path` since the last seen SHA. For each new commit, in order, diff old
vs new: Compare API file list for `dir_added`, or fetch both file versions and diff parsed entries.
One signal per added token. Evidence: commit URL, plus the PR URL when the commit belongs to a PR.
Also poll **open PRs** that touch the path. A token in an unmerged PR is the earliest trace. Signal
kind `pr_opened` vs `merged`. Same token later merged → no new alert, just an evidence update.
If an entry carries a website (Trust Wallet `info.json` does), add the domain to the CT watchlist.

Seed list: the three above, plus official token lists of the priority chains where one exists. The
builder finds and verifies the exact paths; the owner confirms the list in `config.yaml`.

### 6.2 ct
Watchlist = `config.yaml` domains + auto-added domains (from 6.1). Poll
`https://crt.sh/?q=%25.<domain>&output=json` every 6h per domain, staggered. Dedupe by
(domain, subdomain name). New subdomain → signal. Severity high if the left label matches the
`ct.hot_labels` list (`claim, airdrop, stake, staking, points, tge, token, bridge, rewards, app`),
low otherwise. Low-severity signals are stored and appear in a daily digest, not as live pushes.
Evidence: crt.sh certificate URL. Wildcards and expired certs are ignored.

### 6.3 ground_truth
Every 10 min, snapshot the documented public endpoints: Coinbase Exchange `/currencies` + `/products`,
Kraken `/0/public/Assets` + `AssetPairs`, OKX `/api/v5/public/instruments?instType=SPOT`, Binance
`/api/v3/exchangeInfo`, Bybit `/v5/market/instruments-info?category=spot`. Also Robinhood, but only if a documented
public endpoint exists (the builder checks; otherwise it is skipped and noted in the README). New
asset/pair after baseline → `listing_events` row (exchange, symbol, first_seen_at, raw). No alert,
but `/status` shows a count, and a matching earlier signal is noted on that signal ("listed on X").

## 7. Enrichment (buyable + liquidity)

For signals with (chain, address): GeckoTerminal `networks/{net}/tokens/{addr}/pools` → buyable =
any pool with liquidity ≥ `enrich.min_liquidity_usd` (default $1k). Record top pool reserve USD, DEX,
pool URL, 24h volume, pool count. On a GeckoTerminal error or unknown network, fall back to DexScreener. Chain-name
mapping lives in config (`chains.<key>.geckoterminal`, `.dexscreener`).
For signals with only a symbol/domain: search pools by symbol, limited to priority chains. Matches
are shown as **unverified candidates**, never as "the" token.
Check at signal time and again at T+24h. Both snapshots are stored. A token going from not buyable
to buyable within 24h is an interesting outcome on its own.

## 8. Scoring (provisional, tuned later from evaluation)

`score = base(kind) + chain_priority_bonus + earliness_bonus + fusion_bonus`, capped at 100.
- base: `pr_opened` 40, `merged` 30, CT high 30, CT low 10.
- priority chain +15.
- earliness: not buyable yet, or liquidity < $50k → +15.
- fusion: another source hit the same `project_key` within 7 days → +20 (per extra source, max +40).
Alerts go out when score ≥ `alerts.min_score` (default 40). Weights live in config.

**project_key** is normalized: `chain:address` when known, else lowercase symbol, else registered
domain. A small alias table links them (e.g. a domain auto-added from a token entry is linked to that
token). Symbol-only matches are flagged as weak.

## 9. Telegram

Push format (WIB):
```
🔥 72  repo_diff · pr_opened  ·  Monad
CAT — Cash Cat   0xabc…def
Belum bisa dibeli onchain  (0 pool)
Bukti: github.com/trustwallet/assets/pull/1234
Fusion: ct claim.cashcat.xyz (2 hari lalu)
27 Nov 2026 14:05 WIB
```
Commands, accepted only from `TELEGRAM_CHAT_ID`:
- `/status`: watcher health, signals in 24h, ground-truth events in 24h.
- `/watchlist`: CT domains; `/watchlist add <domain>`, `/watchlist rm <domain>`.
- `/mute <project_key|source|chain> [hours=24]`, `/mute list`, `/unmute <x>`.
Daily digest at 08:00 WIB: low-severity signals, plus T+24h re-check changes.

## 9b. Web dashboard and API

API (FastAPI, `X-API-Key` header, bound to 127.0.0.1 by default):
`GET /health`, `GET /signals?source&chain&min_score&before&limit`, `GET /signals/{id}` (with
enrichments, fusion siblings, matching listing events), `GET /listings`, `GET /watchers`,
`GET/POST/DELETE /watchlist`, `GET/POST/DELETE /mutes`, `GET /eval` (latest report as JSON),
`GET /stream` (SSE: new signals, enrichment updates, watcher health changes).

Pages (same design system as before: tokens, Oswald/Mona Sans/Fira Code, numbered sections, a11y rules):
1. `/`: Overview. HADES hero, watcher health cards (OK / failing since …), counts for signals,
   alerts, and ground-truth events in 24h, and a preview of the top 10 signals.
2. `/signals`: live feed (SSE + backfill on reconnect), filter by source/kind/chain/score/buyable,
   LIVE/STALE badge.
3. `/signal/[id]`: evidence link(s), raw diff excerpt, enrichment at T0 and T+24h (buyable, liquidity,
   DEX, pool link), score breakdown, fusion siblings, and "listed on X at …" if ground truth matched.
4. `/listings`: ground-truth log per exchange, with the earliest matching signal and lead time.
5. `/watchlist`: CT domains (add/remove, shows origin: manual or auto) and active mutes.
6. `/eval`: per-watcher hit rate, lead time, false positives, price move. The "small sample" label applies.
Mutes and watchlist changes made in the web and in Telegram write to the same tables.

## 10. Storage (SQLite, WAL)

`watcher_state(name, cursor_json, last_ok, last_err, fail_count)`,
`signals(id, dedupe_key UNIQUE, source, kind, project_key, chain, address, symbol, name, evidence_url, observed_at, score, raw)`,
`enrichments(signal_id, checked_at, buyable, liquidity_usd, dex, pool_url, volume_24h, pool_count, provider)`,
`listing_events(id, exchange, symbol, base_asset, first_seen_at, raw, UNIQUE(exchange, symbol))`,
`watch_domains(domain, origin, added_at)`, `aliases(project_key, alias)`,
`mutes(target, until)`, `alerts_sent(signal_id, sent_at, message_id)`.

## 11. Evaluation (`scripts/evaluate.py`)

For each `listing_events` row: find earlier signals with a matching address or symbol (symbol matches
flagged weak). Report per source and kind:
- hits: signals followed by a listing within 30 days, with lead time (median and min).
- misses: listings with no earlier signal.
- false positives: signals with no listing after 30 days.
- price move: GeckoTerminal OHLCV from signal time to listing time, and listing +24h, when a pool exists.
Output: Markdown + CSV in `reports/`. Small samples are labelled as such. No conclusions are drawn from fewer than 10 hits.

## 12. Repo transition

1. `git tag hades-solana-final` on the current HEAD.
2. Remove `core/ hyperliquid/ scratch/`, the old `hades.db*`, and the old engine contents. In `web/`,
   remove the pages, API routes, components, and libs tied to wallets/PnL/tokens/Hyperliquid/arena/follows.
3. New layout:
```
engine/hades/          package: runner, models, store, enrich, score, notify, bot, api, config, http (retry/rate limit)
engine/hades/watchers/ base.py, repo_diff.py, ct.py, ground_truth.py
engine/scripts/evaluate.py
engine/tests/          pytest, with httpx mocked via respx and recorded fixtures (no live network in tests)
web/                   Next.js dashboard (pages in 9b), vitest
config.example.yaml, .env.example, Dockerfile(s), docker-compose.yml (engine + web), README.md (Indonesian, beginner)
```
`engine/` keeps its location and venv, so the existing test command still works.
4. Rewrite `AGENTS.md`: same secrets/no-keys rules; "every alert must carry an evidence URL"
   replaces the PnL/signature rule; new test command.
5. Old specs/plans stay in `docs/` as history, marked superseded.

## 13. Left to the builder

- Exact file paths and parsers for the seed repos; the real chain folder names in Trust Wallet.
- Whether Robinhood exposes a documented public asset endpoint.
- GeckoTerminal/DexScreener network IDs for the priority chains (verified against their network lists).
- Telegram library: plain `httpx` long-polling is fine and avoids a dependency.

## 14. Agent's calls (please review)

- Repo, package, and brand stay "HADES"; the README calls it HADES v2.
- GeckoTerminal is primary for liquidity, $1k is the "buyable" threshold, re-check at T+24h.
- Ground truth is logged silently and does not alert.
- First run is a silent baseline.
- Open PRs count as signals (earliest trace), not only merged commits.
- Low-severity CT results go into a daily 08:00 WIB digest instead of live pushes.
- Scoring weights and the 40-point alert threshold are starting guesses and are expected to change after evaluation.
- Docker targets the Ubuntu VPS; on Windows dev it runs directly with `python -m hades run` + `npm run dev`.
- Both web and Telegram: web for browsing and digging into evidence, Telegram for push notifications.
- Engine and API run in one process to keep ops simple.
