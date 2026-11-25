#!/usr/bin/env bash
# Demo for docs/DEMO.md -- scenario A: two disks competing for one quota.
#
# Run from the project root.  Builds images locally (no Docker needed) and
# drives the API through scripts/serve.py.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$(pwd)"
WORK="${WORK:-$(mktemp -d /tmp/ext4-demo-quota-XXXXXX)}"
DATA="$WORK/data"
STATE="$WORK/state"
if [[ -z "${PORT:-}" ]]; then
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
fi
BASE="http://127.0.0.1:$PORT"
# 32 + 32 MiB are used at rest; 160 leaves 96 MiB of room.
QUOTA=160
[[ "${1:-}" == "--keep" ]] && KEEP=1 || KEEP=0

mkdir -p "$DATA" "$STATE"
DATA_DIR="$DATA" FORCE=1 ./scripts/make-images.sh >/dev/null

# Delay the extend phase so both jobs can be seen in flight at once.
env DISK_QUOTA_MIB="$QUOTA" DISK_IMAGE_MIB=32 DISK_EXTEND_DELAY_MS=1500 \
    DISK_DATA_DIR="$DATA" DISK_STATE_DB="$STATE/state.db" PORT="$PORT" \
    LOG_LEVEL=warning python3 scripts/serve.py --exit-with-parent \
    >"$WORK/api.log" 2>&1 &
API_PID=$!
# serve.py --exit-with-parent shuts down by itself when this script exits;
# do not wait for it here, or the two would wait on each other.
trap ':' EXIT
for _ in $(seq 1 100); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

show() {
  curl -sS "$BASE/disks" | python3 "$ROOT/scripts/show-status.py"
}

echo "== 1) initial state =="
show

echo
echo "== 2) disk1 -> 80 MiB and disk2 -> 64 MiB at the same time =="
curl -sS -X POST "$BASE/disks/disk1/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 80}' >"$WORK/r1.json" &
R1=$!
sleep 0.3
echo "-- while disk1's job is in flight:"
show
echo "-- a second job for the same disk while one is running:"
curl -sS -o "$WORK/busy.json" -w 'HTTP %{http_code}\n' \
     -X POST "$BASE/disks/disk1/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 72}' || true
python3 -m json.tool <"$WORK/busy.json"
curl -sS -X POST "$BASE/disks/disk2/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 64}' >"$WORK/r2.json" &
R2=$!
sleep 0.3
echo "-- both jobs in flight:"
show
wait "$R1" "$R2"
echo "-- finished:"
python3 -m json.tool <"$WORK/r1.json" | head -12
python3 -m json.tool <"$WORK/r2.json" | head -12

echo
echo "== 3) a request that does not fit: disk2 -> 96 MiB =="
echo "-- used is 144/160, so only 16 MiB are free:"
curl -sS -o "$WORK/reject.json" -w 'HTTP %{http_code}\n' \
     -X POST "$BASE/disks/disk2/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 96}'
python3 -m json.tool <"$WORK/reject.json"
echo "-- both capacities are unchanged:"
show

echo
echo "== 4) a request that fits exactly: disk2 -> 80 MiB =="
curl -sS -o "$WORK/fit.json" -w 'HTTP %{http_code}\n' \
     -X POST "$BASE/disks/disk2/resize" -H 'content-type: application/json' \
     -d '{"target_mib": 80}'
python3 -m json.tool <"$WORK/fit.json" | head -14

echo
echo "== 5) final state and content check =="
show
python3 "$ROOT/scripts/inspect-image.py" "$DATA/disk1.ext4" "$DATA/disk2.ext4"
[[ "$KEEP" == "1" ]] && echo "demo data kept in $WORK"
exit 0
