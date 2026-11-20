"""Raw Hyperliquid fills -> core TradeEvents.

Ordering: HL ``tid`` is NOT monotonic, even inside one millisecond (verified on the
real fixture: sorting by tid breaks the ``startPosition`` chain on 797 of 1312
fills). The authoritative order of one coin's fills is the ``startPosition`` chain:
each fill starts where the previous one ended. :func:`order_fills` sorts by time
and, inside a millisecond, re-chains each coin's fills by ``startPosition``.

Side/direction come from position arithmetic, not from HL's ``dir`` text (``dir``
says "Open Long" for an increase too): with ``s = startPosition`` and the signed
delta ``d`` (+sz for a buy ``B``, -sz for a sell ``A``), ``e = s + d``:

- ``s == 0``                    -> open (direction by the sign of ``d``)
- same sign, ``|e| > |s|``      -> increase
- same sign, ``0 < |e| < |s|``  -> reduce;  ``e == 0`` -> close
- sign change                   -> flip: close ``|s|`` + open ``|e|`` (two legs)

A fill carrying a ``liquidation`` object of this wallet always maps to ``close``.
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


def is_own_liquidation(fill: Mapping, addr: str | None = None) -> bool:
    """True when the fill liquidated *this* wallet (not one where we were the liquidator).

    Without ``addr`` (or without ``liquidatedUser``) any liquidation fill counts.
    """
    liq = fill.get("liquidation")
    if not liq:
        return False
    victim = liq.get("liquidatedUser") if isinstance(liq, Mapping) else None
    if addr is None or not victim:
        return True
    return str(victim).lower() == addr.lower()


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


def _require_tid(f: Mapping) -> None:
    if f.get("tid") is None:
        raise ValueError(f"HL fill without tid (coin={f.get('coin')}, time={f.get('time')})")


def order_fills(fills: Iterable[Mapping]) -> list[dict]:
    """Perp fills, de-duplicated by tid, in true execution order (see module doc)."""
    seen: set = set()
    uniq: list[dict] = []
    for f in fills:
        _require_tid(f)
        if is_spot(f) or f["tid"] in seen:
            continue
        seen.add(f["tid"])
        uniq.append(dict(f))
    # group by (time, coin), keeping first-seen order of the groups inside a ms
    by_time = sorted(range(len(uniq)), key=lambda i: int(uniq[i]["time"]))
    out: list[dict] = []
    last_end: dict[str, Decimal] = {}
    i = 0
    while i < len(by_time):
        t = int(uniq[by_time[i]]["time"])
        j = i
        while j < len(by_time) and int(uniq[by_time[j]]["time"]) == t:
            j += 1
        groups: dict[str, list[dict]] = {}
        for k in by_time[i:j]:
            groups.setdefault(uniq[k]["coin"], []).append(uniq[k])
        for coin, grp in groups.items():
            for f in _chain(grp, last_end.get(coin)):
                out.append(f)
                last_end[coin] = Decimal(str(f["startPosition"])) + signed_delta(f)
        i = j
    return out


def _chain(grp: list[dict], prev_end: Decimal | None) -> list[dict]:
    """Order one coin's same-ms fills so each starts where the previous ended."""
    if len(grp) == 1:
        return grp
    rest = list(grp)
    starts = [Decimal(str(f["startPosition"])) for f in rest]
    ends = [s + signed_delta(f) for s, f in zip(starts, rest)]
    # first: the fill continuing prev_end, else the one no other fill leads into
    first = None
    if prev_end is not None:
        first = next((k for k, s in enumerate(starts) if s == prev_end), None)
    if first is None:
        first = next((k for k, s in enumerate(starts) if s not in ends), 0)
    order = [first]
    used = {first}
    while len(order) < len(rest):
        cur_end = ends[order[-1]]
        nxt = next((k for k in range(len(rest)) if k not in used and starts[k] == cur_end), None)
        if nxt is None:  # broken chain: keep the remaining fills in input order
            nxt = next(k for k in range(len(rest)) if k not in used)
        order.append(nxt)
        used.add(nxt)
    return [rest[k] for k in order]


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


def fill_legs(fills: Iterable[Mapping], addr: str | None = None) -> list[Leg]:
    """Ordered perp legs with position ids ``{coin}:{n}`` (n bumps when flat)."""
    legs: list[Leg] = []
    episode: dict[str, int] = {}
    for f in order_fills(fills):
        coin = f["coin"]
        n = episode.setdefault(coin, 0)
        s = Decimal(str(f["startPosition"]))
        d = signed_delta(f)
        if d == 0:
            continue
        e = s + d
        px = Decimal(str(f["px"]))
        fee = Decimal(str(f.get("fee") or "0"))
        cpnl = Decimal(str(f.get("closedPnl") or "0"))
        liq = is_own_liquidation(f, addr)
        sz = abs(d)

        def leg(side, direction, size, fee_, cpnl_, pid, st, en, suffix="") -> Leg:
            return Leg(f, coin, side, direction, size, px, fee_, cpnl_, pid, st, en, liq, suffix)

        if s == 0:
            legs.append(leg("open", _dir(d), sz, fee, cpnl, f"{coin}:{n}", s, e))
        elif e != 0 and (e > 0) != (s > 0):  # flip
            close_sz, open_sz = abs(s), abs(e)
            close_fee = fee * close_sz / sz
            legs.append(leg("close", _dir(s), close_sz, close_fee, cpnl, f"{coin}:{n}",
                            s, ZERO, ":close"))
            n += 1
            legs.append(leg("open", _dir(e), open_sz, fee - close_fee, ZERO, f"{coin}:{n}",
                            ZERO, e, ":open"))
        else:
            grows = (d > 0) == (s > 0)
            if liq and not grows:
                side = "close"
            elif grows:
                side = "increase"
            else:
                side = "close" if e == 0 else "reduce"
            legs.append(leg(side, _dir(s), sz, fee, cpnl, f"{coin}:{n}", s, e))
        if e == 0:
            n += 1
        episode[coin] = n
        _check_dir(f, legs[-1])
    return legs


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
    for lg in fill_legs(fills, addr):
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
        ))
    return out
