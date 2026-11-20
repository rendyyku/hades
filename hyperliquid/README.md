# hades-hl: the Hyperliquid head

Read-only Hyperliquid wallet discovery, backfill and scoring for HADES. No keys, no
signing: only the public info API plus two unofficial app feeds (leaderboard, vault
list).

```
hades-hl init-db
hades-hl discover --limit 200
hades-hl backfill --all --days 30      # or --address 0x...
hades-hl rescore
```

`HL_DATABASE_URL` selects the SQLite database (default `sqlite:///hades_hl.db`).

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
- Fills are keyed by `(address, tid)`: HL gives both counterparties of a trade the same
  `tid`. `init-db` (and every command) migrates an older `tid`-only table in place, in
  one transaction.
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
  rule). Probing stops at `--limit` accepted or 5 x `--limit` probe requests.
