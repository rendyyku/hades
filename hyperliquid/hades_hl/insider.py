"""Insider-pattern tags with on-chain evidence (spec 8.2, Task 7).

A tag without evidence is a defect: every :class:`Tag` carries evidence rows
``{tid, hash, coin, time, detail}`` (``time`` = fill epoch ms) and :func:`persist_tags`
refuses a tag with no evidence or an incomplete row.

The rules work on the OPEN legs of a wallet's stored fills inside its contiguous
coverage (kv ``hl:cov:{addr}``): a leg that opens a position from flat, including the
open leg of a flip (:func:`open_legs`; order and legs from :mod:`hades_hl.normalize`
with ``szDecimals``, ruling P24). Notional = leg size x fill price.

- ``pre_listing`` (:func:`tag_pre_listing`): an open within 24 h of the coin's listing
  with notional >= 5 x the wallet's usual (median) open notional. The listing time is
  the first *traded* 1d candle of ``candleSnapshot(interval=1d, startTime=0)`` (HL pads
  old coins with empty ``n == 0`` candles: BTC's series starts in 2020 at volume 0),
  cached permanently in kv ``hl:listing:{coin}``. Only coins the wallet opened are
  fetched.
- ``pre_move`` (:func:`tag_pre_move`): among the wallet's opens with notional >= its
  median open notional over the coverage (ruling P38), the 200 largest (cap applied
  after the floor): the best close in the position's favour (up for long, down for
  short) within 30 min after the open is >= 5% above (below) the open fill price, in at
  least 3 DISTINCT move events (ruling P40: qualifying opens of one coin less than
  30 min after the previous qualifying open belong to the same event; every row is kept
  as evidence with its ``event_index``). Closes come
  from candles whose close time ``T`` is in ``[open, open + 30 min)``. HL serves only
  the latest 5000 candles per interval (live probe 2026-11-21), so the finest interval
  that still reaches the open is used: 1m (~3.4 d), 5m (~17 d), 15m (~51 d), 1h
  (~204 d; only when an hourly close falls inside the window); older opens are
  unmeasured. Each open's candles are cached in kv ``hl:candles:{coin}:{iv}:{lo}:{hi}``
  (reruns are free); missing ones are fetched with at most one request per open (opens
  of one coin are merged into one request when close together). Empty slices are
  never cached.
- ``fresh_whale`` (:func:`tag_fresh_whale`): the first fill is < 7 days old and one of
  the first 5 opens has notional >= 50,000 USD. ``first_seen`` (stored on
  ``wallets.first_seen``) is the earliest fill HL can give us: unknown (None, tag
  skipped) when the coverage is capped / history partial; else the oldest stored fill,
  and when that is < 7 days old, one ``userFillsByTime[0, cov_start)`` request confirms
  no older fill exists (the coverage only spans the backfill window).

Tags are idempotent: :func:`persist_tags` replaces the wallet's rows for every rule it
evaluated, so a rule that no longer fires removes its stale tag. A rule whose inputs were
incomplete (a listing / candle fetch error, a failed pre-coverage check) is NOT
evaluated: its existing rows (and a known ``first_seen``) are kept, with a WARNING.

Fills with a zero hash (``0x`` + all zeros: not a linkable transaction) are never
evidence; a rule left without its required evidence does not fire.
"""

from __future__ import annotations

import bisect
import logging
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from hades_hl.client import HLClient, HLError, HLRateLimited
from hades_hl.db import Tag as TagRow
from hades_hl.db import Wallet
from hades_hl.normalize import Leg, SzDecimals, fill_legs
from hades_hl.pipeline import (
    _ensure_wallet,
    get_coverage,
    kv_get_json,
    kv_set_json,
    stored_fills,
)

log = logging.getLogger(__name__)

MIN_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000

TAG_KINDS = ("pre_listing", "pre_move", "fresh_whale")
EVIDENCE_KEYS = ("tid", "hash", "coin", "time", "detail")

PRE_LISTING_WINDOW_H = 24
PRE_LISTING_MULT = 5
PRE_MOVE_PCT = 0.05
PRE_MOVE_WINDOW_MIN = 30
PRE_MOVE_MIN_OCCURRENCES = 3
PRE_MOVE_MAX_OPENS = 200
FRESH_MAX_AGE_DAYS = 7
FRESH_FIRST_OPENS = 5
FRESH_MIN_NOTIONAL = 50_000

