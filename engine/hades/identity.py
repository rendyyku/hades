"""Project identity (spec 8): one normalized key per token / symbol / domain."""

from functools import lru_cache

import tldextract

from hades.config import ChainCfg


@lru_cache(maxsize=1)
def _extract() -> tldextract.TLDExtract:
    # offline: use the public suffix list bundled with tldextract, never fetch it
    return tldextract.TLDExtract(suffix_list_urls=())


def normalize_address(chain_cfg: ChainCfg | None, address: str) -> str:
    """EVM addresses are case-insensitive (checksum casing varies between lists); others are not."""
    if (chain_cfg is not None and chain_cfg.evm) or address.startswith("0x"):
        return address.lower()
    return address


def registered_domain(host: str) -> str:
    ext = _extract()(host.strip().lower().rstrip("."))
    if not ext.suffix or not ext.domain:
        return host.strip().lower()
    return f"{ext.domain}.{ext.suffix}"


def project_key(*, chain: str | None = None, address: str | None = None, symbol: str | None = None,
                domain: str | None = None, chains: dict[str, ChainCfg] | None = None) -> str:
    if chain and address:
        return f"{chain}:{normalize_address((chains or {}).get(chain), address)}"
    if symbol:
        return f"sym:{symbol.lower()}"
    if domain:
        return f"dom:{registered_domain(domain)}"
    raise ValueError("project_key needs chain+address, symbol or domain")


def dedupe_key(source: str, *parts: str | None) -> str:
    return source + ":" + ":".join(p.lower() for p in parts if p is not None)
