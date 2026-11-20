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

    def __post_init__(self) -> None:
        require_utc("leader_time", self.leader_time)
        require_utc("observed_time", self.observed_time)
