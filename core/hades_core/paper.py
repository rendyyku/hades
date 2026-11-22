"""Paper copy simulator (spec section 7 + 12a).

Pure and deterministic: same events, same model, same config -> same result.

Accounting:
- equity = bankroll + realized PnL - fees - holding costs + unrealized PnL of open
  positions.
- Every position is isolated margin: its total price loss (realized + unrealized,
  before fees) is capped at its margin. When an observed price puts the loss at or
  beyond the margin, the position is liquidated.
- long / yes / no profit when their own price rises; short profits when it falls.
  Polymarket NO is its own token priced 0..1, so it behaves like yes on that price.
- Holding cost (HL funding, ...): if the model defines
  ``holding_cost(pos, start, end) -> Decimal``, every open position is charged for the
  interval since its last charge at each processing step (each processed event, which
  is also when marks are taken), the last processed step included, so a close charges
  up to its own time first. Positive is a cost, negative is income. It moves cash
  (equity) immediately and is deducted from the position's ClosedTrade pnl at close.
  It is not part of the isolated-margin cap.

Sizing (ruling P65). One event is one leader ORDER: a head merges the fills of one
order into one event before it gets here.
- open: margin = ``risk_fraction`` (5%) of the current equity, notional = margin x
  min(leader leverage, max leverage), and the copy ratio ``r`` = our qty / the leader's
  size after that opening order is fixed for the life of the position.
- increase: we add ``r`` x the leader's size delta. reduce: we cut the leader's
  fraction (delta / leader size) of OUR actual position, so a reduce can never take
  more than we hold or flip us. reduce / close / resolve are never capped.
- adjust (ruling P69): a late piece of the order that opened the position. No copy
  trade, no fee, never priced so never missed, no closed trade: the leader's size grows
  and ``r`` becomes our qty / the leader's size after that piece. It is listed in
  ``fills`` with size 0 and no price. Later increases scale by the new ``r``.
- hard caps on every margin we add (open or increase), measured against the equity at
  that moment: one position's margin never exceeds ``max_position_margin_fraction``
  (20%) of the equity, and the margin committed over all open positions never exceeds
  ``max_total_margin_fraction`` (100%) of it. The part beyond a cap is not copied: it
  is recorded as a missed fill with ``reason == "cap"`` (its size is the qty not
  copied) and costs nothing; the leader's size is still tracked in full. An open with
  no room at all is not opened and the rest of that episode is ignored. The caps
  hold on the equity left after the fill's own fee. They limit what we ADD; they
  never force a reduce when the equity falls later.
- a missed fill carries a ``reason``: ``"no_price"`` (the model had no price) or
  ``"cap"``.

Event handling:
- Input is de-duplicated by ``source_ref`` (first occurrence in input order wins), then
  sorted by :func:`event_sort_key` = ``(observed_time, seq, source_ref)``: ``seq`` is the
  head's own order of events sharing an observed_time (a flip's close before its open).
- Ignoring is per leader position *episode*: a position the leader opened before
  ``follow_started_at`` (or that we missed / were liquidated out of) is ignored until
  the episode ends: a close/resolve, or a reduce that takes the leader's tracked size
  to 0. Then the key (position_id, or the wallet:market:direction fallback) is free.
- An increase/reduce/close/resolve for a position we never held is a pure no-op: no
  fill, no price observation, no equity point.

Periodic marks (``mark_times``, optional): at each mark time t (>= follow_started_at,
processed before any event observed at or after t) holding costs are accrued to t,
every open position is marked through ``mark`` (which may liquidate it, as at an
event) and an equity point ``(t, equity)`` is appended. This shows unrealized
drawdown between events. Without ``mark_times`` the result is unchanged.

Display point (``display_time``, optional; ruling P54): the equity at one last moment
(the head's unsaved "now") for display only. It is computed after everything else from
``mark`` (else each market's last price) and the holding cost accrued so far, with the
loss of every position capped at its margin, and returned as
``PaperResult.display_point``. It is NOT history: it never liquidates, never charges,
never adds a fill, a closed trade or an ``equity_curve`` point, so the gates (which
read ``equity_curve`` and ``closed_trades``) cannot see it and a later run reproduces
the same history whatever the price was at that moment. It is meant to be at or after
every event and mark time.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from hades_core.events import Direction, Side, TradeEvent, require_utc

ZERO = Decimal("0")
ONE = Decimal("1")

Mark = Callable[[str, datetime], "Decimal | None"]


class ExecutionModel(Protocol):
    max_leverage: Decimal

    def fill_price(self, ev: TradeEvent) -> Decimal | None:
        """Our copy fill price for this event; None means missed (no live price)."""
        ...

    def fee(self, notional: Decimal) -> Decimal: ...

    # Optional, detected with getattr (absent means 0):
    # def holding_cost(self, pos: OpenPosition, start: datetime, end: datetime) -> Decimal
    #     Cost of holding ``pos`` over [start, end]; positive = cost, negative = income.


def event_sort_key(ev: TradeEvent) -> tuple[datetime, int, str]:
    """The simulator's processing order: ``(observed_time, seq, source_ref)``."""
    return (ev.observed_time, ev.seq, ev.source_ref)


