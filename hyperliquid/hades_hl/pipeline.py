"""Discovery, backfill and rescore for the Hyperliquid head (spec 8.2).

- :func:`discover`: leaderboard candidates (unofficial feed, see :mod:`hades_hl.client`),
  ranked by the leaderboard's 30-day ("month") PnL only to pick *who to backfill*; the
  real ranking is our own recomputed score (:func:`rescore`). The leaderboard (~38 MB)
  is fetched at most once per 24 h: only the parsed candidate list is cached in ``kv``.
  Vaults (from the HL app's vault list, also cached 24 h) are excluded.
- :func:`backfill`: idempotent fills + funding import, resuming from the max stored
  ``time`` (inclusive; the DB de-duplicates). Every page is committed as it arrives, so
  an ``HLRateLimited`` mid-way keeps earlier pages (then re-raises).
- :func:`rescore`: stats over the stored fills/funding (ruling P17: ``oldest_fill_time``
  is the oldest *stored* fill; ``history_partial`` from backfill) and the percentile
  score, persisted on ``wallets``. ``sz_decimals`` comes from ``meta`` (ruling P24),
  cached in ``kv`` for 24 h by :func:`load_sz_decimals`.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hades_hl.client import HLClient, HLError, HLRateLimited, HLUnavailable
from hades_hl.db import Fill, Funding, Kv, Wallet, insert_fills, insert_funding, utcnow
from hades_hl.normalize import sz_decimals_by_coin
from hades_hl.scoring import is_bot, score_detailed
from hades_hl.stats import wallet_stats

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
CACHE_TTL_MS = DAY_MS  # leaderboard, vault list and meta: refreshed at most once per 24 h
CANDIDATE_CACHE_MAX = 5000  # cached candidates (sorted by month PnL); bounds the kv row
KV_LEADERBOARD = "hl:leaderboard_candidates"
KV_VAULTS = "hl:vault_addresses"
KV_SZ_DECIMALS = "hl:sz_decimals"
LB_WINDOW = "month"  # the leaderboard's 30-day window

# stats_json keys that are metadata, not stats: kept across rescore
META_KEYS = ("backfill", "leaderboard")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


# -- kv cache -------------------------------------------------------------------------

def kv_get_json(session: Session, key: str) -> Any:
    row = session.get(Kv, key)
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except ValueError:
        log.warning("kv %s: unparseable JSON, ignoring", key)
        return None


def kv_set_json(session: Session, key: str, value: Any) -> None:
    raw = json.dumps(value, separators=(",", ":"))
    row = session.get(Kv, key)
    if row is None:
        session.add(Kv(key=key, value=raw))
    else:
        row.value = raw
    session.flush()


def _fresh(cached: Any, now_ms: int) -> bool:
    return (isinstance(cached, dict) and isinstance(cached.get("fetched_at"), int)
            and 0 <= now_ms - cached["fetched_at"] < CACHE_TTL_MS)


# -- discovery -----------------------------------------------------------------------

class Discovered(list):
    """``list[str]`` of discovered addresses, plus why it may be empty."""

    error: str | None = None


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_candidate(row: dict) -> dict | None:
    """Leaderboard row -> ``{address, account_value, pnl_month, roi_month, vlm_month,
    display_name}`` (None without an address). Unknown windows give None fields."""
    addr = row.get("ethAddress")
    if not addr:
        return None
    perf: dict[str, Any] = {}
    for item in row.get("windowPerformances") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[1], dict):
            perf[str(item[0])] = item[1]
    month = perf.get(LB_WINDOW) or {}
    return {
        "address": str(addr).lower(),
        "account_value": _f(row.get("accountValue")),
        "pnl_month": _f(month.get("pnl")),
        "roi_month": _f(month.get("roi")),
        "vlm_month": _f(month.get("vlm")),
        "display_name": row.get("displayName"),
    }


def rank_candidates(rows: list[dict]) -> list[dict]:
    """Parsed, de-duplicated (first wins after sorting) and sorted by month PnL desc."""
    parsed = [c for c in (parse_candidate(r) for r in rows) if c is not None]
    parsed.sort(key=lambda c: -(c["pnl_month"] if c["pnl_month"] is not None else float("-inf")))
    out: list[dict] = []
    seen: set[str] = set()
    for c in parsed:
        if c["address"] in seen:
            continue
        seen.add(c["address"])
        out.append(c)
    return out


async def leaderboard_candidates(
    session: Session, client: HLClient, now: datetime | None = None
) -> list[dict]:
    """Ranked candidates, from the kv cache when < 24 h old. Raises HLUnavailable when
    the feed is down and nothing (not even a stale cache) is available."""
    now_ms = _ms(_now(now))
    cached = kv_get_json(session, KV_LEADERBOARD)
    if _fresh(cached, now_ms):
        return cached["rows"]
    try:
        rows = await client.leaderboard()
    except HLUnavailable as e:
        if isinstance(cached, dict) and cached.get("rows"):
            log.warning("leaderboard unavailable (%s); using the stale cache from %s",
                        e, cached.get("fetched_at"))
            return cached["rows"]
        raise
    ranked = rank_candidates(rows)
    log.info("leaderboard: %d rows, %d unique candidates", len(rows), len(ranked))
    ranked = ranked[:CANDIDATE_CACHE_MAX]
    kv_set_json(session, KV_LEADERBOARD, {"fetched_at": now_ms, "n_rows": len(rows),
                                          "rows": ranked})
    session.commit()
    return ranked


async def vault_addresses(
    session: Session, client: HLClient, now: datetime | None = None
) -> set[str]:
    """Every vault address, cached 24 h. Unavailable -> stale cache, else empty set."""
    now_ms = _ms(_now(now))
    cached = kv_get_json(session, KV_VAULTS)
    if _fresh(cached, now_ms):
        return set(cached["addresses"])
    try:
        vaults = await client.vault_addresses()
    except HLUnavailable as e:
        stale = set(cached["addresses"]) if isinstance(cached, dict) else set()
        log.warning("vault list unavailable (%s); using %d cached vault addresses",
                    e, len(stale))
        return stale
    kv_set_json(session, KV_VAULTS, {"fetched_at": now_ms, "addresses": sorted(vaults)})
    session.commit()
    return vaults


async def discover(
    session: Session, client: HLClient, limit: int = 200, *, now: datetime | None = None
) -> Discovered:
    """Top ``limit`` non-vault leaderboard candidates by month PnL, upserted into
    ``wallets`` (leaderboard fields under ``stats_json['leaderboard']``, informational
    only). Known wallets that turn out to be vaults get ``is_vault=True``.

    If the leaderboard is unavailable (and uncached) this logs and returns an empty
    result with ``error`` set; the ``trades``-WS fallback is a later task.
    """
    out = Discovered()
    try:
        cands = await leaderboard_candidates(session, client, now)
    except HLUnavailable as e:
        out.error = f"leaderboard unavailable: {e}"
        log.warning("discover: %s; no candidates (WS-trades fallback not implemented yet)", e)
        return out
    vaults = await vault_addresses(session, client, now)
    existing = {w.address: w for w in session.scalars(select(Wallet))}
    skipped_vaults = 0
    for c in cands:
        addr = c["address"]
        if addr in vaults:
            skipped_vaults += 1
            w = existing.get(addr)
            if w is not None and not w.is_vault:
                w.is_vault = True
            continue
        if len(out) >= limit:
            continue  # keep scanning only to mark known vaults
        out.append(addr)
        lb = {k: v for k, v in c.items() if k != "address"}
        w = existing.get(addr)
        if w is None:
            session.add(Wallet(address=addr, stats_json={"leaderboard": lb}))
        else:
            w.stats_json = {**(w.stats_json or {}), "leaderboard": lb}
    session.commit()
    log.info("discover: %d candidates (%d vaults skipped)", len(out), skipped_vaults)
    return out


# -- backfill ------------------------------------------------------------------------

@dataclass
class BackfillResult:
    new_fills: int
    truncated: bool
    new_funding: int = 0
    funding_truncated: bool = False


def _ensure_wallet(session: Session, addr: str) -> Wallet:
    w = session.get(Wallet, addr)
    if w is None:
        w = Wallet(address=addr, stats_json={})
        session.add(w)
        session.flush()
    return w


def _set_backfill_meta(w: Wallet, **kw: Any) -> None:
    sj = dict(w.stats_json or {})
    meta = dict(sj.get("backfill") or {})
    meta.update(kw)
    sj["backfill"] = meta
    w.stats_json = sj  # reassign: JSON columns do not track in-place mutation


async def backfill(
    session: Session, client: HLClient, addr: str, days: int = 90, *,
    now: datetime | None = None,
) -> BackfillResult:
    """Import ``addr``'s fills and funding. Idempotent: resumes from the max stored
    ``time`` (inclusive), else from ``now - days``. Pages are committed as they arrive;
    on ``HLRateLimited`` the progress so far is kept and the error re-raised.

    ``history_partial`` (sticky) is set in ``stats_json['backfill']`` when the fills
    read was truncated (10k-fill cap or same-ms page skip, see ``HLPage``).
    """
    addr = addr.lower()
    now_dt = _now(now)
    now_ms = _ms(now_dt)
    w = _ensure_wallet(session, addr)
    session.commit()
    floor = now_ms - int(days * DAY_MS)
    last_fill = session.scalar(select(func.max(Fill.time)).where(Fill.address == addr))
    last_fund = session.scalar(select(func.max(Funding.time)).where(Funding.address == addr))
    counts = {"fills": 0, "funding": 0}

    def on_fills(page: list[dict]) -> None:
        counts["fills"] += insert_fills(session, addr, page)
        session.commit()

    def on_funding(page: list[dict]) -> None:
        counts["funding"] += insert_funding(session, addr, page)
        session.commit()

    prev_partial = bool(((w.stats_json or {}).get("backfill") or {}).get("history_partial"))
    try:
        fpage = await client.user_fills_by_time(
            addr, last_fill if last_fill is not None else floor, on_page=on_fills)
        _set_backfill_meta(w, history_partial=prev_partial or fpage.truncated)
        session.commit()
        fund = await client.user_funding(
            addr, last_fund if last_fund is not None else floor, on_page=on_funding)
    except HLRateLimited:
        session.rollback()
        _set_backfill_meta(w, last_error="rate_limited", attempted_at=now_dt.isoformat())
        session.commit()
        log.warning("backfill %s: rate limited; kept %d new fills, %d new funding rows",
                    addr, counts["fills"], counts["funding"])
        raise
    _set_backfill_meta(w, history_partial=prev_partial or fpage.truncated,
                       funding_partial=fund.truncated, last_error=None,
                       backfilled_at=now_dt.isoformat(), days=days)
    session.commit()
    return BackfillResult(counts["fills"], fpage.truncated, counts["funding"], fund.truncated)


# -- rescore -------------------------------------------------------------------------

async def load_sz_decimals(
    session: Session, client: HLClient, now: datetime | None = None
) -> dict[str, int]:
    """``{coin: szDecimals}`` from ``meta`` (ruling P24), cached in kv for 24 h.
    On an HL error: the stale cache if any, else ``{}`` (both logged)."""
    now_ms = _ms(_now(now))
    cached = kv_get_json(session, KV_SZ_DECIMALS)
    if _fresh(cached, now_ms):
        return dict(cached["sz_decimals"])
    try:
        szd = sz_decimals_by_coin(await client.meta())
    except HLError as e:
        stale = dict(cached["sz_decimals"]) if isinstance(cached, dict) else {}
        log.warning("meta unavailable (%s); using %d cached szDecimals", type(e).__name__,
                    len(stale))
        return stale
    kv_set_json(session, KV_SZ_DECIMALS, {"fetched_at": now_ms, "sz_decimals": szd})
    session.commit()
    return szd


def fill_dict(r: Fill) -> dict:
    """A stored fill back in HL's wire shape (what normalize/pnl/stats consume)."""
    f: dict[str, Any] = {
        "tid": r.tid, "coin": r.coin, "px": str(r.px), "sz": str(r.sz), "side": r.side,
        "dir": r.dir, "startPosition": str(r.start_position), "closedPnl": str(r.closed_pnl),
        "fee": str(r.fee), "time": r.time, "hash": r.hash, "crossed": r.crossed,
    }
    if r.liquidation:
        f["liquidation"] = {"liquidatedUser": r.liquidated_user}
    if r.builder_fee is not None:
        f["builderFee"] = str(r.builder_fee)
    return f


