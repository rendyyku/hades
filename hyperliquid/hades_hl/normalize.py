"""Raw Hyperliquid fills -> core TradeEvents.

Ordering: HL ``tid`` is NOT monotonic, even inside one millisecond (verified on the
real fixture: sorting by tid breaks the ``startPosition`` chain on 797 of 1312
fills). The authoritative order of one coin's fills is the ``startPosition`` chain:
each fill starts where the previous one ended. :func:`order_fills` sorts by time
and, inside a millisecond, re-chains each coin's fills by ``startPosition`` (ruling P23:
backtracking search for groups <= 12 fills, deterministic ``(startPosition, tid)``
tie-break, fallback ``(startPosition, tid)`` order with a warning).

Side/direction come from position arithmetic, not from HL's ``dir`` text (``dir``
says "Open Long" for an increase too): with ``s = startPosition`` and the signed
delta ``d`` (+sz for a buy ``B``, -sz for a sell ``A``), ``e = s + d``:

- ``s == 0``                    -> open (direction by the sign of ``d``)
- same sign, ``|e| > |s|``      -> increase
- same sign, ``0 < |e| < |s|``  -> reduce;  ``e == 0`` -> close
- sign change                   -> flip: close ``|s|`` + open ``|e|`` (two legs)

Liquidation (ruling P18) follows the same arithmetic: a partial one is ``reduce``, a
full one ``close``; in a liquidation flip only the close leg is flagged liquidated.
Only fills whose ``liquidation.liquidatedUser`` is ``addr`` count (``addr`` is
required). Fees are ``fee + builderFee`` (P20). Position ids: :func:`normalize_fills`.
Spot fills (``@123`` or ``PURR/USDC``) are dropped; HIP-3 ``dex:COIN`` are perps.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from hades_core.events import TradeEvent

log = logging.getLogger(__name__)

HEAD = "hyperliquid"
ZERO = Decimal(0)


def ms_to_dt(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def is_spot(fill: Mapping) -> bool:
    coin = str(fill.get("coin", ""))
    return coin.startswith("@") or "/" in coin or fill.get("dir") in ("Buy", "Sell")


def signed_delta(fill: Mapping) -> Decimal:
    sz = Decimal(str(fill["sz"]))
    return sz if fill["side"] == "B" else -sz


def is_own_liquidation(fill: Mapping, addr: str) -> bool:
    """True when the fill liquidated *this* wallet (not one where ``addr`` was the
    liquidator / backstop). A liquidation object without ``liquidatedUser`` counts."""
    liq = fill.get("liquidation")
    if not liq:
        return False
    victim = liq.get("liquidatedUser") if isinstance(liq, Mapping) else None
    if not victim:
        return True
    return str(victim).lower() == addr.lower()


def fill_fee(fill: Mapping) -> Decimal:
    """Total fee of a fill: ``fee`` + ``builderFee`` (when present), in USDC."""
    return Decimal(str(fill.get("fee") or "0")) + Decimal(str(fill.get("builderFee") or "0"))


def leverage_by_coin(clearinghouse_state: Mapping | None) -> dict[str, Decimal]:
    """``{coin: leverage}`` from a ``clearinghouseState`` response ({} if unavailable)."""
    out: dict[str, Decimal] = {}
    for ap in (clearinghouse_state or {}).get("assetPositions") or []:
        pos = (ap or {}).get("position") or {}
        lev = (pos.get("leverage") or {}).get("value")
        coin = pos.get("coin")
        if coin is None or lev is None:
            continue
        try:
            v = Decimal(str(lev))
        except InvalidOperation:
            continue
        if v > 0:
            out[coin] = v
    return out


def sz_decimals_by_coin(meta: Mapping | None) -> dict[str, int]:
    """``{coin: szDecimals}`` from a ``meta`` response ({} if unavailable)."""
    out: dict[str, int] = {}
    for u in (meta or {}).get("universe") or []:
        if u and u.get("name") is not None and u.get("szDecimals") is not None:
            out[u["name"]] = int(u["szDecimals"])
    return out


def _require_tid(f: Mapping) -> None:
    if f.get("tid") is None:
        raise ValueError(f"HL fill without tid (coin={f.get('coin')}, time={f.get('time')})")


def _start(f: Mapping) -> Decimal:
    return Decimal(str(f["startPosition"]))


def _end(f: Mapping) -> Decimal:
    return _start(f) + signed_delta(f)


def order_fills(fills: Iterable[Mapping]) -> list[dict]:
    """Perp fills, de-duplicated by tid, in true execution order (see module doc)."""
    return _order(fills)[0]


def _order(fills: Iterable[Mapping]) -> tuple[list[dict], int]:
    """(ordered perp fills, number of same-ms groups with no full chain)."""
    seen: set = set()
    uniq: list[dict] = []
    for f in fills:
        _require_tid(f)
        if is_spot(f) or f["tid"] in seen:
            continue
        seen.add(f["tid"])
        uniq.append(dict(f))
    # bucket by (time, coin); coins inside one ms in sorted order (independent of input)
    buckets: dict[int, dict[str, list[dict]]] = {}
    for f in uniq:
        buckets.setdefault(int(f["time"]), {}).setdefault(f["coin"], []).append(f)
    out: list[dict] = []
    broken = 0
    last_end: dict[str, Decimal] = {}
    for t in sorted(buckets):
        for coin in sorted(buckets[t]):
            grp, ok = _chain(buckets[t][coin], last_end.get(coin))
            if not ok:
                broken += 1
                log.warning("hl %s @%d: no full startPosition chain over %d same-ms fills; "
                            "falling back to (startPosition, tid) order", coin, t, len(grp))
            out.extend(grp)
            last_end[coin] = _end(grp[-1])
    return out, broken


CHAIN_SEARCH_MAX = 12  # same-ms groups up to this size get an exhaustive search


def _tie(f: Mapping) -> tuple:
    return (_start(f), f["tid"])


def _chain(grp: list[dict], prev_end: Decimal | None) -> tuple[list[dict], bool]:
    """Order one coin's same-ms fills so each starts where the previous ended.

    Returns ``(order, full_chain)``. The first fill must start at ``prev_end``; if no
    full chain does, (a gap before this group) the start is chosen as with no previous
    end: ``startPosition == 0`` first, then the lowest tid. Candidates are tried in
    ``(startPosition, tid)`` order, so the result does not depend on input order.
    Groups of <= CHAIN_SEARCH_MAX fills are searched exhaustively (backtracking),
    larger ones greedily. With no full chain: ``sorted by (startPosition, tid)``.
    """
    fs = sorted(grp, key=_tie)
    if len(fs) == 1:
        return fs, True
    starts = [_start(f) for f in fs]
    ends = [_end(f) for f in fs]
    free_first = sorted(range(len(fs)), key=lambda k: (starts[k] != 0, fs[k]["tid"]))
    attempts = []
    if prev_end is not None:
        attempts.append([k for k in range(len(fs)) if starts[k] == prev_end])
    attempts.append(free_first)
    for firsts in attempts:
        order = _search(starts, ends, firsts) if len(fs) <= CHAIN_SEARCH_MAX else \
            _greedy(starts, ends, firsts)
        if order is not None:
            return [fs[k] for k in order], True
    return fs, False


def _search(starts: list, ends: list, firsts: list[int]) -> list[int] | None:
    n = len(starts)
    full = (1 << n) - 1
    dead: set[tuple[int, int]] = set()

    def go(order: list[int], mask: int) -> bool:
        if mask == full:
            return True
        last = order[-1]
        if (mask, last) in dead:
            return False
        for k in range(n):  # indices are already in (startPosition, tid) order
            if not mask & (1 << k) and starts[k] == ends[last]:
                order.append(k)
                if go(order, mask | (1 << k)):
                    return True
                order.pop()
        dead.add((mask, last))
        return False

    for k in firsts:
        order = [k]
        if go(order, 1 << k):
            return order
    return None


def _greedy(starts: list, ends: list, firsts: list[int]) -> list[int] | None:
    n = len(starts)
    for first in firsts:
        order, used = [first], {first}
        while len(order) < n:
            nxt = next((k for k in range(n) if k not in used and starts[k] == ends[order[-1]]),
                       None)
            if nxt is None:
                break
            order.append(nxt)
            used.add(nxt)
        if len(order) == n:
            return order
    return None


@dataclass(frozen=True)
class Leg:
    """One position-level piece of a fill (a flip fill yields two legs)."""

    fill: dict
    coin: str
    side: str  # open | increase | reduce | close
    direction: str  # long | short
    size: Decimal  # absolute size of this leg
    price: Decimal
    fee: Decimal  # this leg's share of the fill fee
    closed_pnl: Decimal  # HL closedPnl attributed to this leg (open legs: 0)
    position_id: str
    start: Decimal  # signed position before this leg
    end: Decimal  # signed position after this leg
    liquidation: bool
    ref_suffix: str  # "" or ":close" / ":open" for flip legs

    @property
    def time_ms(self) -> int:
        return int(self.fill["time"])

    @property
    def source_ref(self) -> str:
        return f"{self.fill.get('hash')}:{self.fill['tid']}{self.ref_suffix}"


def _dir(x: Decimal) -> str:
    return "long" if x > 0 else "short"


@dataclass(frozen=True)
class Normalized:
    legs: list[Leg]
    # startPosition chain breaks: fills not starting where the coin's previous fill
    # ended (a missing fill, or a same-ms group with no full chain)
    chain_gaps: int


def fill_legs(addr: str, fills: Iterable[Mapping]) -> list[Leg]:
    """Ordered perp legs with stable position ids (see :func:`normalize_fills`)."""
    return normalize_fills(addr, fills).legs


def normalize_fills(addr: str, fills: Iterable[Mapping]) -> Normalized:
    """Ordered perp legs of ``addr`` with stable position ids (ruling P22).

    - ``{coin}:{tid}``: an episode opened (from flat) by fill ``tid``;
    - ``{coin}:{tid}f``: the open leg of flip fill ``tid``;
    - ``{coin}:pre{tid}``: history starts mid-position at fill ``tid``;
    - ``{coin}:gap{tid}``: fill ``tid`` does not start where the coin's previous fill
      ended (a missing fill); logged and counted in ``chain_gaps``. A gap fill with
      ``startPosition == 0`` gets the plain ``{coin}:{tid}`` (ruling P25) but is still
      counted in ``chain_gaps``.
    Ids depend only on the fills of the episode itself, so they are stable across
    window shifts and re-normalization.
    """
    # a same-ms group with no full chain always leaves >= 1 break in its fallback order,
    # which the per-fill check below counts; _order only logs it
    ordered, _ = _order(fills)
    gaps = 0
    legs: list[Leg] = []
    cur: dict[str, str | None] = {}  # coin -> open episode id (None when flat)
    last_end: dict[str, Decimal] = {}
    for f in ordered:
        coin, tid = f["coin"], f["tid"]
        s = _start(f)
        d = signed_delta(f)
        e = s + d
        if coin not in last_end:
            cur[coin] = f"{coin}:{tid}" if s == 0 else f"{coin}:pre{tid}"
        elif s != last_end[coin]:
            gaps += 1
            log.warning("hl %s %s: chain gap at tid %s (startPosition %s, previous end %s); "
                        "new episode", addr, coin, tid, s, last_end[coin])
            # ruling P25: a gap fill from flat opens a normal episode (plain id);
            # it still counts as a chain gap
            cur[coin] = f"{coin}:{tid}" if s == 0 else f"{coin}:gap{tid}"
        elif s == 0:
            cur[coin] = f"{coin}:{tid}"
        last_end[coin] = e
        if d == 0:
            continue
        pid = cur[coin]
        px = Decimal(str(f["px"]))
        fee = fill_fee(f)
        cpnl = Decimal(str(f.get("closedPnl") or "0"))
        liq = is_own_liquidation(f, addr)
        sz = abs(d)

        def leg(side, direction, size, fee_, cpnl_, pid_, st, en, liq_, suffix="") -> Leg:
            return Leg(f, coin, side, direction, size, px, fee_, cpnl_, pid_, st, en, liq_,
                       suffix)

        if s == 0:
            legs.append(leg("open", _dir(d), sz, fee, cpnl, pid, s, e, liq))
        elif e != 0 and (e > 0) != (s > 0):  # flip: the liquidation (if any) is the close
            close_sz, open_sz = abs(s), abs(e)
            close_fee = fee * close_sz / sz
            legs.append(leg("close", _dir(s), close_sz, close_fee, cpnl, pid, s, ZERO, liq,
                            ":close"))
            pid = cur[coin] = f"{coin}:{tid}f"
            legs.append(leg("open", _dir(e), open_sz, fee - close_fee, ZERO, pid, ZERO, e,
                            False, ":open"))
        else:  # same sign: increase, reduce (a partial liquidation too) or close
            if (d > 0) == (s > 0):
                side = "increase"
            else:
                side = "close" if e == 0 else "reduce"
            legs.append(leg(side, _dir(s), sz, fee, cpnl, pid, s, e, liq))
        if e == 0:
            cur[coin] = None
        _check_dir(f, legs[-1])
    return Normalized(legs, gaps)


def _check_dir(f: Mapping, last: Leg) -> None:
    text = str(f.get("dir", ""))
    if ">" in text or not ("Long" in text or "Short" in text):
        return
    if ("Long" in text) != (last.direction == "long"):
        log.warning("hl fill %s: dir %r disagrees with startPosition-derived %s",
                    f.get("tid"), text, last.direction)


def fills_to_events(
    addr: str,
    fills: Iterable[Mapping],
    observed_time_fn: Callable[[Mapping], datetime] | None = None,
    *,
    leverage: Mapping[str, Decimal] | None = None,
) -> list[TradeEvent]:
    """Map raw HL fills of ``addr`` to TradeEvents, in execution order.

    ``observed_time_fn(fill) -> datetime`` gives the observed time; by default it is the
    leader time (historical normalization; the live watcher passes its own).
    ``leverage``: ``{coin: leverage}``, e.g. :func:`leverage_by_coin` of the wallet's
    clearinghouseState; coins without an entry get ``leverage=None``.
    """
    lev = leverage or {}
    out: list[TradeEvent] = []
    for seq, lg in enumerate(fill_legs(addr, fills)):
        leader_time = ms_to_dt(lg.time_ms)
        observed = observed_time_fn(lg.fill) if observed_time_fn else leader_time
        out.append(TradeEvent(
            head=HEAD,
            wallet=addr,
            market_id=lg.coin,
            side=lg.side,  # type: ignore[arg-type]
            direction=lg.direction,  # type: ignore[arg-type]
            size=lg.size,
            price=lg.price,
            notional_usd=lg.size * lg.price,
            fee_usd=lg.fee,
            leader_time=leader_time,
            observed_time=observed,
            source_ref=lg.source_ref,
            leverage=lev.get(lg.coin),
            position_id=lg.position_id,
            group_id=None,
            seq=seq,
        ))
    return out
