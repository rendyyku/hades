# HADES — Solana Smart-Money & Insider Intelligence

Status: superseded by 2026-11-27-hades-early-signals-design.md (code kept at tag `hades-solana-final`).

Status: DRAFT, waiting for owner review
Owner: rendyyku (Phantom Intelligence)
Date: 2026-11-19

## 1. Intent

Build our own on-chain intelligence tool, in the spirit of Arkham and Narrative Alpha, focused on
**Solana memecoins**. It answers three questions:

1. Which wallets are actually the most profitable, as proven by their swaps on-chain?
2. Which wallets are "insiders": linked to the deployer, sniping the first block, bundling, or
   repeatedly entering a token before it runs?
3. What are those wallets buying or selling *right now*?

Phase 1: the owner uses it himself to trade. If the results are good, Phase 3 rents it out as a SaaS.
The brand is **HADES**, god of the underworld and of hidden wealth (Plouton, "the wealthy one").
Narrative hook: "Smart money always leaves a trail. Hades sees what lies beneath the surface."

## 2. Goals / Non-goals / Anti-goals

Goals (Phase 1)
- A data engine that indexes swaps, computes per-wallet PnL (realized + unrealized), and scores wallets.
- Insider detection with **evidence** (tx signatures that can be checked on Solscan), not just a label.
- Website dashboard (built first): leaderboard, wallet profile, token forensics, live feed, watchlist.
- Realtime: activity from wallets on the watchlist appears on the live feed within seconds.

