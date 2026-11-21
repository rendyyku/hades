# HADES

HADES is a private, self-hosted tool for studying Solana memecoin trading. It finds tokens that "ran" (from DexScreener boosts), reads their early on-chain buyers through Helius, rebuilds each buyer's swap history and average-cost PnL in SOL, and scores wallets with a transparent formula. It also tags insider patterns: snipers, bundles, deployer-linked funding and repeat early buyers. A Next.js dashboard shows the leaderboard, wallet and token forensics, a funding-flow map, a watchlist and live alerts fed by a Helius webhook. It is read-only: HADES never holds private keys and never signs or sends transactions. Scores describe past behaviour and predict nothing.

Phase 2a adds a second head for **Hyperliquid perps** (`hyperliquid/`, CLI `hades-hl`) and a shared pure library (`core/`, `hades-core`). The HL head discovers wallets from the Hyperliquid leaderboard, backfills their fills and funding, scores them, tags insider patterns, and paper-copies the wallets it follows in real time. Each follow climbs a ladder (PAPER → NOTIF → COPY) only through a consistency gate. It is paper only: no keys, no signing, no orders.

Modules: Judgement (leaderboard) · Styx (funding trace) · Cerberus (watchlist and live alerts, fan-in over every head) · Tartarus (insider/bundle) · Paper Arena (paper copies, gate and ladder).
Phase 1 (Solana): design `docs/superpowers/specs/2026-11-19-hades-design.md` · plan `docs/superpowers/plans/2026-11-19-hades-phase1.md`.
Phase 2a (three heads, Hyperliquid): design `docs/superpowers/specs/2026-11-20-hades-three-heads-design.md` · plan `docs/superpowers/plans/2026-11-20-hades-phase2a.md`.

## Three heads

[Architecture diagram (Archify, interactive HTML)](docs/architecture/hades-phase2a.html) (download and open it locally in a browser; GitHub shows the HTML source instead of rendering it) · source: `docs/architecture/hades-phase2a.architecture.json`.

| Part | Path | Runs as | Port |
|---|---|---|---|
| `hades-core` | `core/` | library (pure): trade events, head API contract, paper simulator, gate, ladder | – |
| Hyperliquid head | `hyperliquid/` | `hades-hl serve`: the head API **and** the background runtime in one process | 8010 |
| Solana head (Phase 1 engine) | `engine/` | `hades serve` | 8000 |
| Web | `web/` | Next.js | 3000 |

```
                      Hyperliquid public API / WS (read-only: info, userFills, prices, leaderboard)
                                   ▲                                 ▲
   ┌───────────── hyperliquid/ (hades-hl) ─────────────┐             │
   │ CLI: init-db · discover · backfill · rescore ·    │─────────────┘
   │      tag · follow-sync · follow-add/remove        │
   │ serve (one process):                              │
   │   runtime: WS fill watcher, price recorder,       │──── hades-core (core/):
   │     paper runs every 60 s, reconcile, ladder,     │     simulate · gate · ladder ·
   │     alerts, daily backfill/rescore/follow sync    │     head contract models
   │   FastAPI /api/* (X-Hades-Key) + /api/stream SSE  │
   │ SQLite hades_hl.db                                │
   └──────────────────────────┬────────────────────────┘
                              │ server-side fetch / SSE with HL_API_KEY
   ┌──────────────────────────▼──────────────────────────────────────────┐
   │ web/ (Next.js): /hyperliquid · /arena · /live (Cerberus fan-in) ... │
   │   per-head server-side proxies (lib/heads.ts, lib/headProxy.ts)     │
   └──────────────────────────▲──────────────────────────────────────────┘
                              │ server-side fetch / SSE with HADES_API_KEY
                      engine/ (Solana head, below)
```

Everything binds 127.0.0.1. Each head has its own key (`HADES_API_KEY`, `HL_API_KEY`); the browser only talks to Next.js server routes.

## Solana head architecture (Phase 1)

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
(cd hyperliquid && uv sync --extra dev)  # hyperliquid/.venv with hades-hl and hades-core (editable path dependency)
(cd web && npm ci)

