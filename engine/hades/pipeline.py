"""Signal pipeline (spec 5/8): dedupe -> project resolution -> enrich -> score -> alert -> SSE."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from hades.bus import Bus
from hades.config import AppConfig
from hades.enrich import check as enrich_check
from hades.identity import project_key
from hades.models import ListingEvent, Signal
from hades.score import score
from hades.store import Store

log = logging.getLogger(__name__)

LISTING_MATCH_DAYS = 30
BUYABLE_NOW_PREFIX = "Sekarang bisa dibeli"


class Notifier(Protocol):
    def is_muted(self, sig: Signal | dict, now: datetime) -> bool: ...
    async def send_signal(self, signal_id: int, prefix: str = "") -> int | None: ...


def _token_of(key: str) -> tuple[str, str] | None:
    """`chain:address` project keys -> (chain, address); sym:/dom: keys -> None."""
    head, _, rest = key.partition(":")
    if not rest or head in ("sym", "dom"):
        return None
    return head, rest


class Pipeline:
    def __init__(self, store: Store, http: Any, cfg: AppConfig, notifier: Notifier, bus: Bus,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), coingecko_key: str = "") -> None:
        self.store = store
        self.http = http
        self.cfg = cfg
        self.notifier = notifier
        self.bus = bus
        self.clock = clock
        self.coingecko_key = coingecko_key
        self.priority_chains = {k: v for k, v in cfg.chains.items() if v.priority}

    async def _enrich(self, chain: str | None, address: str | None, symbol: str | None, now: datetime):
        return await enrich_check(
            self.http, self.cfg.chains.get(chain) if chain else None, address, symbol,
            min_liq=self.cfg.enrich.min_liquidity_usd, priority_chains=self.priority_chains, now=now,
            coingecko_key=self.coingecko_key,
        )

    def _target(self, row_or_sig: dict | Signal) -> tuple[str | None, str | None, str | None]:
        get = row_or_sig.get if isinstance(row_or_sig, dict) else lambda k: getattr(row_or_sig, k)
        chain, address, symbol = get("chain"), get("address"), get("symbol")
        if not address:
            tok = _token_of(self.store.resolve(get("project_key")))
            if tok and tok[0] in self.cfg.chains:
                chain, address = tok
                symbol = None
        return chain, address, symbol

    async def handle(self, sig: Signal) -> None:
        now = self.clock()
        sid = self.store.insert_signal(sig)
        if sid is None:
            upgraded = self.store.upgrade_signal(sig.dedupe_key, sig.kind, sig.evidence_url)
            if upgraded is not None:
                self.bus.publish("signal_updated", self.store.get_signal(upgraded))
            return
        if sig.website:
            self.store.add_alias(sig.project_key, project_key(domain=sig.website))
        enr = await self._enrich(*self._target(sig), now)
        self.store.add_enrichment(sid, enr)
        canon = self.store.resolve(sig.project_key)
        window = now - timedelta(days=self.cfg.scoring.fusion_window_days)
        total, detail = score(sig, enr, self.store.sources_for_project(canon, window), self.cfg)
        self.store.set_score(sid, total, detail)
        if sig.severity == "high" and total >= self.cfg.alerts.min_score and not self.notifier.is_muted(sig, now):
            msg_id = await self.notifier.send_signal(sid)
            if msg_id is not None:
                self.store.mark_sent(sid, msg_id, now)
        self.bus.publish("signal", self.store.get_signal(sid))

    async def recheck_due(self, now: datetime) -> None:
        for sid in self.store.due_rechecks(now, self.cfg.enrich.recheck_after_s):
            row = self.store.get_signal(sid)
            before = self.store.enrichments(sid)[-1]["buyable"]
            enr = await self._enrich(*self._target(row), now)
            self.store.add_enrichment(sid, enr)
            self.bus.publish("signal_updated", self.store.get_signal(sid))
            if before is False and enr.buyable is True and not self.notifier.is_muted(row, now):
                msg_id = await self.notifier.send_signal(sid, prefix=BUYABLE_NOW_PREFIX)
                if msg_id is not None:
                    self.store.mark_sent(sid, msg_id, now)

    async def match_listing(self, ev: ListingEvent) -> None:
        since = ev.first_seen_at - timedelta(days=LISTING_MATCH_DAYS)
        base = ev.base_asset.lower()
        for row in self.store.signals_since(since):
            if row["observed_at"] <= ev.first_seen_at and (row["symbol"] or "").lower() == base:
                self.store.mark_listed(row["id"], ev.exchange, ev.first_seen_at)
                self.bus.publish("signal_updated", self.store.get_signal(row["id"]))
