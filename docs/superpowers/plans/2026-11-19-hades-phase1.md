# HADES Phase 1 (Engine + API + Web) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Solana memecoin smart-money & insider engine with a FastAPI API and a Next.js dashboard, for self-use.

**Architecture:** Token-first: runner tokens (DexScreener + manual seed) → early buyers (Helius) → wallet history backfill → avg-cost PnL → scoring + insider tags with evidence. A Helius webhook pushes swaps from watchlist wallets into `alerts` and SSE. Next.js reads the API on the server side using `X-Hades-Key`.

**Tech Stack:** Python 3.12, SQLAlchemy 2, SQLite (WAL), httpx, FastAPI, uvicorn, sse-starlette, Typer, pydantic-settings, pytest + respx. Next.js 15, TypeScript, Tailwind v4, lucide-react, lightweight-charts, vitest.

**Spec:** `docs/superpowers/specs/2026-11-19-hades-design.md`

## Global Constraints

- Only SOL/WSOL-quoted swaps count toward PnL; other swaps are stored with `sol_amount=None`.
- PnL = average cost per (wallet, mint), in SOL. Closed = remaining ≤ 1% of max holdings.
- Bot: `trades_per_day > 150` OR `median_hold_sec < 30`. Score only for `tokens_closed ≥ 5`, window 30 days.
- Score = `40·pct(total_pnl_sol) + 25·win_rate + 20·pct(median_roi) + 15·consistency` (0–100).
- Insider: sniper ≤ 2 slots after pool creation; deployer_link trace ≤ 2 hops; bundle ≥ 3 wallets same slot + same funder; repeat_early = top-50 buyers in ≥ 3 runners.
- Runner: `priceChange.h24 ≥ 300` OR (`fdv ≥ 1_000_000` AND age ≤ 7 days), AND `liquidity.usd ≥ 30_000`.
- Default limits: `HELIUS_MAX_PAGES_PER_ADDRESS=20`, `EARLY_BUYERS_N=100`, `SIG_SCAN_MAX_PAGES=30`, `RATE_LIMIT_RPS=8`, backfill skipped if < 6 hours old.
- All `/api/*` require the header `X-Hades-Key == HADES_API_KEY` → 401 otherwise. `/webhook/helius` checks the `Authorization == HELIUS_WEBHOOK_SECRET` header.
- Secrets only in `.env` (gitignored). No private keys / signing at all.
- Web: tokens `--bg #111122 --surface rgba(255,255,255,.05) --border rgba(255,255,255,.10) --fg #F2F2F2 --muted #9A9AB5 --primary #3D3DFF --ember #FF5A1F --gain #22C55E --loss #EF4444`; fonts Oswald 300 (display, uppercase), Mona Sans (UI), Fira Code (numbers, tabular-nums); Lucide icons, no emoji as icons; contrast ≥ 4.5:1; focus ring; prefers-reduced-motion.
- A "LIVE" badge only if SSE is connected and the last event is < 60 s old; otherwise `STALE · <relative time>`.

## Review Focus

1. A multi-hop swap (SOL→USDC→MEME in one tx) must be recorded as 1 buy MEME with the total SOL in, not 2 swaps. → Task 2 `test_parse_multihop_swap`.
2. A wallet selling more than it bought (tokens from a transfer/airdrop): the excess is not counted as profit; realized is only computed on tracked holdings, the excess goes to `untracked_sell_tokens`. → Task 4 `test_oversell_not_profit`.
3. Helius 429 storm: retry with exponential backoff max 5x, then raise `HeliusRateLimited` without corrupting the DB (per-wallet transaction). → Task 2 `test_retry_then_raise`.
4. Duplicate webhook delivery (Helius retries): idempotent, no double alert. → Task 9 `test_webhook_idempotent`.
5. API down / SSE disconnected: web shows an error/stale state, not a crash. → Task 10 `test_api_error_state` + Task 12 `liveBadge` tests.

---

### Task 1: Engine skeleton, config, DB models

**Files:**
- Create: `engine/pyproject.toml`, `engine/hades/__init__.py`, `engine/hades/config.py`, `engine/hades/db.py`, `engine/tests/conftest.py`, `engine/tests/test_db.py`, `.gitignore`, `.env.example`

