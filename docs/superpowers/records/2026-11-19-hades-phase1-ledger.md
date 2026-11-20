# SDD ledger — plan: docs/superpowers/plans/2026-11-19-hades-phase1.md

Spec: docs/superpowers/specs/2026-11-19-hades-design.md
Branch: feat/phase1 (from main 28f798a; ledger-ignore commit 8211ffe)
Global constraints for subagents: .superpowers/sdd/2026-11-19-hades-phase1/global-constraints.md

## Pre-flight scan

| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T1→T2..T9 | Settings fields, ORM models, session fixture | consistent (webhook fields present) |
| T2→T6,T7,T9 | HeliusClient methods, parse_swaps, ParsedSwap | consistent; T9 adds upsert_webhook to helius.py (declared Modify) |
| T3→T6 | DexScreener, is_runner, best_pair, price_native | consistent |
| T4→T5 | compute_positions, unrealized_pnl, Position fields | consistent (first_buy_time/last_sell_time used for hold) |
| T5→T6 | wallet_stats, score_wallets, persist_stats | consistent |
| T6↔T7 | T7 modifies pipeline.py (tag_token after early_buyers, tag_repeat_early in rescore) | consistent |
| T7→T8 | WalletTag rows for WalletRow.tags / TagRow | consistent |
| T8↔T9 | T9 modifies api/main.py; watchlist routes in T8 call sync_webhook added in T9 | ruling R3 |
| T8→T10..T12 | API shapes | consistent |
| T10→T11,T12 | apiGet, format utils, components | consistent |
| each task self-consistency | tests vs code | T5 frequency test 4600/30=153.3>150 ok; T4 oversell values ok (proceeds on 100 of 150 = 2.0) |

