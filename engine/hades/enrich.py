"""Enrichment (spec 7): can the token be bought on-chain yet, and how deep is the liquidity?

GeckoTerminal is primary (wide network coverage), DexScreener the fallback. Never raises: a failed
check comes back as Enrichment(buyable=None, error=...).
"""

import logging
from datetime import datetime
from typing import Any

from hades.config import ChainCfg
from hades.http import Http, SourceError
from hades.models import Enrichment

log = logging.getLogger(__name__)

GT_API = "https://api.geckoterminal.com/api/v2"
DS_API = "https://api.dexscreener.com"
GT_RPS = 0.5  # public tier ~30 req/min
MAX_CANDIDATES = 5


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _gt_pool(net: str, p: dict) -> dict:
    a = p.get("attributes", {}) or {}
    return {
        "address": a.get("address"),
        "name": a.get("name") or "",
        "liquidity_usd": _num(a.get("reserve_in_usd")) or 0.0,
        "volume_24h": _num((a.get("volume_usd") or {}).get("h24")),
        "dex": ((p.get("relationships") or {}).get("dex") or {}).get("data", {}).get("id"),
        "pool_url": f"https://www.geckoterminal.com/{net}/pools/{a.get('address')}",
    }


def _from_pools(provider: str, pools: list[dict], min_liq: float, now: datetime) -> Enrichment:
    if not pools:
        return Enrichment(provider=provider, checked_at=now, buyable=False, pool_count=0)
    top = max(pools, key=lambda p: p["liquidity_usd"])
    return Enrichment(
        provider=provider, checked_at=now, buyable=top["liquidity_usd"] >= min_liq,
        liquidity_usd=top["liquidity_usd"], dex=top["dex"], pool_url=top["pool_url"],
        volume_24h=top["volume_24h"], pool_count=len(pools),
    )


async def _geckoterminal(http: Http, net: str, address: str, min_liq: float, now: datetime,
                         headers: dict) -> Enrichment:
    try:
        data = await http.get_json(f"{GT_API}/networks/{net}/tokens/{address}/pools", headers=headers)
    except SourceError as e:
        if e.status == 404:  # token not indexed = not tradable yet
            return Enrichment(provider="geckoterminal", checked_at=now, buyable=False, pool_count=0)
        raise
    pools = [_gt_pool(net, p) for p in data.get("data", []) if isinstance(p, dict)]
    return _from_pools("geckoterminal", pools, min_liq, now)


async def _dexscreener(http: Http, chain_id: str, address: str, min_liq: float, now: datetime) -> Enrichment:
    data = await http.get_json(f"{DS_API}/token-pairs/v1/{chain_id}/{address}")
    pools = [{
        "liquidity_usd": _num((p.get("liquidity") or {}).get("usd")) or 0.0,
        "volume_24h": _num((p.get("volume") or {}).get("h24")),
        "dex": p.get("dexId"), "pool_url": p.get("url"),
    } for p in (data if isinstance(data, list) else []) if isinstance(p, dict)]
    return _from_pools("dexscreener", pools, min_liq, now)


async def _by_symbol(http: Http, symbol: str, min_liq: float, priority_chains: dict[str, ChainCfg],
                     now: datetime, headers: dict) -> Enrichment:
    candidates: list[dict] = []
    errors = []
    for key, cfg in priority_chains.items():
        if not cfg.geckoterminal:
            continue
        try:
            data = await http.get_json(f"{GT_API}/search/pools", params={"query": symbol, "network": cfg.geckoterminal},
                                       headers=headers)
        except SourceError as e:
            errors.append(f"{key}: {e}")
            continue
        for p in data.get("data", []):
            pool = _gt_pool(cfg.geckoterminal, p)
            if pool["name"].split(" / ")[0].strip().lower() == symbol.lower():
                candidates.append({"chain": key, **pool})
    if errors and not candidates and len(errors) == sum(1 for c in priority_chains.values() if c.geckoterminal):
        return Enrichment(provider="geckoterminal", checked_at=now, buyable=None, verified=False,
                          error="; ".join(errors))
    candidates.sort(key=lambda c: c["liquidity_usd"], reverse=True)
    candidates = candidates[:MAX_CANDIDATES]
    return Enrichment(
        provider="geckoterminal", checked_at=now, verified=False, candidates=candidates,
        buyable=any(c["liquidity_usd"] >= min_liq for c in candidates), pool_count=len(candidates),
    )


async def check(http: Http, chain: ChainCfg | None, address: str | None, symbol: str | None, *,
                min_liq: float, priority_chains: dict[str, ChainCfg], now: datetime,
                coingecko_key: str = "") -> Enrichment:
    headers = {"x-cg-demo-api-key": coingecko_key} if coingecko_key else {}
    try:
        if address and chain is not None:
            errors = []
            if chain.geckoterminal:
                try:
                    return await _geckoterminal(http, chain.geckoterminal, address, min_liq, now, headers)
                except SourceError as e:
                    errors.append(f"geckoterminal: {e}")
            if chain.dexscreener:
                try:
                    return await _dexscreener(http, chain.dexscreener, address, min_liq, now)
                except SourceError as e:
                    errors.append(f"dexscreener: {e}")
            return Enrichment(provider="none", checked_at=now, buyable=None,
                              error="; ".join(errors) or "chain has no liquidity provider configured")
        if symbol:
            return await _by_symbol(http, symbol, min_liq, priority_chains, now, headers)
        return Enrichment(provider="none", checked_at=now, buyable=None, error="no address or symbol")
    except Exception as e:  # noqa: BLE001 - enrichment must never break the pipeline
        log.exception("enrichment failed")
        return Enrichment(provider="none", checked_at=now, buyable=None, error=f"{type(e).__name__}: {e}")
