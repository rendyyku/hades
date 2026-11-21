"""Paper-copy runtime of the Hyperliquid head (spec 7, 12a, 12b; rulings P11, P43).

:func:`run_paper` rebuilds a followed wallet's paper copy from scratch on every run:

- events: every ``paper_events`` row of the wallet that is *settled* (observed at
  least ``SETTLE_S`` = 30 s before ``now``: the model's price window
  ``[observed + 1 s, observed + 30 s]`` must be over, or a fill would be judged missed
  only because its price was not recorded yet). Later events wait for the next run.
- simulate (core) with :class:`~hades_hl.live.HLExecutionModel` over the recorded
  prices, ``follow_started_at`` = the follow's ``started_at``, and a ``mark`` function
  (the latest recorded mid at or before the step, at most ``MARK_MAX_AGE_S`` old) so
  open positions are marked to market at every step of the equity curve.
- gates: the default (HL) gate in full, and the rolling 28-day gate; then
  ``next_ladder``. A change updates the follow, writes ``ladder_history`` with the gate
  snapshot, and alerts.
- a JSON snapshot (kv ``hl:paper:{addr}``) for the API: the :class:`PaperSummary`, open
  positions, winrate with n, both gates, a downsampled equity curve and the latest
  fills.

:func:`run_paper_live` first makes sure the funding rates of the coins involved are
cached (``FundingRates.ensure``, ruling P43), so the simulator charges funding.

:func:`reconcile` (ruling P11): when our copy holds a position but the leader's live
``clearinghouseState`` shows them flat (or flipped) in that coin, a synthetic ``close``
event is persisted: ``source_ref = reconcile:{addr}:{position_id}:{ts_ms}``,
``observed_time = now``, ``seq`` from the persisted counter; the model prices it like
any event. :class:`Reconciler` limits it to once per 5 min per wallet.

Callers commit (``reconcile``: before their next ``await``, so no other writer can
take a ``seq`` in between).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from hades_core.contract import PaperSummary
from hades_core.events import TradeEvent
from hades_core.gate import DEFAULT_GATE, GateConfig, GateResult, evaluate_gate, evaluate_rolling
from hades_core.ladder import gap, next_ladder
from hades_core.paper import PaperConfig, PaperResult, simulate
from hades_hl import alerts, pipeline
from hades_hl.db import Fill, Follow, Funding, LadderHistory, PaperEvent, Price
from hades_hl.follows import KV_PAPER
from hades_hl.live import (
    KV_SEQ,
    PRICE_WINDOW_S,
    FundingRates,
    HLExecutionModel,
    event_from_json,
    event_to_json,
)
from hades_hl.normalize import HEAD

log = logging.getLogger(__name__)

GATE: GateConfig = DEFAULT_GATE  # spec 12b: Hyperliquid keeps the default gate
ROLLING_DAYS = 28
SETTLE_S = PRICE_WINDOW_S  # an event is processed once its price window is over
MARK_MAX_AGE_S = 900  # a mark older than 15 min is not used
RERUN_AFTER_S = 3600  # re-evaluate at least hourly (the gate depends on time too)
RECONCILE_INTERVAL_S = 300  # P11: at most once per 5 min per wallet
CURVE_MAX_POINTS = 500
FILLS_KEEP = 200
ZERO = Decimal(0)

Pending = Callable[[str], bool]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# -- inputs --------------------------------------------------------------------------

def load_events(session: Session, addr: str, until: datetime | None = None) -> list[TradeEvent]:
    q = select(PaperEvent).where(PaperEvent.address == addr)
    if until is not None:
        q = q.where(PaperEvent.observed_time <= until)
    out: list[TradeEvent] = []
    for r in session.scalars(q.order_by(PaperEvent.observed_time, PaperEvent.seq)):
        try:
            out.append(event_from_json(r.json))
        except Exception as e:  # a corrupt row must not stop the copy
            log.warning("hl paper %s: unreadable event %s (%s)", addr, r.source_ref,
                        type(e).__name__)
    return out


class RecordedPrices:
    """Recorded mids (``prices``) read through ``session``: ``price_at`` for the
    execution model, ``mark`` for curve marking (never a mid recorded after ``at``)."""

    def __init__(self, session: Session, mark_max_age_s: float = MARK_MAX_AGE_S) -> None:
        self.session = session
        self.mark_max_age_ms = int(mark_max_age_s * 1000)

    def price_at(self, coin: str, t: datetime, within_s: float = PRICE_WINDOW_S) -> Decimal | None:
        lo = _ms(t)
        return self.session.scalar(select(Price.mid).where(
            Price.coin == coin, Price.time >= lo, Price.time <= lo + int(within_s * 1000))
            .order_by(Price.time).limit(1))

    def mark(self, coin: str, at: datetime) -> Decimal | None:
        hi = _ms(at)
        return self.session.scalar(select(Price.mid).where(
            Price.coin == coin, Price.time <= hi, Price.time >= hi - self.mark_max_age_ms)
            .order_by(Price.time.desc()).limit(1))


def leader_pnl(session: Session, addr: str, start: datetime, end: datetime) -> Decimal:
    """The leader's realized perp PnL over [start, end]: closedPnl - fee of the stored
    fills, plus stored funding (the gap report's leader side)."""
    lo, hi = _ms(start), _ms(end)
    total = ZERO
    for coin, cpnl, fee in session.execute(select(Fill.coin, Fill.closed_pnl, Fill.fee).where(
            Fill.address == addr, Fill.time >= lo, Fill.time <= hi)):
        if coin.startswith("@") or "/" in coin:  # spot
            continue
        total += Decimal(cpnl) - Decimal(fee)
    for usdc in session.scalars(select(Funding.usdc).where(
            Funding.address == addr, Funding.time >= lo, Funding.time <= hi)):
        total += Decimal(usdc)
    return total


# -- snapshot ------------------------------------------------------------------------

def snapshot(session: Session, addr: str) -> dict | None:
    snap = pipeline.kv_get_json(session, KV_PAPER.format(addr))
    return snap if isinstance(snap, dict) else None


def _gate_dict(g: GateResult) -> dict[str, Any]:
    return {"passed": g.passed, "checks": dict(g.checks), "values": dict(g.values)}


def _downsample(curve: list[tuple[datetime, Decimal]], n: int) -> list[list[str]]:
    if len(curve) > n:
        step = (len(curve) - 1) / (n - 1)
        curve = [curve[round(i * step)] for i in range(n)]
    return [[t.isoformat(), str(eq)] for t, eq in curve]


def _fill_json(f) -> dict:
    return {"ref": f.ev_ref, "market_id": f.market_id, "side": f.side,
            "direction": f.direction, "size": str(f.size),
            "price": None if f.price is None else str(f.price), "fee": str(f.fee),
            "realized_pnl": str(f.realized_pnl), "missed": f.missed,
            "position_id": f.position_id, "at": None if f.at is None else f.at.isoformat()}


def needs_run(session: Session, addr: str, now: datetime) -> bool:
    """Never run, last run over an hour ago, or settled events newer than it."""
    snap = snapshot(session, addr)
    if snap is None or not snap.get("computed_at"):
        return True
    computed = datetime.fromisoformat(snap["computed_at"])
    if now - computed >= timedelta(seconds=RERUN_AFTER_S):
        return True
    last = int(snap.get("last_seq") or 0)
    return bool(session.scalar(select(func.count()).select_from(PaperEvent).where(
        PaperEvent.address == addr, PaperEvent.seq > last,
        PaperEvent.observed_time <= now - timedelta(seconds=SETTLE_S))))


# -- run -----------------------------------------------------------------------------

def run_paper(session: Session, addr: str, now: datetime, *,
              funding: FundingRates | None = None, cfg: PaperConfig | None = None,
              gate: GateConfig = GATE) -> PaperSummary:
    """Re-simulate ``addr``'s paper copy, apply the ladder, alert, snapshot. Flushes;
    the caller commits. Raises KeyError if ``addr`` was never followed."""
    f = session.get(Follow, addr)
    if f is None:
        raise KeyError(addr)
    cfg = cfg or PaperConfig()
    prev = snapshot(session, addr)
    events = load_events(session, addr, until=now - timedelta(seconds=SETTLE_S))
    prices = RecordedPrices(session)
    model = HLExecutionModel(prices, funding)
    res: PaperResult = simulate(events, model, cfg, f.started_at, prices.mark)

    full = evaluate_gate(res, f.started_at, now, gate)
    rolling = evaluate_rolling(res, now, gate, ROLLING_DAYS)
    current = f.ladder or "PAPER"
    new = next_ladder(current, full, rolling)  # type: ignore[arg-type]
    gate_snap = {"full": _gate_dict(full), "rolling": _gate_dict(rolling),
                 "config": {"min_days": gate.min_days, "min_closed": gate.min_closed,
                            "max_dd": str(gate.max_dd), "max_top_share": str(gate.max_top_share),
                            "rolling_days": ROLLING_DAYS}}
    change = None
    if new != current:
        f.ladder = new
        f.ladder_changed_at = now
        h = LadderHistory(address=addr, from_=current, to=new, gate_json=gate_snap, at=now)
        session.add(h)
        session.flush()
        change = alerts.LadderChange(current, new, now, h.id, gate_snap)
        log.info("hl paper %s: ladder %s -> %s", addr, current, new)
    elif f.ladder is None:
        f.ladder = current

    last_seq = int(prev.get("last_seq") or 0) if prev else None
    new_events = [e for e in events if last_seq is None or e.seq > last_seq]
    # events are judged at the ladder they happened under (before this change)
    alerts.emit_alerts(session, addr, new_events, change, ladder=current, now=now)

    curve = [(t, eq) for t, eq in res.equity_curve if t <= now]
    equity = curve[-1][1] if curve else cfg.bankroll
    net = equity - cfg.bankroll
    closed = [c for c in res.closed_trades if c.closed_at <= now]
    wins = sum(1 for c in closed if c.pnl_usd > 0)
    lead = leader_pnl(session, addr, f.started_at, now)
    summary = PaperSummary(
        address=addr, ladder=new, equity_usd=equity, net_pnl_usd=net,
        max_dd=Decimal(full.values["max_dd"]), closed_trades=len(closed),
        days_followed=max((now - f.started_at).days, 0), gap_usd=gap(lead, net),
        origin=f.origin if f.origin in ("auto", "manual") else "manual")
    seqs = [e.seq for e in events]
    snap = {
        "summary": summary.model_dump(mode="json"),
        "computed_at": now.isoformat(),
        "follow_started_at": f.started_at.isoformat(),
        "last_seq": max(seqs + [last_seq or 0]),
        "events": len(events),
        "missed": sum(1 for x in res.fills if x.missed),
        "open_positions": [{"position_id": p.position_id, "market_id": p.market_id,
                            "direction": p.direction, "qty": str(p.qty),
                            "entry_price": str(p.entry_price), "margin": str(p.margin),
                            "leverage": str(p.leverage), "leader_size": str(p.leader_size)}
                           for p in res.open_positions.values()],
        "winrate": (wins / len(closed)) if closed else None,
        "n": len(closed),
        "gate": _gate_dict(full),
        "rolling": _gate_dict(rolling),
        "leader_pnl_usd": str(lead),
        "equity_curve": _downsample(curve, CURVE_MAX_POINTS),
        "fills": [_fill_json(x) for x in res.fills[-FILLS_KEEP:]],
        "funding_missing_hours": funding.missing_hours if funding is not None else None,
    }
    pipeline.kv_set_json(session, KV_PAPER.format(addr), snap)
    session.flush()
    return summary


async def ensure_funding(session: Session, client: Any, funding: FundingRates, addr: str,
                         now: datetime) -> None:
    """P43: cache the hourly funding of every coin of ``addr``'s events since the
    follow start (errors are logged; uncached hours then count 0)."""
    f = session.get(Follow, addr)
    if f is None:
        raise KeyError(addr)
    first: dict[str, datetime] = {}
    for e in load_events(session, addr, until=now):
        t = max(e.observed_time, f.started_at)
        if e.market_id not in first or t < first[e.market_id]:
            first[e.market_id] = t
    for coin in sorted(first):
        try:
            await funding.ensure(client, coin, first[coin], now, now=now)
        except Exception as e:
            log.warning("hl paper %s: funding for %s unavailable (%s: %s)", addr, coin,
                        type(e).__name__, e)


async def run_paper_live(session: Session, client: Any, addr: str, now: datetime, *,
                         funding: FundingRates, cfg: PaperConfig | None = None) -> PaperSummary:
    """:func:`run_paper` after :func:`ensure_funding`. ``session`` must hold no
    uncommitted writes (the funding cache commits through its own connection)."""
    await ensure_funding(session, client, funding, addr, now)
    return run_paper(session, addr, now, funding=funding, cfg=cfg)


# -- P11 reconcile ---------------------------------------------------------------------

def _leader_positions(state: Any) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for ap in (state or {}).get("assetPositions") or []:
        pos = (ap or {}).get("position") or {}
        coin, szi = pos.get("coin"), pos.get("szi")
        if coin is None or szi is None:
            continue
        try:
            out[str(coin)] = Decimal(str(szi))
        except Exception:
            continue
    return out


def _pending_reconciles(session: Session, addr: str, since: datetime | None) -> set[str]:
    """Position ids with a reconcile close not yet processed by a paper run."""
    prefix = f"reconcile:{addr}:"
    q = select(PaperEvent.source_ref).where(PaperEvent.address == addr,
                                            PaperEvent.source_ref.like(prefix + "%"))
    if since is not None:
        q = q.where(PaperEvent.observed_time > since)
    return {ref[len(prefix):].rsplit(":", 1)[0] for ref in session.scalars(q)}


async def reconcile(session: Session, client: Any, addr: str, now: datetime, *,
                    pending: Pending | None = None) -> list[TradeEvent]:
    """P11: emit a synthetic close for every open paper position whose leader is flat
    (or flipped) in that coin per ``clearinghouseState``. Skipped while the watcher
    has a pending recovery for ``addr`` (``pending``): the missing fills are coming."""
    snap = snapshot(session, addr)
    opens = (snap or {}).get("open_positions") or []
    if not opens or (pending is not None and pending(addr)):
        return []
    computed = datetime.fromisoformat(snap["computed_at"]) if snap.get("computed_at") else None
    waiting = _pending_reconciles(session, addr, computed)
    todo = [p for p in opens if p["position_id"] not in waiting]
    if not todo:
        return []
    try:
        state = await client.clearinghouse_state(addr)
    except Exception as e:
        log.warning("hl reconcile %s: clearinghouseState failed (%s: %s)", addr,
                    type(e).__name__, e)
        return []
    leader = _leader_positions(state)
    # no await below: the seq counter is read and bumped atomically w.r.t. the watcher
    key = KV_SEQ.format(addr)
    seq = int(pipeline.kv_get_json(session, key) or 0)
    prices = RecordedPrices(session)
    out: list[TradeEvent] = []
    for p in todo:
        szi = leader.get(p["market_id"], ZERO)
        holds = (szi > 0 and p["direction"] == "long") or (szi < 0 and p["direction"] == "short")
        if holds:
            continue
        seq += 1
        size = Decimal(p.get("leader_size") or "0")
        mid = prices.mark(p["market_id"], now) or ZERO
        ev = TradeEvent(head=HEAD, wallet=addr, market_id=p["market_id"], side="close",
                        direction=p["direction"], size=size, price=mid,
                        notional_usd=size * mid, fee_usd=ZERO, leader_time=now,
                        observed_time=now,
                        source_ref=f"reconcile:{addr}:{p['position_id']}:{_ms(now)}",
                        position_id=p["position_id"], seq=seq)
        session.add(PaperEvent(source_ref=ev.source_ref, address=addr,
                               json={**event_to_json(ev), "via": "reconcile",
                                     "leader_szi": str(szi)},
                               observed_time=now, delay_ms=None, seq=seq))
        out.append(ev)
        log.info("hl reconcile %s: leader flat in %s; synthetic close of %s", addr,
                 p["market_id"], p["position_id"])
    if out:
        pipeline.kv_set_json(session, key, seq)
        session.flush()
    return out


class Reconciler:
    """At most one reconcile check per ``interval_s`` per wallet with open positions."""

    def __init__(self, interval_s: float = RECONCILE_INTERVAL_S) -> None:
        self.interval = timedelta(seconds=interval_s)
        self.last: dict[str, datetime] = {}

    def due(self, session: Session, addr: str, now: datetime) -> bool:
        last = self.last.get(addr)
        if last is not None and now - last < self.interval:
            return False
        return bool((snapshot(session, addr) or {}).get("open_positions"))

    async def maybe(self, session: Session, client: Any, addr: str, now: datetime, *,
                    pending: Pending | None = None) -> list[TradeEvent]:
        if not self.due(session, addr, now):
            return []
        self.last[addr] = now
        return await reconcile(session, client, addr, now, pending=pending)


__all__ = ["GATE", "KV_PAPER", "RECONCILE_INTERVAL_S", "ROLLING_DAYS", "RecordedPrices",
           "Reconciler", "ensure_funding", "leader_pnl", "load_events", "needs_run",
           "reconcile", "run_paper", "run_paper_live", "snapshot"]
