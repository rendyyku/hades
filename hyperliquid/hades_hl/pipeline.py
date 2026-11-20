"""Discovery, backfill and rescore for the Hyperliquid head (spec 8.2).

- :func:`discover`: leaderboard candidates (unofficial feed, see :mod:`hades_hl.client`),
  prefiltered (ruling P29: account value >= 10k, 0 < month volume <= 200 x account
  value, month PnL > 0, not a vault) and then ranked by the leaderboard's 30-day
  ("month") PnL only to pick *who to backfill*; the real ranking is our own recomputed
  score (:func:`rescore`). The leaderboard (~38 MB) is fetched at most once per 24 h:
  only the filtered candidate list is cached in ``kv``. Vaults come from the HL app's
  vault list (also cached 24 h).
- :func:`backfill`: idempotent fills + funding import. Fills are fetched NEWEST FIRST
  (ruling P27, :meth:`HLClient.user_fills_backward`) and the contiguous covered range is
  kept in kv ``hl:cov:{addr}`` = ``{"start", "end", "capped"}``. Every page is committed
  as it arrives, so an ``HLRateLimited`` mid-way keeps earlier pages (then re-raises).
- :func:`rescore`: stats over the stored fills/funding inside the coverage (ruling P17
  with P27: ``oldest_fill_time`` = coverage start; ``history_partial`` = the coverage
  start was set by the cap) and the percentile score, persisted on ``wallets``.
  ``sz_decimals`` comes from ``meta`` (ruling P24), cached in ``kv`` for 24 h by
  :func:`load_sz_decimals`, and also sets the chain tolerance (ruling P28).
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

from hades_hl.client import FILLS_HISTORY_CAP, HLClient, HLError, HLRateLimited, HLUnavailable
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
KV_COV = "hl:cov:{}"  # per-address fills coverage (ruling P27)

# discovery prefilter (ruling P29), applied before the cache cap
MIN_ACCOUNT_VALUE = 10_000.0
MAX_VLM_TO_ACCOUNT_VALUE = 200.0

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
    """``list[str]`` of discovered addresses, plus why it may be empty.

    ``vaults_checked``: False when the vault list was unavailable with no cache (M6),
    so vaults may be among the candidates (they are flagged on a later successful run).
    ``prefilter``: the P29 counts (``before``, ``after`` and one count per exclusion).
    """

    error: str | None = None
    vaults_checked: bool = True
    prefilter: dict | None = None


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


def prefilter_reason(c: dict, vaults: set[str]) -> str | None:
    """Why a parsed candidate is excluded by the P29 prefilter (None: kept)."""
    av, vlm, pnl = c["account_value"], c["vlm_month"], c["pnl_month"]
    if c["address"] in vaults:
        return "vault"
    if av is None or av < MIN_ACCOUNT_VALUE:
        return "account_value"
    if vlm is None or vlm <= 0:
        return "volume"
    if vlm > MAX_VLM_TO_ACCOUNT_VALUE * av:
        return "churn"
    if pnl is None or pnl <= 0:
        return "pnl"
    return None


PREFILTER_REASONS = ("account_value", "volume", "churn", "pnl", "vault")


def prefilter_candidates(ranked: list[dict], vaults: set[str]) -> tuple[list[dict], dict]:
    """(kept candidates in input order, counts ``{before, after, <reason>: n}``)."""
    counts = dict.fromkeys(PREFILTER_REASONS, 0)
    kept: list[dict] = []
    for c in ranked:
        why = prefilter_reason(c, vaults)
        if why is None:
            kept.append(c)
        else:
            counts[why] += 1
    return kept, {"before": len(ranked), "after": len(kept), **counts}


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
    session: Session, client: HLClient, now: datetime | None = None,
    vaults: "VaultSet | None" = None,
) -> dict:
    """The kv candidate entry ``{fetched_at, n_rows, rows, prefilter, vaults_checked}``:
    prefiltered (P29, before the CANDIDATE_CACHE_MAX cap) and ranked candidates, from the
    kv cache when < 24 h old. Raises HLUnavailable when the feed is down and nothing
    (not even a stale cache) is available."""
    now_ms = _ms(_now(now))
    cached = kv_get_json(session, KV_LEADERBOARD)
    if _fresh(cached, now_ms):
        return cached
    try:
        rows = await client.leaderboard()
    except HLUnavailable as e:
        if isinstance(cached, dict) and cached.get("rows"):
            log.warning("leaderboard unavailable (%s); using the stale cache from %s",
                        e, cached.get("fetched_at"))
            return cached
        raise
    if vaults is None:
        vaults = await vault_addresses(session, client, now)
    ranked = rank_candidates(rows)
    kept, counts = prefilter_candidates(ranked, vaults)
    log.info("leaderboard: %d rows, %d unique candidates; prefilter (P29) %d -> %d "
             "(excluded: account value<10k %d, no volume %d, volume>200x AV %d, "
             "month pnl<=0 %d, vault %d)%s", len(rows), len(ranked), counts["before"],
             counts["after"], counts["account_value"], counts["volume"], counts["churn"],
             counts["pnl"], counts["vault"],
             "" if vaults.checked else "; vault list NOT checked")
    entry = {"fetched_at": now_ms, "n_rows": len(rows), "rows": kept[:CANDIDATE_CACHE_MAX],
             "prefilter": counts, "vaults_checked": vaults.checked}
    kv_set_json(session, KV_LEADERBOARD, entry)
    session.commit()
    return entry


class VaultSet(set):
    """Vault addresses; ``checked`` is False when the list was unavailable and uncached."""

    checked: bool = True


async def vault_addresses(
    session: Session, client: HLClient, now: datetime | None = None
) -> VaultSet:
    """Every vault address, cached 24 h. Unavailable -> stale cache, else an empty set
    with ``checked=False`` (M6; logged)."""
    now_ms = _ms(_now(now))
    cached = kv_get_json(session, KV_VAULTS)
    if _fresh(cached, now_ms):
        return VaultSet(cached["addresses"])
    try:
        vaults = await client.vault_addresses()
    except HLUnavailable as e:
        if isinstance(cached, dict) and isinstance(cached.get("addresses"), list):
            log.warning("vault list unavailable (%s); using %d cached vault addresses",
                        e, len(cached["addresses"]))
            return VaultSet(cached["addresses"])
        log.warning("vault list unavailable (%s) and not cached: vaults are NOT excluded "
                    "from candidates this run (they are flagged on a later run)", e)
        out = VaultSet()
        out.checked = False
        return out
    kv_set_json(session, KV_VAULTS, {"fetched_at": now_ms, "addresses": sorted(vaults)})
    session.commit()
    return VaultSet(vaults)


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
    vaults = await vault_addresses(session, client, now)
    try:
        entry = await leaderboard_candidates(session, client, now, vaults)
    except HLUnavailable as e:
        out.error = f"leaderboard unavailable: {e}"
        out.vaults_checked = vaults.checked
        log.warning("discover: %s; no candidates (WS-trades fallback not implemented yet)", e)
        return out
    cands = entry.get("rows") or []
    out.prefilter = entry.get("prefilter")
    out.vaults_checked = bool(entry.get("vaults_checked", True)) and vaults.checked
    existing = {w.address: w for w in session.scalars(select(Wallet))}
    marked = 0
    for addr, w in existing.items():  # M6: a later successful vault list flags known vaults
        if addr in vaults and not w.is_vault:
            w.is_vault = True
            marked += 1
    skipped_vaults = 0
    for c in cands:
        addr = c["address"]
        if addr in vaults:  # cached before the vault list was available
            skipped_vaults += 1
            continue
        if len(out) >= limit:
            break
        out.append(addr)
        lb = {k: v for k, v in c.items() if k != "address"}
        w = existing.get(addr)
        if w is None:
            session.add(Wallet(address=addr, stats_json={"leaderboard": lb}))
        else:
            w.stats_json = {**(w.stats_json or {}), "leaderboard": lb}
    session.commit()
    log.info("discover: %d candidates (%d cached vaults skipped, %d known wallets marked "
             "as vaults)%s", len(out), skipped_vaults, marked,
             "" if out.vaults_checked else "; vault list NOT checked")
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


def get_coverage(session: Session, addr: str) -> dict | None:
    """``{"start", "end", "capped"}`` (ms) of ``addr``'s contiguous fills coverage."""
    c = kv_get_json(session, KV_COV.format(addr.lower()))
    if isinstance(c, dict) and isinstance(c.get("start"), int) and isinstance(c.get("end"), int):
        return c
    return None


