# HADES Phase 2a: hades-core + Hyperliquid head + Paper Arena

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the shared paper-copy core and the Hyperliquid head. HL wallets are discovered, ranked, insider-tagged and auto-followed, then paper-copied forward under realistic execution. Results show in a Paper Arena on the existing website.

**Architecture:**
- Three-engine design (option B + pure core). `core/` holds pure, deterministic logic: events, contract models, paper simulator, gate and ladder.
- `hyperliquid/` is an independent FastAPI service on port 8010 with its own SQLite DB and CLI `hades-hl`. It depends on `core/` as a path dependency.
- The Next.js app reaches each head through a generic server-side head proxy.
- The Solana engine is untouched in this plan, apart from the README and dev script.

**Tech stack:**
- Python 3.12: FastAPI, SQLAlchemy 2, httpx, websockets, typer, pydantic v2, pytest, respx.
- Next.js 15, Tailwind v4, vitest.

**Spec:** `docs/superpowers/specs/2026-11-20-hades-three-heads-design.md`. Sections 7, 8.2, 9, 10, 12a and 12b are binding.

## Global Constraints

- Secrets only in `.env` (gitignored). Never print or log keys. No private keys, no signing, no order placement anywhere.
- Copy price is never the leader's price. If no live price is recorded within 30 s after `observed_time`, the event is `missed`. No history fill-in.
- History is for discovery only. Paper results start at `follow_started_at`, and positions the leader opened before that are ignored entirely.
- Winrate is never rendered or returned without `n`.
- Default gate: ≥ 28 days, ≥ 30 closed paper trades, net paper PnL > 0, max DD < 20%, top trade ≤ 50% of net profit.
- Paper stake: bankroll 10,000 USD per follow; new position = 5% of current paper equity as margin; leader leverage copied, capped at 10x.
- Follows: auto top 10 non-bot by score, re-evaluated daily; never drop an auto-follow with open paper positions; manual add/remove; cap 25; manual removals are never auto-followed again; `origin: auto|manual`.
- NOTIF alerts: opens, closes, size changes ≥ 25%, and every ladder change.
- HL bot/MM/vault rule: vault, OR median fills/day > 300, OR median hold < 60 s, OR maker share > 70%.
- Ports: HL engine 8010, Solana 8000, web 3000. Everything binds to 127.0.0.1.
- Each head's API uses its own `X-Hades-Key`: HL reads `HL_API_KEY` from `.env`; constant-time compare; 503 if unset.
- Test-first. HL adapters are tested against recorded real responses in `hyperliquid/tests/fixtures/`.

## Review Focus

1. **Leader trades that straddle the follow start.** The leader opened before the follow and adds or closes after it. Every event of that position is ignored, and paper never gets a phantom close. Owner: Task 2.
2. **A price feed gap.** The WS drops or the engine restarts mid-position: events are `missed`, open paper positions stay, and the curve keeps marking at recorded prices. Owners: Tasks 2 and 8.
3. **Liquidation of the leader.** The leader's position is liquidated, so paper closes at our recorded price, and paper loss can exceed margin only up to the margin (isolated semantics). Owners: Tasks 2 and 5.
4. **Flip trades.** A single HL fill can go long → short (`dir` "Long > Short"). It splits into close + open events. Owner: Task 5.
5. **Rate-limit 429 during backfill.** Backoff with Retry-After, partial progress persisted, no duplicate fills (unique `tid`). Owner: Task 4.

---

### Task 1: hades-core scaffold, TradeEvent, contract models

**Files:**
- Create:
  - `core/pyproject.toml` (name `hades-core`, package `hades_core`, deps: pydantic>=2)
  - `core/hades_core/__init__.py`
  - `core/hades_core/events.py`
  - `core/hades_core/contract.py`
- Test: `core/tests/test_events.py`, `core/tests/test_contract.py`

**Interfaces:**
- Produces, in `events.py`:
  - `Side = Literal["open","increase","reduce","close","resolve"]`
  - `Direction = Literal["long","short","yes","no"]`
  - `@dataclass(frozen=True) TradeEvent(head:str, wallet:str, market_id:str, side:Side, direction:Direction, size:Decimal, price:Decimal, notional_usd:Decimal, fee_usd:Decimal, leader_time:datetime, observed_time:datetime, source_ref:str, leverage:Decimal|None=None, position_id:str|None=None)`
  - `position_id` groups events of one leader position.
  - All datetimes are tz-aware UTC; construction raises ValueError otherwise.
