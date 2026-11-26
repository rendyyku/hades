"""Ground-truth logger (spec 6.3): when does a pair actually become tradable on a CEX?

Snapshots documented public market endpoints; a pair seen for the first time after the baseline
becomes a ListingEvent. Never emits a Signal. A snapshot less than half the size of the known set
is treated as an outage, so a recovering API does not create thousands of fake "new" listings.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from hades.http import Http, SourceError
from hades.models import ListingEvent, WatchResult
from hades.watchers.base import Watcher

SHRINK_RATIO = 0.5
Parsed = dict[str, str]  # symbol -> base asset


def _coinbase(raw: Any) -> Parsed:
    return {p["id"]: p["base_currency"] for p in raw if p.get("status") == "online"}


def _kraken(raw: Any) -> Parsed:
    out = {}
    for key, p in raw.get("result", {}).items():
        if p.get("status", "online") != "online":
            continue
        ws = p.get("wsname") or key
        out[ws] = ws.split("/")[0] if "/" in ws else p.get("base", ws)
    return out


def _okx(raw: Any) -> Parsed:
    return {p["instId"]: p["baseCcy"] for p in raw.get("data", []) if p.get("state") == "live"}


def _binance(raw: Any) -> Parsed:
    return {p["symbol"]: p["baseAsset"] for p in raw.get("symbols", []) if p.get("status") == "TRADING"}


def _bybit(raw: Any) -> Parsed:
    return {p["symbol"]: p["baseCoin"] for p in raw.get("result", {}).get("list", []) if p.get("status") == "Trading"}


EXCHANGES: dict[str, tuple[str, Callable[[Any], Parsed]]] = {
    "coinbase": ("https://api.exchange.coinbase.com/products", _coinbase),
    "kraken": ("https://api.kraken.com/0/public/AssetPairs", _kraken),
    "okx": ("https://www.okx.com/api/v5/public/instruments?instType=SPOT", _okx),
    "binance": ("https://api.binance.com/api/v3/exchangeInfo", _binance),
    "bybit": ("https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000", _bybit),
}


class GroundTruthWatcher(Watcher):
    def __init__(self, exchange: str, interval_s: int) -> None:
        if exchange not in EXCHANGES:
            raise ValueError(f"unknown exchange {exchange!r}; known: {', '.join(EXCHANGES)}")
        self.exchange = exchange
        self.name = f"ground_truth:{exchange}"
        self.interval_s = interval_s
        self.url, self._parse = EXCHANGES[exchange]

    async def fetch(self, http: Http, state: dict | None) -> Any:
        return await http.get_json(self.url, timeout=30.0)

    def parse(self, raw: Any) -> Parsed:
        try:
            return self._parse(raw)
        except (AttributeError, KeyError, TypeError) as e:
            raise SourceError(f"{self.exchange}: unexpected response shape ({type(e).__name__}: {e})") from e

    def diff(self, state: dict | None, parsed: Parsed, now: datetime) -> WatchResult:
        if not parsed:
            raise SourceError(f"{self.exchange}: empty snapshot")
        known: dict[str, str] = dict((state or {}).get("symbols", {}))
        if state is not None and len(parsed) < SHRINK_RATIO * len(known):
            raise SourceError(f"{self.exchange}: snapshot shrank from {len(known)} to {len(parsed)} pairs")
        listings = []
        if state is not None:
            listings = [ListingEvent(self.exchange, s, parsed[s], now, {"symbol": s, "base": parsed[s]})
                        for s in sorted(set(parsed) - set(known))]
        known.update(parsed)
        return WatchResult(listings=listings, new_state={"symbols": known})