Rulings:
- R1: Fee handling in parse_swaps — wallet SOL net = nativeBalanceChange(feePayer) + tx.fee (fee added back) plus WSOL token net. — spec says "excluding fee" — if wrong, PnL off by ~0.000005 SOL/tx.
- R2: HADES_API_KEY empty → API returns 503 on /api/* (fail closed), not open. — security default — cost: none.
- R3: T8 watchlist routes are plain CRUD; T9 adds the sync_webhook hook call. — keeps T8 independent — cost: none.
- R4: Python env = `uv venv --python 3.12 engine/.venv` (default python is 3.14). — spec says 3.12 — cost: none.
- R5: Manual smoke steps needing HELIUS_API_KEY (T6 s3, T9 s3, T13 s2) deferred until owner adds key to .env; logged as PENDING, not blocking. — owner has not supplied key yet.
- R6: delegate_task has no per-call model param; all subagents inherit session model. — tool limitation — cost: higher token spend.

## Progress
- Task 1: dispatched (BASE 8211ffe, subagent sa-0-bd2f6f8f, deleg_9679b23d)
- Incident: owner put real HELIUS_API_KEY in .env.example; implementer committed it (f5338ce, never pushed). Controller moved key to .env (gitignored, + generated HADES_API_KEY & webhook secret), restored placeholder, amended → 15cb48e, purged reflog/gc. Task 1 HEAD = 15cb48e.
- Task 1: implementer DONE_WITH_CONCERNS (UTCDateTime type; no unique key on alerts → carry to T9). Review dispatched deleg_b9a47bd3.
- Carry to T9: alerts table lacks uniqueness; T9 must dedupe (unique on signature+wallet+mint or check before insert).
- Task 1: complete (commits 8211ffe..15cb48e, review clean; minors noted: BigInteger for slots before Postgres move [Phase 3], init_db idempotent test weak)
- Task 2: dispatched (BASE 15cb48e)
- Task 2: implementer DONE_WITH_CONCERNS (bc74047). Controller verified on real Helius data: WSOL wrap/unwrap double-counts SOL (~2x) → evidence task-2-controller-evidence.md. Ruling R1 superseded by R7.
- R7: sol_net = native+fee + WSOL_net − native_net_from_own_wsol_accounts — matches 4 real cases — if wrong, PnL skewed on PUMP_AMM.
- Task 2: review dispatched deleg_61674e05
- Task 2: review → Needs fixes (2 Critical: WSOL double count, no wrap fixtures; 2 Important: stable+rent quote, pagination partial/continue-cursor). Fix round 1 dispatched deleg_0fb5d67f (fresh implementer; original finished). FIX_BASE bc74047.
- Task 2: minor (deferred): non-JSON 200 → JSONDecodeError; first_incoming_sol default max_pages=5; multi-leg tx full sol_amount per leg; global redactor side effect.
- Controller commit 7ac011c docs: AGENTS.md (home AGENTS.md moved to ~/phantom_agent_backup.py per owner). Task 2 fix rounds base on 7ac011c; review range for T2 fix = bc74047..HEAD excluding 7ac011c docs-only.
- Task 2: fix round 1/5 implementer DONE (d8cc16a, 41 tests). Scoped re-review dispatched deleg_26d7fd61 (incl. real-API check of continue-search regex).
- Task 2: fix round 1/5 (4 addressed, 0 open; commits bc74047..d8cc16a). Re-review: continue-search regex verified on real 404 body.
- Task 2: complete (commits 15cb48e..d8cc16a, review clean after 1 fix round)
- Task 2: minor (deferred): stable leg + large native SOL & no WSOL → None (rare); test wording 'before' invented.
- Carry to T6: address_transactions returns silently when max_pages exhausted (no resume cursor) — backfill should record truncation (e.g., log/flag) not assume full history.
- Task 3: dispatched (BASE d8cc16a)
- Task 3: implementer DONE (cd035ff, 62 tests). Carry to T6: is_runner on best_pair of base-token pairs. Review dispatched.
- Task 3: complete (commits d8cc16a..cd035ff, review clean)
- Task 3: minor (deferred): token_pairs no dedupe; Retry-After ignored; non-Transport httpx errors escape; inf accepted; future pairCreatedAt; test client unclosed; TokenBucket coupling.
- Task 4: dispatched (BASE cd035ff)
- Task 4: implementer DONE (7634ae5, 75 tests). Review dispatched.
- Task 4: complete (commits cd035ff..7634ae5, review clean; real-data hand check reconciled)
- Task 4: minor (deferred): reopen <1% of old peak shows closed; ValueError on unknown side aborts wallet; absolute dust threshold; 0-token buy leaves cost; no wallet assertion; test gaps (oversell after partial w/ multiple buys).
- R8 (T5): window = filter swaps to [now-window_days, now] BEFORE compute_positions (pre-window buys → untracked, conservative). Weekly consistency attributes a position's realized to week of last_sell_time.
- Task 5: dispatched (BASE 7634ae5)
- Task 5: implementer DONE (4ff1c9c, 89 tests). Review dispatched.
- Task 5: complete (commits 7634ae5..4ff1c9c, review clean)
- Task 5: minor (deferred): id(s)-keyed scores / duplicate addresses; sell at exactly now outside weeks; window_days=0 div0; tokens_traded counts boundary sell-only; no exact 4500 edge test.
- R9 (T6): deployer = feePayer of oldest tx on MINT address; pool_created_slot/at = oldest tx on POOL (best base-token pair pairAddress). is_runner judged on best_pair of base-token pairs.
- R10 (T6): backfill on HeliusError saves e.partial swaps, does NOT set last_backfilled_at, re-raises; CLI loops catch+log per wallet and continue. Truncation (len == max_pages*100) logged as warning.
- R11 (T6): live smoke limited: EARLY_BUYERS_N=20, HELIUS_MAX_PAGES_PER_ADDRESS=3, SIG_SCAN_MAX_PAGES=10 via env override, 1 token — protects free-tier credits.
- Task 6: dispatched (BASE 4ff1c9c)
- Task 6: first implementer INTERRUPTED (model stall 1h50m); left uncommitted pipeline.py/cli.py/tests (109 passing). Fresh implementer dispatched to audit+finish+smoke: deleg_68b7c67e.
- Task 6: finisher DONE (a8873f7, 113 tests; smoke OK: 20 buyers, 3166 swaps, 50 scored). Implementer also changed parser (multi-leg → None) and price_native (WSOL-quoted only).
- Controller real-data gate: 483 rent-only "buys" → phantom unrealized +434 SOL. Rulings R12 (buy <0.0025 SOL → sol_amount None) & R13 (price only from WSOL pair liq ≥ $10k). Evidence task-6-controller-evidence.md. Review dispatched.
- Task 6: review → Needs fixes (2 Critical R12/R13 missing → phantom +434 SOL confirmed; Important: stale smoke DB, sol direction (R14), early_buyers saves non-buyers (R15); cli generic catch). Fix round 1 dispatched deleg_ae1b36be (FIX_BASE a8873f7).
- R14: buy requires sol_net<0, sell sol_net>0, else sol_amount None. R15: early_buyers saves only selected buyers' swaps.
- Task 6: minor (deferred): duplicate token_pairs in discover; rescore top-5 formatting untested; multi-leg buys excluded from early buyers (comment).
- Task 6: fix round 1 implementer DONE (f9b6835, 123 tests; smoke rebuilt: 0 rent buys, max unrealized 1.77 SOL). Re-review dispatched.
- Task 6: fix round 1/5 (6 addressed, 0 open; commits a8873f7..f9b6835). Smoke rebuilt & verified.
- Task 6: complete (commits 4ff1c9c..f9b6835, review clean after 1 fix round)
- Task 6: minor (deferred): HeliusError CLI branch no rollback (R10 commits before raise, low risk); dropped sells (None sol) leave phantom open positions (mitigated by R13) — revisit Phase 2.
- R16 (T7): hub filter — an address is a HUB if signatures_oldest(addr, max_pages=3) hits cap (>=3000 sigs) → Wallet.label='hub'. Hubs never count as shared funder for deployer_link or bundle (CEX false positives). Trace stops at a hub.
- R17 (T7): first_incoming_sol uses FUNDER_SCAN_MAX_PAGES=3. Tags idempotent: replace existing (address, tag, mint) rows.
- Task 7: dispatched (BASE f9b6835)
- Task 7: first implementer INTERRUPTED (model stall 20m, no files). Ruling R18: split T7 into 7a (insider.py + tests, deleg_558ed45c) and 7b (CLI/rescore wiring + live smoke) to reduce stall exposure.
- Task 7a: implementer DONE (2cd7fbe, 133 tests). Concern: cached first_funder has no sig/slot → deployer_link evidence can be empty across runs (violates anti-goal). Review dispatched with this flagged.
- Task 7a: review → Needs fixes (Critical: funding evidence empty/misaligned on rerun & overwrites good tags; Important: bundle lacks funder sig; self-buy empty evidence). R19: Wallet.funding_signature/funding_slot + init_db migration; persist_tags refuses bad evidence. Fix round 1 dispatched deleg_86275407 (FIX_BASE 2cd7fbe).
- Task 7a: minor (deferred): stale tags never removed (7b decides); negative caches in-process; repeat_early loads all rows; module-global cache.
- Process note: delegation transcript truncates dispatch context (~300 chars) → reviewers can't read dispatch spec from log; write dispatch rulings to a file going forward.
- Task 7a: fix round 1 implementer DONE (bd48b15, 139 tests). Re-review dispatched.
- Task 7a: fix round 1/5 (3 addressed, 0 open; commits 2cd7fbe..bd48b15). Probe: evidence stable across 5 fresh-client reruns incl. partial cache.
- Task 7a: complete (commits f9b6835..bd48b15, review clean after 1 fix round)
- R20a/b, W1-W4 in task-7b-dispatch.md. Task 7b: dispatched deleg_7dc03aed (BASE bd48b15)
- Task 7b: implementer DONE (5c8876b, 147 tests; smoke sniper=9 bundle=0 deployer_link=0 hubs=12, idempotent). Controller verified all 9 sniper tags on-chain (Δ0-2 slots, buys by feePayer). Review dispatched.
- Task 7b: complete (commits bd48b15..5c8876b, review clean)
- Task 7b: minor (deferred): hubs count is global; backfill tag-failure msg lacks recovery hint; tag cmd sentinel; hub count not asserted; non-Helius errors in tag; pre-existing backfill logs e message; negative caches not persisted (perf).
- Task 8: dispatched deleg_5a87bdf0 (BASE 5c8876b), rulings in task-8-dispatch.md (A1-A8)
- Task 8: implementer DONE (4a60d9f, 162 tests; smoke OK). Deviation: lazy module-level app. Review dispatched.
- Task 8: complete (commits 5c8876b..4a60d9f, review clean)
- Task 8: minor (deferred): malformed JSON 422 before auth; concurrent watchlist POST race; empty cluster lumped; missing-key test single route; datetime suffix untested. (docs public / note max_length / getattr error → folded into T9 B8)
- Task 9: dispatched deleg_c79ec9b1 (BASE 4a60d9f), rulings B1-B8 in task-9-dispatch.md. Live Helius webhook creation deferred to T13 with owner (external side effect).
- Task 9: implementer DONE (6ebcf06, 191 tests; local smoke OK). EventSource-header concern already covered by T12 server SSE proxy. Review dispatched.
- Task 9: complete (commits 4a60d9f..6ebcf06, review clean; race check 30 rounds OK)
- Task 9: minor (deferred, MUST address in T13 before real webhook): create retry may duplicate Helius webhook (use max_retries=0 on POST / lookup by URL); empty address list on last removal. Others: source overwrite on merge; IntegrityError branch untested; ping only when idle; migration dedupe unlogged.
- Task 10: dispatched deleg_82b96d8c (BASE 6ebcf06), rulings C1-C11 in task-10-dispatch.md. C10: GET /api/alerts added in T12. Controller does visual screenshots.
- Task 10: implementer DONE (1d40149, 22 vitest, build ok). Controller visual check at 1440 & 375: matches Hermes-style direction, no horizontal page scroll on mobile. Screenshots in scratch/hades_overview_*. Review dispatched.
- Task 10: review → Needs fixes (Important: focus ring 2.94:1 fails 1.4.11; loss text 4.41:1 on surface/3.84 hover). Rulings C12 (two-tone ring), C13 (transparent table + contrast helper tests). Fix round 1 dispatched deleg_1aedca4d (FIX_BASE 1d40149).
- Task 10: minor (deferred): link underline weak; page error-branch untested; formatSol near 0.01; npm audit 1 high/1 moderate (template) — track.
- Task 10: fix round 1 implementer DONE (f10a7fb, 32 vitest). Ring: outline fg + inner primary shadow (C12 adapted: outline paints over shadow). Controller re-screenshot OK. Re-review dispatched.
- Task 10: fix round 1/5 (5 addressed, 0 open; commits 1d40149..f10a7fb). Contrast math independently verified.
- Task 10: complete (commits 6ebcf06..f10a7fb, review clean after 1 fix round)
- Task 11: dispatched deleg_2dbd1177 (BASE f10a7fb), rulings D1-D6 in task-11-dispatch.md (incl. CSRF Origin guard on watchlist proxy).
- Task 11: implementer DONE (29f2273, 71 vitest, build ok, smoke incl. CSRF 403s). Controller screenshots (preview worktree :3100) OK; mobile no h-scroll. Review dispatched.
- Owner supplied hero art "Hades, Lord of the Many Threads" (1254², bg ≈ #0f1120) → scratch/hades-hero.webp (706KB). Plan addition R21: Task 12b = hero art on Overview + animated SVG threads overlay + Styx Map flow graph (d3 curves) on /token and /wallet. Image gen via 9Router blocked (upstream ToS 403).
- Task 11: complete (commits f10a7fb..29f2273, review clean)
- Task 11: minors folded into T12 (E8: chart figure/logo margin, aria-current, role=region, mobile nav, Address link name). Deferred: chart rebuild on refresh; FundingChain last-hop wording; TxLink label-in-name; zero PnL minus icon; HADES_PUBLIC_ORIGIN (future: exact allow-list, never X-Forwarded-Host).
- Task 12: dispatched deleg_ba58da97 (BASE 29f2273), rulings E1-E8 in task-12-dispatch.md. Ports 3200/8200 (3000/3100/8000/8100 occupied).
- Task 12: implementer DONE (301807f, web 102, engine 199; SSE through web proxy verified). Controller screenshots: token/live/watchlist/chart/mobile nav (wraps, no h-scroll). Chart TV logo still near first point (minor). Review dispatched.
- Task 12: review → Needs fixes (Important: chart logo gutter undersized & lost on resize). E9 ruling + minors (paused counter 200+, reconnect backfill via alerts proxy). Fix round 1 dispatched deleg_6cbb7fcb (FIX_BASE 301807f). Extras (sr-only count, reconnect backoff, Tx column) judged justified.
- Task 12: minor (deferred): Play icon while paused; redundant aria-labelledby; watchlist caption order coupling.
- Task 12: fix round 1 implementer DONE (f4b486e, 120 vitest). Controller re-screenshot 1440/375: line clears logo. Re-review dispatched.
- Task 12: fix round 1/5 (3 addressed, 0 open; commits 301807f..f4b486e)
- Task 12: complete (commits 29f2273..f4b486e, review clean after 1 fix round)
- Task 12: minor (deferred): backfilled rows while paused not counted; duplicate SSE increments pausedCount.
- Task 12b: dispatched (BASE f4b486e), rulings G1-G5 in task-12b-dispatch.md
- Task 12b: implementer DONE (2f0a9c9, web 144, engine 203). Controller visual: hero & Styx map OK; BUG token page 375px h-scroll (sr-only absolute escapes scroller) → evidence task-12b-controller-evidence.md. Review dispatched.
- Task 12b: review → Needs fixes (Important: mobile h-scroll from sr-only escaping non-relative scrollers [6 places]; Styx labels ~7px <1100px). Rulings G6 (shared ScrollRegion relative), G7 (styx min-width 1100). Fix round 1 dispatched deleg_847bef77 (FIX_BASE 2f0a9c9).
- Task 12b: minor (deferred): probe layout twice; hit-rect overlap at 60; pointermove offscreen. Deviations a,b,c,e accepted; d rejected.
- Task 12b: fix round 1 implementer DONE (8eda25a, 152 vitest). Controller: 6 routes @375 sw==cw==375; styx labels readable @768 with in-region scroll. Re-review dispatched.
- Task 12b: fix round 1/5 (6 addressed, 0 open; commits 2f0a9c9..8eda25a). Controller verified 6 routes @375.
- Task 12b: complete (commits f4b486e..8eda25a, review clean after 1 fix round). Nit: MAX_THREADS counts lines.
- Task 13: dispatched deleg_9e1c64ee (BASE 8eda25a), rulings H1-H8 in task-13-dispatch.md. Real Helius webhook creation reserved for controller+owner after T13.
- Task 13: implementer DONE_WITH_CONCERNS (a1e7029, 362969c; engine 216, web 152; E2E runner OGTA live: 15 buyers, 1239 swaps, 0 scored under caps, 0 tags). Controller: .env PUBLIC_WEBHOOK_URL was placeholder → blanked (no webhook created; preview watchlist empty, no kv id). Preview engine restarted. Review dispatched.
- Task 13: complete (commits 8eda25a..362969c, review clean)
- Task 13: minor (deferred): CLI prints stale pinned id; dead pinned id costs extra calls; empty body → None in _rpc; 429 on create not retried; exact URL compare; dev.sh macOS claim (bash 3.2).
- ALL 13 TASKS (+12b) COMPLETE. Final whole-branch review next.
- Final whole-branch review dispatched deleg_b2a6d39e (28f798a..362969c, 24 commits).
- Final review: With fixes (C1 truncated anchors; I1 real-buy filter; I2 window labels; I3 top-score webhook sync; I4 funder truncation; I5 localhost bind + origin allow-list; I6 unpriced cost exposed, scoring unchanged). Ruling: I6 scoring change deferred; I3 cap WEBHOOK_TOP_SCORE_MAX=25. Fix wave dispatched deleg_4b725259 (FIX_BASE 362969c).
- Phase 2 (three heads) spec + plan 2a APPROVED by owner 2026-11-20 (drafts in .superpowers/drafts/). Commit to docs/ after Phase 1 merge; execute subagent-driven. Owner rulings: PM gate 56d/20/25%; auto-follow top10 + manual, cap 25.
- Final fix wave subagent FAILED at 14:01 (Anthropic 429, resets ~17:39) after ~35min; WIP uncommitted in tree (36 files) — backed up to final-fix-wip.patch + untracked tar. Resume after reset.
- Final fix wave DONE: 0de2b5d, 2fa8422, 9d0ef67 (engine 242, web 174). OGTA E2E: history_truncated=True, anchors null, 0 sniper/deployer_link (skipped correctly). Scoped re-review dispatched.
