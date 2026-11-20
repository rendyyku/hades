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
RENT_SIZED_SOL = 0.01  # below this, SOL movement next to a stable leg is rent/fees
RENT_ONLY_MAX_SOL = 0.0025  # R12: a buy spending less than this is a rent-only token receipt


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
    """Net SOL change of the wallet excluding the tx fee, incl. WSOL (ruling R7).

    sol_net = (nativeBalanceChange[wallet] + fee) / 1e9
              + WSOL token net of the wallet
              - native net flowing from the wallet's own WSOL token accounts

    The last term removes wrap/unwrap lamports: when the wallet funds or closes
    its own WSOL account inside the swap, nativeBalanceChange already contains
    those lamports and the WSOL token leg would otherwise count them twice.
    """
    lamports = 0
    for acc in tx.get("accountData") or []:
        if acc.get("account") == wallet:
            lamports += int(acc.get("nativeBalanceChange") or 0)
    lamports += int(tx.get("fee") or 0)  # add the fee back so it is excluded

    wsol = 0.0
    own_wsol_accounts: set[str] = set()
    for tt in tx.get("tokenTransfers") or []:
        if tt.get("mint") != WSOL:
            continue
        amount = float(tt.get("tokenAmount") or 0)
        if tt.get("toUserAccount") == wallet:
            wsol += amount
            if tt.get("toTokenAccount"):
                own_wsol_accounts.add(tt["toTokenAccount"])
        if tt.get("fromUserAccount") == wallet:
            wsol -= amount
            if tt.get("fromTokenAccount"):
                own_wsol_accounts.add(tt["fromTokenAccount"])
    own_wsol_accounts.discard(wallet)

    wrap_lamports = 0  # net lamports own WSOL accounts -> wallet
    for nt in tx.get("nativeTransfers") or []:
        amount = int(nt.get("amount") or 0)
        src, dst = nt.get("fromUserAccount"), nt.get("toUserAccount")
        if src in own_wsol_accounts and dst == wallet:
            wrap_lamports += amount
        elif src == wallet and dst in own_wsol_accounts:
            wrap_lamports -= amount

    return (lamports - wrap_lamports) / LAMPORTS_PER_SOL + wsol


def _moves_for_wallet(tx: dict[str, Any], wallet: str, mints: frozenset[str]) -> bool:
    for tt in tx.get("tokenTransfers") or []:
        if tt.get("mint") in mints and wallet in (tt.get("toUserAccount"), tt.get("fromUserAccount")):
            if float(tt.get("tokenAmount") or 0) > 0:
                return True
    return False


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
    is_buy = next(iter(legs.values())) > 0  # only meaningful for a single leg
    if len(legs) > 1:
        # Several tokens moved in one tx (route/bundle): the SOL cannot be
        # attributed per leg, so no leg may feed SOL PnL.
        sol_amount = None
    elif abs(sol_net) < EPS_SOL:
        # No SOL moved: stable-quoted or token-to-token swap. Not SOL-quoted,
        # so it must not feed SOL PnL.
        sol_amount = None
    elif (sol_net > 0) == is_buy:
        # R14: a buy must spend SOL (sol_net < 0) and a sell must receive SOL
        # (sol_net > 0). Tokens and SOL moving the same way (LP withdraw or
        # deposit, receipts) is not a SOL-quoted trade.
        sol_amount = None
    elif is_buy and abs(sol_net) < RENT_ONLY_MAX_SOL:
        # R12: tokens received for only ATA rent (~0.0015-0.002 SOL) is a
        # receipt (transfer/airdrop/LP), not a purchase. Sells are unaffected.
        sol_amount = None
    elif _moves_for_wallet(tx, wallet, STABLES) and (
        not _moves_for_wallet(tx, wallet, frozenset({WSOL})) or abs(sol_net) < RENT_SIZED_SOL
    ):
        # Stable-quoted swap whose only SOL movement is rent (e.g. creating
        # the token account); booking it as SOL-quoted would fake a near-zero cost.
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
