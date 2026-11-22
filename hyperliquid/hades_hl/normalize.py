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

Tolerance (ruling P28): HL sends float noise in ``startPosition`` (``-613298.4300000001``
where the chain gives ``-613298.43``). Every chain / zero comparison uses
``tol = 10**-szDecimals / 2`` per coin (when szDecimals is unknown: ``max(1e-9,
1e-9 x |position|)``, relative, ruling P34): a start
within ``tol`` of the previous end is snapped to it, and a start or end within ``tol``
of 0 is snapped to 0. Pass ``sz_decimals`` (see :func:`sz_decimals_by_coin`).

One order, one event (ruling P65): a leader order usually fills in several pieces
(178 of the 1003 orders in the real fixture). A copy sized on the first piece and
scaled by the later ones is wrong by orders of magnitude, so the legs of one order on
one position are merged into ONE event before anything is sized: :func:`order_key`
says which legs belong together (same HL ``oid``; without ``oid`` the same coin and
direction in the same millisecond), :class:`OrderBuffer` merges the legs with the same
key, each observed at most ``AGG_WINDOW_S`` (2 s) after the previous piece, and
:func:`order_event` is the merged TradeEvent (size = sum, price = size-weighted
average, ``observed_time`` = the last piece's on the historical path and the emit
time on the live one (ruling P73, :mod:`hades_hl.live`), ``source_ref`` = the first
piece's; the pieces stay available as :attr:`OrderLegs.tids` / ``refs``). A flip inside one
order gives one close and one open.

Per order, not per run (ruling P69): every order has its own buffer, so orders of one
wallet that fill in turns each become one event. Only where merging across another
order would break a position's chain is an order ended early (:meth:`OrderBuffer.add`):
by a piece of an order of the OTHER direction on the same position, and whenever the
order that opened the position is involved. Events come out in the simulator's order
(last piece observed, then arrival).

Late pieces (ruling P69): a piece that can no longer join its order is its own event.
If its order OPENED the position (:attr:`Leg.open_piece`: same ``oid`` as the episode's
opening fill) the event's side is ``adjust``: the copy trades nothing and its ratio is
re-based on the leader's new size. A late piece of an increase / reduce order is a
plain increase / reduce (the copy scales it by the ratio, under the caps).

:func:`group_legs` / :func:`fills_to_events` (history) and the live watcher
(:class:`hades_hl.live.FillWatcher`, piece by piece as they arrive) share the buffer,
so both emit the same structure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    """``{coin: szDecimals}`` from a ``meta`` response ({} if unavailable; a non-dict
    response is logged: :meth:`HLClient.meta` already rejects it, so callers that cache
    the result never see one)."""
    out: dict[str, int] = {}
    if not isinstance(meta, Mapping):  # unavailable or an unexpected shape
        if meta is not None:
            log.warning("hl meta: unexpected shape %s; no szDecimals", type(meta).__name__)
        return out
    for u in meta.get("universe") or []:
        if u and u.get("name") is not None and u.get("szDecimals") is not None:
            out[u["name"]] = int(u["szDecimals"])
    return out


DEFAULT_TOL = Decimal("1e-9")
SzDecimals = Mapping[str, int] | None


def chain_tol(coin: str, sz_decimals: SzDecimals = None,
              position: Decimal | None = None) -> Decimal:
    """Half a size step of ``coin`` (``10**-szDecimals / 2``, ruling P28); without
    szDecimals ``max(1e-9, 1e-9 x |position|)`` (relative float noise, ruling P34)."""
    if sz_decimals and coin in sz_decimals:
        return Decimal(10) ** -int(sz_decimals[coin]) / 2
    if position:
        return max(DEFAULT_TOL, DEFAULT_TOL * abs(Decimal(position)))
    return DEFAULT_TOL


def _near(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def _require_tid(f: Mapping) -> None:
    if f.get("tid") is None:
        raise ValueError(f"HL fill without tid (coin={f.get('coin')}, time={f.get('time')})")


def _start(f: Mapping) -> Decimal:
    return Decimal(str(f["startPosition"]))


def _end(f: Mapping) -> Decimal:
    return _start(f) + signed_delta(f)


def order_fills(fills: Iterable[Mapping], *, sz_decimals: SzDecimals = None) -> list[dict]:
    """Perp fills, de-duplicated by tid, in true execution order (see module doc)."""
    return _order(fills, sz_decimals)[0]


def _order(fills: Iterable[Mapping], sz_decimals: SzDecimals = None) -> tuple[list[dict], int]:
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
            grp_in = buckets[t][coin]
            scale = max([abs(_start(f)) for f in grp_in] + [abs(last_end.get(coin, ZERO))])
            grp, ok = _chain(grp_in, last_end.get(coin), chain_tol(coin, sz_decimals, scale))
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


def _chain(grp: list[dict], prev_end: Decimal | None,
           tol: Decimal = DEFAULT_TOL) -> tuple[list[dict], bool]:
    """Order one coin's same-ms fills so each starts where the previous ended.

    Returns ``(order, full_chain)``. The first fill must start at ``prev_end``; if no
    full chain does, (a gap before this group) the start is chosen as with no previous
    end: ``startPosition == 0`` first, then the lowest tid. Candidates are tried in
    ``(startPosition, tid)`` order, so the result does not depend on input order.
    Groups of <= CHAIN_SEARCH_MAX fills are searched exhaustively (backtracking),
    larger ones greedily. With no full chain: ``sorted by (startPosition, tid)``.
    Positions match within ``tol`` (ruling P28).
    """
    fs = sorted(grp, key=_tie)
    if len(fs) == 1:
        return fs, True
    starts = [_start(f) for f in fs]
    ends = [_end(f) for f in fs]
    free_first = sorted(range(len(fs)),
                        key=lambda k: (not _near(starts[k], ZERO, tol), fs[k]["tid"]))
    attempts = []
    if prev_end is not None:
        attempts.append([k for k in range(len(fs)) if _near(starts[k], prev_end, tol)])
    attempts.append(free_first)
    for firsts in attempts:
        order = _search(starts, ends, firsts, tol) if len(fs) <= CHAIN_SEARCH_MAX else \
            _greedy(starts, ends, firsts, tol)
        if order is not None:
            return [fs[k] for k in order], True
    return fs, False


def _search(starts: list, ends: list, firsts: list[int],
            tol: Decimal = DEFAULT_TOL) -> list[int] | None:
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
            if not mask & (1 << k) and _near(starts[k], ends[last], tol):
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


def _greedy(starts: list, ends: list, firsts: list[int],
            tol: Decimal = DEFAULT_TOL) -> list[int] | None:
    n = len(starts)
    for first in firsts:
        order, used = [first], {first}
        while len(order) < n:
            nxt = next((k for k in range(n)
                        if k not in used and _near(starts[k], ends[order[-1]], tol)),
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
    # P69: an increase filled by the order (HL ``oid``) that opened this episode
    open_piece: bool = False

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


def fill_legs(addr: str, fills: Iterable[Mapping], *,
              sz_decimals: SzDecimals = None) -> list[Leg]:
    """Ordered perp legs with stable position ids (see :func:`normalize_fills`)."""
    return normalize_fills(addr, fills, sz_decimals=sz_decimals).legs


def normalize_fills(addr: str, fills: Iterable[Mapping], *,
                    sz_decimals: SzDecimals = None) -> Normalized:
    """Ordered perp legs of ``addr`` with stable position ids (ruling P22).

    - ``{coin}:{tid}``: an episode opened (from flat) by fill ``tid``;
    - ``{coin}:{tid}f``: the open leg of flip fill ``tid``;
    - ``{coin}:pre{tid}``: history starts mid-position at fill ``tid``;
    - ``{coin}:gap{tid}``: fill ``tid`` does not start where the coin's previous fill
      ended (a missing fill); logged and counted in ``chain_gaps``. A gap fill with
      ``startPosition == 0`` gets the plain ``{coin}:{tid}`` (ruling P25) but is still
      counted in ``chain_gaps``.
    Ids depend only on the fills of the episode itself, so they are stable across
    window shifts and re-normalization. ``sz_decimals`` sets the per-coin chain
    tolerance (ruling P28): starts / ends are snapped to the previous end or to 0.
    """
    # a same-ms group with no full chain always leaves >= 1 break in its fallback order,
    # which the per-fill check below counts; _order only logs it
    ordered, _ = _order(fills, sz_decimals)
    gaps = 0
    legs: list[Leg] = []
    cur: dict[str, str | None] = {}  # coin -> open episode id (None when flat)
    opener: dict[str, tuple] = {}  # coin -> (episode id, oid of its opening fill), P69
    last_end: dict[str, Decimal] = {}
    for f in ordered:
        coin, tid = f["coin"], f["tid"]
        s = _start(f)
        tol = chain_tol(coin, sz_decimals, max(abs(s), abs(last_end.get(coin, ZERO))))
        if coin in last_end and _near(s, last_end[coin], tol):
            s = last_end[coin]  # float noise in startPosition (ruling P28)
        elif _near(s, ZERO, tol):
            s = ZERO
        d = signed_delta(f)
        e = s + d
        if _near(e, ZERO, tol):
            e = ZERO
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
        oid = f.get("oid")

        def leg(side, direction, size, fee_, cpnl_, pid_, st, en, liq_, suffix="") -> Leg:
            if side == "open":
                opener[coin] = (pid_, oid)
            late = side == "increase" and oid is not None and opener.get(coin) == (pid_, oid)
            return Leg(f, coin, side, direction, size, px, fee_, cpnl_, pid_, st, en, liq_,
                       suffix, late)

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


# -- ruling P65: one leader order = one event --------------------------------------------

AGG_WINDOW_S = 2.0  # a piece joins its order if observed at most this long after the last


def order_key(lg: Leg) -> tuple:
    """Legs with the same key are pieces of ONE leader order on ONE position: the same
    coin and episode (so the same direction), both growing or both shrinking it, and
    the same HL ``oid``. A fill without ``oid`` falls back to its millisecond: the same
    coin and direction in the same ms."""
    oid = lg.fill.get("oid")
    order = ("oid", str(oid)) if oid is not None else ("ms", lg.time_ms)
    return (lg.coin, lg.position_id, lg.side in ("open", "increase"), order)


@dataclass(frozen=True)
class OrderLegs:
    """The legs of one leader order on one position, in execution order."""

    legs: tuple[Leg, ...]

    @property
    def coin(self) -> str:
        return self.legs[0].coin

    @property
    def position_id(self) -> str:
        return self.legs[0].position_id

    @property
    def direction(self) -> str:
        return self.legs[0].direction

    @property
    def side(self) -> str:
        """open / increase by the first leg; reduce, or close when the last leg ends
        the position. ``adjust`` (P69) when the first leg is a late piece of the order
        that opened the position."""
        first, last = self.legs[0].side, self.legs[-1].side
        if first == "increase" and self.legs[0].open_piece:
            return "adjust"
        return first if first in ("open", "increase") else last

    @property
    def size(self) -> Decimal:
        return sum((lg.size for lg in self.legs), ZERO)

    @property
    def notional(self) -> Decimal:
        return sum((lg.size * lg.price for lg in self.legs), ZERO)

    @property
    def price(self) -> Decimal:
        """The leader's size-weighted average price (one leg: its own price)."""
        return self.legs[0].price if len(self.legs) == 1 else self.notional / self.size

    @property
    def fee(self) -> Decimal:
        return sum((lg.fee for lg in self.legs), ZERO)

    @property
    def closed_pnl(self) -> Decimal:
        return sum((lg.closed_pnl for lg in self.legs), ZERO)

    @property
    def start(self) -> Decimal:
        return self.legs[0].start

    @property
    def end(self) -> Decimal:
        return self.legs[-1].end

    @property
    def time_ms(self) -> int:
        """The leader time of the first piece (when the order started to fill)."""
        return self.legs[0].time_ms

    @property
    def last_time_ms(self) -> int:
        return self.legs[-1].time_ms

    @property
    def source_ref(self) -> str:
        """The first piece's ref: unchanged for a one-piece order, stable for more."""
        return self.legs[0].source_ref

    @property
    def refs(self) -> list[str]:
        return [lg.source_ref for lg in self.legs]

    @property
    def tids(self) -> list[int]:
        return [int(lg.fill["tid"]) for lg in self.legs]

    @property
    def oid(self) -> str | None:
        oid = self.legs[0].fill.get("oid")
        return None if oid is None else str(oid)


def piece_kind(lg: Leg) -> str:
    """``open``: a piece of the order that opened the episode; else ``grow`` /
    ``shrink``."""
    if lg.side == "open" or lg.open_piece:
        return "open"
    return "grow" if lg.side == "increase" else "shrink"


@dataclass
class PendingOrder:
    """The pieces of one leader order seen so far (not yet an event)."""

    key: tuple  # order_key + the caller's ``stale`` flag
    legs: list[Leg]
    observed: datetime  # when the last piece was observed
    n: int  # arrival index of the last piece
    stale: bool = False
    done: bool = False  # complete: it takes no more pieces
    # the live watcher's bookkeeping
    vias: list[str] = field(default_factory=list)
    leverage: Decimal | None = None

    @property
    def position_id(self) -> str:
        return self.legs[0].position_id

    @property
    def kind(self) -> str:
        return piece_kind(self.legs[0])


class OrderBuffer:
    """One wallet's orders that are still filling (rulings P65, P69): a buffer per
    order, so orders that fill in turns do not cut each other short.

    :meth:`add` puts a piece into its order: the pending order with the same
    :func:`order_key` whose last piece was observed at most ``window_s`` before. An
    order is complete (``done``) when a piece of it comes later than that, when
    :meth:`ready` finds it quiet for ``window_s``, or when the piece of ANOTHER order
    arrives on the same position and merging across it would break the position's
    chain: the two go in opposite directions (a sell summed over a buy that came in
    between could look bigger than the position was), or one of them is the order
    that opened the position (nothing may be sized before its open, and the ratio
    moves with it). Two buy orders, or two sell orders, on one position commute and
    stay independent.

    :meth:`ready` returns the complete orders in the simulator's order (last piece
    observed, then arrival). A complete order waits while an order with an earlier
    last piece is still filling, so events never come out against that order.
    """

    def __init__(self, window_s: float = AGG_WINDOW_S) -> None:
        self.window = timedelta(seconds=window_s)
        self.orders: list[PendingOrder] = []  # not yet taken, by first piece
        self._filling: dict[str, list[PendingOrder]] = {}  # position id -> not done
        self._n = 0

    def __bool__(self) -> bool:
        return bool(self.orders)

    def _finish(self, g: PendingOrder) -> None:
        g.done = True
        live = self._filling.get(g.position_id, [])
        if g in live:
            live.remove(g)
            if not live:
                del self._filling[g.position_id]

    def add(self, lg: Leg, at: datetime, *, stale: bool = False) -> PendingOrder:
        """Put the piece ``lg``, observed at ``at``, into its order; returns it."""
        key = (*order_key(lg), stale)
        kind = piece_kind(lg)
        self._n += 1
        hit = None
        for g in list(self._filling.get(lg.position_id, ())):
            if at - g.observed > self.window:
                self._finish(g)  # quiet for longer than the window
            elif g.key == key:
                hit = g
            elif kind == "open" or g.kind != kind:
                self._finish(g)
        if hit is None:
            hit = PendingOrder(key, [], at, self._n, stale)
            self.orders.append(hit)
            self._filling.setdefault(lg.position_id, []).append(hit)
        hit.legs.append(lg)
        hit.observed = max(hit.observed, at)
        hit.n = self._n
        return hit

    def ready(self, now: datetime | None = None, *, force: bool = False) -> list[PendingOrder]:
        """The complete orders that may be emitted now, in emission order; they stay
        in the buffer until :meth:`drop`. An order is also complete when ``force``,
        with a zero window, or when ``now`` is at least the window after its last
        piece."""
        for g in self.orders:
            if not g.done and (force or self.window <= timedelta(0) or (
                    now is not None and now - g.observed >= self.window)):
                self._finish(g)
        out: list[PendingOrder] = []
        for g in sorted(self.orders, key=lambda g: (g.observed, g.n)):
            if not g.done:
                break
            out.append(g)
        return out

    def drop(self, orders: Iterable[PendingOrder]) -> None:
        gone = {id(g) for g in orders}
        self.orders = [g for g in self.orders if id(g) not in gone]


def group_legs(legs: Iterable[Leg], observed: Callable[[Leg], datetime] | None = None,
               window_s: float = AGG_WINDOW_S) -> list[OrderLegs]:
    """The legs merged order by order (:class:`OrderBuffer`; ``observed(leg)``: default
    its leader time), in emission order: by the last piece of each order."""
    buf = OrderBuffer(window_s)
    for lg in legs:
        buf.add(lg, observed(lg) if observed is not None else ms_to_dt(lg.time_ms))
    return [OrderLegs(tuple(g.legs)) for g in buf.ready(force=True)]


def order_event(addr: str, order: OrderLegs, *, observed: datetime,
                leverage: Decimal | None = None, seq: int = 0) -> TradeEvent:
    """The TradeEvent of one leader order (ruling P65). ``observed``: the event's
    observed time; live it is the emit time (ruling P73: when the buffer flushed the
    order), on the historical path the last piece's. ``leader_time`` is the FIRST
    piece's: an order that started before a follow did is not ours to copy."""
    return TradeEvent(
        head=HEAD,
        wallet=addr,
        market_id=order.coin,
        side=order.side,  # type: ignore[arg-type]
        direction=order.direction,  # type: ignore[arg-type]
        size=order.size,
        price=order.price,
        notional_usd=order.notional,
        fee_usd=order.fee,
        leader_time=ms_to_dt(order.time_ms),
        observed_time=observed,
        source_ref=order.source_ref,
        leverage=leverage,
        position_id=order.position_id,
        group_id=None,
        seq=seq,
    )


def fills_to_events(
    addr: str,
    fills: Iterable[Mapping],
    observed_time_fn: Callable[[Mapping], datetime] | None = None,
    *,
    leverage: Mapping[str, Decimal] | None = None,
    sz_decimals: SzDecimals = None,
) -> list[TradeEvent]:
    """Map raw HL fills of ``addr`` to TradeEvents: one event per leader order and
    position (ruling P65, :func:`group_legs`), in the simulator's order (by the last
    piece of each order; ``seq`` in that order). A late piece of the order that opened
    a position is an ``adjust`` event (ruling P69).

    ``observed_time_fn(fill) -> datetime`` gives the observed time; by default it is the
    leader time (historical normalization; the live watcher merges its own way). An
    event's observed time is its last piece's.
    ``leverage``: ``{coin: leverage}``, e.g. :func:`leverage_by_coin` of the wallet's
    clearinghouseState; coins without an entry get ``leverage=None``.
    ``sz_decimals``: ``{coin: szDecimals}`` for the chain tolerance (ruling P28).
    """
    lev = leverage or {}

    def observed(lg: Leg) -> datetime:
        return observed_time_fn(lg.fill) if observed_time_fn else ms_to_dt(lg.time_ms)

    groups = group_legs(fill_legs(addr, fills, sz_decimals=sz_decimals), observed)
    return [order_event(addr, g, observed=observed(g.legs[-1]), leverage=lev.get(g.coin),
                        seq=seq)
            for seq, g in enumerate(groups)]
