"""Evaluation (spec 11): which watchers actually lead listings, by how much, and how often they cry wolf.

A hit = a signal observed before a ground-truth listing of the same base asset, within the window.
Listings carry no contract address, so every match is on symbol and flagged weak. Symbol-less signals
(CT) borrow the symbol of a token in the same project (alias table) and are flagged indirect.
"""

import csv
import statistics
from datetime import datetime, timedelta
from pathlib import Path

from hades.http import Http, SourceError
from hades.store import Store

WINDOW_DAYS = 30
SMALL_SAMPLE = 10
GT_API = "https://api.geckoterminal.com/api/v2"


def _hours(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 3600, 1)


def with_match_symbols(store: Store, signals: list[dict]) -> list[dict]:
    """Copies of `signals` with `match_symbol`: the signal's own symbol, or (for CT and other
    symbol-less signals) the symbol of a token signal in the same project via the alias table,
    flagged `indirect`."""
    by_project: dict[str, str] = {}
    for s in signals:
        if s["symbol"]:
            by_project.setdefault(store.resolve(s["project_key"]), s["symbol"])
    out = []
    for s in signals:
        own = s["symbol"]
        sym = own or by_project.get(store.resolve(s["project_key"]))
        out.append({**s, "match_symbol": sym, "indirect": own is None and sym is not None})
    return out


def leads(signal: dict, listing: dict, window: timedelta) -> bool:
    """True if `signal` (same base asset) was observed before `listing`, within `window`."""
    sym = signal.get("match_symbol", signal["symbol"])
    return ((sym or "").lower() == listing["base_asset"].lower()
            and signal["observed_at"] < listing["first_seen_at"] <= signal["observed_at"] + window)


def earliest_signal(signals: list[dict], listing: dict, window_days: int = WINDOW_DAYS) -> dict | None:
    """The earliest signal that leads `listing` within the window, or None."""
    window = timedelta(days=window_days)
    matched = [s for s in signals if leads(s, listing, window)]
    if not matched:
        return None
    best = min(matched, key=lambda s: s["observed_at"])
    return {"id": best["id"], "source": best["source"], "kind": best["kind"],
            "observed_at": best["observed_at"], "lead_h": _hours(best["observed_at"], listing["first_seen_at"])}


def evaluate(store: Store, now: datetime, window_days: int = WINDOW_DAYS) -> dict:
    signals = with_match_symbols(store, store.all_signals())
    listings = store.all_listings()
    window = timedelta(days=window_days)
    hits: list[dict] = []
    first_hit: dict[int, float] = {}  # signal id -> lead hours to its first listing
    misses = []
    for lst in listings:
        matched = False
        for s in signals:
            if leads(s, lst, window):
                matched = True
                lead = _hours(s["observed_at"], lst["first_seen_at"])
                hits.append({"signal_id": s["id"], "listing_id": lst["id"], "exchange": lst["exchange"],
                             "symbol": lst["symbol"], "lead_h": lead, "weak": True,
                             "indirect": s["indirect"]})
                first_hit[s["id"]] = min(lead, first_hit.get(s["id"], lead))
        if not matched:
            misses.append({k: lst[k] for k in ("id", "exchange", "symbol", "base_asset", "first_seen_at")})

    groups: dict[tuple[str, str], list[dict]] = {}
    for s in signals:
        groups.setdefault((s["source"], s["kind"]), []).append(s)
    by_source = []
    for (source, kind), rows in sorted(groups.items()):
        lead_hours = [first_hit[s["id"]] for s in rows if s["id"] in first_hit]
        open_ = [s for s in rows if s["id"] not in first_hit]
        by_source.append({
            "source": source, "kind": kind, "signals": len(rows), "hits": len(lead_hours),
            "false_positives": sum(1 for s in open_ if s["observed_at"] + window < now),
            "pending": sum(1 for s in open_ if s["observed_at"] + window >= now),
            "median_lead_h": statistics.median(lead_hours) if lead_hours else None,
            "min_lead_h": min(lead_hours) if lead_hours else None,
            "small_sample": len(lead_hours) < SMALL_SAMPLE,
        })
    return {"generated_at": now, "window_days": window_days, "by_source": by_source, "hits": hits,
            "misses": misses}


def write_report(report: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].strftime("%Y%m%d")
    md, csv_path = out_dir / f"eval-{stamp}.md", out_dir / f"eval-{stamp}.csv"
    cols = ["source", "kind", "signals", "hits", "false_positives", "pending", "median_lead_h", "min_lead_h",
            "small_sample"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(report["by_source"])
    lines = [
        f"# Evaluasi HADES — {report['generated_at']:%Y-%m-%d %H:%M} UTC",
        "",
        f"Jendela: {report['window_days']} hari. Semua kecocokan berdasarkan simbol (lemah).",
        "",
        "| Sumber | Jenis | Sinyal | Hit | False positive | Pending | Median lead (jam) | Min lead (jam) | Catatan |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in report["by_source"]:
        note = "sampel kecil, belum bisa disimpulkan" if r["small_sample"] else ""
        lines.append(f"| {r['source']} | {r['kind']} | {r['signals']} | {r['hits']} | {r['false_positives']} | "
                     f"{r['pending']} | {r['median_lead_h'] if r['median_lead_h'] is not None else '—'} | "
                     f"{r['min_lead_h'] if r['min_lead_h'] is not None else '—'} | {note} |")
    lines += ["", f"Listing tanpa sinyal sebelumnya: {len(report['misses'])}"]
    for m in report["misses"][:50]:
        lines.append(f"- {m['exchange']} {m['symbol']} ({m['first_seen_at']:%Y-%m-%d})")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md, csv_path


async def price_move(http: Http, net: str, pool_address: str, t0: datetime, t1: datetime) -> float | None:
    """Close-to-close % change between the hourly candles at/just before t0 and t1. None if unknown."""
    try:
        data = await http.get_json(f"{GT_API}/networks/{net}/pools/{pool_address}/ohlcv/hour",
                                   params={"before_timestamp": int(t1.timestamp()) + 3600, "limit": 1000})
    except SourceError:
        return None
    candles = sorted((data.get("data", {}).get("attributes", {}).get("ohlcv_list") or []), key=lambda c: c[0])

    def close_at(t: datetime) -> float | None:
        before = [c for c in candles if c[0] <= t.timestamp()]
        return float(before[-1][4]) if before else None

    a, b = close_at(t0), close_at(t1)
    if not a or b is None:
        return None
    return round((b - a) / a * 100, 2)
