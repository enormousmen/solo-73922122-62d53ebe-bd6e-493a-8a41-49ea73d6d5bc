#!/usr/bin/env bash
# Demo for docs/DEMO.md -- scenario C: corrupted primary ext4 superblock
# magic, offline repair from a known backup superblock, concurrency rules,
# byte preservation and crash-before-replace retry.
#
# Runs locally without Docker (same code path as compose); the equivalent
# docker compose commands are documented in docs/DEMO.md.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"
WORK="${WORK:-$(mktemp -d /tmp/ext4-demo-repair-XXXXXX)}"
DATA="$WORK/data"
STATE="$WORK/state"
if [[ -z "${PORT:-}" ]]; then
  PORT="$(python3 - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PY
)"
fi
BASE="http://127.0.0.1:$PORT"
mkdir -p "$DATA" "$STATE"

echo "== 0) build a fault sample (32 MiB 4k image, backup SB at block 4096) =="
FAULT_DISK=disk1 bash scripts/make-fault-sample.sh "$DATA" >/dev/null
SHA_CORRUPT="$(sha256sum "$DATA/disk1.ext4" | cut -d' ' -f1)"
echo "corrupted disk1 sha256: $SHA_CORRUPT"

API_PID=""
API_FIFO="$WORK/api.stdin"
mkfifo "$API_FIFO"

start_api() {  # $1 = extra env assignments
  env DISK_QUOTA_MIB=160 DISK_DATA_DIR="$DATA" DISK_STATE_DB="$STATE/state.db" \
      PORT="$PORT" LOG_LEVEL=warning $1 \
      python3 scripts/serve.py --exit-with-parent <"$API_FIFO" >"$WORK/api.log" 2>&1 &
  API_PID=$!
  exec {API_FD}>"$API_FIFO"
  trap '' PIPE
  for _ in $(seq 1 100); do
    curl -fsS "$BASE/healthz" >/dev/null 2>&1 && return 0
    sleep 0.1
  done
  echo "API did not start; see $WORK/api.log" >&2
  tail -20 "$WORK/api.log" >&2 || true
  return 1
}

stop_api() {
  [[ -n "$API_PID" ]] || return 0
  echo stop >&"$API_FD" 2>/dev/null || true
  exec {API_FD}>&- 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
  API_PID=""
}

echo
echo "== 1) service starts with the bad disk; GET /disks marks the fault =="
start_api "DISK_REPAIR_DELAY_MS=1500"
curl -sS "$BASE/disks" | python3 scripts/show-status.py

echo
echo "== 2) repair disk1 from backup SB 4096; concurrently probe expansions =="
curl -sS -X POST "$BASE/disks/disk1/repair" \
     -H 'content-type: application/json' \
     -d '{"backup_superblock": 4096}' >"$WORK/repair.json" &
REPAIR_CURL=$!
sleep 0.5
echo "-- disk1 expansion while its repair runs (expect HTTP 409 disk_busy):"
curl -sS -o "$WORK/busy.json" -w 'HTTP %{http_code}\n' \
     -X POST "$BASE/disks/disk1/resize" \
     -H 'content-type: application/json' -d '{"target_mib": 48}'
python3 -c 'import json; d=json.load(open("'$WORK'/busy.json")); print(" ", d["detail"]["error"], d["detail"].get("reason"))'
echo "-- disk2 expansion to 64 MiB at the same time (quota allows it):"
curl -sS -o "$WORK/d2.json" -w 'HTTP %{http_code}\n' \
     -X POST "$BASE/disks/disk2/resize" \
     -H 'content-type: application/json' -d '{"target_mib": 64}'
python3 -c 'import json; d=json.load(open("'$WORK'/d2.json")); print(" ", d["status"], "ext4_mib=", d["result"]["ext4_capacity_mib"])'
wait "$REPAIR_CURL"
echo "-- repair result:"
python3 -c 'import json; d=json.load(open("'$WORK'/repair.json")); print(" ", d["status"], "backup", d["backup_superblock"], "len_mib", d["image_length_mib"], "healthy", d["result"]["healthy"], "ext4_mib", d["result"]["ext4_capacity_mib"])'

echo
echo "== 3) invalid backup number changes nothing =="
python3 scripts/corrupt-superblock.py "$DATA/disk1.ext4" >/dev/null
SHA_RECORRUPT="$(sha256sum "$DATA/disk1.ext4" | cut -d' ' -f1)"
curl -sS -o "$WORK/badsb.json" -w 'HTTP %{http_code}\n' \
     -X POST "$BASE/disks/disk1/repair" \
     -H 'content-type: application/json' -d '{"backup_superblock": 999999}'
python3 -c 'import json; print(" ", json.load(open("'$WORK'/badsb.json"))["detail"]["error"])'
SHA_AFTER="$(sha256sum "$DATA/disk1.ext4" | cut -d' ' -f1)"
[[ "$SHA_RECORRUPT" == "$SHA_AFTER" ]] && echo "  PASS: original image byte-identical"

echo
echo "== 4) hard exit before replacement also keeps every byte =="
stop_api
# Start with the crash hook; the repaired staging image passes its checks but
# the service _exits before os.replace().
start_api "DISK_CRASH_BEFORE_REPLACE=disk1"
curl -sS -X POST "$BASE/disks/disk1/repair" \
     -H 'content-type: application/json' \
     -d '{"backup_superblock": 4096}' || echo "  (connection died, as expected)"
set +e
wait "$API_PID"; RC=$?
set -e
API_PID=""
echo "  api exit status: $RC (137 = fault injection before replace)"
SHA_CRASH="$(sha256sum "$DATA/disk1.ext4" | cut -d' ' -f1)"
[[ "$SHA_RECORRUPT" == "$SHA_CRASH" ]] \
  && echo "  PASS: sha256 unchanged by the crashed repair" \
  || { echo "  FAIL: image changed"; exit 1; }

echo
echo "== 5) restart without the hook and retry =="
start_api "DISK_REPAIR_DELAY_MS=0"
curl -sS -X POST "$BASE/disks/disk1/repair" \
     -H 'content-type: application/json' \
     -d '{"backup_superblock": 4096}' | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(" ", d["status"], "healthy", d["result"]["healthy"],
      "length_mib", d["image_length_mib"], "ext4_mib",
      d["result"]["ext4_capacity_mib"])'
curl -sS "$BASE/disks" | python3 scripts/show-status.py
stop_api

echo
echo "== 6) original files are still readable (never mounted) =="
python3 "$ROOT/scripts/inspect-image.py" "$DATA/disk1.ext4"
echo
echo "demo data kept in $WORK"
