"""Response/request models for the HADES read API.

Datetimes are tz-aware UTC (see ``hades.db.UTCDateTime``) and serialise as ISO 8601.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

BASE58_ADDRESS = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"


class WalletRow(BaseModel):
    address: str
    score: float | None
    total_pnl_sol: float | None
    realized_pnl_sol: float | None
    win_rate: float | None
    tokens_closed: int | None
    tags: list[str]
    is_bot: bool


class Overview(BaseModel):
    tracked_wallets: int
    runner_tokens: int
    alerts_24h: int
    last_swap_at: datetime | None
    top: list[WalletRow]


class WalletList(BaseModel):
    items: list[WalletRow]
    total: int


class WalletStatsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    address: str
    window_days: int | None
    realized_pnl_sol: float | None
    unrealized_pnl_sol: float | None
    total_pnl_sol: float | None
    tokens_traded: int | None
    tokens_closed: int | None
    win_rate: float | None
    median_hold_sec: float | None
    trades_per_day: float | None
    score: float | None
    computed_at: datetime | None


class PositionRow(BaseModel):
    mint: str
    symbol: str | None
    bought_tokens: float
    sold_tokens: float
    cost_sol: float
    proceeds_sol: float
    realized_pnl_sol: float
    remaining_tokens: float
    untracked_sell_tokens: float
    closed: bool
    first_buy_time: datetime | None
    last_sell_time: datetime | None


class TagRow(BaseModel):
    tag: str
    mint: str | None
    evidence: dict[str, Any]
    created_at: datetime | None


class FundingHop(BaseModel):
    address: str
    funding_signature: str | None
    funding_slot: int | None


class PnlPoint(BaseModel):
    t: datetime
    cum_pnl_sol: float


class WalletDetail(BaseModel):
    stats: WalletStatsOut | None
    positions: list[PositionRow]
    tags: list[TagRow]
    funding_chain: list[FundingHop]
    # Per-position granularity: one point per position at its last sell.
    pnl_curve: list[PnlPoint]


class TokenOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mint: str
    symbol: str | None
    name: str | None
    deployer: str | None
    pool_address: str | None
    pool_created_slot: int | None
    pool_created_at: datetime | None
    first_seen_at: datetime | None
    peak_mcap_usd: float | None
    is_runner: bool | None
    # Wallet.first_funder of the deployer (Task 12b, Styx map).
    deployer_funder: str | None = None


class EarlyBuyer(BaseModel):
    address: str
    slot: int
    signature: str
    sol_amount: float | None
    tags: list[str]
    funder: str | None = None  # Wallet.first_funder
    funder_is_hub: bool = False  # funder's Wallet.label == "hub"


class Bundle(BaseModel):
    cluster: str
    funder: str | None
    wallets: list[str]


class TokenForensics(BaseModel):
    token: TokenOut
    early_buyers: list[EarlyBuyer]
    bundles: list[Bundle]


class WatchlistIn(BaseModel):
    address: str = Field(pattern=BASE58_ADDRESS)
    note: str | None = Field(default=None, max_length=280)


class WatchlistRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    address: str
    note: str | None
    added_at: datetime | None


class AlertRow(BaseModel):
    """Payload of the SSE ``alert`` event."""

    id: int
    wallet: str
    mint: str
    symbol: str | None
    side: str
    sol_amount: float | None
    signature: str
    block_time: datetime | None