@dataclass
class PaperConfig:
    bankroll: Decimal = Decimal("10000")
    risk_fraction: Decimal = Decimal("0.05")
    max_leverage: Decimal = Decimal("10")
    # ruling P65: hard caps on the margin we add, as fractions of the current equity
    max_position_margin_fraction: Decimal = Decimal("0.20")  # one position
    max_total_margin_fraction: Decimal = Decimal("1")  # all open positions together


@dataclass
class PaperFill:
    ev_ref: str
    market_id: str
    side: Side
    direction: Direction
    size: Decimal
    price: Decimal | None
    fee: Decimal
    realized_pnl: Decimal
    missed: bool
    position_id: str | None = None
    at: datetime | None = None
    # why a missed fill was missed: "no_price" | "cap" (P65); None when copied
    reason: str | None = None


@dataclass
class ClosedTrade:
    position_id: str
    market_id: str
    pnl_usd: Decimal  # realized PnL net of every fee and holding cost of the position
    opened_at: datetime
    closed_at: datetime
    group_id: str | None = None  # TradeEvent.group_id of the opening event


@dataclass
class OpenPosition:
    position_id: str
    market_id: str
    direction: Direction
    qty: Decimal
    entry_price: Decimal  # average entry
    margin: Decimal
    leverage: Decimal
    leader_size: Decimal
    # P65: our qty / the leader's size after the opening order; fixed at open, re-based
    # only by a late piece of that order (P69 ``adjust``)
    ratio: Decimal = ZERO


@dataclass
class PaperResult:
    fills: list[PaperFill] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    open_positions: dict[str, OpenPosition] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    # display-only (time, equity) at ``display_time``; never part of ``equity_curve``
    display_point: tuple[datetime, Decimal] | None = None


@dataclass
class _Pos:
    pos: OpenPosition
    opened_at: datetime
    realized: Decimal = ZERO  # realized price PnL from partial reduces (before fees)
    fees: Decimal = ZERO
    holding: Decimal = ZERO  # accumulated holding cost (positive = paid)
    accrued_to: datetime | None = None
    group_id: str | None = None

    def unit_pnl(self, price: Decimal) -> Decimal:
        diff = price - self.pos.entry_price
        return -diff if self.pos.direction == "short" else diff

    def unrealized(self, price: Decimal) -> Decimal:
        """Unrealized price PnL, floored so total loss never exceeds the margin."""
        raw = self.unit_pnl(price) * self.pos.qty
        return max(raw, -self.pos.margin)


def _key(ev: TradeEvent) -> str:
    if ev.position_id is not None:
        return ev.position_id
    return f"{ev.wallet}:{ev.market_id}:{ev.direction}"


def _dedupe(events: Iterable[TradeEvent]) -> list[TradeEvent]:
    """Drop repeated source_refs; the first occurrence in input order wins."""
    seen: set[str] = set()
    out: list[TradeEvent] = []
    for ev in events:
        if ev.source_ref not in seen:
            seen.add(ev.source_ref)
            out.append(ev)
    return out


