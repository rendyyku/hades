"""Follows of the Hyperliquid head (spec 12b Q2).

- Auto: the top ``AUTO_FOLLOW_N`` (10) non-bot, non-vault, scored wallets by score,
  re-evaluated daily (:func:`sync_auto_follows`). An auto-follow that falls out of the
  top is dropped, except while it (possibly) has open paper positions.
- Manual: the owner adds / removes (:func:`add_follow` / :func:`remove_follow`). A
  manual removal is remembered (``removed_manually``) and never re-auto-followed; a
  manual add clears it. Adding an existing auto-follow turns it manual (never auto-
  dropped).
- Cap: at most ``MAX_FOLLOWS`` (25) active follows; a manual add beyond it raises
  :class:`FollowCapError`, auto-follows beyond it are skipped.
- ``started_at`` = the follow time. Paper copying starts there; there is no history
  fill-in. A re-follow (after any removal) is a new follow: ``started_at`` = now and the
  ladder restarts at PAPER (P51: the ``ladder_history`` row is ``from_`` the ladder the
  wallet had when it was removed; removal keeps it). Every follow start clears the old
  paper snapshot (I3) and sets the alert watermark ``hl:follow_seq:{addr}`` to the
  wallet's current event seq, so events from before the follow never alert.

Callers commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hades_hl import pipeline
from hades_hl.db import Follow, Kv, LadderHistory, PaperEvent, Wallet

AUTO_FOLLOW_N = 10
MAX_FOLLOWS = 25
KV_PAPER = "hl:paper:{}"  # paper snapshot per address (written by paper_runtime)
KV_FOLLOW_SEQ = "hl:follow_seq:{}"  # alert watermark: event seq at the follow start
KV_SEQ = "hl:seq:{}"  # per-address event counter (live.KV_SEQ)

_ADDR = re.compile(r"^0x[0-9a-f]{40}$")


class FollowCapError(Exception):
    """Adding this follow would exceed the follow cap."""


@dataclass
class FollowDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept_open: list[str] = field(default_factory=list)  # out of the top, open positions
    skipped_cap: list[str] = field(default_factory=list)  # in the top, over the cap


def normalize_address(addr: str) -> str:
    a = (addr or "").strip().lower()
    if not _ADDR.match(a):
        raise ValueError(f"not a Hyperliquid address: {addr!r}")
    return a


def active_follows(session: Session) -> list[Follow]:
    return list(session.scalars(select(Follow).where(Follow.removed_at.is_(None))
                                .order_by(Follow.started_at, Follow.address)))


def active_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Follow)
                              .where(Follow.removed_at.is_(None))) or 0)


def has_open_positions(session: Session, addr: str) -> bool:
    """True if the last paper snapshot has open positions, or if events arrived after
    it (they may have opened one): conservative, so an auto-follow is never dropped
    with a copy possibly open."""
    snap = pipeline.kv_get_json(session, KV_PAPER.format(addr))
    if isinstance(snap, dict) and snap.get("open_positions"):
        return True
    mark = pipeline.kv_get_json(session, KV_FOLLOW_SEQ.format(addr))
    last = mark if isinstance(mark, int) else 0
    if isinstance(snap, dict) and snap.get("last_seq") is not None:
        last = max(last, int(snap["last_seq"]))
    q = select(func.count()).select_from(PaperEvent).where(PaperEvent.address == addr,
                                                           PaperEvent.seq > last)
    return bool(session.scalar(q))


def _start_follow(session: Session, f: Follow, origin: str, now: datetime) -> None:
    prev = f.ladder  # P51: the stored ladder (kept at removal), None for a new follow
    f.origin = origin
    f.started_at = now
    f.removed_at = None
    f.removed_manually = False
    f.ladder = "PAPER"
    f.ladder_changed_at = now
    session.add(LadderHistory(address=f.address, from_=prev, to="PAPER",
                              gate_json={"reason": f"follow ({origin})"}, at=now))
    # I3: a new follow starts from a clean paper snapshot; P51: alert watermark
    old = session.get(Kv, KV_PAPER.format(f.address))
    if old is not None:
        session.delete(old)
        session.flush()
    seq = pipeline.kv_get_json(session, KV_SEQ.format(f.address))
    pipeline.kv_set_json(session, KV_FOLLOW_SEQ.format(f.address),
                         seq if isinstance(seq, int) else 0)


def _new_follow(session: Session, addr: str, origin: str, now: datetime) -> Follow:
    f = session.get(Follow, addr)
    if f is None:
        f = Follow(address=addr, origin=origin, started_at=now, removed_manually=False)
        session.add(f)
        session.flush()
        f.removed_at = None
    _start_follow(session, f, origin, now)
    if session.get(Wallet, addr) is None:  # so backfill / rescore cover it
        session.add(Wallet(address=addr, stats_json={}))
    session.flush()
    return f


def add_follow(session: Session, addr: str, now: datetime, *,
               cap: int = MAX_FOLLOWS) -> Follow:
    """Manual follow. An active follow becomes manual (same follow, not restarted)."""
    addr = normalize_address(addr)
    f = session.get(Follow, addr)
    if f is not None and f.removed_at is None:
        f.origin = "manual"
        session.flush()
        return f
    if active_count(session) >= cap:
        raise FollowCapError(f"follow cap {cap} reached")
    return _new_follow(session, addr, "manual", now)


def remove_follow(session: Session, addr: str, now: datetime) -> bool:
    """Manual removal, remembered so the wallet is never re-auto-followed. Returns True
    if an active follow was stopped (an unknown address is still remembered)."""
    addr = normalize_address(addr)
    f = session.get(Follow, addr)
    if f is None:
        session.add(Follow(address=addr, origin="manual", started_at=now, removed_at=now,
                           removed_manually=True))
        session.flush()
        return False
    was_active = f.removed_at is None
    if was_active:
        f.removed_at = now
    f.removed_manually = True
    session.flush()
    return was_active


def top_candidates(session: Session, n: int) -> list[str]:
    """The best ``n`` scored, non-bot, non-vault wallets never removed manually."""
    removed = select(Follow.address).where(Follow.removed_manually.is_(True))
    q = (select(Wallet.address)
         .where(Wallet.score.is_not(None), Wallet.is_bot.is_(False),
                Wallet.is_vault.is_(False), Wallet.address.not_in(removed))
         .order_by(Wallet.score.desc(), Wallet.address)
         .limit(n))
    return list(session.scalars(q))


def sync_auto_follows(session: Session, now: datetime, *, n: int = AUTO_FOLLOW_N,
                      cap: int = MAX_FOLLOWS) -> FollowDiff:
    diff = FollowDiff()
    top = top_candidates(session, n)
    top_set = set(top)
    for f in active_follows(session):
        if f.origin != "auto" or f.address in top_set:
            continue
        if has_open_positions(session, f.address):
            diff.kept_open.append(f.address)
            continue
        f.removed_at = now
        diff.removed.append(f.address)
    session.flush()
    count = active_count(session)
    for addr in top:
        f = session.get(Follow, addr)
        if f is not None and f.removed_at is None:
            continue
        if count >= cap:
            diff.skipped_cap.append(addr)
            continue
        _new_follow(session, addr, "auto", now)
        count += 1
        diff.added.append(addr)
    session.flush()
    return diff


__all__ = ["AUTO_FOLLOW_N", "MAX_FOLLOWS", "FollowCapError", "FollowDiff", "active_follows",
           "add_follow", "has_open_positions", "remove_follow", "sync_auto_follows"]