**Interfaces:**
- Produces: `Settings` (pydantic-settings, env prefix-less: `HELIUS_API_KEY, DATABASE_URL="sqlite:///./hades.db", HADES_API_KEY, HELIUS_WEBHOOK_SECRET, HELIUS_WEBHOOK_ID="", PUBLIC_WEBHOOK_URL="", HELIUS_MAX_PAGES_PER_ADDRESS=20, EARLY_BUYERS_N=100, SIG_SCAN_MAX_PAGES=30, RATE_LIMIT_RPS=8`), `get_settings() -> Settings` (lru_cache).
- Produces: ORM models `Token, Swap, Wallet, WalletStats, WalletTag, Watchlist, Alert` with the columns from spec §6; `make_engine(url) -> Engine` (sets `PRAGMA journal_mode=WAL` for sqlite), `init_db(engine)`, `session_scope(engine)` context manager.
- Produces: pytest fixture `session` (in-memory sqlite).

- [ ] Step 1: Write `test_db.py::test_swap_pk_dedup` — inserting the same Swap twice `(signature, wallet, mint)` via `session.merge` → `count == 1`; `test_wal_enabled` on a tmp-file sqlite → `PRAGMA journal_mode` returns `wal`.
- [ ] Step 2: `cd engine && pip install -e .[dev] && pytest -q` → FAIL (module not found).
- [ ] Step 3: Implement config.py, db.py. `.gitignore`: `.env, *.db, *.db-*, __pycache__, .venv, node_modules, .next`.
- [ ] Step 4: `pytest -q` → PASS.
- [ ] Step 5: `git commit -m "feat(engine): skeleton, settings, db models"`

### Task 2: Helius client + swap parser

**Files:**
- Create: `engine/hades/sources/helius.py`, `engine/hades/ingest/parse.py`, `engine/tests/fixtures/helius_swap_simple.json`, `helius_swap_multihop.json`, `helius_swap_usdc_quote.json`, `engine/tests/test_helius.py`, `engine/tests/test_parse.py`

**Interfaces:**
- Produces: `class HeliusClient(api_key, rps, max_retries=5)` async with:
  - `async address_transactions(address, *, tx_type="SWAP", before=None, max_pages) -> list[dict]` (GET `https://api.helius.xyz/v0/addresses/{address}/transactions`, page 100, stops at max_pages or empty)
  - `async signatures_oldest(address, max_pages) -> list[str]` (RPC `getSignaturesForAddress` limit 1000, pages backward, returns oldest→newest)
  - `async parse_transactions(signatures: list[str]) -> list[dict]` (POST `/v0/transactions`, chunk 100)
  - `async first_incoming_sol(address) -> tuple[str, str, int] | None` → (funder, signature, slot) of the oldest native transfer into the address.
  - Raises `HeliusRateLimited` after the retries run out (backoff 0.5·2^n s on 429/5xx).
- Produces: `WSOL = "So11111111111111111111111111111111111111112"`; `parse_swaps(tx: dict) -> list[ParsedSwap]` with `ParsedSwap(signature, wallet, mint, slot, block_time, side, token_amount: float, sol_amount: float | None, dex)`. Rule: the wallet = `feePayer`; compute the wallet's net change per mint from `tokenTransfers` + `nativeTransfers`/`accountData.nativeBalanceChange` (excluding the fee); a non-SOL, non-stable mint with a positive net = buy, negative = sell; sol_amount = abs net SOL (+WSOL); if SOL net ≈ 0 and the quote is USDC/USDT → `sol_amount=None`.

- [ ] Step 1: `test_parse.py`: `test_parse_simple_buy` (fixture simple → 1 ParsedSwap side=buy, sol_amount=1.5, token_amount=1_000_000); `test_parse_multihop_swap` (SOL→USDC→MEME → exactly 1 buy MEME, sol_amount=2.0); `test_parse_usdc_quote` → sol_amount is None. Fixtures are written by hand in the Helius Enhanced Tx format (fields: signature, slot, timestamp, feePayer, source, tokenTransfers[], nativeTransfers[], accountData[]).
- [ ] Step 2: `test_helius.py` with respx: `test_pagination_stops_at_max_pages` (server always returns 100 items → exactly max_pages calls); `test_retry_then_raise` (6× 429 → raises HeliusRateLimited, 6 calls); `test_retry_recovers` (2× 429 then 200 → returns data).
- [ ] Step 3: `pytest -q` → FAIL.
- [ ] Step 4: Implement (rate limiter: async token bucket in helius.py).
- [ ] Step 5: `pytest -q` → PASS. Commit `feat(engine): helius client and swap parser`.

