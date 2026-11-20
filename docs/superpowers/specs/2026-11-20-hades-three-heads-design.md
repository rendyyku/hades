# HADES Phase 2: Three Heads (Solana, Hyperliquid, Polymarket) + Paper Copy

Status: draft, awaiting owner approval. Date: 2026-11-20.

## 1. Intent

HADES Phase 1 hunts the most profitable and insider-like wallets on Solana memecoins. The owner wants two more "heads" that hunt in markets with different rules:
- Hyperliquid perps
- Polymarket prediction markets

A hunted wallet is never trusted on its history alone. HADES paper-copies it forward, under realistic conditions. Only wallets that stay consistent earn a notification, and later (out of scope here) real copy trading.

The tool is for the owner first and is later rented as a SaaS, possibly per head. This matches the owner's standing principles:
- no fake winrates
- every claim traceable to raw data
- losses shown openly
- staged forward-testing before trust

## 2. Goals

1. **Two new heads.** Each one:
   - discovers candidate wallets
   - ingests their trade history
   - computes PnL in that market's own terms
   - ranks them
   - flags insider-like patterns, with evidence
2. **Paper copy for every head, Solana included:**
   - a shadow portfolio per followed wallet
   - mirrors only trades seen *after* the follow starts (forward only, no hindsight)
   - prices at HADES's observed time, with modelled delay, slippage and fees
   - fixed virtual stake rules
3. **Ladder per followed wallet: PAPER → NOTIF → COPY.** Promotion is evaluated automatically against the consistency gate (section 7). Demotion is automatic too. COPY is a recorded state only; nothing executes.
4. **One website.**
   - A tab per head.
   - A combined "Paper Arena" leaderboard of followed wallets across heads, ranked by *our* paper result, not theirs.
   - A head that is down shows as offline without breaking the others.
5. **Alerts (Cerberus).** NOTIF-level wallets push alerts to the web live feed now, and to Telegram later.

## 3. Non-goals (this phase)

- Real order execution, private keys or signing of any kind. COPY is a label.
- Telegram delivery. The alert contract is built so Telegram can plug in later.
- Multi-user accounts and billing. These belong to the SaaS phase.
- Spot trading on Hyperliquid. Perps only for now.

## 4. Anti-goals

These would make it a failure even if it works:
- **A leaderboard showing a winrate without sample size.** "WR 100%" on 3 bets must never appear unqualified.
- **Paper results that use the leader's fill price.** That is hindsight and flatters every wallet.
- **An insider tag without an evidence row** (tx hash / fill id / market id + time).
- **One head's failure taking down another head or the website.**
- **Backtesting the copy on history and presenting it as performance.** History is for discovery only.

## 5. Approaches considered

- **A. One engine with a venue layer.** Shared DB and schema with a venue column.
  - Least code.
  - But the three markets have different PnL models (avg-cost spot vs leveraged perps vs binary shares resolving to 0/1), so the shared schema becomes lowest-common-denominator, and one bad migration or crash hits all heads.
- **B. Three fully separate engines, one website.**
  - Maximum isolation.
  - But paper sim, consistency gate, ladder and alert logic would be written three times and drift apart.
- **C. Bolt tables onto the Solana engine.** Fastest, and the worst technical debt for SaaS.

