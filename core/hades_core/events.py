"""Universal trade event: every head translates its world into TradeEvents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

Side = Literal["open", "increase", "reduce", "close", "resolve"]
Direction = Literal["long", "short", "yes", "no"]


def require_utc(name: str, value: datetime) -> None:
    """Raise ValueError unless ``value`` is a tz-aware datetime at UTC offset 0."""
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be tz-aware UTC, got naive datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be UTC, got offset {value.utcoffset()}")


@dataclass(frozen=True)
class TradeEvent:
    """One leader fill, as observed by a head.

    - ``size``: the leader's absolute size delta of *this* fill (not the resulting
      position size), always >= 0 in meaning; the simulator uses ``abs(size)``.
    - ``price``: the leader's own fill price. Informational only: the paper copy never
      fills at it, the ExecutionModel supplies our price.
    - ``position_id``: groups every event of one leader position episode (open ..
      close/resolve). When None the simulator keys by ``wallet:market_id:direction``.
    - ``group_id``: optional wider grouping for gate concentration checks (Polymarket:
      the event/condition id spanning several outcome tokens).
    - ``seq``: ordering tie-break inside one ``observed_time`` (the head's true order of
      same-time events); default 0.
    """

    head: str
    wallet: str
    market_id: str
    side: Side
    direction: Direction
    size: Decimal
    price: Decimal
    notional_usd: Decimal
    fee_usd: Decimal
    leader_time: datetime
    observed_time: datetime
    source_ref: str
    leverage: Decimal | None = None
    # Groups every event of one leader position (open .. close/resolve).
    position_id: str | None = None
    # Wider concentration group (Polymarket event/condition id); see GateConfig.
    group_id: str | None = None
    # Tie-break for events with the same observed_time: the head's execution order
    # (e.g. a flip's close leg before its open leg). The simulator sorts by
    # (observed_time, seq, source_ref).
    seq: int = 0

    def __post_init__(self) -> None:
        require_utc("leader_time", self.leader_time)
        require_utc("observed_time", self.observed_time)