cp .env.example .env                    # engine: HELIUS_API_KEY, HADES_API_KEY, ...; HL head: HL_API_KEY, HL_DATABASE_URL, ...
cp web/.env.example web/.env.local      # web: HADES_API_URL/HADES_API_KEY and HL_API_URL/HL_API_KEY (same keys)
```

`hades-hl` reads `HL_*` settings from the environment or `.env` in the current directory or its parent. `HL_API_KEY` is the HL head's `X-Hades-Key`; without it the HL API answers 503.

**One database, one working directory.** The default `HL_DATABASE_URL=sqlite:///hades_hl.db` is relative to the directory the command runs in. `scripts/dev.sh` serves from the repo root, so run every `hades-hl` command from the repo root too (`uv run --project hyperliquid hades-hl ...`), or set an absolute path (`sqlite:///C:/path/to/hades_hl.db`, `sqlite:////abs/path/hades_hl.db`). Otherwise the CLI and `serve` silently use two different databases: follows added by the CLI never reach the running head, and the 28-day follow clock forks.

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

## Hyperliquid head

```bash
hades-hl init-db                      # create/migrate hades_hl.db (HL_DATABASE_URL; idempotent)
hades-hl discover --limit 200         # leaderboard candidates (cached 24 h), vaults excluded, one-request probe each (bot rule + ≥ 30 closed positions in 30 d)
hades-hl backfill --all --days 30     # fills (newest first, coverage tracked) + funding; or --address 0x...
hades-hl rescore                      # stats + scores from the stored fills
hades-hl tag --all                    # insider tags with evidence: pre_listing, pre_move, fresh_whale (or --address)
hades-hl follow-sync                  # auto-follow the top HL_AUTO_FOLLOW_N (10) non-bot wallets by score
hades-hl follow-add 0x... [0x...]     # manual follows (cap HL_MAX_FOLLOWS = 25); follow-remove, follows
hades-hl paper-run --all              # one paper run, prints the summaries
hades-hl serve --port 8010            # API on 127.0.0.1:8010 + runtime (WS fills, prices, paper every 60 s, daily rescore)
hades-hl watch --seconds 60           # debug: fill watcher + price recorder, prints fills and median delay
```

**Open positions.** The Hyperliquid wallet page opens with the wallet's live open positions (total unrealized PnL, account value, then per position: side, size, entry, mark, value, unrealized PnL, ROE, leverage and margin mode, liquidation price, margin). The head serves them at `GET /api/wallets/{address}/positions` from Hyperliquid's public `clearinghouseState`, cached 5 s per address; the page polls every 5 s through the web proxy and shows `LIVE`, or `STALE` with the last data when a poll fails.

`serve --no-api` runs only the runtime; `serve --seconds N` stops after N seconds. Details of discovery, backfill coverage and the runtime: `hyperliquid/README.md`.

### Paper rules

- **Copy price.** We copy at our own recorded mid (moved against us by half-spread and slippage), never at the leader's fill price.
- **Missed.** A leader fill with no live price recorded within 30 s of its observation is a missed copy. So is a fill that is more than 10 minutes old when we first observe it (after an outage), on every path: WebSocket recovery and REST polling alike. Missed events are stored and counted, never priced.
- **Start.** Paper copying starts at `follow_started_at`. A position the leader opened before that is ignored until it ends; there is no history fill-in.
- **Sizing.** Bankroll 10,000 USD. Each copy commits 5% of the current paper equity as margin, at the leader's leverage capped at 10x. Every position is isolated: its *price* loss is capped at its margin, and it is liquidated when that is reached. Funding is excluded from that cap.
- **Funding** is charged as a holding cost on open copies.

### Gate and ladder