async def backfill(
    session: Session, client: HLClient, addr: str, days: int = 90, *,
    now: datetime | None = None,
) -> BackfillResult:
    """Import ``addr``'s fills and funding (ruling P27). Idempotent.

    Fills are fetched newest first (:meth:`HLClient.user_fills_backward`) and the
    contiguous covered range is kept in kv ``hl:cov:{addr}``:

    - first run (or coverage older than ``now - days``): ``[now - days, now]``;
    - later runs: ``[cov_end, now]``. If that segment hits the cap, the coverage
      restarts at the oldest fill of the new segment (``capped``); else only the end
      moves. If ``days`` reaches further back than an uncapped coverage start,
      ``[now - days, cov_start]`` is fetched too.

    Pages are committed as they arrive; on ``HLRateLimited`` the progress so far is
    kept, the coverage is NOT advanced, ``last_error="rate_limited"`` is recorded and
    the error re-raised. ``stats_json['backfill']['history_partial']`` = the coverage
    start was set by the cap (or a same-ms overflow was seen).
    Funding resumes from the max stored funding ``time`` (else ``now - days``).
    """
    addr = addr.lower()
    now_dt = _now(now)
    now_ms = _ms(now_dt)
    w = _ensure_wallet(session, addr)
    session.commit()
    floor = now_ms - int(days * DAY_MS)
    cov = get_coverage(session, addr)
    if cov is not None and cov["end"] < floor:
        log.info("backfill %s: coverage ended before now - %d days; starting over", addr, days)
        cov = None
    last_fund = session.scalar(select(func.max(Funding.time)).where(Funding.address == addr))
    counts = {"fills": 0, "funding": 0}

    def on_fills(page: list[dict]) -> None:
        counts["fills"] += insert_fills(session, addr, page)
        session.commit()

    def on_funding(page: list[dict]) -> None:
        counts["funding"] += insert_funding(session, addr, page)
        session.commit()

    meta = (w.stats_json or {}).get("backfill") or {}
    same_ms = bool(meta.get("same_ms_overflow")) if cov is not None else False
    try:
        seg_start = floor if cov is None else cov["end"]
        fpage = await client.user_fills_backward(addr, seg_start, now_ms, on_page=on_fills)
        same_ms = same_ms or (fpage.truncated and not fpage.capped)
        if cov is None or fpage.capped:
            cov = {"start": fpage.cov_start, "end": now_ms, "capped": fpage.capped}
        else:
            cov = {**cov, "end": now_ms}
            if not cov["capped"] and cov["start"] > floor:  # more days than before
                older = await client.user_fills_backward(
                    addr, floor, cov["start"], FILLS_HISTORY_CAP, on_page=on_fills)
                same_ms = same_ms or (older.truncated and not older.capped)
                cov = {"start": older.cov_start, "end": now_ms, "capped": older.capped}
        truncated = fpage.truncated or cov["capped"]
        kv_set_json(session, KV_COV.format(addr), cov)
        _set_backfill_meta(w, history_partial=cov["capped"] or same_ms,
                           same_ms_overflow=same_ms)
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
    _set_backfill_meta(w, funding_partial=fund.truncated, last_error=None,
                       backfilled_at=now_dt.isoformat(), days=days)
    session.commit()
    return BackfillResult(counts["fills"], truncated, counts["funding"], fund.truncated)


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