### Task 3: DexScreener client + runner filter

**Files:**
- Create: `engine/hades/sources/dexscreener.py`, `engine/tests/fixtures/dex_pairs.json`, `engine/tests/test_dexscreener.py`

**Interfaces:**
- Produces: `class DexScreener` async: `token_pairs(mints: list[str]) -> list[dict]` (GET `https://api.dexscreener.com/tokens/v1/solana/{comma,mints}` max 30/req), `boosted_tokens() -> list[str]` (GET `/token-boosts/top/v1` + `/token-profiles/latest/v1`, filter chainId=solana), `price_native(mint) -> float | None` (best pair by liquidity).
- Produces: `is_runner(pair: dict, now_ms: int) -> bool` according to the Global Constraints; `best_pair(pairs) -> dict | None` (largest liquidity.usd).

- [ ] Step 1: Tests: `test_runner_by_change` (h24=350, liq 40k → True); `test_runner_by_fdv_young` (fdv 2M, pairCreatedAt 3 days, liq 50k → True); `test_not_runner_low_liq` (h24=900, liq 10k → False); `test_not_runner_old_fdv` (fdv 5M, 30 days, h24 20 → False); `test_best_pair`.
- [ ] Step 2: FAIL → implement → PASS. Commit `feat(engine): dexscreener client and runner filter`.

### Task 4: PnL engine (average cost)

**Files:**
- Create: `engine/hades/pnl.py`, `engine/tests/test_pnl.py`

**Interfaces:**
- Consumes: `Swap` rows (Task 1).
- Produces: `@dataclass Position(mint, bought_tokens, sold_tokens, cost_sol, proceeds_sol, realized_pnl_sol, remaining_tokens, remaining_cost_sol, max_holding, untracked_sell_tokens, first_buy_time, last_sell_time, closed: bool)`; `compute_positions(swaps: Iterable[Swap]) -> dict[str, Position]` (only for a single wallet, ignores sol_amount None, sorted by (block_time, slot, signature)); `unrealized_pnl(pos, price_native: float | None) -> float` (0 if price None).

- [ ] Step 1: Tests:
  - `test_simple_round_trip`: buy 100 tokens/1 SOL, sell 100/3 SOL → realized 2.0, closed True.
  - `test_avg_cost_partial`: buy 100/1, buy 100/3, sell 100/4 → realized 2.0 (avg 0.02), remaining 100, remaining_cost 2.0, closed False.
  - `test_oversell_not_profit`: buy 100/1, sell 150/3 → realized computed only on 100 (proceeds 2.0, realized 1.0), `untracked_sell_tokens == 50`, closed True.
  - `test_ignores_null_sol`: a swap with sol_amount None does not change the position.
  - `test_unrealized`: remaining 100, remaining_cost 2, price 0.05 → 3.0.
- [ ] Step 2: FAIL → implement → PASS. Commit `feat(engine): average-cost pnl`.

### Task 5: Wallet stats, bot filter, scoring

**Files:**
- Create: `engine/hades/scoring.py`, `engine/tests/test_scoring.py`

**Interfaces:**
- Consumes: `compute_positions`, `unrealized_pnl` (Task 4).
- Produces: `wallet_stats(address, swaps, prices: dict[str, float], now: datetime, window_days=30) -> WalletStatsData` (fields as in the `wallet_stats` table + `is_bot`, `median_roi`, `consistency`); `score_wallets(stats: list[WalletStatsData]) -> list[WalletStatsData]` (fills in `score` per the formula, `pct` = percentile rank 0..1 among eligible non-bot wallets; ineligible → score None); `persist_stats(session, stats)`.
- median_hold_sec = median(last_sell_time − first_buy_time) of closed positions; trades_per_day = swaps in window / window_days; consistency = number of weeks (out of 4) with realized > 0 / 4; median_roi = median(realized / cost_sol) of closed positions.

- [ ] Step 1: Tests: `test_bot_by_frequency` (4600 swaps/30 days → is_bot); `test_bot_by_hold` (median hold 10s → is_bot); `test_ineligible_under_5_closed` → score None; `test_score_ordering` (3 eligible wallets, the one with the highest PnL & win rate gets the highest score, all 0..100); `test_window_excludes_old` (swaps 40 days ago are not counted).
- [ ] Step 2: FAIL → implement → PASS. Commit `feat(engine): wallet stats, bot filter, scoring`.

