# SDD ledger: plan docs/superpowers/plans/2026-11-20-hades-phase2a.md

- **Spec:** docs/superpowers/specs/2026-11-20-hades-three-heads-design.md (owner approved 2026-11-20).
- **Branch:** feat/phase2a-hl, cut from main 71a326f, which is the Phase 1 merge.
  - fd8513d docs: specs and plan
  - next commit: archived Phase 1 ledger
- **Phase 1 ledger archive:** docs/superpowers/records/2026-11-19-hades-phase1-ledger.md
- **CI:** GitHub Actions run 44731466417 on main was `startup_failure`, path "BuildFailed", 0 s. The YAML parses fine, so the cause is account-level (likely private-repo Actions billing or minutes). Owner to check github.com/settings/billing. This does not block local work.

## Pre-flight scan

| Pair / task | Shared file or interface | Finding | Ruling |
|---|---|---|---|
| T1 ↔ T2 | TradeEvent fields, incl. position_id and leverage | consistent | — |
| T2 ↔ T3 | PaperResult (equity_curve, closed_trades) → evaluate_gate | consistent. T9 needs a "rolling" gate, but T3 defines no function for it | P1 |
| T2 self | `open_positions: dict[str, ...]` is unspecified | the implementer chooses the value type | P2 |
| T3 ↔ T9 | next_ladder(current, gate_full, gate_rolling) | needs evaluate_rolling | P1 |
| T4 ↔ T5..T10 | DB tables | consistent | — |
| T4 self | `hades-core @ file:../core` dependency syntax | uv's relative-path dependency form must be used | P3 |
| T4 self | leaderboard endpoint unverified | spec §8.2 fallback applies | P4 |
| T5 ↔ T2 | fills_to_events position_id `coin:n` vs T2 ignore-by-position_id | consistent | — |
| T8 ↔ T2 | HLExecutionModel implements the ExecutionModel Protocol, returns None when missed | consistent | — |
| T9 ↔ T10 | PaperSummary from the core contract | consistent | — |
| T11 self | WalletTable.test.tsx is a Solana component; the HL tab needs a winrate render | — | P5 |
| T12 ↔ Phase 1 | `web/app/api/stream/route.ts` fan-in must keep the Phase 1 Host allow-list and key-hiding | — | P6 |
| T13 ↔ T4/T11 | dev.sh, HL port 8010, env names HL_API_URL / HL_API_KEY | consistent | — |

## Rulings

- **P1.** T3 adds `evaluate_rolling(result, now, cfg, days=28) -> GateResult` (checks net>0 and dd<max_dd over the window [now-28d, now]). T9 uses it.
  - Why: the ladder needs a rolling gate and no task defined one.
  - If wrong: small rework in T3/T9.
- **P2.** T2 `open_positions: dict[str, OpenPosition]`, where `OpenPosition(position_id, market_id, direction, qty, entry_price, margin, leverage, leader_size)`.
  - Why: T12 shows open positions.
  - If wrong: a rename.
- **P3.** T4 depends on core via `[tool.uv.sources] hades-core = { path = "../core", editable = true }` plus `dependencies = ["hades-core"]`. CI installs `pip install -e core -e "hyperliquid[dev]"`.
  - Why: `file:../` URLs are not portable.
  - If wrong: an install-config tweak.
- **P4.** If the HL leaderboard endpoint is unavailable or unstable, discovery falls back to large trades from WS `trades` and recent fills (spec §8.2). Fixtures record whichever works.
  - If wrong: weaker discovery, nothing else.
- **P5.** T11 adds `formatWinrate(w, n)` to `web/lib/format.ts` (tested in format.test.ts) and uses it on the HL pages. Solana's WalletTable is not modified.
  - Why: keep Phase 1 untouched.
  - If wrong: a cosmetic inconsistency.
- **P6.** T12's stream fan-in keeps the `originGuard` Host check before any upstream fetch and never exposes keys. Per-head keys stay server-only.
  - Why: the Phase 1 I5 fix.
  - If wrong: a security regression, so the reviewer must check it.
- **P7.** Subagent model: `delegate_task` has no per-call model parameter, so every subagent inherits the session model.
  - Why: tool limitation.
  - Cost: higher spend.

