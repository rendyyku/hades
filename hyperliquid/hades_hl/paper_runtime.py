"""Paper-copy runtime of the Hyperliquid head (spec 7, 12a, 12b; rulings P11, P43).

:func:`run_paper` rebuilds a followed wallet's paper copy from scratch on every run:

- settle once (ruling P52): an event is SETTLED by the first run that sees it at least
  ``SETTLE_S`` = 31 s after its ``observed_time`` (the model's price window
  ``[observed + 1 s, observed + 30 s]`` is over): the model price
  (:class:`~hades_hl.live.HLExecutionModel` over the recorded prices) is stored in the
  row (``fill_price``), or the row is marked ``missed``. It is never re-priced: the
  simulation reads the stored price through :class:`SettledModel`. Younger events
  wait for the next run. Stale recovered fills (P47) arrive already settled as missed.
- hourly marks (P48/P52): for every hour with a recorded price, the mark (latest mid at
  or before the hour, at most ``MARK_MAX_AGE_S`` old) is persisted in ``marks`` BEFORE
  the raw prices are pruned (2 days). simulate (core) gets ``mark_times`` = every final
  hour in ``[follow_start, now)``, read from ``marks``. So the curve shows unrealized
  drawdown between events and a re-run after pruning gives the same history.
- the "now" point (P54) is display-only: simulate's ``display_time`` = ``now`` reads the
  live recorded price WITHOUT liquidating, charging or adding history. The snapshot
  shows it as the last curve point and as the headline equity / net PnL. Liquidation,
  closed trades, fills, both gates, ``max_dd`` and so the ladder read the persisted
  history only (events and hourly marks), so they can lag the headline by up to an
  hour, and no run can produce a result a later run would not reproduce.
- gates: the default (HL) gate in full, and the rolling 28-day gate; then
  ``next_ladder``. A change updates the follow, writes ``ladder_history`` with the gate
  snapshot, and alerts. No flapping (final review I1): a promotion needs the full gate
  AND a passing rolling gate, a demotion happens when the rolling gate fails, and after
  a demotion no promotion for ``REPROMOTE_COOLDOWN`` (24 h; the snapshot's
  ``ladder_cooldown_until``), so a rolling net hovering around zero cannot flip the
  ladder every run.
- a JSON snapshot (kv ``hl:paper:{addr}``) for the API: the :class:`PaperSummary`, open
  positions, winrate with n, both gates, a downsampled equity curve, the latest fills,
  and the gap (P49): ``gap_usd`` = per copied and closed position, the leader's
  closedPnl on it scaled by our qty / the leader's qty, minus our pnl on it; plus the
  raw ``leader_realized_usd`` (the leader's closedPnl on positions opened since the
  follow start). Pre-follow positions are in neither.

:func:`run_paper_live` first makes sure the funding rates of the coins involved are
cached (``FundingRates.ensure``, ruling P43), so the simulator charges funding.

:func:`reconcile` (ruling P11): when our copy holds a position but the leader's live
``clearinghouseState`` shows them flat (or flipped) in that coin, a synthetic ``close``
event is persisted: ``source_ref = reconcile:{addr}:{position_id}:{ts_ms}``,
``observed_time = now``, ``seq`` from the persisted counter; the model prices it like
any event. A coin is skipped while any of its events is unsettled or newer than the
snapshot (I2: a live close may be on its way). An answer that is failed, empty,
malformed or partial is "unknown", never "flat": nothing is closed (final review I4).
A HIP-3 coin (``dex:COIN``) is checked against that dex's own state, since the default
state lists main-dex positions only. :class:`Reconciler` limits it to once per 5 min
per wallet.

Callers commit (``reconcile``: before their next ``await``, so no other writer can
take a ``seq`` in between).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from hades_core.contract import PaperSummary
from hades_core.events import TradeEvent
from hades_core.gate import DEFAULT_GATE, GateConfig, GateResult, evaluate_gate, evaluate_rolling
from hades_core.ladder import gap, next_ladder
from hades_core.paper import PaperConfig, PaperResult, simulate
from hades_hl import alerts, pipeline
from hades_hl.client import check_clearinghouse_state
from hades_hl.db import Fill, Follow, LadderHistory, Mark, PaperEvent, Price, _insert_ignore
from hades_hl.follows import KV_FOLLOW_SEQ, KV_PAPER
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
# I1 hysteresis: after a demotion, no promotion for this long (see next_ladder)
REPROMOTE_COOLDOWN = timedelta(hours=24)
SETTLE_S = PRICE_WINDOW_S + 1  # P52: settled once its price window is over (31 s)
MARK_MAX_AGE_S = 900  # a mark older than 15 min is not used
PRICE_KEEP_DAYS = 2  # P52: raw prices are pruned after 2 days (marks are kept)
MARK_FINAL_S = 60  # an hourly mark is computed once the hour is 60 s past
HOUR = timedelta(hours=1)
HOUR_MS = 3_600_000
KV_MARKS_EMPTY = "hl:marks_empty:{}"  # per coin: final hours scanned with no price
EMPTY_MARKS_KEEP_MS = (PRICE_KEEP_DAYS + 2) * 24 * HOUR_MS  # beyond: no raw prices anyway
RERUN_AFTER_S = 3600  # re-evaluate at least hourly (the gate depends on time too)
RECONCILE_INTERVAL_S = 300  # P11: at most once per 5 min per wallet
CURVE_MAX_POINTS = 500
FILLS_KEEP = 200
ZERO = Decimal(0)

Pending = Callable[[str], bool]


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# -- inputs --------------------------------------------------------------------------

def _rows(session: Session, addr: str, until: datetime | None = None) -> list[PaperEvent]:
    q = select(PaperEvent).where(PaperEvent.address == addr)
    if until is not None:
        q = q.where(PaperEvent.observed_time <= until)
    return list(session.scalars(q.order_by(PaperEvent.observed_time, PaperEvent.seq)))


def _event(addr: str, r: PaperEvent) -> TradeEvent | None:
    try:
        return event_from_json(r.json)
    except Exception as e:  # a corrupt row must not stop the copy
        log.warning("hl paper %s: unreadable event %s (%s)", addr, r.source_ref,
                    type(e).__name__)
        return None


def load_events(session: Session, addr: str, until: datetime | None = None) -> list[TradeEvent]:
    return [e for e in (_event(addr, r) for r in _rows(session, addr, until)) if e is not None]


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


class SettledModel:
    """The execution model over SETTLED prices (P52): ``fill_price`` returns the price
    stored at settle time (None = missed); fees, funding and leverage come from
    ``base``."""

    def __init__(self, base: Any, settled: dict[str, Decimal | None]) -> None:
        self.base = base
        self.settled = settled
        self.max_leverage = base.max_leverage

    def fill_price(self, ev: TradeEvent) -> Decimal | None:
        return self.settled.get(ev.source_ref)

    def fee(self, notional: Decimal) -> Decimal:
        return self.base.fee(notional)

    def holding_cost(self, pos: Any, start: datetime, end: datetime) -> Decimal:
        hc = getattr(self.base, "holding_cost", None)
        return ZERO if hc is None else Decimal(hc(pos, start, end))


def settle(session: Session, addr: str, now: datetime, model: Any) -> int:
    """P52: price (or mark missed) every unsettled event of ``addr`` whose price window
    is over; store it once. Returns the number settled. Flushes."""
    n = 0
    for r in session.scalars(select(PaperEvent).where(
            PaperEvent.address == addr, PaperEvent.settled_at.is_(None),
            PaperEvent.observed_time <= now - timedelta(seconds=SETTLE_S))):
        ev = _event(addr, r)
        px = None if ev is None else model.fill_price(ev)
        r.fill_price = px
        r.missed = px is None
        r.settled_at = now
        n += 1
    if n:
        session.flush()
    return n


def _hour_ceil(dt: datetime) -> int:
    t = _ms(dt)
    return -(-t // HOUR_MS) * HOUR_MS


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def record_marks(session: Session, coins: set[str] | list[str], since: datetime,
                 now: datetime) -> int:
    """P48/P52: persist the hourly mark of each coin for every hour ``h`` in
    ``[since, now - MARK_FINAL_S]`` not stored yet that has a recorded price (the latest
    mid at or before ``h``, at most ``MARK_MAX_AGE_S`` old). Returns rows written."""
    lo = _hour_ceil(since)
    hi = _ms(now) - MARK_FINAL_S * 1000
    if hi < lo or not coins:
        return 0
    prices = RecordedPrices(session)
    rows: list[dict] = []
    keep_from = _ms(now) - EMPTY_MARKS_KEEP_MS
    for coin in sorted(coins):
        have = set(session.scalars(select(Mark.hour_ts).where(
            Mark.coin == coin, Mark.hour_ts >= lo, Mark.hour_ts <= hi)))
        key = KV_MARKS_EMPTY.format(coin)
        raw = pipeline.kv_get_json(session, key)
        empty = {int(h) for h in raw if isinstance(h, int)} if isinstance(raw, list) else set()
        before = set(empty)
        for h in range(lo, hi + 1, HOUR_MS):
            if h in have or h in empty:
                continue
            mid = prices.mark(coin, _dt(h))
            if mid is not None:
                rows.append({"coin": coin, "hour_ts": h, "mid": str(mid)})
            else:
                empty.add(h)  # final hour with no price: never re-scanned (minor)
        empty = {h for h in empty if h >= keep_from}
        if empty != before:
            pipeline.kv_set_json(session, key, sorted(empty))
    if rows:
        _insert_ignore(session, Mark.__table__, rows)
        session.flush()
    return len(rows)


def event_coins(session: Session, addr: str) -> set[str]:
    out: set[str] = set()
    for js in session.scalars(select(PaperEvent.json).where(PaperEvent.address == addr)):
        coin = (js or {}).get("market_id")
        if coin:
            out.add(str(coin))
    return out


class Marks:
    """The simulator's ``mark``.

    - At a final hour boundary ``h`` (``h <= now - MARK_FINAL_S``): the persisted hourly
      mark of ``h`` (or None).
    - At ``now``: the live recorded price. Only simulate's ``display_time`` asks for
      it (the display-only final point, P54); ``now`` is never a mark time.
    - Any other time (event times): None, so the simulator keeps each market's last
      known price - the fill price or a mark at/after it. A persisted mark is never
      re-applied after its own hour (N1: it can predate a fresher fill).
    """

    def __init__(self, session: Session, coins: set[str], start: datetime,
                 now: datetime) -> None:
        self.now = now
        self.final_ms = _ms(now) - MARK_FINAL_S * 1000  # hours up to here are persisted
        self.live = RecordedPrices(session)
        self.hourly: dict[tuple[str, int], Decimal] = {}
        if coins:
            for coin, h, mid in session.execute(select(Mark.coin, Mark.hour_ts, Mark.mid).where(
                    Mark.coin.in_(sorted(coins)), Mark.hour_ts >= _ms(start) - HOUR_MS,
                    Mark.hour_ts <= self.final_ms)):
                self.hourly[(coin, h)] = mid

    def __call__(self, coin: str, at: datetime) -> Decimal | None:
        if at >= self.now:
            return self.live.mark(coin, at)
        t = _ms(at)
        if t % HOUR_MS or t > self.final_ms:
            return None  # an event time (or an hour not final yet): keep the last price
        return self.hourly.get((coin, t))

    def times(self, start: datetime) -> list[datetime]:
        """Final hour boundaries strictly after ``start`` (the start point is the
        simulator's own). An hour less than ``MARK_FINAL_S`` old is not a point yet, so
        a run never shows an unmarked point a later run would re-mark. ``now`` is not
        a mark time (P54): a mark can liquidate, the display point must not."""
        first = _ms(start) // HOUR_MS * HOUR_MS + HOUR_MS
        hours = range(first, min(self.final_ms, _ms(self.now) - 1) + 1, HOUR_MS)
        return [_dt(h) for h in hours]


def _leader_cpnl(session: Session, addr: str, r: PaperEvent) -> Decimal:
    """The leader's HL closedPnl of this event's leg (stored by the watcher; older rows:
    the stored fill's, a flip's open leg 0; reconcile events 0)."""
    js = r.json or {}
    v = js.get("leader_closed_pnl")
    if v is not None:
        return Decimal(str(v))
    ref = r.source_ref
    if ref.startswith("reconcile:"):
        return ZERO
    parts = ref.split(":")
    if len(parts) < 2 or (len(parts) > 2 and parts[2] == "open"):
        return ZERO
    try:
        tid = int(parts[1])
    except ValueError:
        return ZERO
    cpnl = session.scalar(select(Fill.closed_pnl).where(Fill.address == addr, Fill.tid == tid))
    return ZERO if cpnl is None else Decimal(cpnl)


def gap_report(session: Session, addr: str, start: datetime, rows: list[PaperEvent],
               events: dict[str, TradeEvent], res: PaperResult,
               now: datetime) -> tuple[Decimal, Decimal]:
    """P49: ``(gap_usd, leader_realized_usd)``.

    - gap: over the copied positions closed by ``now``, the sum of (the leader's
      closedPnl on that position x our open qty / the leader's open size) minus our pnl
      on it.
    - leader_realized: the leader's raw closedPnl on positions opened since ``start``.
    Pre-follow positions are in neither; missed / uncopied positions are not in the gap.
    """
    by_pos: dict[str, Decimal] = {}
    post_follow: set[str] = set()
    for r in rows:
        ev = events.get(r.source_ref)
        if ev is None or ev.position_id is None:
            continue
        if ev.side == "open" and ev.leader_time >= start:
            post_follow.add(ev.position_id)
        cp = _leader_cpnl(session, addr, r)
        if cp:
            by_pos[ev.position_id] = by_pos.get(ev.position_id, ZERO) + cp
    leader_realized = sum((v for k, v in by_pos.items() if k in post_follow), ZERO)
    opens = {(f.position_id, f.at): f for f in res.fills
             if f.side == "open" and not f.missed}
    scaled = ours = ZERO
    for ct in res.closed_trades:
        if ct.closed_at > now or ct.position_id not in post_follow:
            continue
        f = opens.get((ct.position_id, ct.opened_at))
        ev = events.get(f.ev_ref) if f is not None else None
        if f is None or ev is None or not ev.size:
            continue
        scaled += by_pos.get(ct.position_id, ZERO) * f.size / abs(ev.size)
        ours += ct.pnl_usd
    return gap(scaled, ours), leader_realized


# -- snapshot ------------------------------------------------------------------------

def snapshot(session: Session, addr: str) -> dict | None:
    snap = pipeline.kv_get_json(session, KV_PAPER.format(addr))
    return snap if isinstance(snap, dict) else None


def _gate_dict(g: GateResult) -> dict[str, Any]:
    return {"passed": g.passed, "checks": dict(g.checks), "values": dict(g.values)}


def gate_config(gate: GateConfig = GATE) -> dict[str, Any]:
    """The gate thresholds as stored with each ladder change (and served by the API)."""
    return {"min_days": gate.min_days, "min_closed": gate.min_closed,
            "max_dd": str(gate.max_dd), "max_top_share": str(gate.max_top_share),
            "rolling_days": ROLLING_DAYS}


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


def cooldown_until(session: Session, addr: str) -> datetime | None:
    """I1 hysteresis: the end of the promotion cool-down, ``REPROMOTE_COOLDOWN`` after
    ``addr``'s latest demotion (a ``ladder_history`` row NOTIF/COPY -> PAPER); None if
    it was never demoted."""
    at = session.scalar(select(func.max(LadderHistory.at)).where(
        LadderHistory.address == addr, LadderHistory.to == "PAPER",
        LadderHistory.from_.in_(("NOTIF", "COPY"))))
    if at is None:
        return None
    if at.tzinfo is None:  # func.max bypasses the UTCDateTime result processing
        at = at.replace(tzinfo=timezone.utc)
    return at + REPROMOTE_COOLDOWN


def follow_seq(session: Session, addr: str) -> int:
    """The alert watermark (P51): the wallet's event seq when the follow started."""
    v = pipeline.kv_get_json(session, KV_FOLLOW_SEQ.format(addr))
    return v if isinstance(v, int) else 0


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
    prices = RecordedPrices(session)
    model = HLExecutionModel(prices, funding)
    settle(session, addr, now, model)
    coins = event_coins(session, addr)
    # persist the hourly marks while the raw prices are still there (P52)
    record_marks(session, coins,
                 max(f.started_at, now - timedelta(days=PRICE_KEEP_DAYS) - HOUR), now)
    rows = [r for r in _rows(session, addr, until=now - timedelta(seconds=SETTLE_S))
            if r.settled_at is not None]
    by_ref: dict[str, TradeEvent] = {}
    for r in rows:
        e = _event(addr, r)
        if e is not None:
            by_ref[r.source_ref] = e
    events = list(by_ref.values())
    settled = {r.source_ref: (None if r.missed else r.fill_price) for r in rows}
    marks = Marks(session, coins, f.started_at, now)
    # P54: ``now`` is the display point, never a mark time: it cannot liquidate and is
    # not in ``res.equity_curve``, so the gates below read persisted history only
    res: PaperResult = simulate(events, SettledModel(model, settled), cfg, f.started_at,
                                marks, mark_times=marks.times(f.started_at),
                                display_time=now)

    full = evaluate_gate(res, f.started_at, now, gate)
    rolling = evaluate_rolling(res, now, gate, ROLLING_DAYS)
    current = f.ladder or "PAPER"
    # I1 hysteresis: no promotion while cooling down after a demotion
    cool = cooldown_until(session, addr) if current == "PAPER" else None
    cooling = cool is not None and now < cool
    new = next_ladder(current, full, rolling, cooling_down=cooling)  # type: ignore[arg-type]
    gate_snap = {"full": _gate_dict(full), "rolling": _gate_dict(rolling),
                 "config": gate_config(gate)}
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
    # alert watermark: never alert events from before the follow started (P51)
    mark_seq = max(last_seq or 0, follow_seq(session, addr))
    new_events = [e for e in events if e.seq > mark_seq]
    # events are judged at the ladder they happened under (before this change)
    alerts.emit_alerts(session, addr, new_events, change, ladder=current, now=now)

    curve = [(t, eq) for t, eq in res.equity_curve if t <= now]
    if res.display_point is not None and (not curve or res.display_point[0] > curve[-1][0]):
        curve.append(res.display_point)  # shown, never stored as history (P54)
    equity = curve[-1][1] if curve else cfg.bankroll
    net = equity - cfg.bankroll
    closed = [c for c in res.closed_trades if c.closed_at <= now]
    wins = sum(1 for c in closed if c.pnl_usd > 0)
    gap_usd, lead = gap_report(session, addr, f.started_at, rows, by_ref, res, now)
    summary = PaperSummary(
        address=addr, ladder=new, equity_usd=equity, net_pnl_usd=net,
        max_dd=Decimal(full.values["max_dd"]), closed_trades=len(closed),
        days_followed=max((now - f.started_at).days, 0), gap_usd=gap_usd,
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
        "ladder_cooldown_until": cool.isoformat() if cooling and cool is not None else None,
        "gap_usd": str(gap_usd),
        "leader_realized_usd": str(lead),
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

def _leader_positions(state: Any) -> dict[str, Decimal] | None:
    """``{coin: signed size}`` of a COMPLETE ``clearinghouseState`` answer, else None
    (unknown; final review I4). Unknown: not the documented shape, any position entry
    without a coin or a parseable size, or no positions at all while the margin
    summary still reports a position notional (a partial answer)."""
    try:
        state = check_clearinghouse_state(state)
    except Exception:
        return None
    out: dict[str, Decimal] = {}
    for ap in state["assetPositions"]:
        pos = ap.get("position") if isinstance(ap, dict) else None
        coin, szi = (pos.get("coin"), pos.get("szi")) if isinstance(pos, dict) else (None, None)
        if not isinstance(coin, str) or szi is None:
            return None
        try:
            out[coin] = Decimal(str(szi))
        except Exception:
            return None
    if not out:
        try:
            ntl = Decimal(str(state["marginSummary"].get("totalNtlPos") or "0"))
        except Exception:
            return None
        if ntl != 0:
            return None
    return out


def _dex(coin: str) -> str | None:
    """The HIP-3 dex of ``dex:COIN``; None for a main-dex coin."""
    return coin.split(":", 1)[0] if ":" in coin else None


async def _leader_state(client: Any, addr: str, dex: str | None) -> dict[str, Decimal] | None:
    """The leader's positions on one dex, or None when unknown (logged)."""
    try:
        state = await (client.clearinghouse_state(addr) if dex is None
                       else client.clearinghouse_state(addr, dex=dex))
    except Exception as e:
        log.warning("hl reconcile %s: clearinghouseState%s failed (%s: %s); skipped", addr,
                    "" if dex is None else f" (dex {dex})", type(e).__name__, e)
        return None
    leader = _leader_positions(state)
    if leader is None:
        log.warning("hl reconcile %s: clearinghouseState%s is empty, malformed or partial; "
                    "treated as unknown, nothing closed", addr,
                    "" if dex is None else f" (dex {dex})")
    return leader


def _busy_coins(session: Session, addr: str, last_seq: int) -> set[str]:
    """I2: coins with an event not yet reflected in the snapshot (unsettled, or newer
    than its ``last_seq``): a live close may be on its way, so no synthetic close."""
    out: set[str] = set()
    for js in session.scalars(select(PaperEvent.json).where(
            PaperEvent.address == addr,
            or_(PaperEvent.settled_at.is_(None), PaperEvent.seq > last_seq))):
        coin = (js or {}).get("market_id")
        if coin:
            out.add(str(coin))
    return out


async def reconcile(session: Session, client: Any, addr: str, now: datetime, *,
                    pending: Pending | None = None) -> list[TradeEvent]:
    """P11: emit a synthetic close for every open paper position whose leader is flat
    (or flipped) in that coin per ``clearinghouseState``. Skipped while the watcher
    has a pending recovery for ``addr`` (``pending``): the missing fills are coming.
    Never closes on a failed, empty, malformed or partial answer (I4)."""
    snap = snapshot(session, addr)
    opens = (snap or {}).get("open_positions") or []
    if not opens or (pending is not None and pending(addr)):
        return []
    busy = _busy_coins(session, addr, int(snap.get("last_seq") or 0))
    todo = [p for p in opens if p["market_id"] not in busy]
    if not todo:
        return []
    # one state per dex involved (main dex: None); an unknown state closes nothing
    leaders: dict[str | None, dict[str, Decimal] | None] = {}
    for dex in sorted({_dex(p["market_id"]) for p in todo}, key=lambda d: d or ""):
        leaders[dex] = await _leader_state(client, addr, dex)
    # an event that arrived during the calls above makes its coin busy (I2)
    busy = _busy_coins(session, addr, int(snap.get("last_seq") or 0))
    todo = [p for p in todo if leaders[_dex(p["market_id"])] is not None
            and p["market_id"] not in busy]
    if not todo:
        return []
    # no await below: the seq counter is read and bumped atomically w.r.t. the watcher
    key = KV_SEQ.format(addr)
    seq = int(pipeline.kv_get_json(session, key) or 0)
    prices = RecordedPrices(session)
    out: list[TradeEvent] = []
    for p in todo:
        szi = leaders[_dex(p["market_id"])].get(p["market_id"], ZERO)  # type: ignore[union-attr]
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


__all__ = ["GATE", "KV_PAPER", "PRICE_KEEP_DAYS", "RECONCILE_INTERVAL_S", "ROLLING_DAYS",
           "Marks", "RecordedPrices", "Reconciler", "SettledModel", "ensure_funding",
           "event_coins", "gap_report", "load_events", "needs_run", "reconcile",
           "record_marks", "run_paper", "run_paper_live", "settle", "snapshot"]
