# Seed verification — 2026-11-27

Checked live against public APIs (unauthenticated GitHub, GeckoTerminal, DexScreener).

## Trust Wallet `blockchains/` folders
- Robinhood Chain → `robinhoodchain` (mapped to chain key `robinhood`). Active: commits 2026-11-08, 11-11, 11-24.
- MegaETH → `megaeth` (last asset commit 2026-01-22).
- Monad → `monad` (last asset commit 2025-11-24).
- HyperEVM → `hyperevm` (last asset commit 2026-11-25).
- Layout confirmed: a new token = one added `blockchains/<chain>/assets/<address>/info.json` (commit 775af41, PR #38879).

## BitGo
- `modules/statics/src/coins/hoodethTokens.ts` = BitGo's Robinhood Chain (HoodETH) token definitions.
- Regex in `config.example.yaml` matches 307/307 mainnet entries; 2 testnet (`thoodeth:`) entries skipped on purpose.
- Names may be single- or double-quoted (`"Kohl's Corporation • Robinhood"`); the regex accepts both.

## Token lists
- `Uniswap/default-token-list` → 404 (moved or removed). No json_tokenlist target is enabled by default.
- No official token-list repo was found for the four priority chains in this pass.

## Network ids
| chain key | GeckoTerminal | DexScreener |
|---|---|---|
| robinhood | robinhood | robinhood |
| megaeth | megaeth | megaeth |
| monad | monad | monad |
| hyperevm | hyperevm | hyperevm |
| ethereum | eth | ethereum |
| base | base | base |
| solana | solana | solana |

## Robinhood ground truth
Skipped: no documented public asset/pair endpoint found.

## Live dry runs
- `hades once` baseline OK for: trustwallet robinhoodchain, BitGo hoodeth, ground_truth binance/coinbase/kraken/okx/bybit, ct.
- Replay from the previous commit:
  - Trust Wallet robinhoodchain → 1 signal (0x64a2…94f4, symbol "FAKE ETH", evidence PR #38879), GeckoTerminal: buyable, $2.6k, 3 pools.
  - BitGo hoodeth → 1 signal (rlusd, evidence PR #10172), GeckoTerminal: not buyable (0 pools).
- Note: registries accept junk/scam tokens (the "FAKE ETH" entry). Scores and evaluation, not the raw feed, decide what is worth attention.