## Progress
- P8 Ruling: Tasks 1-3 (all pure hades-core, sequential deps, small) dispatched to ONE implementer with 3 separate commits, reviewed as one package — saves 2 dispatch+review seats — cost if wrong: larger review diff.
- Tasks 1-3: dispatched (BASE ae9cd03)
- Tasks 1-3: implementer DONE_WITH_CONCERNS (548dc47, 1556e30, 025f5a4; core 46 tests).
- Ruling P9: PM per-event top share — ClosedTrade gets optional group_id; GateConfig.top_share_by_group (POLYMARKET_GATE=True) groups by group_id or position_id — spec 12b says "single event" — cost if wrong: small.
- Ruling P10: gate net check requires BOTH equity-based net>0 AND realized closed-trade net>0; top share uses realized closed-trade profits — honest + no unrealized inflation — cost: slightly stricter gate.
- Ruling P11: missed-close reconciliation is the head's job (HL Task 9 reconciles vs clearinghouseState and emits synthetic close with source_ref 'reconcile:<...>' priced by model) — core stays pure — cost: HL task scope +.
- Ruling P12: core adds optional holding-cost hook: ExecutionModel may define holding_cost(pos, start, end)->Decimal (default 0 if absent); simulate charges it on each step. HL funding implemented in Task 8 — spec 8.2 funding while held — cost: small.
- Ruling P13: approximate liquidation (no maintenance margin/fee) accepted; README limitation in Task 13.
- Tasks 1-3: review dispatched.
- Tasks 1-3: review → Needs fixes (I1 fallback-key ignore leak; P9/P10/P12 unimplemented; test gaps). Fix round 1 dispatched deleg_0c368c74 (FIX_BASE 025f5a4). Deferred: M5 fill price marks opposite side; M8 committed margin unbounded (README T13).
- Tasks 1-3: fix round 1 implementer DONE (8386706, core 65).
- Ruling P14: holding cost not capped by margin — funding is accrued cash cost in reality (HL deducts funding from margin; approximation acceptable) — cost if wrong: slightly pessimistic paper PnL.
- Ruling P15: rolling gate keeps equity-net only (P10 applies to the full gate) — rolling is a demotion guard; unrealized losses should demote — cost: none.
- Tasks 1-3: fix round 1/5 (all addressed; 025f5a4..8386706)
- Tasks 1-3: complete (commits ae9cd03..8386706, review clean after 1 fix round)
- Tasks 1-3 minor (deferred): liquidation seeds ignored with leader_size possibly 0 (fallback key edge); market_id group fallback dead branch; missed close leaves copy open (head reconciles per P11); holding_cost should return Decimal.
- Task 4: dispatched (BASE 8386706)
- Task 4: implementer DONE (8862af1; hl 30, core 65). Leaderboard endpoint works (~38MB, 46k rows) → cache, fetch rarely. WS user-sub limit = 10 unique users/IP (Task 8: WS ≤10, poll rest). userFunding 500/page.
- Ruling P16: paging restarts at last time inclusive + dedupe by tid (deviation from brief) — prevents boundary loss — cost: none.
- Ruling P17 (for T5/T6): wallets with truncated (10k-fill) history get stats.history_partial=true, window_days = actual covered span; scored only if covered span ≥ 7 days, else score None with reason "history too short (hyperactive)" — honest + effectively excludes HFT — cost: some active humans unscored.
- Task 4: review dispatched.
- Task 4: complete (commits 8386706..8862af1, review clean). Minors carried into Task 5: M1 same-ms >2000 sets truncated, M3 funding page cap flag, M4 missing tid fail loud. Deferred: M2 docstring, M5 shape checks, M6 retry-after floor, M7 env/db path anchoring, M8 mask db url, M9 strict sleeps assert, M10 fixture trimmed_from.
- Task 5: dispatched (BASE 8862af1)
- Task 5: implementer DONE (65aa795; hl 83, core 65). closedPnl cross-check 0.43% mismatch on real fixture. Fills ordered via startPosition chain (tid not time-ordered) — later code must use order_fills.
- Ruling P18: partial liquidation (position after fill ≠ 0) → `reduce`; full → `close` — mirrors leader honestly — cost: small.
- Ruling P19: liquidation counting requires addr (make it a required arg) — avoids counting liquidator fills — cost: none.
- Ruling P20: fee_usd = fee + builderFee when builderFee present — conservative — cost: none.
- Task 5: review dispatched.
- Task 5: review → Needs fixes (CRITICAL: core simulate sort (observed_time, source_ref) undoes chain order — 17/51 fixture positions wrong; P18-P20 unmet; liq-flip double count; position_id unstable/gap reuse; greedy chain).
- Ruling P21: TradeEvent.seq (default 0); simulate sorts (observed_time, seq, source_ref).
- Ruling P22: position_id = coin:<opening tid> ('f' suffix for flip open, 'pre<tid>' mid-position start, 'gap<tid>' after chain gap) — stable across windows — cost: id format change (no consumers yet).
- Ruling P23: same-ms chain backtracking ≤12 fills, deterministic tie-break (startPosition, tid), warn + chain_gaps stat on failure.
- Task 5: fix round 1 dispatched deleg_a164188e (FIX_BASE 65aa795).
- Task 5: fix round 1 implementer DONE (6a50293, f3b1b0c; hl 110, core 67). Per-fill mismatch 1.09% → 0.000% with HL avg-entry rounding (sz_decimals).
- Ruling P24: callers (Task 6 pipeline, Task 9 runtime) MUST pass sz_decimals from meta — exact closedPnl parity — cost: false mismatch flags if forgotten.
- Ruling P25: gap fill with startPosition==0 uses plain coin:<tid> (carried to Task 6 as a 1-line change) — more stable id — cost: none.
- Task 5: scoped re-review dispatched.
- Task 5: fix round 1/5 (all addressed; 65aa795..f3b1b0c)
- Task 5: complete (commits 8862af1..f3b1b0c, review clean after 1 fix round). Minor deferred: perf-time assert flaky risk; misnamed fixture test.
- Ruling P26: paper_events.seq is a persisted per-address monotonic counter assigned at ingest (Task 8/9), not per-batch — cross-batch ordering — cost: none.
- Task 6: dispatched (BASE f3b1b0c)
- Task 6: implementer FAILED 20:43 (Anthropic 429, reset 22:39) during live smoke; WIP uncommitted, backed up task-6-wip.patch/.tar. Resume dispatched.
- Task 6: implementer DONE_WITH_CONCERNS (a69c34a; hl 134). Smoke: 5 wallets, 0 scored (3 bot @10k cap, 2 n<30 no fills in 30d), history_partial 3/5.
- Ruling P27: backfill pages BACKWARD from now (userFillsByTime with endTime = oldest seen − 0, inclusive + tid dedupe) until start or 10k cap; store contiguous coverage [cov_start, cov_end] in kv per address; P17 window_days = now − cov_start of the contiguous range ending at latest fetch — recent data is what matters for copying — cost: rework of backfill paging.
- Ruling P28: startPosition chain comparison uses tolerance = 10^-szDecimals/2 (or rounding to szDecimals) — HL float noise — cost: none.
- Ruling P29: discovery prefilter: leaderboard rows with accountValue ≥ 10,000 USD, month volume > 0, month volume ≤ 200 × accountValue (drops HFT/MM), month PnL > 0; rank by month PnL; vaults excluded — scoreable humans — cost: may miss some high-turnover discretionary traders.
- Task 6: review dispatched.
- Task 6: review → Needs fixes (CRITICAL C1/P27 forward paging keeps oldest; P28/P29 unmet; I4 tid-only PK collides across wallets). Fix round 1 dispatched deleg_47e33c9a (FIX_BASE a69c34a).
- Task 6: fix round 1 implementer DONE_WITH_CONCERNS (5b620a1; hl 156, core 67). Probes: HL returns OLDEST 2000 of window ascending → backward window walking; counterparties share tid (confirmed). Prefilter 46,288→3,294. Smoke 10 wallets: all bot (window 0.04-4 days), newest-first backfill works, 0xb83d gaps 1274→0.
- Ruling P30: discovery adds bot-proxy prefilter month vlm ≤ 30×AV (was 200×) AND a cheap activity probe per candidate (one userFills call = latest ≤2000 fills): skip before backfill if implied fills/day > 300 or median hold < 60s; iterate down the ranking until `limit` non-bot candidates found (probe budget 5×limit) — cost if wrong: misses high-turnover humans.
- Ruling P31: funding fetched only over [cov_start, cov_end] — funding rows outside covered fills are useless — cost: none.
- Task 6: scoped re-review dispatched (round 1). P30/P31 to be implemented in round 2 with any re-review findings.
- Task 6: fix round 1/5 (all addressed; a69c34a..5b620a1). Round 2 dispatched deleg_16c9ef3e (FIX_BASE 5b620a1): P30, P31, pager early-exit coverage minor. Deferred cosmetic: vaults_checked stale in 24h cache; pre-fix cache unfiltered ≤24h; cap cuts inside ms.
- Task 6: fix round 2 implementer DONE_WITH_CONCERNS (9b86ca4; hl 170). Smoke: prefilter 46,302→2,469; probe 25→3 accepted; rescore 0 scored (2 bot by median fills/day, 1 n<30).
- Ruling P32: probe rejects on max(mean, median) fills/day > 300 (align with bot rule) — carried to Task 7 as a 1-line change — cost: none.
- Ruling P33: default probe budget 10×limit; Task 13 smoke uses discover --limit 20 — HL top-PnL is bot-dominated — cost: more probe calls (cheap).
- Ruling P34: chain tolerance fallback without szDecimals = max(1e-9, 1e-9×|position|) relative — carried to Task 7 — cost: none.
- Task 6: scoped re-review round 2 dispatched.
- Task 6: fix round 2/5 (all addressed; 5b620a1..9b86ca4)
- Task 6: complete (commits f3b1b0c..9b86ca4, review clean after 2 fix rounds). Minor deferred: same_ms_overflow flag misused on early exit; early-exit coverage never resumes; probe-rejected wallets already in DB still backfilled; maker-share fallback includes spot. Carried to T7: P32, P34, meta shape check in HLClient.meta.
- Task 7: dispatched (BASE 9b86ca4)
- Task 7: implementer DONE_WITH_CONCERNS (21e4e4d; hl 198). Smoke: 3 wallets, 0 tags (real negative; best move 1.94%).
- Ruling P35: candle resolution fallback 1m→5m→15m→1h accepted; evidence detail MUST record the candle interval used — honest evidence — cost: none.
- Ruling P36: listing = first candle with n>0 or v>0 accepted.
- Ruling P37: fresh_whale extra userFillsByTime[0,cov_start) check accepted; skip on failure.
- Ruling P38: pre_move counts only opens with notional ≥ wallet median open notional — reduces noise from dust opens — cost: may miss small repeated insiders.
- Ruling P39: scaling-in traders produce few opens → README Known limitation (T13).
- Task 7: review dispatched.
- Task 7: review → Needs fixes (I1 non-429 HLError deletes valid tags — reproduced; I2 P38 unmet; I3 no real failure test). Ruling P40: pre_move counts distinct move events (overlapping 30-min windows on same coin count once). Fix round 1 dispatched deleg_5ea53b2c (FIX_BASE 21e4e4d).
- Task 7: fix round 1 implementer DONE (796c16b; hl 218). Scoped re-review dispatched.
- Task 7: fix round 1/5 (all addressed; 21e4e4d..796c16b)
- Task 7: complete (commits 9b86ca4..796c16b, review clean after 1 fix round). Minor carried to T8: cached [] candle slice treated as miss. Deferred: zero-hash opens occupy cap slots; M2, M7, M8, M9.
- Task 8: dispatched (BASE 796c16b)
- Task 8: implementer DONE_WITH_CONCERNS (063637a; hl 252). Smoke: allMids ~every 12s (deprecated), fastAssetCtxs ~1/s; snapshot suppressed; 0 live fills in window.
- Ruling P41: Task 9 runtime uses fastAssetCtxs (coins-filtered) as primary price feed; allMids fallback — 12s staleness skews copy price — cost: more WS traffic.
- Ruling P42: record prices only for watch-set coins (coins traded by follows in last 30d + open paper positions), prune prices older than 14 days daily (Task 9) — DB growth — cost: none.
- Ruling P43: slippage on OUR copy notional estimate is correct (our order impact). Task 9 must FundingRates.ensure() before simulate.
- Ruling P44: recovered fills observed_time = REST recovery response time (implementer's choice; never earlier than the fill could be known) — accepted.
- Task 8: review dispatched.
- Task 8: review FAILED 01:35 (Anthropic 429); re-dispatched 14:16 as deleg_fed72d8e.
- Task 8: review → Needs fixes (I1 failed recovery loses gap fills; I2 last_seen advanced before commit — both reproduced). Ruling P45: price window [obs+1s, obs+30s]. Fix round 1 dispatched deleg_dbd52f03 (FIX_BASE 063637a). Carried to T9: persist last_seen (M6), full poll budget (M7).
- Task 8: fix round 1 implementer DONE (b3d5b27; hl 265).
- Ruling P46: while a recovery is pending for an address, live fills for that address are BUFFERED (stored, not emitted); on recovery success, gap fills + buffered fills are emitted together, chain-ordered, seq assigned in leader order — otherwise simulate (sorted by observed_time) can see a close before its open (recovered later) → phantom position — cost: delayed emission during outages (honest observed_time = release time).
- Task 8: fix round 2 dispatched (P46) before re-review; single re-review will cover 063637a..HEAD.
- Task 8: fix round 2 (P46) DONE (e9fceea; hl 268). Scoped re-review over 063637a..e9fceea dispatched.
- Task 8: fix rounds 2/5 (all addressed; 063637a..e9fceea)
- Task 8: complete (commits 796c16b..e9fceea, review clean after 2 fix rounds). Carried to T9: per-address recover lock (stale floor race), M6 persist last_seen+floor, M7 poll budget.
- Task 9: dispatched (BASE e9fceea)
- Task 9: implementer DONE (606b806; hl 328, core 67). Smoke: serve 180s, fastAssetCtxs 997 rows/10 coins, 0 live fills, 3 manual follows PAPER $10k.
- Ruling P47: recovery max age 10 min — recovered fills with leader_time older than (recovery time − 10 min) are stored + emitted as `missed` (counted in missed_count, never priced) — a real copier offline would miss them — cost: fewer copies after long outages.
- Ruling P48: run_paper adds hourly mark points (recorded prices) from follow start to now plus a final mark at now, so equity curve/rolling DD include unrealized — needs core simulate support for extra mark times (add optional `mark_times: Iterable[datetime]` param) — cost: small core change.
- Ruling P49: gap = Σ over copied positions of (leader closedPnl on that position × our_qty/leader_qty) − our pnl on that position; uncopied/missed positions excluded; also report leader realized raw separately — apples-to-apples — cost: more computation.
- Ruling P50: daily job runs incremental backfill for followed + top-candidate wallets before rescore.
- Ruling P51: re-follow writes a ladder_history row when resetting to PAPER; alert watermark set at follow start so the first paper run never alerts on pre-existing events.
- Task 9: review dispatched.
- Task 9: review → Needs fixes (CRITICAL price pruning rewrites paper history — reproduced: closed trade vanished after prune; I1 first trade in new coin always missed; I2 reconcile duplicate closes; I3 stale snapshot on re-follow; I4 watcher unsupervised; P47-P50 unmet, P51 partial).
- Ruling P52: settle-once — paper_events store resolved fill_price/missed at first run after obs+31s; never re-priced; hourly marks persisted in `marks` table; raw prices pruned after 2 days — paper history immutable — cost: schema additions.
- Ruling P53: recorder records ALL coins (watch ≤1/s, others ≤1/5s) + on_fill adds coin to watch set — no structural miss on new coins — cost: ~4M rows/day, pruned at 2 days.
- Task 9: fix round 1 dispatched deleg_df97fa68 (FIX_BASE 606b806).
- Task 9: fix round 1 implementer DONE (870130e, 77043db; hl 350, core 70). Sanity serve 90s ok; all-coin recorder 61 rows/s.
- Ruling P54: liquidation is evaluated only at persisted marks (hourly) and events; the unsaved "now" point is display-only — documented README limitation (intra-hour liquidation may be missed) — cost: optimistic on wick liquidations.
- Ruling P55: prune deletes in batches (e.g. 50k rows per statement) — avoid long locks — carried to Task 13 if not done.
- Task 9: scoped re-review dispatched.
- Task 9: re-review FAILED 15:40 (Anthropic 429, quoted reset ~93h); re-dispatched 21:46 as deleg_75f1c87b after owner session resumed.
- Task 9: deleg_75f1c87b lost on Hermes shutdown (no live children on resume 2026-11-21). Scoped re-review re-dispatched over 606b806..77043db. Dispatched as deleg_6035cc81 (report → task-9-rereview-r1.md).
- Ruling P56: Task 13 README gets an Archify architecture diagram (hades-core; HL head: discovery, live, paper runtime, ladder; API :8010; web proxy :3000), skill at C:/Users/phantom/AppData/Local/hermes/skills/creative/archify, generated from repo evidence, HTML under docs/ + linked from README — owner request 2026-11-21 — cost: one extra Task 13 step.
- Ruling P57: token economy — every dispatch tells subagents to run git/pytest through rtk (PATH: $LOCALAPPDATA/Microsoft/WinGet/Packages/rtk-ai.rtk_Microsoft.Winget.Source_8wekyb3d8bbwe/rtk.exe) and to return only the short contract — owner request — cost: rtk filtering could hide a needed detail (fall back to raw command).
- Task 9: fix round 1/5 (12 addressed, 1 new Important open — N1 stale hourly mark re-applied at events causes false liquidation; P55 not done; commits 606b806..77043db). Minors folded into round 2.
- Task 9: fix round 2 dispatched (FIX_BASE 77043db), findings task-9-findings-r2.md. deleg_88dcb385.
- Task 9: fix round 2 implementer DONE_WITH_CONCERNS (4b11892; hl 357, core 70). Concern: hourly mark (latest price ≤15min before hour) may predate a fill made just before the hour during a recorder gap. Scoped re-review dispatched.
- Task 9: fix round 2/5 (all addressed; 77043db..4b11892)
- Task 9: complete (commits e9fceea..4b11892, review clean after 2 fix rounds). Minor deferred: hourly mark can predate a fill observed <30s before the hour during a recorder gap (optional hardening: store mark price time, skip marks older than last observation); prune commits daily job pending work before KV_DAILY (harmless).
- Task 10: dispatched (BASE 4b11892) deleg_4d78990b, report task-10-report.md
- Task 10: implementer DONE_WITH_CONCERNS (2607ef4; hl 391, core 70). Concerns: serve also runs API :8010 (coupled lifetime); gate_config moved to paper_runtime; pytest filter for starlette TestClient deprecation; SSE polls alerts table 1s; last_ingest_at = leader fill time; median_delay_ms computed in API.
- Ruling P58: `hades-hl serve` hosting API + runtime in one process accepted (one command for dev.sh, Task 13) — cost: an API crash stops recording (reviewer to check supervision).
- Ruling P59: SSE via 1s DB poll of alerts accepted — works cross-process — cost: ≤1s latency.
- Task 10: review dispatched.
- Task 10: review deleg_586c3efd interrupted (model response hung 2h17m, no report); re-dispatched.
- Task 10: complete (commits 4b11892..2607ef4, review clean). Minor deferred: serve crash path untested (port busy → runtime cancelled, not stopped); last_ingest_at = leader fill time + full fills scan per health call; median_delay_ms set ≠ missed_count set (include pre-follow/unsettled); global pytest filter for starlette TestClient warning (scope to test_api.py); unknown follow origin → "manual".
- Task 11: dispatched (BASE 2607ef4) deleg_6e356470, report task-11-report.md (P5 carried: formatWinrate in format.ts, Solana WalletTable untouched)
- Task 11: implementer DONE_WITH_CONCERNS (1d4fa50; vitest 215/215, lint clean, build ok, next start offline smoke ok).
- Ruling P60: headFetch adds a third result `{ok:false, offline:false, status}` for upstream HTTP errors (404/409 must not read as offline); body still never forwarded — Task 12 fan-in uses `offline` to label — cost: contract wider than brief.
- Task 11: review dispatched.
- Task 11: review written (task-11-review.md) before reviewer hung on model response; verdict ✅ Approved.
- Task 11: complete (commits 2607ef4..1d4fa50, review clean). Minor deferred: unset HL_API_KEY sends empty header → shows HTTP 503 (short-circuit "not configured"); 2xx key-echo reported as offline; follow-cap text hardcodes 25; formatWinrate doc says n positive but accepts 0; windowLabel "Last 0d"; FollowButton optimistic/rollback + wallet page 404/offline untested; Nav "Judgement"→"Solana" + order (Task 12).
- Task 12: dispatched (BASE 1d4fa50) deleg_1a54290b, report task-12-report.md (P6, P60, nav rename carried)
- Task 12: implementer DONE_WITH_CONCERNS (f46c2e8; vitest 248/248, lint clean, build ok). Concerns: LiveFeed backfills via new /api/heads/[head]/alerts, old /api/alerts left unused; per-head reconnect gap not backfilled; all-heads-down → STALE badge after 60s; Solana alerts without block_time sort last; streamProxy.ts replaced by streamMerge.ts (security tests ported); tsc error at lib/headProxy.test.ts:49 (Task 11 test code).
- Task 12: review dispatched.
- Task 12: review → Needs fixes (I1 stalled head blocks whole Cerberus fan-in, streamMerge.ts:179; ⚠️ HL alert field names unverified; minors: idle watchdog, lost security assertions, reconnect backfill). Fix round 1 dispatched deleg_9b4c65b3 (FIX_BASE f46c2e8), findings task-12-findings-r1.md.
- Task 12: minor (deferred): Solana alerts without block_time sort last (dropped first at 200 cap); /api/alerts + lib/alertsProxy unused (delete); Nav.test.tsx no RED.
- Task 12: fix round 1 implementer DONE (d7fc825; vitest 257/257, tsc clean, lint clean, build ok). V1 no mismatch (FeedTable fixture test added). Minor deferred: arena "Closed / 30" hardcodes 30; LiveFeed M3 wiring untested (no DOM env). Scoped re-review dispatched.
- Task 12: fix round 1/5 (all addressed; f46c2e8..d7fc825)
- Task 12: complete (commits 1d4fa50..d7fc825, review clean after 1 fix round). Minor deferred: head offline at SSR then first up:true → no backfill until reload (live.ts:175, LiveFeed.tsx:132); FeedTable fixture lacks HL close alert.
- Task 13: dispatched (BASE d7fc825) deleg_9e8bb07d, report task-13-report.md (P56 Archify, README limitations P13/P14/P17/P39/P47/P54 carried; P55 already done in T9 r2)
- Ruling P61 (owner approved 2026-11-22): implementer and fix-round work goes to Devin CLI (Fable 5.2 low/medium, `-p --prompt-file`, skill devin-cli); reviews stay with Hermes subagents (independent model); ledger/rulings stay with controller — saves Anthropic tokens — cost: Devin credits; same-family model reviewing.
- Task 13: implementer DONE_WITH_CONCERNS (7c9fc19, 74201aa; core 70, hl 391, engine 242, web vitest 257, lint/build ok; Archify finalize pass). Smoke: discover 20 accepted, backfill 64,748 fills, rescore 0 scored (14 n<30, 6 bot) → follow-sync 0; serve 10 min 28,963 prices, 0 fills, 0 paper events; extra 10 min with 5 manual follows: 0 fills (leaders idle, confirmed); web /hyperliquid /arena 200. Concerns: discover accepts unscoreable wallets; dev.sh Ctrl-C leaves children on Windows; diagram source links 404 until push.
- Ruling P62: discovery probe also rejects candidates whose probe window shows < 30 closed trades in the last 30 days (same n rule as rescore), iterating down the ranking until `limit` scoreable candidates — otherwise auto-follow is empty by construction — cost: more probe calls.
- Task 13: review dispatched.
- Task 13: review ✅ Approved (task-13-review.md). Minors: README:123 margin cap excludes funding; uv.lock not gitignored; dev.sh Windows non-interactive Ctrl-C child leak; README:12 raw HTML link. Smoke: copy path on real fills unproven.
- Task 13: fix round 1 dispatched to Devin CLI (Fable 5.2 medium, dangerous mode, proc_3b318e94cd5b, FIX_BASE 74201aa): P62 + dev.sh cleanup + minors; brief task-13-findings-r1.md, log task-13-devin-r1.log.
- Task 13: Devin fix round 1 committed 6f07daf; scoped re-review dispatched while Devin runs the optional smoke (no code changes expected).
- Task 13: fix round 1/5 (all addressed; 74201aa..6f07daf; Devin CLI implementer)
- Task 13: complete (commits d7fc825..6f07daf, review clean after 1 fix round). Minor deferred: dev.sh live timeout -s INT check undocumented (README:140 claim); dev.sh exits 0 after Ctrl-C; stricter probe may exhaust budget at large --limit; copy path on real fills unproven by smoke.
- Ruling P63: final whole-branch review (71a326f..6f07daf, ~20k lines excl. generated) split into two parallel read-only area reviews (engine: core+hyperliquid+CI; web+ops: web, dev.sh, READMEs) on Devin CLI Fable 5.2 high (most capable available; independent of the Hermes implementers) — diff too large for one pass — cost: cross-area issues only caught via the contract checks both carry.
- Final review dispatched: proc_0f6ae557978a (engine → final-review-engine.md), proc_4845627a3939 (web → final-review-web.md).
- Ruling P64 (owner acc 2026-11-22): Task 14 scope addition — HL wallet page "Open positions" (GET /api/wallets/{address}/positions from HL clearinghouseState, 5s server cache; web section polled 5s with LIVE badge; polling not WS because WS user-subs (10/IP) are reserved for follows). Starts after the final review returns; implementer Devin, reviewer Hermes.
- Product discussion with Devin (Fable 5.2 medium, read-only) → product-memo-devin.md.
- Final review (web+ops) done: With fixes — C0/I4/M15 (final-review-web.md). Important: paper page lacks fills/open positions; unfollow one-click irreversible w/o confirm; README claims COPY owner action that does not exist; gap + paper link missing on HL wallet pages. Caveat: reviewer git diff calls rejected (accept-edits mode) → reviewed working tree at HEAD. T4 M7 DB-path anchoring upgraded to must-fix (doc line). Waiting on engine review before ONE fix wave.
- Final review (engine) done: With fixes — C1/I4/M12 (final-review-engine.md). C1: P47 staleness skipped for polled follows (11th+) → old fills copied at today price, settled permanently. I: ladder NOTIF↔PAPER flapping; paper_events.source_ref collides across wallets; now-mark can liquidate (vs P54); reconcile mass-closes on empty clearinghouseState. Reviewer shell calls rejected → working-tree review (tree = 6f07daf, clean).
- Task 13: Devin (still running smoke) added b65c22b perf(hl) probe recount after the re-review — include in the fix-wave re-review range (6f07daf..). Fix wave + Task 14 QUEUED until the Task 13 Devin process (proc_3b318e94cd5b) exits — never two Devin writers on one checkout. Prompt final-fix-wave-prompt.md.
- Task 13 Devin finished: hl 398, core 70; dev.sh cleanup verified live (INT and TERM, ports free). Smoke2: 200 probed, 19 accepted (all via undecided branch), rescore 0 scored (n<30 18, bot 1), follows 0. No top-200 candidate has ≥30 flat-to-flat closed positions in 30d (max 12). OWNER DECISION PENDING on HL n threshold / ranking.
- Final fix wave + Task 14 launched on Devin (FIX_BASE b65c22b).
- Final fix wave DONE_WITH_CONCERNS (Devin): 291bf29 fix(hl) engine wave, 7654d78 fix(web) web+docs wave, 060b0ee docs live smoke, cae295e feat(hl) Task 14 open positions. Suites: core 77, hl 464, web lint ok, vitest 308, build ok. Live smoke 25 min: 4,809 real fills, copy path works end to end.
- CRITICAL found in smoke: copy sized on leader first fill; order filling in pieces multiplies size (0.0002 BTC first fill → $1.7M copy, equity −40% in 6 min).
- Ruling P65: aggregate one leader order (same oid; fallback coin+dir same ms; live buffer ≤2 s) into one event before sizing; open margin 5% equity × min(lev,10); scale by ratio fixed at open; caps: per-position margin ≤ 20% equity, total committed margin ≤ equity; excess counted missed reason `cap`; core enforces caps, HL aggregates — global constraint says 5% per new position, the piece bug violated it — cost if wrong: caps may under-copy aggressive scalers.
- Ruling P66: ladder demotion cool-down 24 h (implementer reading) accepted — prevents NOTIF↔PAPER flapping — cost: a broken wallet stays NOTIF ≤24 h longer.
- Ruling P67: gates (full and rolling) ignore the unsaved live point (consistent with P54) — cost: none.
- Ruling P68: paper_events re-key migration is one-way; owner must back up hades_hl.db before upgrading (README note) — cost: no downgrade path.
- P65 fix dispatched to Devin (p65-sizing-prompt.md). Then ONE scoped re-review over 6f07daf..HEAD.
- P65 DONE (3d7c0ab; core 86, hl 491). Live 9 min: 4,170 legs → 3,755 events; max open copy $5,010.50 (= cap); max position $16,985 (< ~$20k cap). 202/3,377 orders still split (160 >2 s, 42 interleaved).
- Ruling P69: aggregation keyed per (address, oid); a late piece of an order that opened our copy only updates ratio r (no size growth, not a trade); late pieces of increase/reduce scale by r under P65 caps — cost: small extra event kind. Dispatched to Devin (p69-prompt.md).
- Ruling P70: caps are checked only when margin is added; committed margin may exceed caps after equity falls (no forced reduce) — mirrors a real copier — cost: slightly higher paper risk after drawdowns.
- Ruling P71: pre-P65 per-fill events stay stored; the next run re-sizes them under the new rules (sizes are derived, settled prices stay immutable per P52) — only smoke DBs predate the fix — cost: smoke DB histories change once.
- P69 Devin run exited 1 (Cognition connection error, retryable) with uncommitted core changes; resumed with `devin -c` (p69-resume.log) at 11:03.
- Owner (2026-11-22): "atur aja" → sequencing C and HL discovery option (b) approved (rank profitable non-bot wallets by closed-trade count, keep n≥30).
- P69 DONE (9ba8641; core 91, hl 508). Ruling P72: an opposite-direction order (or any order while an opening order fills) still ends a buffered order (11/3,440 orders in replay) — merging across would risk closing our copy wrongly — cost: rare split copies, still under P65 caps. Minor deferred: adjust events render as size-0 fill rows in web; snapshot events count includes adjusts; no live run for P69.
- Final fix-wave scoped re-review dispatched over 6f07daf..9ba8641.
- Network outage ~11:15 (DNS getaddrinfo failures; HL WS dropped; web re-review subagent failed). Recovered. Web re-review re-dispatched deleg_1306add9. Engine re-review deleg_c610eaf5 task 1 still running. Local web restarted (dev.sh, view DB) — Task 14 positions endpoint verified live.
- Final re-review (web) done: all web findings + Task 14 web ADDRESSED (66/66 touched web tests). New Important: P68 backup note missing from README.md (~:107) and hyperliquid/README.md (:36-39). Minors: P69 adjust row unlabeled; paper fetch serialized on wallet page; unfollow text "never auto-followed again" should say "until re-added by hand". Waiting for engine re-review to batch the residual fix.
- Final re-review (engine) done: ready to merge; C1, I1-I4, MUST-FIX T4 M5/T13 ADDRESSED; rulings P11,P26/P68,P47,P52,P54/P67,P64,P65,P66,P69-P72 OK; money check OK (1.7002 BTC, $500 margin, ~$5k notional). Minors: ~1 s look-ahead on aggregated orders; cli watch median double-count; on_fill delivery order not seq-ordered (no consumer depends).
- Ruling P73: aggregated order price window = [emit_time+1s, emit_time+30s], emit_time = buffer flush (when a real copier could know the order complete); observed_time = emit_time — honesty, no look-ahead — cost: ~2 s later copy price.
- Ruling P74: positions route 404 for wallets unknown to the head — accepted.
- Residuals dispatched to Devin (residuals-prompt.md): P73, P68 README note, unfollow text, adjust row label, cli watch dedupe.
- Residuals DONE (b8f1fb7; core 91, hl 519, web lint ok, vitest 309). Minor deferred to UX slice 1: paper page "Median delay" = feed delay of last fill, understates copy lag by ~3 s (caption). Pre-P73 rows keep old observed_time (P52). Residual scoped re-review dispatched.
