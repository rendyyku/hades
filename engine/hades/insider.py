"""Insider detection (Styx & Tartarus).

Tags wallets that are likely insiders of a token:

- ``sniper``        buy within ``SNIPER_MAX_SLOTS`` slots of pool creation.
- ``bundle``        >= ``BUNDLE_MIN_WALLETS`` distinct wallets buying in the same
                    slot, funded by the same (non-hub) wallet.
- ``deployer_link`` buyer's funding chain (<= 2 hops) reaches the deployer, or
                    shares a non-hub funder with the deployer's chain.
- ``repeat_early``  wallet among the first ``REPEAT_EARLY_TOP_N`` buyers of
                    >= ``REPEAT_EARLY_MIN_RUNNERS`` runners.

Every tag carries evidence with non-empty, equal-length ``signatures`` and
``slots`` lists; :func:`persist_tags` refuses anything else. Funding hops are
backed by ``Wallet.funding_signature``/``funding_slot`` so evidence survives
across processes. Nothing in this module commits except :func:`tag_token`.
"""

from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from hades.db import Swap, Token, Wallet, WalletTag

FUNDER_SCAN_MAX_PAGES = 3
HUB_SIG_THRESHOLD = 3000
REPEAT_EARLY_TOP_N = 50
REPEAT_EARLY_MIN_RUNNERS = 3
SNIPER_MAX_SLOTS = 2
BUNDLE_MIN_WALLETS = 3

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tag:
    address: str
    tag: str
    mint: str | None
    evidence: dict = field(default_factory=dict)


# -- in-process caches (per helius client) -------------------------------------


@dataclass
class _ClientCache:
    not_hub: set[str] = field(default_factory=set)
    # address -> (funder, sig, slot) | None, as returned by first_incoming_sol
    funding: dict[str, tuple[str, str, int] | None] = field(default_factory=dict)


_CACHES: "weakref.WeakKeyDictionary[Any, _ClientCache]" = weakref.WeakKeyDictionary()


def _cache(helius: Any) -> _ClientCache:
    c = _CACHES.get(helius)
    if c is None:
        c = _ClientCache()
        _CACHES[helius] = c
    return c


def _wallet(session: Session, address: str) -> Wallet:
    w = session.get(Wallet, address)
    if w is None:
        w = Wallet(address=address)
        session.add(w)
    return w


# -- hubs & funder tracing -----------------------------------------------------


async def is_hub(session: Session, helius: Any, address: str) -> bool:
    """True if ``address`` is a high-traffic hub (exchange, router...).

    ``Wallet.label == 'hub'`` short-circuits. Otherwise the address is a hub if
    it has >= HUB_SIG_THRESHOLD signatures within FUNDER_SCAN_MAX_PAGES pages; a
    positive result is persisted as a label, a negative one cached in-process.
    """
    w = session.get(Wallet, address)
    if w is not None and w.label == "hub":
        return True
    cache = _cache(helius)
    if address in cache.not_hub:
        return False
    sigs = await helius.signatures_oldest(address, max_pages=FUNDER_SCAN_MAX_PAGES)
    hub = len(sigs) >= HUB_SIG_THRESHOLD
    if hub:
        _wallet(session, address).label = "hub"
    else:
        cache.not_hub.add(address)
    return hub


async def trace_funder_detailed(
    session: Session, helius: Any, address: str, hops: int = 2
) -> tuple[list[str], list[str], list[int]]:
    """Follow first-SOL-funder links up to ``hops`` hops.

    Returns ``(chain, funding_sigs, funding_slots)`` where ``chain`` is
    ``[address, f1, f2, ...]`` and ``funding_sigs[i]``/``funding_slots[i]``
    describe the transfer ``chain[i+1] -> chain[i]``.

    A hop is read from ``Wallet`` (``first_funder`` + ``funding_signature`` +
    ``funding_slot``) when all are stored, else from this process's memo, else
    fetched via ``first_incoming_sol`` and stored on ``Wallet``. A legacy row
    with ``first_funder`` but no ``funding_signature`` is re-fetched. Every
    returned hop has a real signature/slot. Stops on no funder, or after
    appending a hub. Never commits.
    """
    cache = _cache(helius)
    chain = [address]
    sigs: list[str] = []
    slots: list[int] = []
    cur = address
    for _ in range(hops):
        w = session.get(Wallet, cur)
        if (
            w is not None and w.first_funder and w.funding_signature
            and w.funding_slot is not None
        ):
            funder, sig, slot = w.first_funder, w.funding_signature, w.funding_slot
        else:
            if cur in cache.funding:
                res = cache.funding[cur]
            else:
                res = await helius.first_incoming_sol(cur, max_pages=FUNDER_SCAN_MAX_PAGES)
                cache.funding[cur] = res
            if res is None or not res[0] or not res[1] or res[2] is None:
                break
            funder, sig, slot = res
            w = _wallet(session, cur)
            w.first_funder, w.funding_signature, w.funding_slot = funder, sig, int(slot)
        chain.append(funder)
        sigs.append(sig)
        slots.append(int(slot))
        if await is_hub(session, helius, funder):
            break
        cur = funder
    return chain, sigs, slots


