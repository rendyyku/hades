#!/usr/bin/env bash
# Start the HADES engine (API) and the web dev server together; Ctrl-C stops both.
#   ENGINE_PORT (default 8000)  WEB_PORT (default 3000)
# Both bind to 127.0.0.1 only. Needs bash >= 4.3 (`wait -n`): Linux bash and Windows
# git-bash work; macOS ships bash 3.2, so install a newer bash first. Run from anywhere.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE_PORT="${ENGINE_PORT:-8000}"
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

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo "stopping engine and web..."
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
(cd "$ROOT/web" && exec npm run dev -- -p "$WEB_PORT") &
pids+=("$!")
set +m

echo "engine: http://127.0.0.1:$ENGINE_PORT   web: http://127.0.0.1:$WEB_PORT   (Ctrl-C to stop)"
wait -n || true  # first one to exit (or Ctrl-C) stops both via the EXIT trap
