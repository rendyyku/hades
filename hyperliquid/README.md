# hades-hl: the Hyperliquid head

Read-only Hyperliquid wallet discovery, backfill and scoring for HADES. No keys, no
signing: only the public info API plus two unofficial app feeds (leaderboard, vault
list).

```
hades-hl init-db
hades-hl discover --limit 200
hades-hl backfill --all --days 30      # or --address 0x...
hades-hl rescore
hades-hl tag --all                    # insider tags (pre_listing, pre_move, fresh_whale); or --address
hades-hl follow-sync                  # auto-follow the top 10 non-bot wallets
hades-hl follow-add 0x... [0x...]     # manual follows (cap 25); follow-remove, follows
hades-hl paper-run --all              # one paper run, prints the summaries
hades-hl serve                        # API on 127.0.0.1:8010 + background runtime (--no-api: runtime only)
```

`HL_DATABASE_URL` selects the SQLite database (default `sqlite:///hades_hl.db`). The
default is relative to the working directory: run every command and `serve` from the
same directory (`scripts/dev.sh` uses the repo root), or set an absolute path, or the
CLI and the running head use two different databases.

## Operational notes

- **`backfill --all` exits 0 even when some wallets failed** (rate limited or other HL
  errors). Each failure is printed on its own `[i/N] <address>: ...` line and counted in
  the final `backfill done: X ok, Y failed` summary; check the per-wallet log. A
  rate-limited wallet keeps the pages it already received; its coverage is not
  advanced, so the next run fetches the same segment again (duplicates are ignored).
  `backfill --address` exits 1 on failure.
- **Liquidation fills migrated from pre-Task-6 databases lack `liquidated_user`.** The
  column was added later; old rows have it NULL, and a liquidation without a known
  victim is treated as this wallet's own liquidation. Re-backfilling the wallet (on a
  fresh DB, or after deleting its fills and `hl:cov:<address>`) restores it.
- Fills are keyed by `(address, tid)` and paper events by `(address, source_ref)`: HL
  gives both counterparties of a trade the same `tid` and hash. `init-db` (and every
  command) migrates an older `tid`-only / `source_ref`-only table in place, in one
  transaction per table; a failure leaves the old table untouched.
  **Back up `hades_hl.db` before upgrading: the `paper_events` re-key migration is
  one-way.** Nothing converts the re-keyed table back, so the backup is the only way
  to return to an older version: stop `serve` and copy the file (with its `-wal` /
  `-shm` files, if present) first.
- One leader order is one paper event. `fills.oid` (added in place to an older
  database; NULL on fills stored before) says which fills are one order; without it
  the fallback is the same coin and direction in the same millisecond. An event made
  of several fills keeps the first fill's `hash:tid` as `source_ref`, lists all of
  them in its JSON (`tids`, `pieces`) and has one `paper_event_pieces` row per fill,
  so a fill delivered again is never emitted twice. Events stored before this are one
  per fill and stay so. Orders are buffered per (address, order), so interleaved
  orders of one wallet each become one event. A piece that arrives after its order
  was emitted is its own event; when that order opened the position its side is
  `adjust` (settled without a price, `missed` false, no alert): the copy does not
  trade and its ratio follows the leader's new size. An event's `observed_time` is
  the time it was emitted (the buffer flush: when we could know the order was
  complete), and the copy price is the first recorded mid in `[observed_time + 1 s,
  observed_time + 30 s]`; when the last fill reached us is kept in the event JSON
  (`last_piece_observed`), and `delay_ms` is that fill's feed delay.
- Backfill fetches fills newest first and records the contiguous covered range in kv
  `hl:cov:<address>`; stats use only that range. Wallets with more than 10,000 fills
  in the window are capped (`history_partial`), keeping the newest 10,000. If the
  pager stops early (request budget, or a window that brings nothing new) the coverage
  starts where fetching stopped and the history is marked partial. Funding is fetched
  only over the fills coverage.
- Discovery prefilters the leaderboard (account value >= 10k, 0 < month volume <= 30 x
  account value, month PnL > 0, not a vault) before ranking by month PnL. If the vault
  list is unavailable and uncached, `discover` says `vaults checked: no`; vaults are
  flagged on the next successful run and are never scored.
- Each candidate then gets a one-request activity probe (`userFills`, latest <= 2000
  fills), cached 24 h in kv `hl:probe:<address>`. It is rejected if it has no fill in
  30 days, or if fills/day > 300, median hold < 60 s or maker share > 70% (the bot
  rule), or (reason `n<30`) if it shows fewer than 30 closed positions in the last 30
  days: the same count and threshold `rescore` uses, so discovery only keeps wallets
  that can be scored. That last check applies only when the sample covers the 30 days
  (under 2000 fills = the whole history, or an oldest fill >= 30 days back); a full
  2000-fill sample spanning less is undecided and passes. Probing stops at `--limit`
  accepted or 10 x `--limit` probe requests.
- **Open positions (`GET /api/wallets/{address}/positions`).** Live from
  `clearinghouseState` (main perp dex) through the same client and rate weight budget
  as the runtime, cached 5 s per address; concurrent requests share one upstream call
  and a failure is remembered for the same 5 s. 400 on a malformed address, 404 for a
  wallet not in the database, 502 `hyperliquid upstream unavailable` when the call
  fails, times out (8 s) or answers an unexpected body; the upstream body is never
  forwarded. `scripts/record_positions_fixture.py 0x...` re-records the test fixture.
- **Paper runtime (`serve`).** Daily (in the background): incremental backfill of the
  follows and the top 50 candidates, rescore, auto-follow sync, then hourly marks and
  pruning of raw prices older than 2 days. Every 60 s: a paper run for follows with new
  settled events or not run for an hour. An event is settled once, by the first run
  31 s after it was observed: its model price (or `missed`) is stored in
  `paper_events` and never re-priced. Hourly marks are kept in `marks`, so the
  equity curve (marked every hour) does not change when raw prices are pruned. A
  fill more than 10 min old when it is observed (after an outage; recovery and REST
  polling alike) is stored as missed. Every follow with open paper
  positions is checked against `clearinghouseState` at most every 5 min; a copy the
  leader no longer holds gets a synthetic `reconcile:` close, unless an event of that
  coin is still pending. A failed, empty, malformed or partial answer is "unknown" and
  closes nothing; a HIP-3 coin (`dex:COIN`) is checked against that dex's own state. Prices are recorded from `fastAssetCtxs` (falls back to
  `allMids` after 3 failed connections): the watch set (BTC, ETH, coins the follows
  traded in 30 days, open paper positions) at most 1 row/s, every other perp coin at
  most every 5 s. A dead watcher or recorder is restarted. The fill watcher persists its
  cursor per wallet (kv `hl:last_seen:<address>`, `hl:floor:<address>`); after a
  restart, fills made while it was down are recovered and copied with an
  `observed_time` no earlier than the recovery time.
