"""Turn Helius Enhanced Transactions into per-wallet swap legs."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY4MCcCX6CsHmwEh"
STABLES = frozenset({USDC, USDT})
QUOTE_MINTS = frozenset({WSOL}) | STABLES

LAMPORTS_PER_SOL = 1_000_000_000
EPS_SOL = 1e-6
EPS_TOKEN = 1e-12


@dataclass(frozen=True)
class ParsedSwap:
    signature: str
    wallet: str
    mint: str
    slot: int
    block_time: datetime
    side: Literal["buy", "sell"]
    token_amount: float
    sol_amount: float | None
    dex: str


def _wallet_sol_net(tx: dict[str, Any], wallet: str) -> float:
    """Net SOL change of the wallet excluding the tx fee (ruling R1), incl. WSOL."""
    lamports = 0
    for acc in tx.get("accountData") or []:
        if acc.get("account") == wallet:
            lamports += int(acc.get("nativeBalanceChange") or 0)
    lamports += int(tx.get("fee") or 0)  # add the fee back so it is excluded
    sol = lamports / LAMPORTS_PER_SOL
    for tt in tx.get("tokenTransfers") or []:
        if tt.get("mint") != WSOL:
            continue
        amount = float(tt.get("tokenAmount") or 0)
        if tt.get("toUserAccount") == wallet:
            sol += amount
        if tt.get("fromUserAccount") == wallet:
            sol -= amount
    return sol


def parse_swaps(tx: dict[str, Any]) -> list[ParsedSwap]:
    if tx.get("transactionError"):
        return []
    wallet = tx.get("feePayer")
    if not wallet:
        return []

    token_net: dict[str, float] = defaultdict(float)
    for tt in tx.get("tokenTransfers") or []:
        mint = tt.get("mint")
        if not mint:
            continue
        if mint in QUOTE_MINTS:
            continue
        amount = float(tt.get("tokenAmount") or 0)
        if tt.get("toUserAccount") == wallet:
            token_net[mint] += amount
        if tt.get("fromUserAccount") == wallet:
            token_net[mint] -= amount

    legs = {m: n for m, n in token_net.items() if abs(n) > EPS_TOKEN}
    if not legs:
        return []

    sol_net = _wallet_sol_net(tx, wallet)
    sol_amount: float | None = abs(sol_net)
    if abs(sol_net) < EPS_SOL:
        # No SOL moved: stable-quoted (USDC/USDT) or token-to-token swap.
        # Either way it is not SOL-quoted, so it must not feed SOL PnL.
        sol_amount = None

    block_time = datetime.fromtimestamp(int(tx["timestamp"]), timezone.utc)
    return [
        ParsedSwap(
            signature=tx["signature"],
            wallet=wallet,
            mint=mint,
            slot=int(tx["slot"]),
            block_time=block_time,
            side="buy" if net > 0 else "sell",
            token_amount=abs(net),
            sol_amount=sol_amount,
            dex=tx.get("source") or "UNKNOWN",
        )
        for mint, net in legs.items()
    ]
