# HADES

HADES is a private, self-hosted tool for studying Solana memecoin trading. It finds tokens that "ran" (from DexScreener boosts), reads their early on-chain buyers through Helius, rebuilds each buyer's swap history and average-cost PnL in SOL, and scores wallets with a transparent formula. It also tags insider patterns: snipers, bundles, deployer-linked funding and repeat early buyers. A Next.js dashboard shows the leaderboard, wallet and token forensics, a funding-flow map, a watchlist and live alerts fed by a Helius webhook. It is read-only: HADES never holds private keys and never signs or sends transactions. Scores describe past behaviour and predict nothing.

Modules: Judgement (leaderboard) · Styx (funding trace) · Cerberus (watchlist and live alerts) · Tartarus (insider/bundle).
Design: `docs/superpowers/specs/2026-11-19-hades-design.md` · Plan: `docs/superpowers/plans/2026-11-19-hades-phase1.md`.

## Architecture

```
 DexScreener (boosts, pairs, prices)        Helius (Enhanced API, RPC, webhooks)
            │                                   │                  ▲ create/update/delete
            ▼                                   ▼                  │ (watchlist changes,
   ┌──────────────────────── engine/ (Python 3.12) ────────────┐   │  hades webhook-sync)
   │ hades CLI: init-db · discover · seed · backfill · tag ·   │───┘
   │            rescore · serve · webhook-sync                 │
   │ ingest/parse → pnl → scoring → insider                    │
   │ SQLite: tokens, swaps, wallets, wallet_stats,             │◄── POST /webhook/helius
   │         wallet_tags, watchlist, alerts, kv                │    (Authorization = secret)
   │ FastAPI  /api/* (X-Hades-Key)  /api/stream (SSE)          │
   └──────────────────────────────┬────────────────────────────┘
                                  │ server-side fetch with X-Hades-Key
   ┌──────────────────────────────▼────────────────────────────┐
   │ web/ (Next.js 15): / · /wallets · /wallet/[address] ·     │
   │   /token/[mint] · /live · /watchlist                      │
   │   same-origin proxies for SSE, alerts and watchlist       │
   │   writes; the API key never reaches the browser           │
   └───────────────────────────────────────────────────────────┘
```

## Setup

