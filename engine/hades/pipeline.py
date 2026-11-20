"""Discovery + backfill pipeline: seed tokens, find early buyers, backfill, rescore.

Every function takes its session and clients explicitly so tests can inject
fakes. Clients only need the methods used here (see HeliusClient/DexScreener).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from hades.config import Settings, get_settings
from hades.db import Swap, Token, Wallet, is_real_buy
from hades.ingest.parse import ParsedSwap, parse_swaps
from hades.insider import replace_repeat_early
from hades.pnl import compute_positions
from hades.scoring import persist_stats, score_wallets, wallet_stats
from hades.sources.dexscreener import _num, base_address, best_pair, is_runner, sol_price_native
from hades.sources.helius import PAGE_LIMIT, PARSE_CHUNK, HeliusError, sigs_truncated

log = logging.getLogger(__name__)

BACKFILL_FRESH_FOR = timedelta(hours=6)
WINDOW_DAYS = 30


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_swap(ps: ParsedSwap, source: str = "backfill") -> Swap:
    return Swap(
        signature=ps.signature,
        wallet=ps.wallet,
        mint=ps.mint,
        slot=ps.slot,
        block_time=ps.block_time,
        side=ps.side,
        token_amount=ps.token_amount,
        sol_amount=ps.sol_amount,
        source=source,
        dex=ps.dex,
    )


def _ensure_wallet(session: Session, address: str) -> Wallet:
    w = session.get(Wallet, address)
    if w is None:
        w = Wallet(address=address)
        session.add(w)
    return w


def _parse_all(txs: Iterable[dict]) -> list[ParsedSwap]:
    out: list[ParsedSwap] = []
    for tx in txs:
        try:
            out.extend(parse_swaps(tx))
        except (KeyError, TypeError, ValueError) as e:  # malformed tx: skip, keep going
            log.warning("skipping unparseable tx %s: %s", tx.get("signature"), type(e).__name__)
    return out


class EarlyBuyers(list):
    """list[str] of buyer wallets plus ``truncated`` (the pool scan hit its page cap,
    so these are the earliest *observed* buyers, not the launch buyers)."""

    def __init__(self, items=(), truncated: bool = False) -> None:
        super().__init__(items)
        self.truncated = truncated


async def _oldest_tx(helius: Any, address: str, max_pages: int) -> tuple[dict | None, bool]:
    """(oldest tx seen, truncated). A truncated scan's oldest tx is NOT an anchor."""
    sigs = await helius.signatures_oldest(address, max_pages)
    if not sigs:
        return None, False
    truncated = sigs_truncated(sigs, max_pages)
    if truncated:
        log.warning("signature scan of %s truncated at %d pages; its oldest tx is not an anchor",
                    address, max_pages)
    txs = await helius.parse_transactions([sigs[0]])
    return (txs[0] if txs else None), truncated


# -- seed ----------------------------------------------------------------------