**Chosen: B + a shared pure library (the owner delegated the choice; this is the assistant's call).**
- Three engines, each its own process, port, SQLite DB and failure domain.
- The truly universal logic lives in `core/` (`hades-core`), a pure-Python package with no I/O, no DB and no network.
- Each engine depends on it as a normal path dependency.
- The website talks to each engine through one identical **Head API contract** (section 9).

This keeps "three different worlds" isolated while the rules that must be identical (paper accounting, gate, ladder) are written and tested once.

## 6. Repository layout

    hades/
      core/           hades-core: paper sim, gate, ladder, contract models (pure)
      engine/         Solana head (existing, port 8000)
      hyperliquid/    Hyperliquid head (port 8010, own .venv optional, own DB)
      polymarket/     Polymarket head (port 8020, own DB)
      web/            one Next.js site; per-head proxies; Paper Arena

Each head has its own CLI (`hades`, `hades-hl`, `hades-pm`), its own `serve`, and its own tests. `.env` keeps one key set per head with prefixes (`HL_*`, `PM_*`). Neither public API needs a key.

## 7. hades-core (pure)

**Universal event.** Every head translates its world into:

    TradeEvent(head, wallet, market_id, side: open|increase|reduce|close|resolve,
               direction: long|short|yes|no, size, price, notional_usd,
               fee_usd, leader_time, observed_time, source_ref)

`source_ref` is the evidence pointer: a tx signature, an HL fill hash+tid, or a Polymarket trade tx/market id.

**Paper simulator.**
- Input: an ordered stream of TradeEvents for one followed wallet, plus a head-specific `ExecutionModel`.
- Output: paper fills, a position book, and an equity curve.
- Rules:
  - Entry and exit price = `ExecutionModel.price(event, observed_time)`. Never `event.price`.
  - Fees come from the ExecutionModel.
  - Sizing: fixed virtual stake per new position, a percentage of virtual bankroll (default 100 USD per position, bankroll 10,000 USD). Increases and reduces scale proportionally to the leader's size change.
  - Missed events (the head was offline) are recorded as `missed` and never retro-filled.
- Pure and deterministic. Same events, same output.

**Consistency gate** (owner-approved defaults, configurable):
- at least 28 days since the follow started
- at least 30 closed paper trades
- net paper PnL > 0 after fees and slippage
- max drawdown of the paper equity < 20%
- no single trade > 50% of total net profit

**Ladder.**
- PAPER → NOTIF when the gate passes.
- NOTIF → PAPER when, over a rolling 28-day window, the gate fails on net PnL or drawdown.
- NOTIF → COPY only by explicit owner action (a recorded state, no execution).
- Every transition is stored with its gate snapshot, for auditability.

**Gap report.** Leader PnL vs paper PnL over the same window. This gap is the honest "can we actually copy this" number and is shown everywhere a wallet is shown.

## 8. Heads

### 8.1 Solana (existing engine)

- Adds a TradeEvent adapter from stored swaps.
- Execution model:
  - the observed price is the first trusted price after `observed_time`
  - slippage is size-vs-liquidity based, with a default floor of 3%, because memecoin pools are thin
  - plus fees
- Phase 1 rules (floor, real-buy filter, truncation safety) still apply.
- Discovery is unchanged.

### 8.2 Hyperliquid

- **Data:** public `POST https://api.hyperliquid.xyz/info`.
  - `userFills` / `userFillsByTime` return at most 2000 per call and only the latest 10,000 fills.
  - `clearinghouseState` gives positions; `allMids` / `candleSnapshot` give prices.
  - A WebSocket `userFills` subscription is used for followed wallets.
  - All of it is rate-limited. The builder must respect the published weight limits.
- **Discovery:**
  - Candidates come from the public leaderboard feed the HL app uses. Verify the source and its stability; fall back to wallets seen in large trades via the `trades` WS.
  - Rank by our own recomputed stats, never the leaderboard's numbers.
- **PnL:**
  - Per fill, use HL's `closedPnl` and `fee`, cross-checked with our own reconstruction from `startPosition` + size.
  - Funding is fetched via `userFunding` and included in net.
  - Liquidations are detected and counted as losses.
- **Stats:** net PnL, ROI on margin, Sharpe-like consistency over daily PnL, max drawdown, trade count, average leverage, winrate **with N**.
- **Insider patterns** (each with evidence fill ids and time deltas):
  - `pre_listing`: opened a position in a coin within its first N hours of listing, large relative to their usual size.
  - `pre_move`: large position opened within X minutes before a ≥ Y% move, repeatedly (≥ 3 occurrences).
  - `fresh_whale`: an account under 7 days old whose first trades are large.
- **Execution model:** mid price at observed_time + 1s, half-spread + size-based slippage, HL taker fee. Funding applies while held.

### 8.3 Polymarket

- **Data:** public, no auth.
  - `gamma-api.polymarket.com` gives markets and events.
  - `data-api.polymarket.com` gives a user's trades, positions and activity, plus its leaderboard endpoint if available.
  - CLOB `book` / `prices-history` give the price at a time; the CLOB WebSocket covers followed wallets' markets.
  - The builder verifies exact endpoints and limits.
- **Discovery:** the leaderboard plus wallets seen in large trades on active markets.
- **PnL:** per position, buy cost vs sell proceeds or resolution payout (1 or 0 per share). Unresolved positions are marked at the current mid.
- **Stats:**
  - net PnL, ROI per bet, resolved-bet count
  - winrate **with N**, the **average entry price**, and a **penny-picker flag**: median entry ≥ 0.95 on the winning side
  - market diversity: the number of distinct events, and the share of profit from the top event
- **Ranking rule against fake WR 100%:**
  - Sort by a score that rewards net profit and ROI at entry prices below 0.80.
  - Require ≥ 20 resolved bets.
  - Penny pickers are shown but capped in ranking.
- **Insider patterns** (each with evidence trade tx and market id):
  - `fresh_wallet_bet`: a wallet under 14 days old whose first big bet is in a single market and wins.
  - `pre_resolution`: a large buy at < 0.50 within 24 hours before resolution, on the winning side, repeated (≥ 2).
  - `single_market_whale`: more than 80% of lifetime volume in one event.
- **Execution model:** best ask at observed_time (from the book or price history), plus size-based slippage and fees. Resolution pays 1 or 0.
- **Legal note:** Polymarket access from Indonesia and resale of its data must be checked before the SaaS phase. This does not block self-use.

## 9. Head API contract (identical on every head)

Every head serves these, guarded by its own `X-Hades-Key`:

- `GET  /api/health` → `{head, ok, last_ingest_at, version}`
- `GET  /api/wallets?sort=&limit=` → ranked wallets in a common shape: `{address, score, net_pnl_usd, trades, winrate, n, tags[], penny_picker?}`. Head-specific fields go under `extra`.
- `GET  /api/wallets/{address}` → common detail plus `extra`.
- `GET  /api/follows` and `POST/DELETE /api/follows/{address}` → start or stop paper-following.
- `GET  /api/paper/{address}` → paper fills, equity curve, gate status, ladder history, gap report.
- `GET  /api/alerts?limit=` and `GET /api/stream` (SSE) → the existing alert shape plus a `head` field.

The Solana engine adds these routes next to its existing ones; no breaking changes. Shared Pydantic models for the contract live in `hades-core`, so every head serializes identically.

## 10. Website

- **Nav:** Overview · Solana · Hyperliquid · Polymarket · Paper Arena · Cerberus · Watchlist.
- **Per-head tabs:** that head's leaderboard and wallet pages. The Solana pages are the existing ones.
- **Paper Arena:** all followed wallets across heads, with:
  - ladder state
  - our net paper PnL
  - drawdown
  - days followed
  - closed trades / 30
  - leader-vs-paper gap
  - a sparkline
- Each head is reached through its own server-side proxy, using the existing pattern. If one head times out, its section renders "offline".
- **Cerberus:** merges streams from all heads and filters by head.
- **Design:** the existing HADES system. Head accents: Solana ember, Hyperliquid electric blue, Polymarket a third token to be defined, contrast-checked.

## 11. Delivery order (each a separate plan, merged before the next)

1. Phase 1 fix wave lands and merges (in progress).
2. `hades-core` (paper sim, gate, ladder, contract) + Hyperliquid head + Paper Arena with HL only. HL has the cleanest data, so it proves the paper engine fastest.
3. Polymarket head.
4. Solana adapter + execution model, so memecoin wallets can be paper-followed too.
5. Telegram delivery for NOTIF wallets (all heads).

## 12. Constraints

- Secrets go only in `.env` (gitignored). No private keys anywhere. Never print or log keys.
- Free or public data first. Respect rate limits; cost guards are configurable, as in Phase 1.
- Windows dev box; each head must run on its own (`serve`) and together (`scripts/dev.sh` extended).
- Test-first. Pure core logic is unit-tested with fixtures; adapters are tested against recorded real API responses.
- Localhost bind and an origin allow-list, inherited from the Phase 1 fixes.

## 12a. Settled after builder check (assistant's calls unless marked owner)

- **Paper stake** (core):
  - Each followed wallet has its own virtual bankroll of 10,000 USD.
  - Each new leader position is sized as `risk_fraction` × current paper equity, with a default of 5%.
  - On Hyperliquid, the stake is *margin*, and we copy the leader's leverage, capped at 10x. So notional = stake × leverage. This makes the drawdown gate meaningful.
  - On Polymarket and Solana, the stake is the cash spent.
  - Increases and reduces scale with the leader's relative size change.
  - Positions the leader opened before `follow_started_at` are ignored entirely, including their later reduces and closes.
- **Copy price source:** each head records its own live price for followed wallets' markets.
  - Hyperliquid: `allMids` / l2Book via WS.
  - Polymarket: CLOB book via WS for markets with open paper positions or markets the wallet trades.
  - Solana: the trusted pair price.
  - If no live price is recorded within 30 s after `observed_time`, the event is marked `missed`. It is never priced from coarser history, to keep the no-hindsight promise.
  - Recorded prices are stored for audit.
- **Bot / MM / vault exclusion** (per head, shown as a tag; excluded from ranking and from auto-follow):
  - Hyperliquid: vault addresses; more than 300 fills per day on median; median hold under 60 s; or maker share over 70%.
  - Polymarket: more than 200 trades per day on median, or both sides of the same market held simultaneously in more than 30% of markets (market-making).
  - Solana: the Phase 1 rule, unchanged.
- **Follow capacity and ingest:**
  - Each head has a configurable cap, default 25 follows.
  - Hyperliquid uses WS `userFills` per followed wallet up to the per-IP subscription limit (the builder verifies the limit). Beyond that it polls `userFillsByTime` within the rate-limit budget. Measured observed delay is stored per event.
- **Alerts:**
  - A NOTIF wallet alerts on opens and closes. Increases and reduces only alert when the size change is at least 25%.
  - Every ladder change alerts.
  - Phase 1 watchlist and score-70 alerts continue unchanged.
  - Following a Solana wallet adds it to the Helius webhook address set, like the watchlist. This requires `PUBLIC_WEBHOOK_URL`; when it is unset, the Solana paper copy runs on polling backfill with a measured, larger delay.

## 12b. Owner decisions (approved 2026-11-20)

- **Q1: Polymarket gate.** It replaces the default gate for the Polymarket head only:
  - at least 56 days followed
  - at least 20 closed paper bets (resolved or sold)
  - net paper PnL > 0
  - max drawdown < 25%
  - no single event > 50% of net profit

  Solana and Hyperliquid keep the default gate. The gate config is per head in core.
- **Q2: Following.**
  - Each head auto-follows its top 10 non-bot wallets by score. The top 10 is re-evaluated daily; an auto-follow is never dropped while it has open paper positions.
  - The owner can add or remove wallets manually, with a per-head cap of 25.
  - Manual removals are remembered and never re-auto-followed.
  - Each follow records `origin: auto|manual`.

## 13. Left to the builder

- Exact endpoint choices and rate-limit weights for HL and PM, verified against live docs.
- Exact score formulas per head, within the rules above. The formulas are documented, and winrate is never shown without N.
- DB schema per head.
- Polling vs WebSocket mix, provided followed wallets are near-real-time (≤ 10 s observed delay target, measured and stored).