- Produces, in `contract.py` (pydantic):
  - `HeadHealth{head, ok, last_ingest_at, version}`
  - `WalletRow{address, score: float|None, net_pnl_usd, trades:int, winrate: float|None, n:int, tags:list[str], is_bot:bool, extra:dict}`
  - `PaperSummary{address, ladder: Literal["PAPER","NOTIF","COPY"], equity_usd, net_pnl_usd, max_dd, closed_trades, days_followed, gap_usd, origin}`
  - `Alert{id, head, kind, wallet, market_id, payload:dict, created_at}`

**Steps:**
- [ ] Write failing tests:
  - `test_event_requires_utc` (naive datetime → ValueError)
  - `test_event_is_hashable_and_frozen`
  - `test_walletrow_winrate_requires_n`: a model validator rejects `winrate is not None and n == 0`
  - `test_contract_roundtrip_json`
- [ ] Run `cd core && uv venv --python 3.12 .venv && uv pip install -e ".[dev]" && .venv/Scripts/python -m pytest -q` → FAIL (module missing).
- [ ] Implement.
- [ ] Run the tests → PASS.
- [ ] Commit `feat(core): trade event and head contract models`.

### Task 2: Paper simulator

**Files:**
- Create: `core/hades_core/paper.py`
- Test: `core/tests/test_paper.py`

**Interfaces:**
- Consumes: TradeEvent.
- Produces:
  - `class ExecutionModel(Protocol)`:
    - `def fill_price(self, ev: TradeEvent) -> Decimal | None`. None means missed.
    - `def fee(self, notional: Decimal) -> Decimal`
    - `max_leverage: Decimal`
  - `@dataclass PaperConfig(bankroll=Decimal("10000"), risk_fraction=Decimal("0.05"), max_leverage=Decimal("10"))`
  - `@dataclass PaperFill(ev_ref, market_id, side, direction, size, price, fee, realized_pnl, missed: bool)`
  - `@dataclass PaperResult(fills: list[PaperFill], equity_curve: list[tuple[datetime, Decimal]], open_positions: dict[str, ...], closed_trades: list[ClosedTrade])`
  - `ClosedTrade(position_id, market_id, pnl_usd, opened_at, closed_at)`
  - `def simulate(events: Iterable[TradeEvent], model: ExecutionModel, cfg: PaperConfig, follow_started_at: datetime, mark: Callable[[str, datetime], Decimal|None] | None = None) -> PaperResult`

**Rules:**
- Sort by `(observed_time, source_ref)`.
- Ignore any event with `leader_time < follow_started_at`, and every later event sharing that `position_id`.
- `open`:
  - margin = risk_fraction × current equity
  - leverage = min(ev.leverage or 1, max_leverage)
  - qty = margin × lev / price
- `increase` / `reduce`: scale our qty by the leader's relative change. The leader's prior size is tracked per position_id.
- `close` / `resolve`: close everything.
- Loss per position is capped at its margin (liquidation).
- A missed open means the position is never opened, and its later events are ignored. A missed close keeps the position open, marked to the next recorded price via `mark`.

**Steps:**
- [ ] Write failing tests:
  - `test_uses_model_price_not_leader_price`
  - `test_ignores_positions_opened_before_follow` (Review Focus 1)
  - `test_missed_open_ignores_rest_of_position`
  - `test_missed_close_keeps_position_open` (Review Focus 2)
  - `test_sizing_5pct_equity_with_leverage_cap` (leader 25x → 10x; equity 10,000 → margin 500, notional 5,000)
  - `test_increase_reduce_scale_proportionally`
  - `test_loss_capped_at_margin` (Review Focus 3)
  - `test_fees_reduce_equity`
  - `test_deterministic`