LISTING_RESOLUTION = "1d"
KV_LISTING = "hl:listing:{}"
KV_CANDLES = "hl:candles:{}:{}:{}:{}"  # coin, interval, lo, hi (ms)
LISTING_EMPTY_RETRY_MS = 7 * DAY_MS  # a coin with no candles is re-asked after a week
# HL serves the latest 5000 candles per interval; keep a margin for the time between
# choosing the interval and the request
CANDLE_INTERVALS: tuple[tuple[str, int], ...] = (
    ("1m", MIN_MS), ("5m", 5 * MIN_MS), ("15m", 15 * MIN_MS), ("1h", HOUR_MS))
CANDLE_HORIZON = 4900
MERGE_MAX_CANDLES = 1000  # one merged request spans at most this many candles


@dataclass
class Tag:
    address: str
    tag: str
    evidence: list[dict] = field(default_factory=list)


# -- helpers ---------------------------------------------------------------------------

def _to_ms(x: datetime | int) -> int:
    if isinstance(x, datetime):
        return int(x.timestamp() * 1000)
    return int(x)


def is_zero_hash(h: Any) -> bool:
    """``0x`` followed by only zeros (HL's placeholder for fills without a tx)."""
    return isinstance(h, str) and h[:2].lower() == "0x" and set(h[2:]) <= {"0"}


def has_evidence_hash(lg: Leg) -> bool:
    h = lg.fill.get("hash")
    return isinstance(h, str) and bool(h) and not is_zero_hash(h)


def open_legs(addr: str, fills: Iterable[Mapping], sz_decimals: SzDecimals = None) -> list[Leg]:
    """Legs that open a position from flat (a flip's open leg included), in order."""
    return [lg for lg in fill_legs(addr, fills, sz_decimals=sz_decimals) if lg.side == "open"]


def notional(lg: Leg) -> Decimal:
    return lg.size * lg.price


def median_open_notional(legs: Sequence[Leg]) -> Decimal | None:
    return statistics.median([notional(lg) for lg in legs]) if legs else None


def evidence_row(lg: Leg, **detail: Any) -> dict:
    """One evidence row for an open leg: ``{tid, hash, coin, time, detail}``."""
    return {
        "tid": int(lg.fill["tid"]),
        "hash": lg.fill.get("hash"),
        "coin": lg.coin,
        "time": lg.time_ms,
        "detail": {
            "leg": "flip_open" if lg.ref_suffix == ":open" else "open",
            "direction": lg.direction,
            "size": str(lg.size),
            "px": str(lg.price),
            "notional_usd": float(notional(lg)),
            **detail,
        },
    }


# -- rules (pure) ----------------------------------------------------------------------

def tag_pre_listing(
    fills: Iterable[Mapping], listings: Mapping[str, datetime | int],
    usual_notional: Decimal | float | None = None, *, address: str,
    sz_decimals: SzDecimals = None, window_h: float = PRE_LISTING_WINDOW_H,
    mult: float = PRE_LISTING_MULT,
) -> list[Tag]:
    """Opens within ``window_h`` (inclusive) after the coin's listing with notional
    >= ``mult`` x ``usual_notional`` (default: the median open notional of ``fills``)."""
    legs = open_legs(address, fills, sz_decimals)
    usual = (Decimal(str(usual_notional)) if usual_notional is not None
             else median_open_notional(legs))
    if usual is None or usual <= 0:
        return []
    window_ms = int(window_h * HOUR_MS)
    floor = Decimal(str(mult)) * usual
    ev = []
    for lg in legs:
        lst = listings.get(lg.coin)
        if lst is None:
            continue
        lst_ms = _to_ms(lst)
        dt = lg.time_ms - lst_ms
        if not 0 <= dt <= window_ms or notional(lg) < floor or not has_evidence_hash(lg):
            continue
        ev.append(evidence_row(
            lg, listing_ms=lst_ms, listing_resolution=LISTING_RESOLUTION,
            hours_after_listing=dt / HOUR_MS,
            usual_notional_usd=float(usual), ratio_to_usual=float(notional(lg) / usual)))
    return [Tag(address, "pre_listing", ev)] if ev else []