Non-goals (Phase 1)
- Telegram alerts (Phase 2, its own plan; reuses the engine's `alerts` table).
- Auto-buy / copy-trade. No private keys in the system at all.
- Other chains (EVM, Hyperliquid), billing and multi-user (Phase 3).
- Indexing ALL of Solana. We only index around runner tokens and tracked wallets.

Anti-goals (a failure even if it technically runs)
- Numbers that cannot be verified. Every PnL figure and every insider tag must be traceable to signatures.
- A leaderboard full of MEV bots / arbitrage bots disguised as "smart money".
- A fake "live" label: if the data is stale, the UI must say so (last-updated time + stale state).
- API key costs that blow up because of unlimited pagination.

## 3. Constraints

- Dev machine: Windows 11 (later can be deployed to the owner's Ubuntu VPS). Python 3.12+, Node 20+.
- Data: Helius (Enhanced Transactions API + Webhooks + RPC), DexScreener public API (free).
  Birdeye is optional, not a dependency in Phase 1.
- A Helius webhook needs a public URL → use ngrok in dev (already installed on the machine).
- Secrets only in `.env` (gitignored). Repo is private: `github.com/rendyyku/hades`.
- Every API call goes through a page cap + rate limiter that can be configured.

## 4. Approaches considered (how to find smart money)

A. **Token-first (CHOSEN)**: take tokens that already ran (DexScreener filter + manual seed) → pull
   their first N buyers → backfill each buyer's swap history → compute PnL & score.
   + Cheap, directly targets wallets that were "early" on winners. + Insider detection is natural
   (there is a deployer & pool creation slot). − Biased toward tokens we know about; mitigated with
   recurring discovery.
B. Wallet-first: index all swaps on Raydium/Pump.fun/Meteora in real time, compute PnL for every wallet.
   + Complete. − Data volume is huge (millions of swaps/day), needs our own Geyser/indexer, expensive.
   Too early for Phase 1.
C. Buy data from a third party (Birdeye/GMGN top-traders API).
   + Fast. − Cannot be verified, costs money, no edge (everyone sees the same thing), weak for a SaaS.

Choice: A for Phase 1 (owner's call to "go ahead"; the approach was chosen by the agent). B becomes
the path to Phase 3 if a SaaS needs coverage.

## 5. Architecture

```
DexScreener ──► discovery ──► runner tokens ──► early buyers (Helius sigs+parse)
                                                     │
Helius Enhanced Tx ◄── backfill wallet history ◄─────┘
        │
        ▼
   swaps (normalized) ──► pnl (avg-cost) ──► wallet_stats ──► scoring + bot filter
        │                                         ▲
        └──► insider (funding trace, snipe, bundle, repeat-early) ──► wallet_tags (+evidence)

Helius Webhook ──► /webhook/helius ──► swaps + alerts ──► SSE /api/stream ──► web Live Feed
FastAPI (/api/*) ◄── Next.js web (dashboard)
```

- `engine/` — Python package `hades`: SQLAlchemy 2 + SQLite (WAL) in Phase 1, `DATABASE_URL` is
  ready to switch to Postgres. FastAPI + uvicorn. Typer CLI. httpx async.
- `web/` — Next.js 15 (App Router, TypeScript), Tailwind CSS v4. Server components fetch to the API.
- Single-owner auth: every `/api/*` requires the header `X-Hades-Key` = `HADES_API_KEY`.
  Web reads it on the server side (never sent to the browser).

## 6. Data model (main tables)

- `tokens(mint PK, symbol, name, deployer, pool_address, pool_created_slot, pool_created_at,
  first_seen_at, peak_mcap_usd, is_runner)`
- `swaps(signature, wallet, mint, PK(signature,wallet,mint), slot, block_time, side['buy'|'sell'],
  token_amount, sol_amount, source['backfill'|'webhook'], dex)`
- `wallets(address PK, first_funder, funded_at, label, is_bot, last_backfilled_at)`
- `wallet_stats(address PK, window_days, realized_pnl_sol, unrealized_pnl_sol, total_pnl_sol,
  tokens_traded, tokens_closed, win_rate, median_hold_sec, trades_per_day, score, computed_at)`
- `wallet_tags(address, tag, mint NULL, evidence_json, created_at)` — tag ∈
  `deployer_link | sniper | bundle | repeat_early | bot`
- `watchlist(address PK, note, added_at)` and `alerts(id, wallet, mint, side, sol_amount, signature,
  block_time, created_at, delivered_tg BOOL)`

## 7. Calculation rules (pinned)

- Only swaps with SOL/WSOL as the quote side are counted in Phase 1 (memecoins are almost all
  SOL-quoted). Other swaps are stored with `sol_amount = NULL` and ignored by PnL.
- PnL uses the **average cost** method per (wallet, mint), in SOL. Realized = Σ(sell_sol −
  avg_cost × token_sold). Unrealized = remaining_tokens × current price_native (DexScreener) − remaining cost.
- A token is "closed" if remaining_tokens ≤ 1% of max holdings. Win = realized_pnl > 0 on a closed token.
- Bot filter: `trades_per_day > 150` OR `median_hold_sec < 30` → `is_bot = true`, excluded from the leaderboard.
- Score (0–100), only for wallets with `tokens_closed ≥ 5` in a 30-day window:
  `score = 40·norm(total_pnl_sol) + 25·win_rate + 20·norm(median ROI per token) + 15·consistency`
  where `norm` = percentile rank among eligible wallets, consistency = share of the 4 weeks with PnL > 0.
- Insider rules:
  - `sniper`: buy within ≤ 2 slots after `pool_created_slot`.
  - `deployer_link`: first_funder (trace ≤ 2 hops of incoming SOL transfers) == deployer or the deployer's funder.
  - `bundle`: ≥ 3 different wallets buying the same mint in the same slot AND sharing the same first_funder.
  - `repeat_early`: among the first 50 buyers in ≥ 3 different runner tokens.
  - Evidence = list of signatures + slots, shown in the UI with Solscan links.
- Runner (auto discovery): Solana, `priceChange.h24 ≥ 300%` OR `fdv ≥ $1M with age ≤ 7 days`, liquidity ≥ $30k.
  Plus manual seed `hades seed <mint>`.

## 8. Cost & limit guards

- `HELIUS_MAX_PAGES_PER_ADDRESS` (default 20), `EARLY_BUYERS_N` (default 100),
  `SIG_SCAN_MAX_PAGES` (default 30 × 1000 signatures to find the pool's earliest tx).
- Global rate limiter (default 8 req/s) + retry with backoff on 429/5xx.
- Wallet backfill is skipped if `last_backfilled_at` < 6 hours ago.

## 9. Website — design system

Reference: hermes-agent.nousresearch.com (structure & mood) + UI/UX Pro Max output for "crypto
analytics dashboard" (pattern: Real-Time / Operations; Fira Code for data; a11y checklist).

- Colors (tokens, no raw hex in components):
  `--bg #111122` (navy-black, Hermes DNA), `--surface rgba(255,255,255,.05)`,
  `--border rgba(255,255,255,.10)`, `--fg #F2F2F2`, `--muted #9A9AB5`,
  `--primary #3D3DFF` (electric blue, Hermes), `--ember #FF5A1F` (Hades brand highlight, sparing),
  `--gain #22C55E`, `--loss #EF4444`.
- Fonts: Display **Oswald 300, uppercase, tight tracking** (huge headings like Hermes);
  UI text **Mona Sans**; numbers/addresses **Fira Code** with `tabular-nums`.
- Elements: 1px translucent borders, cards with backdrop-blur 12px, numbered sections
  ("#1 DISCOVER", "#2 TRACE"…), Lucide icons (no emoji as icons).
- Required: contrast ≥ 4.5:1, visible focus ring, `prefers-reduced-motion`, responsive
  375/768/1024/1440, gain/loss shown with a +/− sign and an icon, not just color.
- "LIVE" badge only when the SSE is connected and the last event is < 60 s old; otherwise "STALE · <time>".
- Module names (agent's call, adjustable): **Judgement** (leaderboard), **Styx** (funding trace),
  **Cerberus** (watchlist & live alerts), **Tartarus** (insider/bundle list).

Pages:
1. `/` Overview: Hermes-style hero ("HADES" in giant type + tagline), key metrics (tracked wallets,
   runner tokens, alerts 24h), Top 10 Judgement, Cerberus live preview.
2. `/wallets` Judgement: table sortable by score/PnL/win rate, filter tag, hide bots.
3. `/wallet/[address]`: stats, cumulative PnL curve, per-token positions, insider tags + evidence,
   Styx funding chain, "Add to watchlist" button.
4. `/token/[mint]`: forensics — deployer, pool slot, first 100 buyers + tags, bundle clusters.
5. `/live` Cerberus: live feed (SSE) for watchlist + top-score wallets.
6. `/watchlist`: manage watchlist (add/remove + note); syncs the address list to the Helius webhook.

## 10. Phases

- Phase 1 (this plan): engine + API + website + realtime webhook. Self-use.
- Phase 2: Telegram bot (Cerberus alerts → TG), reads `alerts.delivered_tg = false`.
- Phase 3: SaaS — multi-user, auth, billing (QRIS/USDT like Phantom), Postgres, own indexer (approach B).

## 11. Left to the builder

- Exact table layout & component structure, as long as it follows the tokens/rules above.
- Choice of chart library (recommended: `lightweight-charts` or Recharts).
- Small details of Helius response parsing, as long as the fixture tests pass.

## 12. Agent's calls (please review)

- Token-first approach (§4); SQLite first; average-cost PnL; numeric thresholds in §7–8.
- Copy-trade completely excluded (no private keys) — answers the earlier open question with "manual execution".
- Hades sub-module names, and the electric blue + ember colors.