async def seed_token(
    session: Session, helius: Any, dex: Any, mint: str, settings: Settings, now: datetime | None = None
) -> Token:
    """Upsert a Token from DexScreener's best pair + its oldest mint/pool txs.

    A truncated scan (page cap hit, every page full) never writes a fake anchor:
    a truncated mint scan stores ``deployer=NULL``, a truncated pool scan stores
    ``pool_created_slot/at=NULL``, and either sets ``history_truncated``.
    """
    now = _now(now)
    pairs = [p for p in await dex.token_pairs([mint]) if base_address(p) == mint]
    if not pairs:
        raise ValueError(f"no DexScreener pairs with base token {mint}")
    best = best_pair(pairs)
    base = best.get("baseToken") or {}
    pool = best.get("pairAddress")

    tok = session.get(Token, mint)
    if tok is None:
        tok = Token(mint=mint, first_seen_at=now)
        session.add(tok)
    tok.symbol = base.get("symbol") or tok.symbol
    tok.name = base.get("name") or tok.name
    tok.pool_address = pool
    tok.is_runner = is_runner(best, int(now.timestamp() * 1000))
    fdv = _num(best.get("fdv"))
    if fdv is not None:
        tok.peak_mcap_usd = fdv

    max_pages = settings.SIG_SCAN_MAX_PAGES
    create_tx, mint_truncated = await _oldest_tx(helius, mint, max_pages)
    if mint_truncated:
        tok.deployer = None
    elif create_tx and create_tx.get("feePayer"):
        tok.deployer = create_tx["feePayer"]
    pool_truncated = False
    if pool:
        pool_tx, pool_truncated = await _oldest_tx(helius, pool, max_pages)
        if pool_truncated:
            tok.pool_created_slot = None
            tok.pool_created_at = None
        elif pool_tx:
            if pool_tx.get("slot") is not None:
                tok.pool_created_slot = int(pool_tx["slot"])
            if pool_tx.get("timestamp") is not None:
                tok.pool_created_at = datetime.fromtimestamp(int(pool_tx["timestamp"]), timezone.utc)
    tok.history_truncated = mint_truncated or pool_truncated

    session.commit()
    return tok


# -- early buyers --------------------------------------------------------------


async def early_buyers(
    session: Session, helius: Any, token: Token, n: int, *, sig_max_pages: int | None = None
) -> EarlyBuyers:
    """First n unique buy wallets of token's pool, ordered by (slot, signature).

    Parses the pool's signatures oldest-first in chunks of 100 and stops once
    n buyers are found. Only the SOL-quoted swaps of the token made by the
    selected buyers are saved (source='backfill', R15); other wallets seen in
    the pool are not saved, since their histories would be partial.

    Multi-leg buys (one tx buying several tokens) have sol_amount=None and so
    never make a wallet an early buyer (deferred minor 7). A buy is
    :func:`hades.db.is_real_buy` (positive SOL amount).

    The result's ``truncated`` is True when the pool scan hit its page cap; the
    token is then flagged ``history_truncated`` (anchors are left to seed_token).
    """
    if not token.pool_address:
        raise ValueError(f"token {token.mint} has no pool_address; seed it first")
    if sig_max_pages is None:
        sig_max_pages = get_settings().SIG_SCAN_MAX_PAGES
    sigs = await helius.signatures_oldest(token.pool_address, sig_max_pages)
    truncated = bool(sigs) and sigs_truncated(sigs, sig_max_pages)
    if truncated:
        log.warning("pool scan of %s truncated at %d pages; buyers are the earliest observed, "
                    "not the launch buyers", token.pool_address, sig_max_pages)
        token.history_truncated = True

    buyers: list[str] = []
    seen: set[str] = set()
    pool_swaps: list[ParsedSwap] = []
    for i in range(0, len(sigs), PARSE_CHUNK):
        if len(buyers) >= n:
            break
        txs = await helius.parse_transactions(sigs[i : i + PARSE_CHUNK])
        swaps = [s for s in _parse_all(txs) if s.mint == token.mint and s.sol_amount is not None]
        swaps.sort(key=lambda s: (s.slot, s.signature))
        pool_swaps.extend(swaps)
        for s in swaps:
            if is_real_buy(s) and s.wallet not in seen and len(buyers) < n:
                seen.add(s.wallet)
                buyers.append(s.wallet)

    for s in pool_swaps:
        if s.wallet in seen:
            session.merge(_to_swap(s))
    for w in buyers:
        _ensure_wallet(session, w)
    session.commit()
    return EarlyBuyers(buyers, truncated=truncated)


# -- backfill ------------------------------------------------------------------


def _save_wallet_swaps(session: Session, address: str, txs: list[dict]) -> int:
    swaps = [s for s in _parse_all(txs) if s.wallet == address]
    for s in swaps:
        session.merge(_to_swap(s))
    return len(swaps)