def select_pre_move_opens(legs: Sequence[Leg], max_opens: int = PRE_MOVE_MAX_OPENS) -> list[Leg]:
    """Opens with notional >= the median open notional of ``legs`` (the coverage;
    ruling P38), then the ``max_opens`` largest of those (ties: earlier first)."""
    med = median_open_notional(legs)
    if med is None:
        return []
    big = [lg for lg in legs if notional(lg) >= med]
    return sorted(big, key=lambda lg: (-notional(lg), lg.time_ms, int(lg.fill["tid"])))[
        :max_opens]


def _index_candles(prices: Mapping[str, Iterable[Mapping]]) -> dict[str, tuple[list, list]]:
    out: dict[str, tuple[list, list]] = {}
    for coin, rows in prices.items():
        by_t: dict[int, Mapping] = {}
        for c in rows:
            by_t.setdefault(int(c["T"]), c)
        ts = sorted(by_t)
        out[coin] = (ts, [by_t[t] for t in ts])
    return out


def tag_pre_move(
    fills: Iterable[Mapping], prices: Mapping[str, Iterable[Mapping]],
    move_pct: float = PRE_MOVE_PCT, window_min: float = PRE_MOVE_WINDOW_MIN,
    min_occurrences: int = PRE_MOVE_MIN_OCCURRENCES, *, address: str,
    sz_decimals: SzDecimals = None, max_opens: int = PRE_MOVE_MAX_OPENS,
) -> list[Tag]:
    """Among the opens at or above the median open notional (the ``max_opens`` largest
    of them), those whose best favourable candle close within ``[open, open + window)``
    (by candle close time ``T``) moved >= ``move_pct`` from the open fill price; a tag
    when they form >= ``min_occurrences`` distinct events (ruling P40: a qualifying open
    of the same coin less than ``window`` after the previous qualifying one joins its
    event). Opens whose fill has no usable hash are not evidence and do not count.
    ``prices``: ``{coin: [candle]}`` with HL's ``T`` (close ms) and ``c`` (close).
    Every candle must carry its interval ``i``."""
    all_legs = open_legs(address, fills, sz_decimals)
    med = median_open_notional(all_legs)
    legs = select_pre_move_opens(all_legs, max_opens)
    idx = _index_candles(prices)
    window_ms = int(window_min * MIN_MS)
    need = Decimal(str(move_pct))
    hits = []
    for lg in legs:
        if lg.coin not in idx or lg.price <= 0 or not has_evidence_hash(lg):
            continue
        ts, cs = idx[lg.coin]
        lo = bisect.bisect_left(ts, lg.time_ms)
        hi = bisect.bisect_left(ts, lg.time_ms + window_ms)
        best: tuple[Decimal, Mapping] | None = None
        for c in cs[lo:hi]:
            close = Decimal(str(c["c"]))
            pct = (close - lg.price) / lg.price
            if lg.direction == "short":
                pct = -pct
            if best is None or pct > best[0]:
                best = (pct, c)
        if best is None or best[0] < need:
            continue
        pct, c = best
        hits.append(evidence_row(
            lg, move_pct=float(pct), best_close=str(c["c"]), best_close_time=int(c["T"]),
            minutes_after_open=(int(c["T"]) - lg.time_ms) / MIN_MS,
            interval=c.get("i"), window_min=window_min,
            median_open_notional_usd=float(med)))
    hits.sort(key=lambda e: (e["time"], e["tid"]))
    events = 0
    last: dict[str, tuple[int, int]] = {}  # coin -> (previous qualifying open ms, event)
    for e in hits:
        prev = last.get(e["coin"])
        if prev is not None and e["time"] < prev[0] + window_ms:
            ev_i = prev[1]
        else:
            events += 1
            ev_i = events
        last[e["coin"]] = (e["time"], ev_i)
        e["detail"]["event_index"] = ev_i
    if events < min_occurrences:
        return []
    for e in hits:
        e["detail"]["distinct_events"] = events
    return [Tag(address, "pre_move", hits)]


