# HADES v2 — agent rules

- Early signals from public data (repo diffs, Certificate Transparency, CEX ground truth) + on-chain liquidity check. Spec: docs/superpowers/specs/2026-11-27-hades-early-signals-design.md; plan: docs/superpowers/plans/2026-11-27-hades-v2-early-signals.md.
- v1 (Solana smart-money, Hyperliquid) lives only at git tag `hades-solana-final`.
- Python: engine/.venv/Scripts/python.exe (3.12), tests: `cd engine && .venv/Scripts/python.exe -m pytest -q`.
- Web: web/ (Next.js 15, Tailwind v4), tests: `npm test`, build: `npm run build`.
- Secrets live only in `.env` (gitignored). Never put real keys in `.env.example`, fixtures, logs, or commits.
- No private keys / signing / auto-buy anywhere in the codebase.
- Every alert must carry an evidence URL (commit, PR, or crt.sh certificate).
- Only documented public endpoints; respect rate limits. Tests never hit the live network.
- Commit messages: conventional commits (feat/fix/chore/docs/test).
