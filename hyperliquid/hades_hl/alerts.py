"""Alerts of the Hyperliquid head (spec 12a "Alerts").

- A NOTIF or COPY wallet alerts on every open and close (a ``resolve`` counts as a
  close; a P11 reconcile close too, flagged ``reconcile: true``). Increases and
  reduces alert only when the size change is at least 25% of the leader's previous
  position size (an unknown previous size, e.g. a pre-follow episode, alerts).
- Every ladder change alerts, whatever the ladder.
- Rows are unique on ``(kind, address, source_ref)``: emitting the same event twice
  (e.g. from the live fill hook and again from the paper run) inserts nothing.
- ``payload_json`` always carries ``head='hyperliquid'``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from hades_core.contract import Alert as AlertModel
from hades_core.events import TradeEvent
from hades_core.paper import event_sort_key
from hades_hl.db import Alert, Follow, PaperEvent, utcnow
from hades_hl.normalize import HEAD

SIZE_CHANGE_ALERT = Decimal("0.25")
ALERT_LADDERS = frozenset({"NOTIF", "COPY"})
ZERO = Decimal(0)


@dataclass
class LadderChange:
    from_: str | None
    to: str
    at: datetime
    history_id: int
    gate: dict[str, Any] = field(default_factory=dict)


def _episode_size_before(session: Session, addr: str, ev: TradeEvent) -> Decimal | None:
    """The leader's position size before ``ev``, rebuilt from the episode's earlier
    events; None when the episode's open was never seen."""
    if ev.position_id is None:
        return None
    rows = session.scalars(select(PaperEvent).where(PaperEvent.address == addr,
                                                    PaperEvent.seq < ev.seq)
                           .order_by(PaperEvent.seq))
    size: Decimal | None = None
    for r in rows:
        js = r.json or {}
        if js.get("position_id") != ev.position_id:
            continue
        sz = abs(Decimal(str(js.get("size", "0"))))
        side = js.get("side")
        if side == "open":
            size = sz
        elif size is None:
            continue
        elif side == "increase":
            size += sz
        elif side == "reduce":
            size = max(size - sz, ZERO)
        else:
            size = ZERO
    return size


def previous_size(session: Session, addr: str, ev: TradeEvent) -> Decimal | None:
    row = session.get(PaperEvent, (addr, ev.source_ref))
    start = (row.json or {}).get("leader_start") if row is not None else None
    if start is not None:
        return abs(Decimal(str(start)))
    return _episode_size_before(session, addr, ev)


def _insert(session: Session, values: dict) -> Alert | None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    elif dialect == "postgresql":  # pragma: no cover
        from sqlalchemy.dialects.postgresql import insert
    else:  # pragma: no cover
        raise NotImplementedError(dialect)
    stmt = insert(Alert).values(**values).on_conflict_do_nothing().returning(Alert.id)
    new_id = session.execute(stmt).scalar()
    return None if new_id is None else session.get(Alert, new_id)


def _event_alert(session: Session, addr: str, ev: TradeEvent, ladder: str,
                 at: datetime) -> dict | None:
    kind = "close" if ev.side == "resolve" else ev.side
    payload: dict[str, Any] = {
        "head": HEAD, "side": ev.side, "direction": ev.direction, "coin": ev.market_id,
        "size": str(ev.size), "leader_price": str(ev.price),
        "leader_time": ev.leader_time.isoformat(),
        "observed_time": ev.observed_time.isoformat(), "position_id": ev.position_id,
        "ladder": ladder, "reconcile": ev.source_ref.startswith("reconcile:"),
    }
    if kind in ("increase", "reduce"):
        prev = previous_size(session, addr, ev)
        payload["prev_size"] = None if prev is None else str(prev)
        if prev is not None and prev > 0:
            pct = abs(ev.size) / prev
            if pct < SIZE_CHANGE_ALERT:
                return None
            payload["change_pct"] = str(pct)
        else:
            payload["change_pct"] = None
    return {"kind": kind, "address": addr, "coin": ev.market_id, "source_ref": ev.source_ref,
            "payload_json": payload, "created_at": at}


def emit_alerts(session: Session, addr: str, new_events: Iterable[TradeEvent],
                ladder_change: LadderChange | None, *, ladder: str | None = None,
                now: datetime | None = None) -> list[Alert]:
    """Insert the alerts due for ``new_events`` (judged at ``ladder``, default the
    follow's current ladder) and ``ladder_change``. Returns only the NEW rows."""
    at = now or utcnow()
    session.flush()
    if ladder is None:
        f = session.get(Follow, addr)
        ladder = (f.ladder if f is not None else None) or "PAPER"
    rows: list[dict] = []
    if ladder in ALERT_LADDERS:
        for ev in sorted(new_events, key=event_sort_key):
            r = _event_alert(session, addr, ev, ladder, at)
            if r is not None:
                rows.append(r)
    if ladder_change is not None:
        lc = ladder_change
        rows.append({"kind": "ladder", "address": addr, "coin": None,
                     "source_ref": f"ladder:{addr}:{lc.history_id}",
                     "payload_json": {"head": HEAD, "from": lc.from_, "to": lc.to,
                                      "at": lc.at.isoformat(), "gate": lc.gate},
                     "created_at": at})
    out = [a for a in (_insert(session, r) for r in rows) if a is not None]
    session.flush()
    return out


def to_contract(row: Alert) -> AlertModel:
    """The head API contract shape of an alert row (spec 9)."""
    return AlertModel(id=row.id, head=HEAD, kind=row.kind, wallet=row.address,
                      market_id=row.coin, payload=dict(row.payload_json or {}),
                      created_at=row.created_at)


__all__ = ["ALERT_LADDERS", "LadderChange", "SIZE_CHANGE_ALERT", "emit_alerts",
           "previous_size", "to_contract"]
