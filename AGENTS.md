# HADES — agent rules

- Solana memecoin smart-money & insider engine. Spec: docs/superpowers/specs/2026-11-19-hades-design.md; plan: docs/superpowers/plans/2026-11-19-hades-phase1.md.
- Python: engine/.venv/Scripts/python.exe (3.12), tests: `cd engine && .venv/Scripts/python.exe -m pytest -q`.
- Web: web/ (Next.js 15, Tailwind v4), tests: `npm test`, build: `npm run build`.
- Secrets live only in `.env` (gitignored). Never put real keys in `.env.example`, fixtures, logs, or commits.
- No private keys / signing / auto-buy anywhere in the codebase.
- Every PnL number and insider tag must be traceable to transaction signatures.
- Commit messages: conventional commits (feat/fix/chore/docs/test).