### Task 6: Discovery + backfill pipeline + CLI

**Files:**
- Create: `engine/hades/pipeline.py`, `engine/hades/cli.py`, `engine/tests/test_pipeline.py`
- Modify: `engine/pyproject.toml` (script `hades = "hades.cli:app"`)

**Interfaces:**
- Consumes: HeliusClient, parse_swaps (T2), DexScreener, is_runner, best_pair (T3), wallet_stats/score_wallets/persist_stats (T5).
- Produces (async, take `session` + clients):
  - `seed_token(mint) -> Token` (DexScreener best pair → pool_address; `signatures_oldest(pool)` → parse the earliest tx → pool_created_slot, deployer = feePayer of the first tx)
  - `early_buyers(token, n) -> list[str]` (parse the oldest signatures until n unique buy wallets; save their swaps)
  - `backfill_wallet(address, force=False) -> int` (skip < 6 hours; save swaps idempotently; commit per wallet)
  - `discover() -> list[Token]` (boosted_tokens → token_pairs → is_runner → seed_token new ones)
  - `rescore() -> int` (prices via DexScreener for open mints, compute + persist all stats)
- CLI (Typer): `hades init-db`, `hades seed <mint>`, `hades discover`, `hades backfill [--wallet X | --token MINT]`, `hades rescore`, `hades serve` (uvicorn hades.api.main:app).

- [ ] Step 1: Tests with fake clients (in-memory classes matching the T2/T3 interfaces): `test_seed_sets_pool_and_deployer`; `test_early_buyers_unique_and_ordered` (fixtures with a duplicate wallet → unique, ordered by slot, max n); `test_backfill_skips_recent` (last_backfilled_at 1 hour ago → 0 calls); `test_backfill_idempotent` (run twice → swap count stays the same).
- [ ] Step 2: FAIL → implement → PASS.
- [ ] Step 3: Manual smoke (needs HELIUS_API_KEY): `hades init-db && hades seed <a known runner mint> && hades backfill --token <mint> && hades rescore` → print the count of swaps & top 5 wallets. Record the output in the PR/commit message.
- [ ] Step 4: Commit `feat(engine): discovery/backfill pipeline and cli`.

### Task 7: Insider detection (Styx & Tartarus)

**Files:**
- Create: `engine/hades/insider.py`, `engine/tests/test_insider.py`
- Modify: `engine/hades/pipeline.py` (call `tag_token(token)` after early_buyers; `rescore` calls `tag_repeat_early()`)

**Interfaces:**
- Consumes: `HeliusClient.first_incoming_sol` (T2), Token/Swap/Wallet/WalletTag.
- Produces: `async trace_funder(address, hops=2) -> list[str]` (funding chain, stores `wallets.first_funder`); `detect_snipers(token, swaps) -> list[Tag]`; `detect_bundles(token, swaps, funders: dict[str,str]) -> list[Tag]`; `async detect_deployer_link(token, buyers) -> list[Tag]`; `tag_repeat_early(session) -> list[Tag]`; `async tag_token(session, token)`. `Tag(address, tag, mint, evidence: dict)` with evidence always containing `signatures: list[str]` and `slots: list[int]` (+ `chain` for deployer_link, `cluster` for bundle).

- [ ] Step 1: Tests: `test_sniper_within_2_slots` (pool slot 1000, buy at 1002 → tag, 1003 → no tag); `test_bundle_needs_3_same_funder` (3 wallets same slot same funder → 3 tags with the same cluster; 2 wallets → none; 3 wallets different funders → none); `test_deployer_link_two_hops` (buyer←X←deployer → tag, chain [buyer,X,deployer]); `test_repeat_early_3_runners` (wallet in the top 50 of 3 runners → tag; 2 → none); `test_evidence_has_signatures` for every tag.
- [ ] Step 2: FAIL → implement → PASS. Commit `feat(engine): insider detection with evidence`.

### Task 8: FastAPI read API + auth

**Files:**
- Create: `engine/hades/api/__init__.py`, `engine/hades/api/main.py`, `engine/hades/api/schemas.py`, `engine/tests/test_api.py`

