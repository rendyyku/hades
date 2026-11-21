#!/usr/bin/env bash
# Start the HADES heads and the web dev server together; Ctrl-C stops all three:
# the Solana engine (hades serve), the Hyperliquid head (hades-hl serve: API + runtime)
# and the web app.
#   ENGINE_PORT (default 8000)  HL_PORT (default 8010)  WEB_PORT (default 3000)
# All bind to 127.0.0.1 only. Needs bash >= 4.3 (`wait -n`): Linux bash and Windows
# git-bash work; macOS ships bash 3.2, so install a newer bash first. Run from anywhere.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE_PORT="${ENGINE_PORT:-8000}"
HL_PORT="${HL_PORT:-8010}"
WEB_PORT="${WEB_PORT:-3000}"
# The web server routes accept only these origins/hosts (see web/.env.example).
export HADES_PUBLIC_ORIGIN="${HADES_PUBLIC_ORIGIN:-http://localhost:$WEB_PORT,http://127.0.0.1:$WEB_PORT}"

if [ -x "$ROOT/engine/.venv/Scripts/hades.exe" ]; then
  HADES="$ROOT/engine/.venv/Scripts/hades.exe"   # Windows venv
elif [ -x "$ROOT/engine/.venv/bin/hades" ]; then
  HADES="$ROOT/engine/.venv/bin/hades"
elif command -v hades >/dev/null 2>&1; then
  HADES="$(command -v hades)"
else
  echo "hades CLI not found: uv venv --python 3.12 engine/.venv && uv pip install -e \"engine[dev]\"" >&2
  exit 1
fi

if [ -x "$ROOT/hyperliquid/.venv/Scripts/hades-hl.exe" ]; then
  HADES_HL="$ROOT/hyperliquid/.venv/Scripts/hades-hl.exe"   # Windows venv
elif [ -x "$ROOT/hyperliquid/.venv/bin/hades-hl" ]; then
  HADES_HL="$ROOT/hyperliquid/.venv/bin/hades-hl"
elif command -v hades-hl >/dev/null 2>&1; then
  HADES_HL="$(command -v hades-hl)"
else
  echo "hades-hl CLI not found: (cd hyperliquid && uv sync --extra dev)" >&2
  exit 1
fi

pids=()
cleanup() {
  # Ignore (not reset) INT/TERM while stopping: `timeout -s INT` and a terminal Ctrl-C
  # signal the whole process group, so a second signal arrives right after the first
  # and with the default action it would kill this shell before the children are stopped.
  trap '' INT TERM
  trap - EXIT
  echo "stopping engine, hyperliquid head and web..."
  for pid in "${pids[@]}"; do
    if command -v taskkill >/dev/null 2>&1; then
      # git-bash: native children (python, node) survive a bash kill; kill the Windows tree
      winpid="$(cat "/proc/$pid/winpid" 2>/dev/null || true)"
      # MSYS_NO_PATHCONV keeps /F etc. literal whether or not path conversion is enabled
      if [ -n "$winpid" ]; then MSYS_NO_PATHCONV=1 taskkill /F /T /PID "$winpid" >/dev/null 2>&1 || true; fi
    fi
    # process group first (npm spawns next), then the pid itself
    kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

set -m  # each background job gets its own process group
(cd "$ROOT" && exec "$HADES" serve --host 127.0.0.1 --port "$ENGINE_PORT") &
pids+=("$!")
(cd "$ROOT" && exec "$HADES_HL" serve --host 127.0.0.1 --port "$HL_PORT") &
pids+=("$!")
(cd "$ROOT/web" && exec npm run dev -- -p "$WEB_PORT") &
pids+=("$!")
set +m

echo "engine: http://127.0.0.1:$ENGINE_PORT   hyperliquid: http://127.0.0.1:$HL_PORT   web: http://127.0.0.1:$WEB_PORT   (Ctrl-C to stop)"
wait -n || true  # first one to exit (or Ctrl-C) stops all via the EXIT trap
