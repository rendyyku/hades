"""Per-wallet Hyperliquid PnL (spec 8.2).

- Realized = sum of HL's per-fill ``closedPnl``. It is cross-checked against our own
  reconstruction: average-entry accounting rebuilt from ``startPosition`` + size.
  Only fills whose entry we fully observed are compared (a position already open at
  the start of the history, or after a chain gap, has an unknown entry price). The
  mismatch is ``sum(|HL - ours| per fill) / sum(|HL closedPnl|)`` over the compared
  fills, in percent (``mismatch_pct``); above 1% it is logged and flagged
  (``closedpnl_mismatch``). The netted ``|sum HL - sum ours| / sum |HL|`` is kept as
  ``aggregate_mismatch_pct`` for information.
  HL computes closedPnl from the average entry *truncated* to the coin's price
  decimals (``6 - szDecimals``); given ``sz_decimals`` (see
  :func:`hades_hl.normalize.sz_decimals_by_coin`) we do the same, which matches the
  real fixture exactly (0.000% vs 1.09% per fill with the untruncated entry).
- Fees: sum of per-fill ``fee`` + ``builderFee`` (negative fee = maker rebate).
- Funding: sum of ``userFunding`` ``delta.usdc`` (negative = paid).
- net = realized - fees + funding.
- Liquidations: the number of position episodes in which this wallet was liquidated
  (partially or fully; in a flip only the closed episode); a liquidated position
  always counts as a loss.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from hades_hl.normalize import Leg, ms_to_dt, normalize_fills

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
    mismatch_pct: Decimal | None = None  # per-fill: sum|HL - ours| / sum|HL|
    closedpnl_mismatch: bool = False
    aggregate_mismatch_pct: Decimal | None = None  # netted: |sum HL - sum ours| / sum|HL|
    chain_gaps: int = 0


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


PERP_PX_DECIMALS = 6


def _reconstruct(
    legs: list[Leg], sz_decimals: Mapping[str, int] | None = None
) -> tuple[Decimal, Decimal, Decimal, Decimal, int]:
    """(ours, hl, sum |per-fill diff|, hl_gross, n) over legs whose entry we observed."""
    entry: dict[str, Decimal | None] = {}
    last_end: dict[str, Decimal] = {}
    ours = hl = absdiff = gross = ZERO
    n = 0
    for lg in legs:
        c = lg.coin
        if c not in last_end or lg.start != last_end[c]:  # history start or chain gap
            entry[c] = ZERO if lg.start == 0 else None
        last_end[c] = lg.end
        cur = entry[c]
        if lg.side in ("open", "increase"):
            if lg.start == 0:
                entry[c] = lg.price
            elif cur is not None:
                entry[c] = (cur * abs(lg.start) + lg.price * lg.size) / abs(lg.end)
        else:  # reduce / close
            if cur is not None:
                if sz_decimals and c in sz_decimals:
                    q = Decimal(1).scaleb(-(PERP_PX_DECIMALS - sz_decimals[c]))
                    cur = cur.quantize(q, rounding=ROUND_DOWN)
                sign = 1 if lg.start > 0 else -1
                mine = (lg.price - cur) * lg.size * sign
                ours += mine
                hl += lg.closed_pnl
                absdiff += abs(lg.closed_pnl - mine)
                gross += abs(lg.closed_pnl)
                n += 1
            if lg.end == 0:
                entry[c] = ZERO  # flat: the next open has a known entry
    return ours, hl, absdiff, gross, n


def _pct(diff: Decimal, gross: Decimal) -> Decimal:
    return (diff / gross * 100) if gross else (ZERO if diff == 0 else Decimal(100))


def wallet_pnl(
    addr: str,
    fills: Iterable[Mapping],
    funding: Iterable[Mapping],
    *,
    sz_decimals: Mapping[str, int] | None = None,
) -> PnlSummary:
    """PnL of wallet ``addr`` over the given perp fills and funding rows (spot ignored).

    ``addr`` tells liquidation fills where this wallet was the victim apart from ones
    where it was the liquidator (only the former count). ``sz_decimals``: optional
    ``{coin: szDecimals}`` for the cross-check's HL-style entry truncation.
    """
    funding = list(funding)
    norm = normalize_fills(addr, fills)
    legs = norm.legs
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

    ours, hl, absdiff, gross, n = _reconstruct(legs, sz_decimals)
    mismatch_pct: Decimal | None = None
    aggregate_pct: Decimal | None = None
    flagged = False
    if n:
        mismatch_pct = _pct(absdiff, gross)
        aggregate_pct = _pct(abs(hl - ours), gross)
        if mismatch_pct > MISMATCH_TOLERANCE_PCT and (gross >= MISMATCH_MIN_GROSS_USD
                                                      or absdiff >= MISMATCH_MIN_GROSS_USD):
            flagged = True
            log.warning("hl closedPnl cross-check %s: HL %s vs ours %s over %d fills "
                        "(per-fill %.2f%%, netted %.2f%%)",
                        addr, hl, ours, n, mismatch_pct, aggregate_pct)

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
        aggregate_mismatch_pct=aggregate_pct,
        chain_gaps=norm.chain_gaps,
    )