def simulate(
    events: Iterable[TradeEvent],
    model: ExecutionModel,
    cfg: PaperConfig,
    follow_started_at: datetime,
    mark: Mark | None = None,
    mark_times: Iterable[datetime] | None = None,
    display_time: datetime | None = None,
) -> PaperResult:
    require_utc("follow_started_at", follow_started_at)
    if display_time is not None:
        require_utc("display_time", display_time)
    pending_marks: list[datetime] = []
    for t in mark_times or ():
        require_utc("mark_time", t)
        if t >= follow_started_at:
            pending_marks.append(t)
    pending_marks = sorted(set(pending_marks), reverse=True)  # pop() yields the earliest
    res = PaperResult(equity_curve=[(follow_started_at, cfg.bankroll)])
    book: dict[str, _Pos] = {}
    # Ignored leader episodes -> the leader's tracked size in that episode
    # (None = unknown: the episode then ends only on close/resolve).
    ignored: dict[str, Decimal | None] = {}
    last_price: dict[str, Decimal] = {}
    base = cfg.bankroll  # bankroll + realized - fees - holding costs
    holding_cost = getattr(model, "holding_cost", None)

    def ignore_step(key: str, ev: TradeEvent) -> None:
        """Apply a leader event to an ignored episode; end the episode when it is over."""
        if ev.side in ("close", "resolve"):
            ignored.pop(key, None)
            return
        size = ignored[key]
        if size is None:
            return
        lead = abs(ev.size)
        if ev.side == "reduce":
            size -= lead
            if size <= ZERO:
                ignored.pop(key, None)
                return
        else:  # open / increase / adjust
            size += lead
        ignored[key] = size

    def close_out(key: str, price: Decimal | None, fee: Decimal, ref: str,
                  side: Side, at: datetime, liquidation: bool) -> None:
        nonlocal base
        p = book.pop(key)
        pos = p.pos
        if liquidation:
            price_pnl = -pos.margin
        else:
            assert price is not None
            price_pnl = p.unrealized(price)
        base += price_pnl - fee
        p.fees += fee
        res.fills.append(PaperFill(ref, pos.market_id, side, pos.direction, pos.qty, price,
                                   fee, price_pnl, False, pos.position_id, at))
        res.closed_trades.append(ClosedTrade(pos.position_id, pos.market_id,
                                             p.realized + price_pnl - p.fees - p.holding,
                                             p.opened_at, at, p.group_id))
        if liquidation:
            # The copy is gone; ignore the rest of the leader's episode.
            ignored[key] = pos.leader_size

    def observe(market_id: str, price: Decimal, at: datetime, ref: str) -> None:
        last_price[market_id] = price
        for key in sorted(k for k, p in book.items() if p.pos.market_id == market_id):
            p = book[key]
            if p.unit_pnl(price) * p.pos.qty <= -p.pos.margin:
                close_out(key, price, ZERO, f"liquidation:{ref}", "close", at, True)

    def accrue(at: datetime) -> None:
        nonlocal base
        if holding_cost is None:
            return
        for key in sorted(book):
            p = book[key]
            if p.accrued_to is not None and at > p.accrued_to:
                cost = Decimal(holding_cost(p.pos, p.accrued_to, at))
                base -= cost
                p.holding += cost
                p.accrued_to = at

    def equity() -> Decimal:
        total = base
        for p in book.values():
            px = last_price.get(p.pos.market_id, p.pos.entry_price)
            total += p.unrealized(px)
        return total

    def room(eq: Decimal, held: Decimal) -> Decimal:
        """P65: the margin a position already holding ``held`` may still add."""
        committed = sum((q.pos.margin for q in book.values()), ZERO)
        return max(min(cfg.max_position_margin_fraction * eq - held,
                       cfg.max_total_margin_fraction * eq - committed), ZERO)

    def fit(want: Decimal, held: Decimal, lev: Decimal) -> Decimal:
        """P65: how much of the margin ``want`` fits under both caps. The caps are
        measured against the equity left after this fill's own fee (the fee of a
        smaller add is assumed no larger)."""
        eq = equity()
        margin = min(want, room(eq, held))
        if margin > ZERO:
            fee = model.fee(margin * lev)
            if fee > ZERO:
                margin = min(margin, room(eq - fee, held))
        return margin

    def missed(ev: TradeEvent, size: Decimal, price: Decimal | None, reason: str,
               at: datetime) -> None:
        res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side, ev.direction,
                                   size, price, ZERO, ZERO, True, ev.position_id, at, reason))

    def mark_until(limit: datetime | None) -> None:
        """Process the periodic marks at or before ``limit`` (all when None)."""
        while pending_marks and (limit is None or pending_marks[-1] <= limit):
            t = pending_marks.pop()
            accrue(t)
            if mark is not None:
                for mkt in sorted({p.pos.market_id for p in book.values()}):
                    px = mark(mkt, t)
                    if px is not None:
                        observe(mkt, px, t, f"mark:{t.isoformat()}")
            res.equity_curve.append((t, equity()))

    ordered = sorted(_dedupe(events), key=event_sort_key)
    for ev in ordered:
        mark_until(ev.observed_time)
        key = _key(ev)
        terminal = ev.side in ("close", "resolve")

        if ev.leader_time < follow_started_at and key not in book and key not in ignored:
            # pre-follow episode: the leader's size is known only if we saw its open
            ignored[key] = ZERO if ev.side == "open" else None
        if key in ignored:
            ignore_step(key, ev)
            continue
        if key not in book and ev.side != "open":
            # increase/reduce/close/resolve for a position we never held: pure no-op
            continue

        at = ev.observed_time
        accrue(at)
        # Mark open positions to the latest recorded prices before acting.
        if mark is not None:
            for mkt in sorted({p.pos.market_id for p in book.values()}):
                px = mark(mkt, at)
                if px is not None:
                    observe(mkt, px, at, ev.source_ref)
        if key in ignored:  # our copy was just liquidated by a mark
            ignore_step(key, ev)
            res.equity_curve.append((at, equity()))
            continue

        lead = abs(ev.size)
        if ev.side == "adjust":
            # P69: the opening order filled some more. Not a trade: no price, no fee.
            pos = book[key].pos
            pos.leader_size += lead
            if pos.leader_size > ZERO:
                pos.ratio = pos.qty / pos.leader_size
            res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side, ev.direction,
                                       ZERO, None, ZERO, ZERO, False, key, at))
            res.equity_curve.append((at, equity()))
            continue
        price = model.fill_price(ev)
        if price is None:
            missed(ev, ZERO, None, "no_price", at)
            p = book.get(key)
            if p is None:  # missed open: never opened, ignore the rest of this episode
                ignored[key] = lead
            else:
                # leader moved without us: keep tracking leader size, our qty is unchanged
                if ev.side in ("increase", "open"):
                    p.pos.leader_size += lead
                elif ev.side == "reduce":
                    p.pos.leader_size = max(p.pos.leader_size - lead, ZERO)
                else:
                    p.pos.leader_size = ZERO
            res.equity_curve.append((at, equity()))
            continue

        observe(ev.market_id, price, at, ev.source_ref)
        if key in ignored:  # liquidated by this very fill price
            ignore_step(key, ev)
            res.equity_curve.append((at, equity()))
            continue
        p = book.get(key)

        if p is None:  # open
            eq = equity()
            want = cfg.risk_fraction * eq
            if want > ZERO and price > ZERO:
                lev = min(ev.leverage or ONE, cfg.max_leverage, model.max_leverage)
                margin = fit(want, ZERO, lev)
                if margin > ZERO:
                    qty = margin * lev / price
                    fee = model.fee(qty * price)
                    base -= fee
                    book[key] = _Pos(
                        OpenPosition(key, ev.market_id, ev.direction, qty, price, margin, lev,
                                     lead, qty / lead if lead > ZERO else ZERO),
                        opened_at=at, fees=fee, accrued_to=at, group_id=ev.group_id)
                    res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side,
                                               ev.direction, qty, price, fee, ZERO, False,
                                               key, at))
                else:  # no room at all: never opened, ignore the rest of this episode
                    ignored[key] = lead
                if margin < want:
                    missed(ev, (want - margin) * lev / price, price, "cap", at)
            else:
                ignored[key] = lead
        elif terminal:
            close_out(key, price, model.fee(p.pos.qty * price), ev.source_ref, ev.side, at, False)
        elif ev.side in ("increase", "open"):
            pos = p.pos
            # P65: the ratio fixed at open (a position opened on a zero-size event has
            # none: fall back to our qty / the leader's size so far)
            ratio = pos.ratio
            if ratio <= ZERO and pos.leader_size > ZERO:
                ratio = pos.qty / pos.leader_size
            want = ratio * lead
            if want > ZERO and price > ZERO:
                want_margin = want * price / pos.leverage
                margin = fit(want_margin, pos.margin, pos.leverage)
                add = want if margin == want_margin else margin * pos.leverage / price
                if add > ZERO:
                    fee = model.fee(add * price)
                    new_qty = pos.qty + add
                    pos.entry_price = (pos.entry_price * pos.qty + price * add) / new_qty
                    pos.qty = new_qty
                    pos.margin += margin
                    p.fees += fee
                    base -= fee
                    res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side,
                                               ev.direction, add, price, fee, ZERO, False,
                                               key, at))
                if add < want:  # the part beyond a cap is not copied
                    missed(ev, want - add, price, "cap", at)
            pos.leader_size += lead
        else:  # reduce
            pos = p.pos
            frac = ONE if pos.leader_size <= ZERO else min(lead / pos.leader_size, ONE)
            if frac >= ONE:
                close_out(key, price, model.fee(pos.qty * price), ev.source_ref, ev.side, at, False)
            else:
                cut = pos.qty * frac
                fee = model.fee(cut * price)
                pnl = max(p.unit_pnl(price) * cut, -pos.margin * frac)
                pos.qty -= cut
                pos.margin -= pos.margin * frac
                pos.leader_size -= lead
                p.realized += pnl
                p.fees += fee
                base += pnl - fee
                res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side, ev.direction,
                                           cut, price, fee, pnl, False, key, at))

        res.equity_curve.append((at, equity()))

    mark_until(None)
    if display_time is not None and display_time >= follow_started_at:
        # P54: read-only. No observe() (so no liquidation), no accrue(): nothing here
        # changes the book, the fills, the closed trades or the equity curve.
        total = base
        for key in sorted(book):
            p = book[key]
            pending = holding_cost is not None and p.accrued_to is not None
            if pending and display_time > p.accrued_to:
                total -= Decimal(holding_cost(p.pos, p.accrued_to, display_time))
            px = mark(p.pos.market_id, display_time) if mark is not None else None
            if px is None:
                px = last_price.get(p.pos.market_id, p.pos.entry_price)
            total += p.unrealized(px)
        res.display_point = (display_time, total)
    res.open_positions = {k: book[k].pos for k in sorted(book)}
    return res