async def trace_funder(session: Session, helius: Any, address: str, hops: int = 2) -> list[str]:
    chain, _, _ = await trace_funder_detailed(session, helius, address, hops=hops)
    return chain


# -- detectors -----------------------------------------------------------------


def _buys(token: Token, swaps: Iterable[Swap]) -> list[Swap]:
    return sorted(
        (s for s in swaps if s.mint == token.mint and s.side == "buy"),
        key=lambda s: (s.slot, s.signature),
    )


def detect_snipers(token: Token, swaps: Iterable[Swap]) -> list[Tag]:
    if token.pool_created_slot is None:
        return []
    seen: set[str] = set()
    tags: list[Tag] = []
    for s in _buys(token, swaps):
        delta = s.slot - token.pool_created_slot
        if not (0 <= delta <= SNIPER_MAX_SLOTS) or s.wallet in seen:
            continue
        seen.add(s.wallet)
        tags.append(
            Tag(s.wallet, "sniper", token.mint,
                {"signatures": [s.signature], "slots": [s.slot], "delta_slots": delta})
        )
    return tags


def detect_bundles(
    token: Token,
    swaps: Iterable[Swap],
    funders: dict[str, str],
    hubs: set[str],
    funding: dict[str, tuple[str, int]] | None = None,
) -> list[Tag]:
    """Same-slot buys by >= BUNDLE_MIN_WALLETS wallets sharing a non-hub funder.

    ``funding[wallet] = (sig, slot)`` is the funder -> wallet transfer. A wallet
    without a known funding signature is excluded (the shared funder must be
    provable), so evidence is ``[buy sig, funding sig]`` / ``[buy slot, funding slot]``.
    """
    funding = funding or {}
    # slot -> funder -> wallet -> first buy in that slot
    groups: dict[int, dict[str, dict[str, Swap]]] = {}
    for s in _buys(token, swaps):
        f = funders.get(s.wallet)
        if not f or f in hubs or not (funding.get(s.wallet) or ("",))[0]:
            continue
        groups.setdefault(s.slot, {}).setdefault(f, {}).setdefault(s.wallet, s)
    tags: list[Tag] = []
    tagged: set[str] = set()
    for slot in sorted(groups):
        for f, wallets in groups[slot].items():
            if len(wallets) < BUNDLE_MIN_WALLETS:
                continue
            cluster = f"{token.mint[:6]}-{slot}-{f[:6]}"
            for w, s in wallets.items():
                if w in tagged:  # one bundle tag per wallet (earliest slot)
                    continue
                tagged.add(w)
                fsig, fslot = funding[w]
                tags.append(
                    Tag(w, "bundle", token.mint,
                        {"signatures": [s.signature, fsig], "slots": [s.slot, int(fslot)],
                         "cluster": cluster, "funder": f})
                )
    return tags


async def detect_deployer_link(
    session: Session,
    helius: Any,
    token: Token,
    buyers: list[str],
    hubs: set[str] | None = None,
    *,
    swaps: Iterable[Swap] | None = None,
) -> list[Tag]:
    deployer = token.deployer
    if deployer is None:
        return []
    dchain, dsigs, dslots = await trace_funder_detailed(session, helius, deployer, hops=2)
    tags: list[Tag] = []
    seen: set[str] = set()
    for b in buyers:
        if b in seen:
            continue
        seen.add(b)
        if b == deployer:
            own = [s for s in _buys(token, swaps or []) if s.wallet == deployer]
            if not own:  # no buy signature -> no evidence -> no tag
                continue
            ev = {"signatures": [own[0].signature], "slots": [own[0].slot], "chain": [deployer]}
            tags.append(Tag(b, "deployer_link", token.mint, ev))
            continue
        bchain, bsigs, bslots = await trace_funder_detailed(session, helius, b, hops=2)
        if deployer in bchain[1:]:
            i = bchain.index(deployer)
            chain = bchain[: i + 1]
            sigs, slots = bsigs[:i], bslots[:i]
        else:
            shared = None
            for n in bchain[1:]:
                if n not in dchain[1:]:
                    continue
                if hubs is not None:
                    hub = n in hubs
                else:
                    w = session.get(Wallet, n)
                    hub = w is not None and w.label == "hub"
                if not hub:
                    shared = n
                    break
            if shared is None:
                continue
            bi, di = bchain.index(shared), dchain.index(shared)
            chain = bchain[: bi + 1] + list(reversed(dchain[:di]))
            sigs, slots = bsigs[:bi] + dsigs[:di], bslots[:bi] + dslots[:di]
        tags.append(
            Tag(b, "deployer_link", token.mint, {"signatures": sigs, "slots": slots, "chain": chain})
        )
    return tags