- **Default gate:** ≥ 28 days since `follow_started_at`, ≥ 30 closed paper trades, net PnL > 0, max drawdown < 20%, and the top trade ≤ 50% of net.
- **Ladder:** `PAPER → NOTIF` when the full gate passes and the rolling 28-day window passes too (net > 0, drawdown < 20%). `NOTIF` drops back to `PAPER` when the rolling window has net ≤ 0 or a drawdown ≥ 20%. After a demotion there is no promotion for 24 hours, so a result hovering around zero cannot flip the ladder on every run. Every change is recorded with both gates and alerts once.
- **`COPY` cannot be set yet.** The design makes `NOTIF → COPY` an explicit owner action, but Phase 2a has no command, API route or button for it, so no wallet can reach `COPY`. It would be a label only: nothing executes.
- **Gap.** The arena, the paper page and the Hyperliquid wallet page show the leader-vs-paper gap: over the positions we copied and closed, the leader's closed PnL scaled to our size, minus our PnL on them. Missed and pre-follow positions are in neither side. The leader's figure is gross (HL `closedPnl`) and ours is net of fees and funding, so the gap is overstated by the leader's own fees and funding.
- **Traceability.** The paper page lists the paper fills (our price, fee, realized PnL, a link to the leader's transaction, missed events marked) and the open paper positions behind every paper number.

## Running the web app

```bash
cd web
npm run dev                          # http://127.0.0.1:3000 (binds to 127.0.0.1 only)
npm run build && npm run start       # production, also 127.0.0.1 only
```

`scripts/dev.sh` starts the Solana engine (8000), the Hyperliquid head (`hades-hl serve`, API and runtime, 8010) and the web dev server (3000) together, all on 127.0.0.1, and stops all three on Ctrl-C or when one exits. It needs bash ≥ 4.3 (`wait -n`): Linux bash and Windows git-bash work; the bash 3.2 that ships with macOS does not. `ENGINE_PORT`, `HL_PORT` and `WEB_PORT` override 8000, 8010 and 3000. If you change `ENGINE_PORT` or `HL_PORT`, point `HADES_API_URL` or `HL_API_URL` at it. dev.sh sets `HADES_PUBLIC_ORIGIN` to `http://localhost:$WEB_PORT,http://127.0.0.1:$WEB_PORT` unless you set it.

`HADES_PUBLIC_ORIGIN` (in `web/.env.local`) is a comma-separated list of exact origins, default `http://localhost:3000,http://127.0.0.1:3000`. Watchlist writes and follow / unfollow (`/api/heads/[head]/follows`) need an `Origin` in the list and a `Host` among its hosts; `/api/alerts`, `/api/heads/[head]/alerts`, `/api/heads/[head]/wallets/[address]/positions` and `/api/stream` need the `Host`. Anything else gets 403. Set it if you use another port or hostname.

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
- **Localhost only.** The engine (`hades serve`), the HL head (`hades-hl serve`) and the web app (`npm run dev` / `npm run start`, `scripts/dev.sh`) bind to 127.0.0.1. The web app has no login: anyone who can reach it can read everything, edit the watchlist and follow or unfollow wallets (an unfollow asks for confirmation: it restarts that wallet's forward test). Do not expose it (port forward, `-H 0.0.0.0`, a tunnel) without a reverse proxy that adds authentication.
- Watchlist writes and follow / unfollow through the web proxies need an allow-listed `Origin` and `Host` (`HADES_PUBLIC_ORIGIN`, CSRF guard). The alerts, open-positions and stream proxies need an allow-listed `Host`, which blocks DNS rebinding.
- ngrok exposes the whole engine, but `/api/*` stays key-protected and `/webhook/helius` checks `Authorization`.
- No private keys, no signing, no trading. The HL head uses only Hyperliquid's public read-only info API and WebSocket (plus the public leaderboard and vault JSON the HL app loads).

## Tests

```bash
(cd core && uv run pytest -q)
(cd hyperliquid && uv run pytest -q)
(cd engine && pytest -q)                # in engine/.venv
(cd web && npm run lint && npx vitest run && npm run build)
```

CI (`.github/workflows/ci.yml`) runs the same four jobs: `engine`, `core`, `hyperliquid` (plain pip: `pip install -e core -e "hyperliquid[dev]"`, since `[tool.uv.sources]` is uv-only) and `web`.

## Known limitations (Hyperliquid head, Phase 2a)

- **No execution.** No keys, no signing, no orders: paper only. `COPY` on the ladder is a label, and nothing can set it yet (no CLI command, API route or button).
- **10k-fill history cap.** HL returns at most the newest 10,000 fills of a wallet, so hyperactive wallets have partial history (`history_partial`). A partial wallet is scored only if its covered span is ≥ 7 days.
- **Approximate liquidation.** No maintenance margin and no fees in the liquidation test: a copy is liquidated when its loss reaches its isolated margin. Committed margin is not bounded across positions (several copies can together commit more than equity).
- **Liquidation timing.** Liquidation is evaluated only at hourly persisted marks and at events, so intra-hour wicks can be missed. The live "now" point (the last point of the equity curve, the headline equity and net PnL) is display-only: it never liquidates and is never stored. The gates, max drawdown and the ladder read the persisted points only, so they can lag the headline by up to an hour.
- **Funding** is charged as an accrued cost and is not capped by the margin.
- **Scaling-in traders** produce few opens, so the insider `pre_move` tag rarely fires for them.
- **Outages.** After a watcher outage, fills older than 10 min when we observe them are counted as missed, for WebSocket and polled follows alike.
- **Polled follows are slower.** Hyperliquid allows 10 wallets per IP on the user WebSocket. The 11th and later follows (by follow start) are polled over REST: every 5 s for one, about every 30 s at the 25-follow cap. The delay is measured per event and shown on the paper page.
- **Live copy path: one short smoke only.** A 25-minute run (2026-11-21 UTC) following 8 very active wallets took 4,809 real fills through the whole path: 4,576 events (56 opens, 54 closes, the rest increases and reduces; 38 on HIP-3 coins), 4,487 priced and settled (none missed), 28 closed paper trades, and 83 trades in which two followed wallets were the two counterparties. There has been no multi-day soak, and no wallet has reached the gate on real data.
- **Split fills over-size the copy (found by that smoke; not fixed).** A copy is sized on the leader's FIRST fill of a position (5% of paper equity), and every later fill scales it by that fill ÷ the leader's size so far. An order that fills in pieces, starting with a small one, therefore multiplies the copy. Observed: a first fill of 0.0002 BTC of a 0.67 BTC order became a 27.8 BTC copy (about $1.7M notional) on the 10,000 USD bankroll, and that follow's paper equity fell 40% in six minutes from fees and slippage. Until sizing is bounded per position, the paper result of a wallet whose orders fill in pieces is not trustworthy.
- **Measured delay needs a synced clock.** The delay is our clock minus Hyperliquid's fill time. On a machine whose clock runs behind it comes out negative (the smoke box was about 0.9 s behind: median −714 ms), and the paper page then shows a dash.
- **Open positions.** The wallet page lists positions on the main perp dex only; positions on HIP-3 dexes (`dex:COIN`) are not listed, and the page says so. It is polled (5 s), not streamed: the 10 user WebSocket subscriptions per IP are reserved for the follows. Each wallet page left open costs one `clearinghouseState` (weight 2) per 5 s of the 1200/min budget; a hidden tab does not poll. Mark is derived (`positionValue / size`): Hyperliquid reports no mark per position.
- **Reconcile scope.** A missed close is repaired from the leader's `clearinghouseState`: the main perp dex, and a HIP-3 dex only for a copy open on that dex. An answer that is failed, empty, malformed or partial closes nothing.
- **Discovery bias.** The HL top-PnL leaderboard is dominated by bots. Discovery prefilters (account value, volume/equity, month PnL, vaults) and probes recent activity with the bot rule and a minimum of 30 closed positions in 30 days, so it can miss high-turnover human traders and ones who trade rarely.
- **Unofficial feeds.** The leaderboard and vault list are the JSON the HL web app loads, not documented API.

## Known limitations (Solana head, Phase 1)

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
