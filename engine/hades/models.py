"""Plain data shapes shared by watchers, pipeline and store."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass(frozen=True)
class Signal:
    source: str
    kind: str
    project_key: str
    evidence_url: str
    observed_at: datetime
    dedupe_key: str
    chain: str | None = None
    address: str | None = None
    symbol: str | None = None
    name: str | None = None
    website: str | None = None
    severity: Literal["high", "low"] = "high"
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_url:
            raise ValueError("Signal needs an evidence_url")
        if self.observed_at.tzinfo is None:
            raise ValueError("Signal.observed_at must be timezone-aware")


@dataclass(frozen=True)
class ListingEvent:
    exchange: str
    symbol: str
    base_asset: str
    first_seen_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WatchResult:
    signals: list[Signal] = field(default_factory=list)
    listings: list[ListingEvent] = field(default_factory=list)
    # None = leave the stored state unchanged
    new_state: dict | None = None
    domains: list[str] = field(default_factory=list)


@dataclass
class Enrichment:
    provider: str
    checked_at: datetime
    buyable: bool | None
    liquidity_usd: float | None = None
    dex: str | None = None
    pool_url: str | None = None
    volume_24h: float | None = None
    pool_count: int = 0
    verified: bool = True
    candidates: list[dict] = field(default_factory=list)
    error: str | None = None
