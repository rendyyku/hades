"""Head API contract models (spec section 9). Every head serializes these identically."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Ladder = Literal["PAPER", "NOTIF", "COPY"]


class HeadHealth(BaseModel):
    head: str
    ok: bool
    last_ingest_at: datetime | None
    version: str


class WalletRow(BaseModel):
    address: str
    score: float | None
    net_pnl_usd: Decimal
    trades: int
    winrate: float | None
    n: int
    tags: list[str] = Field(default_factory=list)
    is_bot: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _winrate_needs_n(self) -> "WalletRow":
        # Winrate is never shown without N: a winrate over zero samples is a lie.
        if self.winrate is not None and self.n == 0:
            raise ValueError("winrate requires n > 0")
        return self


class PaperSummary(BaseModel):
    address: str
    ladder: Ladder
    equity_usd: Decimal
    net_pnl_usd: Decimal
    max_dd: Decimal
    closed_trades: int
    days_followed: int
    gap_usd: Decimal
    origin: Literal["auto", "manual"]


class Alert(BaseModel):
    id: int
    head: str
    kind: str
    wallet: str
    market_id: str | None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