def stored_fills(session: Session, addr: str) -> list[dict]:
    rows = session.scalars(select(Fill).where(Fill.address == addr).order_by(Fill.time, Fill.tid))
    return [fill_dict(r) for r in rows]


def stored_funding(session: Session, addr: str) -> list[dict]:
    rows = session.scalars(select(Funding).where(Funding.address == addr).order_by(Funding.time))
    return [{"time": r.time, "delta": {"type": "funding", "coin": r.coin, "usdc": str(r.usdc)}}
            for r in rows]


@dataclass
class RescoreResult:
    scored: int = 0
    unscored: int = 0
    reasons: Counter = field(default_factory=Counter)
    top: list[tuple[str, float]] = field(default_factory=list)


def rescore(
    session: Session, now: datetime | None = None, *,
    sz_decimals: dict[str, int] | None = None, window_days: float = 30,
) -> RescoreResult:
    """Recompute stats + score for every wallet and persist them (caller commits).

    ``stats_json`` = the stats, ``score_reason`` and the kept metadata keys
    (``backfill``, ``leaderboard``). ``sz_decimals=None`` reads the kv cache (see
    :func:`load_sz_decimals`); an empty mapping disables the per-coin rounding.
    """
    now = _now(now)
    if sz_decimals is None:
        cached = kv_get_json(session, KV_SZ_DECIMALS)
        sz_decimals = dict(cached["sz_decimals"]) if isinstance(cached, dict) else {}
        if not sz_decimals:
            log.warning("rescore: no cached szDecimals (run with a client to fetch meta)")
    wallets = list(session.scalars(select(Wallet)))
    rows: dict[str, dict] = {}
    for w in wallets:
        fills = stored_fills(session, w.address)
        meta = (w.stats_json or {}).get("backfill") or {}
        oldest = (datetime.fromtimestamp(min(int(f["time"]) for f in fills) / 1000, tz=timezone.utc)
                  if fills else None)
        st = wallet_stats(
            w.address, fills, stored_funding(session, w.address), now, window_days,
            history_partial=bool(meta.get("history_partial")),
            oldest_fill_time=oldest, sz_decimals=sz_decimals,
        )
        rows[w.address] = {**st, "is_vault": bool(w.is_vault)}
    scored = score_detailed(rows)
    res = RescoreResult()
    for w in wallets:
        st = dict(rows[w.address])
        st.pop("is_vault", None)
        sc = scored[w.address]
        kept = {k: v for k, v in (w.stats_json or {}).items() if k in META_KEYS}
        w.stats_json = {**st, "score_reason": sc.score_reason, **kept}
        w.score = sc.score
        w.is_bot = is_bot(st, False)
        w.scored_at = now
        if sc.score is None:
            res.unscored += 1
            res.reasons[sc.score_reason] += 1
        else:
            res.scored += 1
    res.top = sorted(((a, s.score) for a, s in scored.items() if s.score is not None),
                     key=lambda x: -x[1])[:10]
    session.flush()
    return res


__all__ = [
    "BackfillResult", "Discovered", "RescoreResult", "backfill", "discover",
    "leaderboard_candidates", "load_sz_decimals", "rescore", "stored_fills",
    "stored_funding", "utcnow", "vault_addresses",
]