def tag_fresh_whale(
    first_seen: datetime | int | None, fills: Iterable[Mapping], *, address: str,
    now: datetime | None = None, sz_decimals: SzDecimals = None,
    max_age_days: float = FRESH_MAX_AGE_DAYS, first_opens: int = FRESH_FIRST_OPENS,
    min_notional: float = FRESH_MIN_NOTIONAL,
) -> list[Tag]:
    """First fill < ``max_age_days`` ago and one of the first ``first_opens`` opens has
    notional >= ``min_notional``. ``first_seen=None`` (unknown) never fires."""
    if first_seen is None:
        return []
    now_ms = _to_ms(now or datetime.now(timezone.utc))
    fs_ms = _to_ms(first_seen)
    age = now_ms - fs_ms
    if not age < max_age_days * DAY_MS:
        return []
    floor = Decimal(str(min_notional))
    ev = [evidence_row(lg, open_index=i, first_seen_ms=fs_ms, account_age_days=age / DAY_MS)
          for i, lg in enumerate(open_legs(address, fills, sz_decimals)[:first_opens], 1)
          if notional(lg) >= floor and has_evidence_hash(lg)]
    return [Tag(address, "fresh_whale", ev)] if ev else []


# -- persistence -----------------------------------------------------------------------

def _check_evidence(tag: Tag) -> None:
    if not isinstance(tag.evidence, list) or not tag.evidence:
        raise ValueError(f"tag {tag.tag} of {tag.address}: no evidence (a tag without "
                         "evidence is a defect)")
    for row in tag.evidence:
        if not isinstance(row, Mapping) or any(k not in row for k in EVIDENCE_KEYS):
            raise ValueError(f"tag {tag.tag} of {tag.address}: evidence row without "
                             f"{EVIDENCE_KEYS}: {row!r}")
        if row["tid"] is None or not row["hash"] or not row["coin"] or row["time"] is None:
            raise ValueError(f"tag {tag.tag} of {tag.address}: evidence row with an empty "
                             f"tid/hash/coin/time: {row!r}")
        if is_zero_hash(row["hash"]):
            raise ValueError(f"tag {tag.tag} of {tag.address}: evidence row with a zero "
                             f"hash: {row!r}")
        if tag.tag == "pre_move" and (not isinstance(row["detail"], Mapping)
                                      or not row["detail"].get("interval")):
            raise ValueError(f"tag pre_move of {tag.address}: evidence row without its "
                             f"candle interval: {row!r}")


def persist_tags(session: Session, address: str, tags: Sequence[Tag],
                 kinds: Sequence[str] = TAG_KINDS) -> None:
    """Replace ``address``'s rows of every rule in ``kinds`` with ``tags`` (rules in
    ``kinds`` without a tag lose their stale row). Validates everything first: raises
    ValueError (and writes nothing) on empty / incomplete evidence. Caller commits."""
    address = address.lower()
    seen: set[str] = set()
    for t in tags:
        if t.address.lower() != address:
            raise ValueError(f"tag {t.tag} is for {t.address}, not {address}")
        if t.tag not in kinds:
            raise ValueError(f"tag {t.tag} is not one of {tuple(kinds)}")
        if t.tag in seen:
            raise ValueError(f"tag {t.tag} given twice for {address}")
        seen.add(t.tag)
        _check_evidence(t)
    session.execute(delete(TagRow).where(TagRow.address == address, TagRow.tag.in_(kinds)))
    for t in tags:
        session.add(TagRow(address=address, tag=t.tag, evidence_json=list(t.evidence)))
    session.flush()


# -- HL data: listings, candles, first_seen ---------------------------------------------