**Interfaces:**
- Produces `app` (FastAPI) with dependency `require_key` (header `X-Hades-Key`) on the `/api` router:
  - `GET /api/overview` → `{tracked_wallets, runner_tokens, alerts_24h, last_swap_at, top: WalletRow[10]}`
  - `GET /api/wallets?sort=score|total_pnl_sol|win_rate&tag=&hide_bots=true&limit=50&offset=0` → `{items: WalletRow[], total}`
  - `GET /api/wallets/{address}` → `{stats, positions: PositionRow[], tags: TagRow[], funding_chain: string[], pnl_curve: [{t, cum_pnl_sol}]}`; 404 if unknown
  - `GET /api/tokens/{mint}` → `{token, early_buyers: [{address, slot, sol_amount, tags[]}], bundles: [{cluster, wallets[]}]}`
  - `GET/POST/DELETE /api/watchlist` (POST body `{address, note}`; validate base58 32–44 chars → 422)
  - `GET /healthz` without auth.
- `WalletRow = {address, score, total_pnl_sol, realized_pnl_sol, win_rate, tokens_closed, tags[], is_bot}`.

- [ ] Step 1: Tests with TestClient + seeded DB: `test_requires_key` (401 without the header), `test_wallets_sorted_and_hide_bots`, `test_wallet_404`, `test_watchlist_validation_422`, `test_token_forensics_shape`.
- [ ] Step 2: FAIL → implement → PASS. Commit `feat(api): read endpoints and key auth`.

### Task 9: Helius webhook, alerts, SSE, watchlist sync (Cerberus)

**Files:**
- Create: `engine/hades/realtime.py`, `engine/tests/test_realtime.py`
- Modify: `engine/hades/api/main.py` (routes), `engine/hades/sources/helius.py` (`upsert_webhook(addresses, url, secret, webhook_id) -> str`)

**Interfaces:**
- Produces: `POST /webhook/helius` (check `Authorization`, body = a list of enhanced tx → parse_swaps → save Swap source='webhook' + Alert for wallets in watchlist or score ≥ 70; idempotent on signature+wallet+mint); `GET /api/stream` SSE event `alert` (JSON AlertRow) + `ping` every 15 s; in-process `AlertBus` (asyncio.Queue fan-out); watchlist POST/DELETE calls `sync_webhook()` (if `PUBLIC_WEBHOOK_URL` is set) → `upsert_webhook` with `webhookType="enhanced"`, `transactionTypes=["SWAP"]`.
- `AlertRow = {id, wallet, mint, symbol, side, sol_amount, signature, block_time}`.

- [ ] Step 1: Tests: `test_webhook_rejects_bad_secret` (401); `test_webhook_creates_alert_for_watchlist`; `test_webhook_idempotent` (same payload 2× → 1 alert); `test_non_watch_low_score_no_alert`; `test_sse_receives_alert` (subscribe to the bus, post webhook, receive the event); `test_sync_webhook_called_on_watchlist_add` (mock upsert_webhook).
- [ ] Step 2: FAIL → implement → PASS.
- [ ] Step 3: Manual: `ngrok http 8000`, set `PUBLIC_WEBHOOK_URL`, add 1 active wallet to the watchlist, wait for a swap → the alert appears in `curl -N -H "X-Hades-Key: …" localhost:8000/api/stream`.
- [ ] Step 4: Commit `feat(realtime): helius webhook, alerts, sse`.

### Task 10: Web scaffold, design tokens, layout, Overview

**Files:**
- Create: `web/` (create-next-app TS, App Router, Tailwind v4, no src dir), `web/app/globals.css` (tokens from Global Constraints as CSS vars + `@theme`), `web/app/layout.tsx` (fonts via `next/font/google`: Oswald 300/400, Mona Sans, Fira Code), `web/lib/api.ts`, `web/lib/format.ts`, `web/components/{Nav,Section,StatCard,WalletTable,LiveBadge,Address}.tsx`, `web/app/page.tsx`, `web/lib/format.test.ts`, `web/lib/api.test.ts`, `web/.env.example` (`HADES_API_URL`, `HADES_API_KEY`)

**Interfaces:**
- Produces: `apiGet<T>(path): Promise<{ok: true, data: T} | {ok: false, error: string}>` (server-only, `import "server-only"`, header X-Hades-Key, `cache: "no-store"`, timeout 8 s); `formatSol(n)` → `"+12.34 SOL"`/`"−3.10 SOL"` (U+2212 minus); `formatPct(0.634)` → `"63.4%"`; `shortAddr(a)` → `"8DC3…qGxg"`; `<Address value>` (Fira Code, copy button, Solscan link); `<Section index="#1" title="DISCOVER">` Hermes-style numbering.
- Overview: Hermes-style hero (`HADES` Oswald 300 ~clamp(64px,12vw,160px), tagline "Smart money always leaves a trail."), 3 StatCards, Top 10 Judgement, Cerberus preview (last 5 alerts). Error state: a card "Engine unreachable · <error>" instead of a crash.

