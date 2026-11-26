"""Provisional signal score (spec 8). Weights live in config; evaluation tunes them later."""

from hades.config import AppConfig
from hades.models import Enrichment, Signal

MAX_SCORE = 100


def score(sig: Signal, enr: Enrichment | None, fusion_sources: set[str], cfg: AppConfig) -> tuple[int, dict]:
    s = cfg.scoring
    chain = cfg.chains.get(sig.chain) if sig.chain else None
    early = (
        enr is not None and enr.buyable is not None
        and (enr.buyable is False or (enr.liquidity_usd is not None and enr.liquidity_usd < cfg.enrich.low_liquidity_usd))
    )
    detail = {
        "base": s.base.get(sig.kind, 0),
        "priority_chain": s.priority_chain if chain is not None and chain.priority else 0,
        "earliness": s.earliness if early else 0,
        "fusion": min(s.fusion_max, s.fusion_per_source * len(fusion_sources - {sig.source})),
    }
    detail["total"] = min(MAX_SCORE, sum(detail.values()))
    return detail["total"], detail