def listing_from_candles(rows: Sequence[Mapping]) -> int | None:
    """Open time of the first traded candle (``n > 0`` or ``v > 0``); the first candle
    when none says so; None for no candles."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: int(r["t"]))
    for r in rows:
        try:
            if int(r.get("n") or 0) > 0 or float(r.get("v") or 0) > 0:
                return int(r["t"])
        except (TypeError, ValueError):
            continue
    return int(rows[0]["t"])


async def listing_ms(session: Session, client: HLClient, coin: str, now_ms: int) -> int | None:
    """Listing time of ``coin`` (kv ``hl:listing:{coin}``, permanent once known).
    HL errors propagate (nothing cached): the caller treats pre_listing as incomplete."""
    key = KV_LISTING.format(coin)
    c = kv_get_json(session, key)
    if isinstance(c, dict):
        if isinstance(c.get("listing_ms"), int):
            return c["listing_ms"]
        f = c.get("fetched_at")
        if isinstance(f, int) and 0 <= now_ms - f < LISTING_EMPTY_RETRY_MS:
            return None
    rows = await client.candle_snapshot(coin, LISTING_RESOLUTION, 0, now_ms)
    t = listing_from_candles(rows)
    kv_set_json(session, key, {"listing_ms": t, "fetched_at": now_ms})
    session.commit()
    return t


def candle_interval(open_ms: int, now_ms: int,
                    window_ms: int = PRE_MOVE_WINDOW_MIN * MIN_MS) -> tuple[str, int] | None:
    """The finest candle interval HL still serves for ``open_ms`` that has a close
    inside ``[open, open + window)``; None when none does or the window is not over."""
    if open_ms + window_ms > now_ms:
        return None
    for name, iv in CANDLE_INTERVALS:
        lo = open_ms // iv * iv
        if lo < now_ms - CANDLE_HORIZON * iv:
            continue
        if lo + iv - 1 >= open_ms + window_ms:  # its first close is after the window
            continue
        return name, iv
    return None


def _expand(name: str, rows: list) -> list[dict]:
    return [{"T": int(T), "c": c, "i": name} for T, c in rows]


@dataclass
class CandleLoad:
    prices: dict[str, list[dict]]
    cached: int = 0
    fetched: int = 0  # opens whose candles were fetched
    requests: int = 0
    unmeasured: int = 0
    errors: int = 0  # opens whose candle request failed (HL error other than 429)


async def load_pre_move_candles(
    session: Session, client: HLClient, legs: Sequence[Leg], now_ms: int,
    window_ms: int = PRE_MOVE_WINDOW_MIN * MIN_MS,
) -> CandleLoad:
    """Candles closing inside each open's window, from kv (any interval) or fetched
    (at most one request per open; nearby opens of one coin share one request).
    Each open's slice is cached in kv as soon as its request returns."""
    out = CandleLoad(prices={})
    need: list[tuple[str, str, int, int, int, str]] = []  # coin, iv name, iv, lo, hi, key
    for lg in legs:
        hi = lg.time_ms + window_ms
        hit = None
        for name, iv in CANDLE_INTERVALS:
            key = KV_CANDLES.format(lg.coin, name, lg.time_ms // iv * iv, hi)
            c = kv_get_json(session, key)
            if isinstance(c, list) and c:  # a cached [] is a miss: never trust a gap
                hit = (name, c)
                break
        if hit is not None:
            out.cached += 1
            out.prices.setdefault(lg.coin, []).extend(_expand(*hit))
            continue
        ch = candle_interval(lg.time_ms, now_ms, window_ms)
        if ch is None:
            out.unmeasured += 1
            continue
        name, iv = ch
        lo = lg.time_ms // iv * iv
        need.append((lg.coin, name, iv, lo, hi, KV_CANDLES.format(lg.coin, name, lo, hi)))
    need.sort(key=lambda n: (n[0], n[1], n[3], n[4]))
    groups: list[list[tuple]] = []
    for n in need:
        g = groups[-1] if groups else None
        if (g is not None and g[0][:2] == n[:2]
                and (max(n[4], max(m[4] for m in g)) - g[0][3]) <= MERGE_MAX_CANDLES * n[2]):
            g.append(n)
        else:
            groups.append([n])
    for g in groups:
        coin, name, iv = g[0][:3]
        g_lo, g_hi = g[0][3], max(m[4] for m in g)
        try:
            rows = await client.candle_snapshot(coin, name, g_lo, g_hi)
        except HLRateLimited:
            raise
        except HLError as e:
            log.warning("tag: %s %s candles unavailable (%s); %d opens unmeasured", coin,
                        name, type(e).__name__, len(g))
            out.unmeasured += len(g)
            out.errors += len(g)
            continue
        out.requests += 1
        for _, _, _, lo, hi, key in g:
            sl = [[int(r["T"]), str(r["c"])] for r in rows
                  if int(r["t"]) >= lo and int(r["T"]) < hi]
            if sl:  # never cache an empty slice: it may just be a gap in HL's answer
                kv_set_json(session, key, sl)
            out.prices.setdefault(coin, []).extend(_expand(name, sl))
            out.fetched += 1
        session.commit()
    return out


async def resolve_first_seen(
    session: Session, client: HLClient, addr: str, fills: Sequence[Mapping],
    cov: Mapping | None, history_partial: bool, now_ms: int,
    max_age_days: float = FRESH_MAX_AGE_DAYS,
) -> int | None:
    """Earliest fill time (ms) HL can give us; None when unknown (capped / partial
    history, no coverage, no fill). A failed pre-coverage check raises the HLError:
    the caller keeps the known ``first_seen`` and skips fresh_whale."""
    if cov is None or history_partial or bool(cov.get("capped")):
        return None
    times = [int(f["time"]) for f in fills]
    if not times:
        return None
    oldest = min(times)
    if now_ms - oldest >= max_age_days * DAY_MS or int(cov["start"]) <= 0:
        return oldest
    # young by the coverage: does HL know an older fill (before the backfill window)?
    earlier = await client.user_fills_range(addr, 0, int(cov["start"]) - 1)
    if earlier:
        oldest = min(oldest, min(int(f["time"]) for f in earlier))
    return oldest


def _ms_dt(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


async def tag_wallet(
    session: Session, client: HLClient, addr: str, *, now: datetime | None = None,
    sz_decimals: SzDecimals = None,
) -> dict[str, Tag]:
    """Run the three rules on ``addr``'s stored fills inside its coverage, store
    ``wallets.first_seen`` and persist the tags (replacing / removing old ones).
    Returns ``{tag: Tag}`` of the rules that fired. HLRateLimited propagates (nothing
    persisted for this wallet; fetched listings / candles stay cached). A rule whose
    inputs were incomplete (other HL errors) is skipped: its stored rows are kept, a
    WARNING names it."""
    addr = addr.lower()
    now = now or datetime.now(timezone.utc)
    now_ms = _to_ms(now)
    w = session.get(Wallet, addr) or _ensure_wallet(session, addr)
    cov = get_coverage(session, addr)
    fills = (stored_fills(session, addr, cov["start"], cov["end"]) if cov is not None
             else stored_fills(session, addr))
    partial = bool(((w.stats_json or {}).get("backfill") or {}).get("history_partial"))
    legs = open_legs(addr, fills, sz_decimals)

    skipped: dict[str, str] = {}  # rule -> why its inputs are incomplete

    fs_known = True
    try:
        first_seen = await resolve_first_seen(session, client, addr, fills, cov, partial,
                                              now_ms)
    except HLRateLimited:
        raise
    except HLError as e:
        fs_known, first_seen = False, None
        skipped["fresh_whale"] = f"pre-coverage fills check failed ({type(e).__name__})"

    listings: dict[str, int] = {}
    bad_listings: list[str] = []
    for coin in sorted({lg.coin for lg in legs}):
        try:
            t = await listing_ms(session, client, coin, now_ms)
        except HLRateLimited:
            raise
        except HLError as e:
            log.warning("tag %s: listing of %s unavailable (%s)", addr, coin,
                        type(e).__name__)
            bad_listings.append(coin)
            continue
        if t is not None:
            listings[coin] = t
    if bad_listings:
        skipped["pre_listing"] = f"listing fetch failed for {', '.join(bad_listings)}"

    sel = select_pre_move_opens(legs, PRE_MOVE_MAX_OPENS)
    load = await load_pre_move_candles(session, client, sel, now_ms)
    if load.unmeasured:
        log.info("tag %s: pre_move measured %d of %d opens (%d unmeasured: older than HL's "
                 "candle horizon or window not over)", addr, len(sel) - load.unmeasured,
                 len(sel), load.unmeasured)
    if load.errors:
        skipped["pre_move"] = f"candle fetch failed for {load.errors} opens"

    kinds = tuple(k for k in TAG_KINDS if k not in skipped)
    for k, why in skipped.items():
        log.warning("tag %s: %s skipped, existing tag kept: %s", addr, k, why)
    tags = (tag_pre_listing(fills, listings, None, address=addr, sz_decimals=sz_decimals)
            + tag_pre_move(fills, load.prices, address=addr, sz_decimals=sz_decimals)
            + tag_fresh_whale(first_seen, fills, address=addr, now=now,
                              sz_decimals=sz_decimals))
    tags = [t for t in tags if t.tag in kinds]
    w = session.get(Wallet, addr)
    if fs_known:
        w.first_seen = _ms_dt(first_seen)
    persist_tags(session, addr, tags, kinds=kinds)
    session.commit()
    return {t.tag: t for t in tags}


__all__ = [
    "TAG_KINDS", "Tag", "candle_interval", "is_zero_hash", "listing_from_candles", "listing_ms",
    "load_pre_move_candles", "median_open_notional", "open_legs", "persist_tags",
    "resolve_first_seen", "select_pre_move_opens", "tag_fresh_whale", "tag_pre_listing",
    "tag_pre_move", "tag_wallet",
]