- [ ] Run → FAIL.
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(core): paper copy simulator`.

### Task 3: Gate, ladder, gap

**Files:**
- Create: `core/hades_core/gate.py`, `core/hades_core/ladder.py`
- Test: `core/tests/test_gate.py`, `core/tests/test_ladder.py`

**Interfaces:**
- `@dataclass(frozen=True) GateConfig(min_days=28, min_closed=30, max_dd=Decimal("0.20"), max_top_share=Decimal("0.50"))`
- `POLYMARKET_GATE = GateConfig(56, 20, Decimal("0.25"), Decimal("0.50"))`
- `@dataclass GateResult(passed: bool, checks: dict[str, bool], values: dict[str, str])`
- `def evaluate_gate(result: PaperResult, follow_started_at, now, cfg: GateConfig) -> GateResult`
  - Max DD is computed on the equity curve.
  - top_share = max(closed pnl) / net profit, evaluated only when net > 0.
- `def next_ladder(current: Literal["PAPER","NOTIF","COPY"], gate_full: GateResult, gate_rolling: GateResult) -> str`
  - PAPER→NOTIF when gate_full passes.
  - NOTIF→PAPER when rolling-28d net ≤ 0 or rolling DD ≥ max_dd.
  - COPY is never automatic.
  - Demotion from COPY → PAPER uses the same rolling rule.
- `def gap(leader_pnl_usd: Decimal, paper_pnl_usd: Decimal) -> Decimal` returns leader − paper.

**Steps:**
- [ ] Write failing tests:
  - boundaries for each check: 27 vs 28 days, 29 vs 30 trades, DD exactly 20% fails, top share exactly 50% passes
  - `test_polymarket_gate_values`
  - `test_ladder_promote`
  - `test_ladder_demote_rolling`
  - `test_copy_never_automatic`
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(core): consistency gate and ladder`.

### Task 4: HL scaffold, config, DB, info client

**Files:**
- Create:
  - `hyperliquid/pyproject.toml` (`hades-hl`, deps: `hades-core @ file:../core`, fastapi, uvicorn, sqlalchemy, httpx, websockets, typer, pydantic-settings; dev: pytest, respx)
  - `hyperliquid/hades_hl/{__init__,config,db,client}.py`
- Test: `hyperliquid/tests/test_client.py`, `test_db.py`
- Fixtures: `hyperliquid/tests/fixtures/` (recorded real responses)

**Interfaces:**
- `Settings` (env prefix `HL_`, reads `../.env`):
  - `API_KEY`
  - `DATABASE_URL=sqlite:///hades_hl.db`
  - `INFO_URL=https://api.hyperliquid.xyz/info`
  - `WS_URL=wss://api.hyperliquid.xyz/ws`
  - `MAX_FOLLOWS=25`, `AUTO_FOLLOW_N=10`
  - `RATE_WEIGHT_PER_MIN=1200` (the builder verifies this against the docs and records the source in a comment)
- DB tables:
  - `wallets(address pk, first_seen, is_vault, is_bot, score, stats_json, scored_at)`
  - `fills(tid pk, address, coin, px, sz, side, dir, start_position, closed_pnl, fee, time, hash, crossed, liquidation bool)`
  - `funding(address, coin, time, usdc, pk(address,coin,time))`
  - `tags(address, tag, evidence_json, created_at, pk(address,tag))`
  - `follows(address pk, origin, started_at, removed_at, removed_manually bool, ladder, ladder_changed_at)`
  - `ladder_history(id, address, from_, to, gate_json, at)`
  - `prices(coin, time, mid, pk(coin,time))`
  - `paper_events(source_ref pk, address, json, observed_time, delay_ms)`
  - `alerts(id, kind, address, coin, payload_json, created_at, unique(kind,address,source_ref))`
  - `kv`
- `class HLClient`:
  - `async user_fills_by_time(addr, start_ms, end_ms=None) -> list[dict]`. Pages forward by the last `time` + 1, until fewer than 2000 rows come back or the 10,000-fill cap is hit. Returns the truncated flag through the `HLPage(fills, truncated)` dataclass.
  - `async user_funding(addr, start_ms) -> list[dict]`
  - `async clearinghouse_state(addr) -> dict`
  - `async all_mids() -> dict[str, Decimal]`
  - `async meta() -> dict`
  - `async leaderboard() -> list[dict]`
  - A token-bucket on weight. On 429, exponential backoff that honours Retry-After, max 5 attempts, then raise `HLRateLimited`.

**Steps:**
- [ ] Record fixtures with a one-off script `hyperliquid/scripts/record_fixtures.py`:
  - one known active address's fills, funding, meta and allMids
  - a leaderboard sample
  - commit the fixtures, not the script output logs
