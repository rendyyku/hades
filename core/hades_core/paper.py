"""Paper copy simulator (spec section 7 + 12a).

Pure and deterministic: same events, same model, same config -> same result.

Accounting:
- equity = bankroll + realized PnL - fees + unrealized PnL of open positions.
- Every position is isolated margin: its total loss (realized + unrealized, before
  fees) is capped at its margin. When an observed price puts the loss at or beyond
  the margin, the position is liquidated.
- long / yes / no profit when their own price rises; short profits when it falls.
  Polymarket NO is its own token priced 0..1, so it behaves like yes on that price.
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


@dataclass
class PaperConfig:
    bankroll: Decimal = Decimal("10000")
    risk_fraction: Decimal = Decimal("0.05")
    max_leverage: Decimal = Decimal("10")


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


@dataclass
class ClosedTrade:
    position_id: str
    market_id: str
    pnl_usd: Decimal  # realized PnL net of every fee paid on the position
    opened_at: datetime
    closed_at: datetime


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


@dataclass
class PaperResult:
    fills: list[PaperFill] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    open_positions: dict[str, OpenPosition] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)


@dataclass
class _Pos:
    pos: OpenPosition
    opened_at: datetime
    realized: Decimal = ZERO  # realized price PnL from partial reduces (before fees)
    fees: Decimal = ZERO

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


def simulate(
    events: Iterable[TradeEvent],
    model: ExecutionModel,
    cfg: PaperConfig,
    follow_started_at: datetime,
    mark: Mark | None = None,
) -> PaperResult:
    require_utc("follow_started_at", follow_started_at)
    res = PaperResult(equity_curve=[(follow_started_at, cfg.bankroll)])
    book: dict[str, _Pos] = {}
    ignored: set[str] = set()
    last_price: dict[str, Decimal] = {}
    base = cfg.bankroll  # bankroll + realized - fees of closed/partial activity

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
                                             p.realized + price_pnl - p.fees, p.opened_at, at))
        if liquidation:
            # The copy is gone; ignore whatever the leader does with this position next.
            ignored.add(key)

    def observe(market_id: str, price: Decimal, at: datetime, ref: str) -> None:
        last_price[market_id] = price
        for key in sorted(k for k, p in book.items() if p.pos.market_id == market_id):
            p = book[key]
            if p.unit_pnl(price) * p.pos.qty <= -p.pos.margin:
                close_out(key, price, ZERO, f"liquidation:{ref}", "close", at, True)

    def equity() -> Decimal:
        total = base
        for p in book.values():
            px = last_price.get(p.pos.market_id, p.pos.entry_price)
            total += p.unrealized(px)
        return total

    ordered = sorted(events, key=lambda e: (e.observed_time, e.source_ref))
    for ev in ordered:
        key = _key(ev)
        terminal = ev.side in ("close", "resolve")

        if ev.leader_time < follow_started_at:
            if key not in book:
                ignored.add(key)
        if key in ignored:
            if terminal and ev.leader_time >= follow_started_at:
                # leader's pre-follow / uncopied position ended; the key may be reused
                ignored.discard(key)
            continue

        at = ev.observed_time
        # Mark open positions to the latest recorded prices before acting.
        if mark is not None:
            for mkt in sorted({p.pos.market_id for p in book.values()}):
                px = mark(mkt, at)
                if px is not None:
                    observe(mkt, px, at, ev.source_ref)

        price = model.fill_price(ev)
        if price is None:
            res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side, ev.direction,
                                       ZERO, None, ZERO, ZERO, True, ev.position_id, at))
            p = book.get(key)
            if ev.side == "open" and p is None:
                ignored.add(key)  # never opened: ignore the rest of this position
            elif p is not None:
                # leader moved without us: keep tracking leader size, our qty is unchanged
                lead = abs(ev.size)
                if ev.side == "increase" or ev.side == "open":
                    p.pos.leader_size += lead
                elif ev.side == "reduce":
                    p.pos.leader_size = max(p.pos.leader_size - lead, ZERO)
                else:
                    p.pos.leader_size = ZERO
            res.equity_curve.append((at, equity()))
            continue

        observe(ev.market_id, price, at, ev.source_ref)
        p = book.get(key)
        lead = abs(ev.size)

        if p is None and ev.side == "open":
            eq = equity()
            margin = cfg.risk_fraction * eq
            if margin > ZERO and price > ZERO:
                lev = min(ev.leverage or ONE, cfg.max_leverage, model.max_leverage)
                qty = margin * lev / price
                fee = model.fee(qty * price)
                base -= fee
                book[key] = _Pos(
                    OpenPosition(key, ev.market_id, ev.direction, qty, price, margin, lev, lead),
                    opened_at=at, fees=fee)
                res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side, ev.direction,
                                           qty, price, fee, ZERO, False, key, at))
            else:
                ignored.add(key)
        elif p is None:
            # increase/reduce/close for a position we never held (e.g. liquidated
            # then key reused, or stray event): nothing to copy.
            pass
        elif terminal:
            close_out(key, price, model.fee(p.pos.qty * price), ev.source_ref, ev.side, at, False)
        elif ev.side in ("increase", "open"):
            pos = p.pos
            if pos.leader_size > ZERO:
                add = pos.qty * lead / pos.leader_size
                fee = model.fee(add * price)
                new_qty = pos.qty + add
                pos.entry_price = (pos.entry_price * pos.qty + price * add) / new_qty
                pos.qty = new_qty
                pos.margin += add * price / pos.leverage
                p.fees += fee
                base -= fee
                res.fills.append(PaperFill(ev.source_ref, ev.market_id, ev.side, ev.direction,
                                           add, price, fee, ZERO, False, key, at))
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

        if terminal:
            # The leader's position is over (even if our copy was just liquidated by
            # this very price): a later event may legitimately reuse the key.
            ignored.discard(key)
        res.equity_curve.append((at, equity()))

    res.open_positions = {k: book[k].pos for k in sorted(book)}
    return res