async def backfill_wallet(
    session: Session,
    helius: Any,
    address: str,
    settings: Settings,
    force: bool = False,
    now: datetime | None = None,
) -> int:
    """Fetch + save a wallet's SWAP history. Returns swaps saved (0 if skipped)."""
    now = _now(now)
    wallet = session.get(Wallet, address)
    last = _aware(wallet.last_backfilled_at) if wallet else None
    if not force and last is not None and now - last < BACKFILL_FRESH_FOR:
        return 0

    max_pages = settings.HELIUS_MAX_PAGES_PER_ADDRESS
    try:
        raw = await helius.address_transactions(address, tx_type="SWAP", max_pages=max_pages)
    except HeliusError as e:
        saved = _save_wallet_swaps(session, address, e.partial)
        _ensure_wallet(session, address)
        session.commit()
        log.warning("backfill %s failed after %d txs (%d swaps saved): %s",
                    address, len(e.partial), saved, type(e).__name__)
        raise

    if len(raw) == max_pages * PAGE_LIMIT:
        log.warning("history of %s truncated at %d pages (%d txs)", address, max_pages, len(raw))
    saved = _save_wallet_swaps(session, address, raw)
    _ensure_wallet(session, address).last_backfilled_at = now
    session.commit()
    return saved


# -- discover ------------------------------------------------------------------


async def discover(
    session: Session, helius: Any, dex: Any, settings: Settings, now: datetime | None = None,
    limit: int = 0,
) -> list[Token]:
    """Seed boosted tokens that are runners and not yet in the tokens table.

    ``limit`` > 0 caps how many NEW runners are seeded this run (0 = unlimited).
    """
    now = _now(now)
    now_ms = int(now.timestamp() * 1000)
    mints = await dex.boosted_tokens()
    if not mints:
        return []
    by_mint: dict[str, list[dict]] = defaultdict(list)
    for p in await dex.token_pairs(mints):
        b = base_address(p)
        if b:
            by_mint[b].append(p)

    new: list[Token] = []
    for mint in mints:
        if limit > 0 and len(new) >= limit:
            break
        best = best_pair(by_mint.get(mint, []))
        if best is None or not is_runner(best, now_ms):
            continue
        if session.get(Token, mint) is not None:
            continue
        try:
            new.append(await seed_token(session, helius, dex, mint, settings, now=now))
        except (ValueError, HeliusError) as e:
            session.rollback()
            log.warning("seed %s failed: %s", mint, e)
    return new


# -- rescore -------------------------------------------------------------------


async def rescore(session: Session, dex: Any, now: datetime | None = None) -> int:
    """Recompute and persist stats + scores for every wallet with swaps.

    Also recomputes the global ``repeat_early`` tags (stale ones are removed).
    """
    now = _now(now)
    start = now - timedelta(days=WINDOW_DAYS)
    wallets = session.scalars(select(Swap.wallet).distinct().order_by(Swap.wallet)).all()
    swaps_by_wallet: dict[str, list[Swap]] = {}
    open_mints: set[str] = set()
    for w in wallets:
        swaps = list(session.scalars(select(Swap).where(Swap.wallet == w)))
        swaps_by_wallet[w] = swaps
        try:
            positions = compute_positions(s for s in swaps if start <= _aware(s.block_time) <= now)
        except ValueError:
            continue  # reported below by wallet_stats
        open_mints.update(m for m, p in positions.items() if p.remaining_tokens > 0 and not p.closed)

    prices: dict[str, float] = {}
    if open_mints:
        mints = sorted(open_mints)
        pairs = await dex.token_pairs(mints)  # one batched call (30 mints/request inside)
        for m in mints:
            price = sol_price_native(pairs, m)
            if price is not None:
                prices[m] = price

    stats = []
    for w, swaps in swaps_by_wallet.items():
        try:
            stats.append(wallet_stats(w, swaps, prices, now, window_days=WINDOW_DAYS))
        except ValueError as e:
            log.warning("skipping wallet %s: %s", w, e)
    scored = score_wallets(stats)
    persist_stats(session, scored)
    replace_repeat_early(session)
    session.commit()
    return len(scored)