Requires Python 3.12 (via [uv](https://docs.astral.sh/uv/)) and Node 22 or newer.

```bash
uv venv --python 3.12 engine/.venv
source engine/.venv/bin/activate        # Windows git-bash: source engine/.venv/Scripts/activate
uv pip install -e "engine[dev]"
(cd web && npm ci)

cp .env.example .env                    # engine: HELIUS_API_KEY, HADES_API_KEY, HELIUS_WEBHOOK_SECRET, ...
cp web/.env.example web/.env.local      # web: HADES_API_URL and the same HADES_API_KEY
```

The engine reads `.env` from the current directory or its parent, so `hades` works from the repo root and from `engine/`. Make `HADES_API_KEY` and `HELIUS_WEBHOOK_SECRET` long random strings, e.g. `python -c "import secrets;print(secrets.token_urlsafe(32))"`.

## Command flow

```bash
hades init-db                       # create/migrate tables (idempotent)
hades discover --limit 3            # seed up to 3 NEW runners from DexScreener boosts (0 = unlimited)
hades seed <MINT>                   # ...or seed one token by hand (pool, deployer, pool creation slot)
hades backfill --token <MINT>       # early buyers → their swap histories → insider tags
hades backfill --wallet <ADDR>      # one wallet (skipped if backfilled < 6 h ago; --force overrides)
hades rescore                       # stats + scores for every wallet, repeat_early tags
hades tag <MINT>                    # re-run insider tagging for a seeded token
hades serve --port 8000             # API on 127.0.0.1:8000
hades webhook-sync                  # push the watchlist to the Helius webhook once
```

## Running the web app

```bash
cd web
npm run dev                          # http://127.0.0.1:3000 (binds to 127.0.0.1 only)
npm run build && npm run start       # production, also 127.0.0.1 only
```

`scripts/dev.sh` starts the engine and the web dev server together (both on 127.0.0.1) and stops both on Ctrl-C. It needs bash ≥ 4.3 (`wait -n`): Linux bash and Windows git-bash work; the bash 3.2 that ships with macOS does not. `ENGINE_PORT` and `WEB_PORT` override 8000 and 3000. If you change `ENGINE_PORT`, point `HADES_API_URL` at it. dev.sh sets `HADES_PUBLIC_ORIGIN` to `http://localhost:$WEB_PORT,http://127.0.0.1:$WEB_PORT` unless you set it.

`HADES_PUBLIC_ORIGIN` (in `web/.env.local`) is a comma-separated list of exact origins, default `http://localhost:3000,http://127.0.0.1:3000`. Watchlist writes need an `Origin` in the list and a `Host` among its hosts; `/api/alerts` and `/api/stream` need the `Host`. Anything else gets 403. Set it if you use another port or hostname.

## Realtime alerts (Helius webhook)

1. Expose the engine with `ngrok http 8000`.
2. Set `PUBLIC_WEBHOOK_URL` in `.env` to the ngrok base URL, e.g. `https://abcd.ngrok.app` (a URL that already ends in `/webhook/helius` also works). Then restart `hades serve`.
3. Add a wallet to the watchlist, either on `/watchlist` or with `POST /api/watchlist`. Every watchlist change syncs the webhook, and the response header `X-Webhook-Sync` reports `ok`, `skipped` or `error`.
4. `hades rescore` re-syncs after scoring when `PUBLIC_WEBHOOK_URL` is set (scores decide the top-score part of the address set).
5. `hades webhook-sync` syncs by hand. It prints the status and the webhook id, never a key.

The webhook follows the watchlist plus the top `WEBHOOK_TOP_SCORE_MAX` (default 25) non-bot wallets with score ≥ 70, best score first (ties by address).

The sync is safe to repeat:
- **Create.** It first lists the existing webhooks and reuses one with the same URL. The create POST is never retried, and the new id is stored in the `kv` table (`HELIUS_WEBHOOK_ID` overrides it).
- **Update.** A stale id (404) is looked up again by URL.
- **Empty address set** (no watched wallets and no wallet scoring ≥ 70). The webhook is deleted.

Alerts fire for swaps by watched wallets or by wallets that score ≥ 70; only the subscribed addresses above are delivered by Helius. Duplicate deliveries are ignored, and bodies over 5 MB are refused with 413.

## Cost guards

Helius bills per request. These settings cap what one run can spend:

| Setting | Default | What it bounds / costs |
|---|---|---|
| `HELIUS_MAX_PAGES_PER_ADDRESS` | 20 | Enhanced-API pages (100 txs each) per wallet backfill: ≤ 20 requests per wallet |
| `EARLY_BUYERS_N` | 100 | wallets backfilled per token: a token backfill costs about N × pages requests |
| `SIG_SCAN_MAX_PAGES` | 30 | `getSignaturesForAddress` pages (1000 sigs each) when scanning a pool for early buyers, plus parse calls for those sigs |
| `RATE_LIMIT_RPS` | 8 | client-side token bucket; 429/5xx are retried with exponential backoff (max 5) |
| `HELIUS_API_KEY` | – | required for seed/discover/backfill/tag and the webhook sync |
| `HELIUS_WEBHOOK_SECRET` | – | Helius echoes it as `Authorization`; the receiver rejects anything else |
| `HELIUS_WEBHOOK_ID` | empty | pins a webhook id (otherwise the id from kv, else a lookup by URL) |
| `WEBHOOK_TOP_SCORE_MAX` | 25 | top-score wallets (non-bot, score ≥ 70) added to the webhook's address set |
| fixed: funder trace | 3 pages | `first_incoming_sol` per tagged wallet, cached in the DB; a wallet with ≥ 3000 signatures gets no funder (its first tx was not reached) |
| fixed: hub probe | 3 pages | an address with ≥ 3000 signatures is labelled `hub` |

A wallet is not backfilled again within 6 hours. For a cheap smoke run use `EARLY_BUYERS_N=15 HELIUS_MAX_PAGES_PER_ADDRESS=2 SIG_SCAN_MAX_PAGES=10`.

## Data rules

- **Swaps.**
  - Only SOL/WSOL-quoted swaps count toward PnL; others are stored with `sol_amount = null`.
  - A multi-hop swap (SOL→USDC→MEME) counts as one buy with the total SOL in.
  - A buy needs a net SOL outflow and a sell a net inflow.
  - Rent-only token receipts (< 0.0025 SOL spent) are not buys.
- **PnL.**
  - Average cost per (wallet, mint), in SOL.
  - A position is closed when ≤ 1 % of max holdings remains.
  - Selling more than the tracked holdings (airdrops, transfers) is not profit; the excess goes to `untracked_sell_tokens`.
- **Prices.** Unrealized PnL uses only WSOL-quoted pairs with liquidity ≥ $10k. Without one, the position stays unpriced.
- **Score.**
  - 30-day window. Only non-bots with ≥ 5 closed tokens get a score.
  - Score = `40·pct(total PnL) + 25·win rate + 20·pct(median ROI) + 15·consistency` (0–100).
  - Bot = more than 150 trades/day or a median hold under 30 s.
- **Runner.** `priceChange.h24 ≥ 300 %`, or (`fdv ≥ $1M` and age ≤ 7 days); in both cases liquidity ≥ $30k.
- **Insider tags.** Every tag stores on-chain evidence (signature/slot).
  - `sniper`: bought ≤ 2 slots after pool creation.
  - `bundle`: ≥ 3 wallets in the same slot with the same funder.
  - `deployer_link`: funding traces to the deployer within 2 hops.
  - `repeat_early`: a top-50 buyer in ≥ 3 runners.
- **Hubs.** Exchange-like addresses (≥ 3000 signatures) are labelled `hub`. They never count as a shared funder, and funding traces stop at them.
- **Buys for tagging.** One definition everywhere (insider tags, early buyers, forensics): `side = buy` with a positive SOL amount. Rent-only receipts and non-SOL-quoted rows never count.
- **Truncated history.** When a mint or pool signature scan hits `SIG_SCAN_MAX_PAGES` with every page full, the oldest tx seen is not the launch. The token gets `history_truncated = true`, `deployer` and `pool_created_slot` stay empty, `sniper` and `deployer_link` are skipped, and `bundle` / `repeat_early` run on the earliest observed buyers. The token page says so.
- **Funders.** A funder is the sender of the oldest plain SOL transfer into the wallet: txs with token transfers or a swap (swap proceeds, WSOL unwraps) are skipped, and when Helius lists instructions the System Program must be one of them. The parsed tx has no account owners, so this approximates "not from a program-owned pool".

## Security

- Keys live only in `.env` and `web/.env.local`, both gitignored. Request URLs are redacted in logs, and errors are logged by type, so the Helius key and the webhook secret are never printed.
- `HADES_API_KEY` stays on the server. The browser only talks to Next.js server routes, which add `X-Hades-Key`. `/api/*` answers 401 without the key, or 503 if no key is configured.
- **Localhost only.** The engine (`hades serve`) and the web app (`npm run dev` / `npm run start`, `scripts/dev.sh`) bind to 127.0.0.1. The web app has no login: anyone who can reach it can read everything and edit the watchlist. Do not expose it (port forward, `-H 0.0.0.0`, a tunnel) without a reverse proxy that adds authentication.
- Watchlist writes through the web proxy need an allow-listed `Origin` and `Host` (`HADES_PUBLIC_ORIGIN`, CSRF guard). The alerts and stream proxies need an allow-listed `Host`, which blocks DNS rebinding.
- ngrok exposes the whole engine, but `/api/*` stays key-protected and `/webhook/helius` checks `Authorization`.
- No private keys, no signing, no trading.

## Known limitations (Phase 1)

- **Capped history.** Backfill reads at most `HELIUS_MAX_PAGES_PER_ADDRESS` pages. Older history is not seen (a warning is logged), so wallets with long histories have partial PnL.
- **Rugged bags.** An open position with no trusted price (rugged, delisted, below the liquidity floor) adds 0 to unrealized PnL, so it does not lower the score. Its cost basis is shown as "Unpriced open cost" on the wallet page (`unpriced_cost_sol`); the score formula ignores it in Phase 1.
- **Windows.** Wallet stats and the score use the last 30 days; positions and the PnL curve on the wallet page are all-time. The page labels each.
- **Truncated tokens.** Old or very busy tokens can have `history_truncated`: no deployer, launch slot or sniper tags. DexScreener `pairCreatedAt` is not used as a fallback anchor.
- **Unattributed legs.** Multi-leg transactions with several token legs get `sol_amount = null`. Sells dropped this way can leave phantom open positions; the $10k price floor limits the damage.
- **PnL edge cases.**
  - A position reopened below 1 % of its old peak can still read as closed.
  - An unknown swap side aborts that wallet's stats.
- **Tags.**
  - Stale insider tags of tokens that are never re-tagged are not removed.
  - The hub count is global.
  - Negative funder lookups are cached only in-process.
- **DexScreener.** `Retry-After` is ignored and token pairs are not de-duplicated.
- **Webhook.**
  - A re-delivered swap can overwrite `source` (backfill vs webhook).
  - SSE pings are sent only when idle.
  - Concurrent watchlist POSTs for the same address can race.
- **Web.**
  - Charts rebuild on refresh.
  - Minor accessibility nits remain (link underline, TxLink label).
  - Paused live-feed counters can over-count duplicate SSE events.
- **Storage.** SQLite only. Slot columns are plain `Integer`; move them to `BigInteger` before Postgres (Phase 3).
- **Dependencies.** `npm audit` reports advisories in the Next.js template dependencies.