def stored_fills(session: Session, addr: str, start_ms: int | None = None,
                 end_ms: int | None = None) -> list[dict]:
    q = select(Fill).where(Fill.address == addr)
    if start_ms is not None:
        q = q.where(Fill.time >= start_ms)
    if end_ms is not None:
        q = q.where(Fill.time <= end_ms)
    return [fill_dict(r) for r in session.scalars(q.order_by(Fill.time, Fill.tid))]


def stored_funding(session: Session, addr: str, start_ms: int | None = None,
                   end_ms: int | None = None) -> list[dict]:
    q = select(Funding).where(Funding.address == addr)
    if start_ms is not None:
        q = q.where(Funding.time >= start_ms)
    if end_ms is not None:
        q = q.where(Funding.time <= end_ms)
    return [{"time": r.time, "delta": {"type": "funding", "coin": r.coin, "usdc": str(r.usdc)}}
            for r in session.scalars(q.order_by(Funding.time))]


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
        meta = (w.stats_json or {}).get("backfill") or {}
        cov = get_coverage(session, w.address)
        if cov is not None:  # P17 + P27: only the contiguous coverage counts
            lo, hi = cov["start"], cov["end"]
            fills = stored_fills(session, w.address, lo, hi)
            funding = stored_funding(session, w.address, lo, hi)
            oldest = datetime.fromtimestamp(lo / 1000, tz=timezone.utc)
        else:  # legacy rows from before coverage tracking: the oldest stored fill
            fills = stored_fills(session, w.address)
            funding = stored_funding(session, w.address)
            oldest = (datetime.fromtimestamp(min(int(f["time"]) for f in fills) / 1000,
                                             tz=timezone.utc) if fills else None)
        st = wallet_stats(
            w.address, fills, funding, now, window_days,
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
        w.is_bot = is_bot(st, bool(w.is_vault))
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
    "BackfillResult", "Discovered", "RescoreResult", "VaultSet", "backfill", "discover",
    "get_coverage", "leaderboard_candidates", "load_sz_decimals", "prefilter_candidates",
    "rescore", "stored_fills", "stored_funding", "utcnow", "vault_addresses",
]