- [ ] Write failing tests:
  - paging stops below 2000
  - truncated at 10k
  - 429 with Retry-After is retried, then `HLRateLimited` (Review Focus 5)
  - a duplicate `tid` insert is ignored (`test_db.py`)
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(hl): scaffold, db and info client`.

### Task 5: HL fills → events, PnL, stats, bot rule, score

**Files:**
- Create: `hyperliquid/hades_hl/{normalize,pnl,stats,scoring}.py`
- Test: `test_normalize.py`, `test_pnl.py`, `test_stats.py`

**Interfaces:**
- `def fills_to_events(addr, fills, observed_time_fn=lambda f: f.time) -> list[TradeEvent]`
  - Maps `dir` "Open Long" / "Close Short" / ... to side and direction, using `startPosition` to tell open from increase and reduce from close.
  - A flip ("Long > Short") becomes two events (Review Focus 4).
  - `position_id = f"{coin}:{n}"`, where n increments whenever the position returns to zero.
  - A liquidation fill becomes `close`.
  - Spot fills (`coin` starting with `@`) are dropped.
- `def wallet_pnl(fills, funding) -> PnlSummary(net_usd, realized_usd, fees_usd, funding_usd, liquidations:int, closed_positions:list)`
  - Realized = sum of `closedPnl`, cross-checked against our own reconstruction within 1%. A mismatch is logged and flagged in stats.
- `def wallet_stats(fills, funding, now, window_days=30) -> dict` with keys:
  - net_pnl_usd, roi_on_margin, daily_sharpe, max_dd, trades, winrate, n, avg_leverage
  - median_fills_per_day, median_hold_s, maker_share
- `def is_bot(stats, is_vault) -> bool`, using the global rule.
- `def score(stats_rows) -> dict[addr, float]`:
  - percentile ranks: net pnl 40%, daily_sharpe 25%, roi 20%, max_dd 15% (inverted)
  - requires n ≥ 30 closed positions, else None
  - bots get None

**Steps:**
- [ ] Write failing tests on fixtures:
  - open, increase, reduce and close mapping
  - flip split
  - liquidation → close
  - spot dropped
  - pnl equals the closedPnl sum minus fees plus funding
  - the bot rule at each threshold
  - score None when n < 30
  - winrate None when n = 0
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(hl): normalization, pnl, stats and scoring`.

### Task 6: HL discovery, backfill, CLI

**Files:**
- Create: `hyperliquid/hades_hl/{pipeline,cli}.py`
- Test: `test_pipeline.py`, `test_cli.py`

**Interfaces:**
- `async discover(session, client, limit:int=200) -> list[str]`: leaderboard candidates, deduplicated, vaults marked (from leaderboard or meta fields).
- `async backfill(session, client, addr, days=90) -> BackfillResult(new_fills, truncated)`: idempotent; resumes from the max stored `time`.
- `def rescore(session, now)`
- CLI `hades-hl`, with typer configured as `pretty_exceptions_show_locals=False`:
  - `init-db`
  - `discover --limit`
  - `backfill --address|--all --days`
  - `rescore`
  - `tag`
  - `serve --port 8010` (host 127.0.0.1)
  - `follow-sync`
  - `paper-run`

**Steps:**
- [ ] Write failing tests (respx):
  - discover dedupes
  - a rerun of backfill inserts 0 new rows
  - a 429 mid-backfill keeps earlier pages (Review Focus 5)
  - CLI output never contains the key
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(hl): discovery, backfill and cli`.

### Task 7: HL insider tags with evidence

**Files:**
- Create: `hyperliquid/hades_hl/insider.py`
- Test: `test_insider.py`

**Interfaces:**
- `def tag_pre_listing(fills, listings: dict[coin, datetime], usual_notional) -> list[Tag]`: opens within 24 h of listing with notional ≥ 5× the wallet's median open notional.
- `def tag_pre_move(fills, prices, move_pct=0.05, window_min=30, min_occurrences=3) -> list[Tag]`: the price moved ≥ 5% in the position direction within 30 min after the open, repeated ≥ 3 times.
- `def tag_fresh_whale(first_seen, fills) -> list[Tag]`: the first fill is less than 7 days ago and one of the first 5 opens has notional ≥ 50,000 USD.
- `Tag(address, tag, evidence: list[{tid, hash, coin, time, detail}])`
- `persist_tags` rejects empty evidence.
- Listing time is the first candle time from `candleSnapshot`, cached in kv.

**Steps:**
- [ ] Write failing tests:
  - each rule: positive, negative and boundary
  - evidence carries `tid` and `hash`
  - empty evidence is rejected
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(hl): insider tags with evidence`.