- [ ] Step 1: vitest tests: `formatSol` signs & minus U+2212, `formatPct`, `shortAddr`; `test_api_error_state`: `apiGet` with fetch mocked to reject → `{ok:false}` (no throw).
- [ ] Step 2: `npm test` FAIL → implement → PASS.
- [ ] Step 3: `npm run build` succeeds; `npm run dev` + the engine is running → screenshot 1440 & 375, check contrast/focus ring.
- [ ] Step 4: Commit `feat(web): design system, layout, overview`.

### Task 11: Judgement (leaderboard) + Wallet profile

**Files:**
- Create: `web/app/wallets/page.tsx`, `web/app/wallet/[address]/page.tsx`, `web/components/{PnlChart,TagBadge,PositionsTable,FundingChain,WatchButton}.tsx`, `web/app/api/watchlist/route.ts` (proxy POST/DELETE to the engine, so the key stays on the server), `web/lib/query.test.ts`, `web/lib/query.ts`

**Interfaces:**
- Consumes: `apiGet`, format utils (T10); API T8.
- Produces: `parseWalletQuery(searchParams) -> {sort, tag, hideBots, page}` (default sort=score, hideBots=true, invalid sort → "score"); `TagBadge` colors: sniper=ember, deployer_link=loss, bundle=ember outline, repeat_early=primary, bot=muted; each tag has a tooltip listing evidence signatures → Solscan link.
- Wallet page: stat cards, PnlChart (lightweight-charts, cumulative realized), positions table (gain/loss with an icon + sign), Styx funding chain (address → address), WatchButton (client, calls `/api/watchlist`).

- [ ] Step 1: Tests `parseWalletQuery` (defaults, invalid sort, page < 1 → 1).
- [ ] Step 2: FAIL → implement → PASS; `npm run build` OK.
- [ ] Step 3: Commit `feat(web): judgement leaderboard and wallet profile`.

### Task 12: Token forensics, Cerberus Live, Watchlist

**Files:**
- Create: `web/app/token/[mint]/page.tsx`, `web/app/live/page.tsx`, `web/app/watchlist/page.tsx`, `web/app/api/stream/route.ts` (server proxy SSE from the engine with the key), `web/components/{LiveFeed,BundleCluster}.tsx`, `web/lib/live.ts`, `web/lib/live.test.ts`

**Interfaces:**
- Produces: `liveBadge(connected: boolean, lastEventAt: number | null, now: number) -> {label: "LIVE"} | {label: "STALE", since: string}` (LIVE only if connected && now−lastEventAt < 60_000; a ping counts as an event); `LiveFeed` (client EventSource to `/api/stream`, max 200 items, pause button, reduced-motion = no slide animation).
- Token page: deployer + pool slot, early buyers table with tags, bundle clusters grouped.
- Watchlist page: add (input address + note, inline validation), list, remove.

- [ ] Step 1: Tests `liveBadge`: connected+10s → LIVE; connected+90s → STALE; disconnected → STALE; lastEventAt null → STALE.
- [ ] Step 2: FAIL → implement → PASS; `npm run build` OK.
- [ ] Step 3: Commit `feat(web): token forensics, live feed, watchlist`.

### Task 13: Run scripts, README, end-to-end smoke

**Files:**
- Create: `README.md`, `scripts/dev.sh` (engine serve + web dev), `.github/workflows/ci.yml` (pytest + vitest + next build; no secrets)

- [ ] Step 1: README: setup (.env, `pip install -e engine[dev]`, `npm i` in web), command flow (init-db → discover/seed → backfill → rescore → serve), how to set up ngrok + webhook.
- [ ] Step 2: E2E smoke with a real Helius key: seed 3 runner tokens → backfill → rescore → open `/wallets`, `/wallet/<top>`, `/token/<mint>`, `/live`; screenshot every page; verify 1 insider tag against Solscan manually.
- [ ] Step 3: CI green on GitHub. Commit `chore: readme, dev script, ci`.
