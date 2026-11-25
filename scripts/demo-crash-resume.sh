#!/usr/bin/env bash
# Demo for docs/DEMO.md -- scenario B: interruption between the file extend
# and resize2fs, then resume the same target after a restart.
#
# Run this from the project root, next to docker-compose.yml.  It builds
# images locally (no Docker needed) and drives the API with the same
# scripts/serve.py launcher the tests use.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"
WORK="${WORK:-$(mktemp -d /tmp/ext4-demo-XXXXXX)}"
DATA="$WORK/data"
STATE="$WORK/state"
SEED="$ROOT/seed"
if [[ -z "${PORT:-}" ]]; then
  PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
fi
BASE="http://127.0.0.1:$PORT"

mkdir -p "$DATA" "$STATE"
DATA_DIR="$DATA" FORCE=1 ./scripts/make-images.sh >/dev/null

# scripts/serve.py stops when it reads a "stop" line from stdin; signals are
# not used so the demo also works in restricted sandboxes.
API_PID=""
API_FIFO="$WORK/api.stdin"
mkfifo "$API_FIFO"

start_api() {  # $1 = extra env assignments
  env DISK_QUOTA_MIB=160 DISK_DATA_DIR="$DATA" DISK_STATE_DB="$STATE/state.db" \
      PORT="$PORT" LOG_LEVEL=warning $1 \
      python3 scripts/serve.py --exit-with-parent <"$API_FIFO" >"$WORK/api.log" 2>&1 &
  API_PID=$!
  # Hold the write end open so the server does not see EOF; use a dedicated
  # fd so a crashed server cannot take the demo shell down with SIGPIPE.
  exec {API_FD}>"$API_FIFO"
  trap '' PIPE
  for _ in $(seq 1 100); do
    curl -fsS "$BASE/healthz" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "API did not start; see $WORK/api.log" >&2
  return 1
}

stop_api() {
  [[ -n "$API_PID" ]] || return 0
  # The server may already be gone (fault injection); the write can then
  # fail, which is fine.
  echo stop >&"$API_FD" 2>/dev/null || true
  exec {API_FD}>&- 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
  API_PID=""
}

echo "== 1) start with fault injection: die after extending disk1 =="
start_api "DISK_CRASH_AFTER_EXTEND=disk1"
curl -sS -X POST "$BASE/disks/disk1/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 96}' || echo "(connection died, as expected)"
set +e
wait "$API_PID"
RC=$?
set -e
API_PID=""
echo "api exit status: $RC (137 = hard exit after extend)"

echo
echo "== 2) inspect the image directly (never mounted) =="
python3 - "$DATA/disk1.ext4" <<'PY'
import os, subprocess, sys
img = sys.argv[1]
size = os.stat(img).st_size
out = subprocess.run(["dumpe2fs", "-h", img], capture_output=True, text=True).stdout
blocks = bs = None
for line in out.splitlines():
    if line.startswith("Block count:"):
        blocks = int(line.split(":")[1])
    elif line.startswith("Block size:"):
        bs = int(line.split(":")[1])
print(f"image length: {size // (1024*1024)} MiB")
print(f"ext4 capacity: {blocks * bs // (1024*1024)} MiB")
PY

echo
echo "== 3) restart without fault injection; quota already charges 96 MiB =="
start_api "DISK_EXTEND_DELAY_MS=0"
curl -sS "$BASE/disks" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("used/quota MiB:", d["used_mib"], "/", d["quota_mib"])
for disk in d["disks"]:
    print(" ", disk["disk"], "length", disk["image_length_mib"], "MiB",
          "ext4", disk["ext4_capacity_mib"], "MiB",
          "pending", disk["pending_target_mib"], "in_flight", disk["in_flight"])
'
echo "-- a different target is refused:"
curl -sS -X POST "$BASE/disks/disk1/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 112}' | head -c 300; echo

echo
echo "== 4) resume with the same target =="
curl -sS -X POST "$BASE/disks/disk1/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 96}' | python3 -m json.tool
stop_api

echo
echo "== 5) verify content and final state =="
python3 "$ROOT/scripts/inspect-image.py" "$DATA/disk1.ext4"
echo "demo data kept in $WORK"