### Task 8: HL live: WS fills, price recorder, polling fallback

**Files:**
- Create: `hyperliquid/hades_hl/live.py`
- Test: `test_live.py` (a fake WS server via `websockets.serve` on 127.0.0.1:0)

**Interfaces:**
- `class PriceRecorder`:
  - subscribes to `allMids`
  - writes `prices` at most once per coin per second
  - `def price_at(coin, t, within_s=30) -> Decimal|None`: the first recorded mid with `t ≤ time ≤ t + 30 s`
- `class FillWatcher`:
  - `userFills` subscription per followed address, up to `WS_MAX_USER_SUBS`. The builder verifies the limit and puts the doc URL in a comment.
  - Addresses beyond that limit are polled with `user_fills_by_time` every 5 s within the weight budget.
  - Every new fill is stored, gets `observed_time = now()` and `delay_ms`, then is handed to `on_fill`.
  - Reconnects with backoff. On reconnect it backfills fills since the last seen `time`, and those fills get `observed_time = reconnect time` (honest delay).
- `class HLExecutionModel(ExecutionModel)`:
  - fill_price = `price_at(coin, observed_time + 1 s)` ± (half-spread 0.02% + slippage `0.0005 * sqrt(notional/100_000)`), against us
  - fee = taker fee 0.045% (the builder verifies it and notes the source)

**Steps:**
- [ ] Write failing tests:
  - price_at within 30 s returns a price; outside it returns None
  - the recorder throttles writes
  - reconnect backfill sets observed_time to the reconnect time
  - the execution model price is worse than mid in both directions
  - the polling fallback is used beyond the subscription cap
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(hl): live fill watcher and price recorder`.

### Task 9: HL follows, paper runtime, ladder, alerts

**Files:**
- Create: `hyperliquid/hades_hl/{follows,paper_runtime,alerts}.py`
- Test: `test_follows.py`, `test_paper_runtime.py`, `test_alerts.py`

**Interfaces:**
- `def sync_auto_follows(session, now) -> FollowDiff`:
  - top `AUTO_FOLLOW_N` non-bot by score
  - never re-adds `removed_manually`
  - never drops an auto-follow that has open paper positions
  - respects `MAX_FOLLOWS`
- `def add_follow(session, addr, now)` and `def remove_follow(session, addr, now)` (manual): cap → `FollowCapError`.
- `def run_paper(session, addr, now) -> PaperSummary`:
  - builds events from `paper_events`
  - runs `simulate` with `HLExecutionModel`
  - evaluates the full and rolling gates
  - applies `next_ladder`
  - writes `ladder_history` on change
- `def emit_alerts(session, addr, new_events, ladder_change)`:
  - for NOTIF/COPY wallets: open, close, and size change ≥ 25%
  - always: ladder changes
  - deduplicated by unique key
- Scheduler inside `serve`:
  - follow sync daily
  - paper run every 60 s for follows with new events
  - rescore daily

**Steps:**
- [ ] Write failing tests:
  - auto top 10 excludes bots
  - manual removal is sticky
  - open positions block the drop
  - cap 25
  - promotion writes a history row and an alert
  - a 20% size change → no alert; 25% → alert
  - the alert unique key prevents duplicates
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(hl): follows, paper runtime, ladder and alerts`.

### Task 10: HL API (head contract)

**Files:**
- Create: `hyperliquid/hades_hl/api.py`
- Test: `test_api.py`

**Interfaces:** the spec §9 routes, all under `require_key`, returning core contract models:
- `GET /api/health`
- `GET /api/wallets?sort=score|pnl&limit≤200`
- `GET /api/wallets/{address}`, which adds `extra`: avg_leverage, liquidations, funding_usd, tags with evidence, pnl_curve
- `GET/POST/DELETE /api/follows[/{address}]`
- `GET /api/paper/{address}`: fills, equity_curve, gate (full and rolling, with checks), ladder_history, gap_usd, missed_count, median_delay_ms
- `GET /api/alerts?limit`
- `GET /api/stream` (SSE; heartbeat 15 s)