def tag_repeat_early(session: Session) -> list[Tag]:
    """Wallets in the first REPEAT_EARLY_TOP_N buyers of >= REPEAT_EARLY_MIN_RUNNERS runners.

    Not persisted here.
    """
    runners = session.scalars(
        select(Token.mint).where(Token.is_runner.is_(True)).order_by(Token.mint)
    ).all()
    hits: dict[str, list[tuple[str, str, int]]] = {}
    for mint in runners:
        rows = session.execute(
            select(Swap.wallet, Swap.signature, Swap.slot)
            .where(Swap.mint == mint, Swap.side == "buy")
            .order_by(Swap.slot, Swap.signature)
        ).all()
        seen: set[str] = set()
        for wallet, sig, slot in rows:
            if wallet in seen:
                continue
            seen.add(wallet)
            hits.setdefault(wallet, []).append((mint, sig, slot))
            if len(seen) >= REPEAT_EARLY_TOP_N:
                break
    tags: list[Tag] = []
    for wallet in sorted(hits):
        h = hits[wallet]
        if len(h) < REPEAT_EARLY_MIN_RUNNERS:
            continue
        tags.append(
            Tag(wallet, "repeat_early", None,
                {"signatures": [x[1] for x in h], "slots": [x[2] for x in h],
                 "mints": [x[0] for x in h]})
        )
    return tags


# -- persistence & orchestration -----------------------------------------------


def _valid_evidence(ev: dict) -> bool:
    sigs, slots = ev.get("signatures"), ev.get("slots")
    return (
        isinstance(sigs, list) and isinstance(slots, list) and len(sigs) > 0
        and len(sigs) == len(slots) and all(isinstance(g, str) and g for g in sigs)
    )


def persist_tags(session: Session, tags: Iterable[Tag]) -> int:
    """Replace WalletTag rows keyed by (address, tag, mint). Never commits.

    Refuses (skips with a warning, leaving any existing row untouched) a tag
    whose evidence ``signatures`` is missing/empty, contains an empty string, or
    differs in length from ``slots``. Returns the number of rows inserted.
    """
    accepted: list[Tag] = []
    for t in tags:
        if _valid_evidence(t.evidence):
            accepted.append(t)
        else:
            log.warning("refusing %s tag for %s (mint %s): bad evidence %r",
                        t.tag, t.address, t.mint, t.evidence)
    tags = accepted
    keys = {(t.address, t.tag, t.mint) for t in tags}
    for address, tag, mint in keys:
        mint_cond = WalletTag.mint.is_(None) if mint is None else WalletTag.mint == mint
        session.execute(
            delete(WalletTag).where(WalletTag.address == address, WalletTag.tag == tag, mint_cond)
        )
    for t in tags:
        _wallet(session, t.address)
        session.add(WalletTag(address=t.address, tag=t.tag, mint=t.mint, evidence_json=dict(t.evidence)))
    session.flush()
    return len(tags)


async def tag_token(session: Session, helius: Any, token: Token) -> list[Tag]:
    swaps = session.scalars(
        select(Swap)
        .where(Swap.mint == token.mint, Swap.side == "buy")
        .order_by(Swap.slot, Swap.signature)
    ).all()
    buyers = list(dict.fromkeys(s.wallet for s in swaps))

    tags = detect_snipers(token, swaps)

    funders: dict[str, str] = {}
    funding: dict[str, tuple[str, int]] = {}
    hubs: set[str] = set()
    for b in buyers:
        chain, fsigs, fslots = await trace_funder_detailed(session, helius, b, hops=2)
        if len(chain) > 1:
            funders[b] = chain[1]
            funding[b] = (fsigs[0], fslots[0])
        for n in chain[1:]:
            w = session.get(Wallet, n)
            if w is not None and w.label == "hub":
                hubs.add(n)

    tags += detect_bundles(token, swaps, funders, hubs, funding)
    tags += await detect_deployer_link(session, helius, token, buyers, hubs, swaps=swaps)
    persist_tags(session, tags)
    session.commit()
    return tags
