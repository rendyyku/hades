"""Per-wallet Hyperliquid PnL (spec 8.2).

- Realized = sum of HL's per-fill ``closedPnl``. It is cross-checked against our own
  reconstruction: average-entry accounting rebuilt from ``startPosition`` + size.
  Only fills whose entry we fully observed are compared (a position already open at
  the start of the history has an unknown entry price). The mismatch is
  ``|HL - ours| / sum(|HL closedPnl|)`` over the compared fills, in percent; above
  1% it is logged and flagged (``closedpnl_mismatch``).
- Fees: sum of per-fill ``fee`` (negative = maker rebate).
- Funding: sum of ``userFunding`` ``delta.usdc`` (negative = paid).
- net = realized - fees + funding.
- Liquidations: the number of position episodes in which this wallet was liquidated;
  a liquidated position always counts as a loss.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from hades_hl.normalize import Leg, fill_legs, ms_to_dt

log = logging.getLogger(__name__)

ZERO = Decimal(0)
MISMATCH_TOLERANCE_PCT = Decimal(1)
MISMATCH_MIN_GROSS_USD = Decimal(1)  # below this the ratio is noise: never flag


@dataclass(frozen=True)
class ClosedPosition:
    coin: str
    direction: str
    position_id: str
    open_time: datetime | None  # None: the position was opened before our history
    close_time: datetime
    realized_usd: Decimal
    fees_usd: Decimal
    funding_usd: Decimal
    liquidated: bool
    partial: bool  # opened before the history: opening fees/funding are missing

    @property
    def net_usd(self) -> Decimal:
        return self.realized_usd - self.fees_usd + self.funding_usd

    @property
    def hold_s(self) -> float | None:
        if self.open_time is None:
            return None
        return (self.close_time - self.open_time).total_seconds()

    @property
    def is_win(self) -> bool:
        return not self.liquidated and self.net_usd > 0


@dataclass(frozen=True)
class PnlSummary:
    net_usd: Decimal
    realized_usd: Decimal
    fees_usd: Decimal
    funding_usd: Decimal
    liquidations: int
    closed_positions: list[ClosedPosition] = field(default_factory=list)
    reconstructed_usd: Decimal = ZERO  # our realized over the compared fills
    compared_realized_usd: Decimal = ZERO  # HL closedPnl over the same fills
    compared_fills: int = 0
    mismatch_pct: Decimal | None = None
    closedpnl_mismatch: bool = False


def funding_coin(row: Mapping) -> str:
    return str((row.get("delta") or {}).get("coin", ""))


def funding_usdc(row: Mapping) -> Decimal:
    return Decimal(str((row.get("delta") or {}).get("usdc") or "0"))


@dataclass
class _Episode:
    coin: str
    direction: str
    position_id: str
    start_ms: int
    partial: bool
    end_ms: int | None = None
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    funding: Decimal = ZERO
    liquidated: bool = False


def _episodes(legs: list[Leg]) -> list[_Episode]:
    eps: dict[str, _Episode] = {}
    for lg in legs:
        ep = eps.get(lg.position_id)
        if ep is None:
            ep = eps[lg.position_id] = _Episode(
                lg.coin, lg.direction, lg.position_id, lg.time_ms, partial=lg.start != 0)
        ep.realized += lg.closed_pnl
        ep.fees += lg.fee
        ep.liquidated = ep.liquidated or lg.liquidation
        if lg.end == 0 or lg.side == "close":
            ep.end_ms = lg.time_ms
    return list(eps.values())


def _attribute_funding(eps: list[_Episode], funding: Iterable[Mapping]) -> None:
    by_coin: dict[str, list[_Episode]] = {}
    for ep in eps:
        by_coin.setdefault(ep.coin, []).append(ep)
    for row in funding:
        coin, t = funding_coin(row), int(row["time"])
        cands = by_coin.get(coin) or []
        hit = next((ep for ep in cands
                    if ep.start_ms <= t and (ep.end_ms is None or t <= ep.end_ms)), None)
        if hit is None and cands and cands[0].partial and t < cands[0].start_ms:
            hit = cands[0]  # position held since before our first fill of the coin
        if hit is not None:
            hit.funding += funding_usdc(row)


def _reconstruct(legs: list[Leg]) -> tuple[Decimal, Decimal, Decimal, int]:
    """(ours, hl, hl_gross, n) over legs whose entry price we observed."""
    entry: dict[str, Decimal | None] = {}
    ours = hl = gross = ZERO
    n = 0
    for lg in legs:
        c = lg.coin
        if c not in entry:
            entry[c] = None if lg.start != 0 else ZERO
        cur = entry[c]
        if lg.side in ("open", "increase"):
            if lg.start == 0:
                entry[c] = lg.price
            elif cur is not None:
                entry[c] = (cur * abs(lg.start) + lg.price * lg.size) / abs(lg.end)
        else:  # reduce / close
            if cur is not None:
                sign = 1 if lg.start > 0 else -1
                ours += (lg.price - cur) * lg.size * sign
                hl += lg.closed_pnl
                gross += abs(lg.closed_pnl)
                n += 1
            if lg.end == 0:
                entry[c] = ZERO  # flat: the next open has a known entry
    return ours, hl, gross, n


def wallet_pnl(
    fills: Iterable[Mapping], funding: Iterable[Mapping], *, addr: str | None = None
) -> PnlSummary:
    """PnL of one wallet over the given perp fills and funding rows (spot is ignored).

    ``addr`` lets liquidation fills where this wallet was the liquidator (not the
    victim) be told apart; without it any liquidation fill counts.
    """
    funding = list(funding)
    legs = fill_legs(fills, addr)
    realized = sum((lg.closed_pnl for lg in legs), ZERO)
    fees = sum((lg.fee for lg in legs), ZERO)
    fund = sum((funding_usdc(r) for r in funding), ZERO)

    eps = _episodes(legs)
    _attribute_funding(eps, funding)
    closed = [
        ClosedPosition(ep.coin, ep.direction, ep.position_id,
                       None if ep.partial else ms_to_dt(ep.start_ms), ms_to_dt(ep.end_ms),
                       ep.realized, ep.fees, ep.funding, ep.liquidated, ep.partial)
        for ep in eps if ep.end_ms is not None
    ]
    closed.sort(key=lambda c: c.close_time)

    ours, hl, gross, n = _reconstruct(legs)
    mismatch_pct: Decimal | None = None
    flagged = False
    if n:
        diff = abs(hl - ours)
        mismatch_pct = (diff / gross * 100) if gross else (ZERO if diff == 0 else Decimal(100))
        if mismatch_pct > MISMATCH_TOLERANCE_PCT and (gross >= MISMATCH_MIN_GROSS_USD or diff
                                                      >= MISMATCH_MIN_GROSS_USD):
            flagged = True
            log.warning("hl closedPnl cross-check %s: HL %s vs ours %s over %d fills (%.2f%%)",
                        addr or "?", hl, ours, n, mismatch_pct)

    return PnlSummary(
        net_usd=realized - fees + fund,
        realized_usd=realized,
        fees_usd=fees,
        funding_usd=fund,
        liquidations=sum(1 for ep in eps if ep.liquidated),
        closed_positions=closed,
        reconstructed_usd=ours,
        compared_realized_usd=hl,
        compared_fills=n,
        mismatch_pct=mismatch_pct,
        closedpnl_mismatch=flagged,
    )