Docs and OpenAPI are disabled.

**Steps:**
- [ ] Write failing tests:
  - 401 without the key, 503 with no key configured
  - winrate comes with n
  - follow cap returns 409
  - `/api/paper` includes `missed_count` and `median_delay_ms`
  - SSE emits an inserted alert
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Commit `feat(hl): head api contract`.

### Task 11: Web: generic head proxy, Hyperliquid tab

**Files:**
- Create:
  - `web/lib/heads.ts`: a registry `{id:'solana'|'hyperliquid', label, baseUrlEnv, keyEnv, accent}`
  - `web/lib/headProxy.ts`
  - `web/app/hyperliquid/page.tsx`
  - `web/app/hyperliquid/wallet/[address]/page.tsx`
- Modify:
  - `web/components/Nav.tsx`
  - `web/app/globals.css`: add the `--hl` accent token, with contrast checked against `lib/contrast.ts`
  - `web/.env.example`: `HL_API_URL`, `HL_API_KEY`
- Test:
  - `web/lib/heads.test.ts`
  - `web/lib/headProxy.test.ts`
  - `web/components/WalletTable.test.tsx` (winrate renders with n)

**Interfaces:**
- `async function headFetch<T>(head, path, init?) -> {ok:true,data:T} | {ok:false, offline:true}`
  - timeout 4 s
  - never forwards the upstream body on error
  - the key header only goes server-side
- The HL wallet page:
  - stats with a window label
  - tags with evidence (fill hash links to `https://app.hyperliquid.xyz/explorer/tx/<hash>`)
  - PnL chart
  - a follow/unfollow button through the existing CSRF/origin-guarded proxy pattern

**Steps:**
- [ ] Write failing tests:
  - offline returns `{offline:true}` and no key leaks
  - the registry has unique ids
  - winrate text is `"64% (n=42)"` and never bare
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Lint and build.
- [ ] Commit `feat(web): head proxy and hyperliquid tab`.

### Task 12: Web: Paper Arena, Cerberus merge

**Files:**
- Create:
  - `web/app/arena/page.tsx`
  - `web/app/arena/[head]/[address]/page.tsx`
  - `web/components/{LadderBadge,GateChecklist,EquityCurve}.tsx`
- Modify:
  - `web/app/live/page.tsx`
  - `web/components/LiveFeed.tsx` (the head filter and merged streams)
  - `web/app/api/stream/route.ts` (fan-in over heads)
- Test:
  - `web/lib/arena.test.ts`
  - `web/lib/streamMerge.test.ts`

**Interfaces:**
- `function arenaRows(perHead: Record<HeadId, PaperSummary[] | 'offline'>) -> ArenaRow[]`: sorted by net paper PnL desc, with offline heads listed separately.
- The detail page shows:
  - our equity curve with drawdown shading
  - the gate checklist, both full and rolling
  - the ladder history
  - the leader-vs-paper gap
  - missed count and median delay
- The stream merge tags each event with `head`. One head's stream erroring does not close the others.

**Steps:**
- [ ] Write failing tests:
  - sort order
  - an offline head is shown separately
  - merge survives one stream error
  - the GateChecklist shows each check's value and threshold
- [ ] Implement.
- [ ] Run → PASS.
- [ ] Lint and build.
- [ ] Commit `feat(web): paper arena and multi-head cerberus`.

### Task 13: Dev script, CI, README, capped live smoke

**Files:**
- Modify:
  - `scripts/dev.sh`: starts HL on 8010
  - `.github/workflows/ci.yml`: adds `core` and `hyperliquid` jobs
  - `README.md`: the three-heads architecture, HL commands, the paper rules, honest limitations (the 10k-fill cap, no execution)

**Steps:**
- [ ] Run all suites (core, hyperliquid, engine, web lint/test/build) → green.
- [ ] Capped live smoke on a fresh `scratch/hades_hl_smoke.db`:
  - `init-db`
  - `discover --limit 20`
  - `backfill --all --days 30`
  - `rescore`
  - `tag`
  - `follow-sync`
  - `serve` on 8310 for 10 minutes with the WS live
  - record: the number of fills observed, median delay_ms, prices recorded, and any paper events
  - web on 3310: `/hyperliquid`, `/arena` → 200
  - stop by port
- [ ] Commit `chore: three-heads dev script, ci and readme`.
