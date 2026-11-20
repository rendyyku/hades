"""One-off: record REAL Hyperliquid public-API responses into tests/fixtures/.

Run from hyperliquid/:  .venv/Scripts/python.exe scripts/record_fixtures.py
No keys involved: the HL info API and the stats-data leaderboard are public.

Writes (each kept under MAX_BYTES, trimming rows if needed):
  leaderboard_sample.json    {"leaderboardRows": [...50 rows]}
  user_fills_by_time.json    userFillsByTime, last 7d, <= 2000 rows (one page)
  user_funding.json          userFunding, last 7d (first page)
  clearinghouse_state.json   clearinghouseState
  meta.json                  meta
  all_mids.json              allMids
  fixture_meta.json          address, recorded_at, window, row counts
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
MAX_BYTES = 500_000
LEADERBOARD_ROWS = 50
FILL_CAP = 2000
WINDOW_MS = 7 * 86_400_000


_LIGHT = {"allMids", "clearinghouseState", "l2Book", "orderStatus", "spotClearinghouseState", "exchangeStatus"}
_PER_ITEM = {"userFillsByTime", "userFunding"}


def info(http: httpx.Client, body: dict):
    """POST /info, then sleep off its weight at 1200/min (= 20 weight/s).

    Weights per https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
    """
    for attempt in range(5):
        r = http.post(INFO_URL, json=body)
        if r.status_code != 429:
            break
        wait = float(r.headers.get("Retry-After") or 15 * 2**attempt)
        print(f"429 on {body['type']}, waiting {wait:.0f}s", file=sys.stderr)
        time.sleep(wait)
    r.raise_for_status()
    data = r.json()
    weight = 2 if body["type"] in _LIGHT else 20
    if body["type"] in _PER_ITEM and isinstance(data, list):
        weight += len(data) // 20
    time.sleep(weight / 20)
    return data


def dump(name: str, data) -> int:
    path = OUT / name
    text = json.dumps(data, indent=None, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text.encode())


def fit_rows(rows: list, wrap=lambda r: r) -> list:
    """Drop rows from the end until the encoded payload is under MAX_BYTES."""
    while rows and len(json.dumps(wrap(rows), separators=(",", ":")).encode()) > MAX_BYTES:
        rows = rows[: int(len(rows) * 0.9)]
    return rows


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)
    start = now - WINDOW_MS
    with httpx.Client(timeout=120) as http:
        lb = http.get(LEADERBOARD_URL)
        lb.raise_for_status()
        rows = lb.json()["leaderboardRows"][:LEADERBOARD_ROWS]
        print(f"leaderboard: {len(rows)} rows kept", file=sys.stderr)

        # Pick an active sampled address: first one with 200..1999 fills (one page) and
        # funding in the window; otherwise the one with the most fills (a full page).
        chosen, fills, funding, best = None, [], [], None
        for row in rows:
            addr = row["ethAddress"]
            f = info(http, {"type": "userFillsByTime", "user": addr, "startTime": start, "endTime": now})
            if not f:
                continue
            fu = info(http, {"type": "userFunding", "user": addr, "startTime": start, "endTime": now})
            if not fu:
                continue
            if 200 <= len(f) < FILL_CAP:
                chosen, fills, funding = addr, f, fu
                break
            if best is None or len(f) > len(best[1]):
                best = (addr, f, fu)
        if chosen is None:
            if best is None:
                print("no active address found in the leaderboard sample", file=sys.stderr)
                return 1
            chosen, fills, funding = best
        fills = fit_rows(fills[:FILL_CAP])
        funding = fit_rows(funding)
        chs = info(http, {"type": "clearinghouseState", "user": chosen})
        meta = info(http, {"type": "meta"})
        mids = info(http, {"type": "allMids"})

    sizes = {
        "leaderboard_sample.json": dump(
            "leaderboard_sample.json",
            {"leaderboardRows": fit_rows(rows, lambda r: {"leaderboardRows": r})},
        ),
        "user_fills_by_time.json": dump("user_fills_by_time.json", fills),
        "user_funding.json": dump("user_funding.json", funding),
        "clearinghouse_state.json": dump("clearinghouse_state.json", chs),
        "meta.json": dump("meta.json", meta),
        "all_mids.json": dump("all_mids.json", mids),
    }
    dump("fixture_meta.json", {
        "address": chosen,
        "recorded_at_ms": now,
        "start_ms": start,
        "end_ms": now,
        "fills": len(fills),
        "funding": len(funding),
        "leaderboard_rows": len(rows),
        "sources": {"info": INFO_URL, "leaderboard": LEADERBOARD_URL},
    })
    for name, size in sizes.items():
        print(f"{name}: {size} bytes", file=sys.stderr)
    print(f"address: {chosen}  fills: {len(fills)}  funding: {len(funding)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
