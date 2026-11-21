"""One-off: record a REAL ``clearinghouseState`` answer for the open-positions tests.

Run from hyperliquid/:  uv run python scripts/record_positions_fixture.py 0x<address>
No keys involved: the HL info API is public and read-only. Pick a wallet with a few
open positions, ideally cross and isolated, long and short, one without a liquidation
price. Writes tests/fixtures/clearinghouse_state_positions.json:
``{"address", "recorded_at_ms", "source", "request", "state"}`` (``state`` is the
response body, unchanged).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

INFO_URL = "https://api.hyperliquid.xyz/info"
OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    request = {"type": "clearinghouseState", "user": argv[1].lower()}
    with httpx.Client(timeout=30) as http:
        r = http.post(INFO_URL, json=request)
        r.raise_for_status()
        state = r.json()
    path = OUT / "clearinghouse_state_positions.json"
    path.write_text(json.dumps({
        "address": request["user"], "recorded_at_ms": int(time.time() * 1000),
        "source": INFO_URL, "request": request, "state": state}, indent=1), encoding="utf-8")
    print(f"{path.name}: {len(state.get('assetPositions') or [])} position(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
