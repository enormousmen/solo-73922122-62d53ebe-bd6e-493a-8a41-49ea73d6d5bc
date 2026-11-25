#!/usr/bin/env bash
# Generate a DATA_DIR in which FAULT_DISK's primary ext4 superblock magic is
# corrupted while a known backup superblock remains available.  The other
# disk is a normal seeded image, so the API can start with the bad disk and
# the "repair vs. other-disk expansion" scenario can be exercised.
#
# No mount/loop device is used.
#
# Usage:
#   scripts/make-fault-sample.sh [DATA_DIR]
#
# Knobs:
#   FAULT_DISK=disk1          which disk to corrupt
#   COMPACT=1                 32 MiB image with -g 4096 -> backup at block 4096
#                             (quota-friendly; default)
#   STANDARD=1                256 MiB conventional geometry -> backup at 32768
#   SAMPLE_MIB=<n>            override size for the fault disk
#   SEED_DIR=./seed
#
# Examples:
#   scripts/make-fault-sample.sh /tmp/fault-data
#   STANDARD=1 scripts/make-fault-sample.sh /tmp/fault-data
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
DATA_DIR="${1:-${DATA_DIR:-$ROOT_DIR/data-fault}}"
SEED_DIR="${SEED_DIR:-$ROOT_DIR/seed}"
FAULT_DISK="${FAULT_DISK:-disk1}"
OTHER_DISK="disk2"
[[ "$FAULT_DISK" == "disk2" ]] && OTHER_DISK="disk1"

mkdir -p "$DATA_DIR"

# 1) Start from the ordinary seeded pair (also validates the seed content).
DATA_DIR="$DATA_DIR" SEED_DIR="$SEED_DIR" IMAGE_MIB=32 FORCE=1 \
  bash "$SCRIPT_DIR/make-images.sh" >/dev/null

FAULT_IMG="$DATA_DIR/$FAULT_DISK.ext4"
SEED="$SEED_DIR/$FAULT_DISK"

# 2) Rebuild the fault disk with a geometry that actually contains a backup
#    superblock.  A default 32 MiB / 4 KiB image has a single block group and
#    therefore NO backup, so either enlarge it or narrow blocks-per-group.
if [[ "${STANDARD:-0}" == "1" ]]; then
  SAMPLE_MIB="${SAMPLE_MIB:-256}"
  echo "== building standard-geometry ${SAMPLE_MIB} MiB fault disk =="
  rm -f "$FAULT_IMG"
  mkfs.ext4 -q -F -b 4096 -L "$FAULT_DISK" -d "$SEED" "$FAULT_IMG" "${SAMPLE_MIB}M"
  truncate -s "${SAMPLE_MIB}M" "$FAULT_IMG"
  e2fsck -f -y "$FAULT_IMG" >/dev/null
  BACKUP=32768
  SUGGEST_QUOTA=320
else
  SAMPLE_MIB="${SAMPLE_MIB:-32}"
  echo "== building compact ${SAMPLE_MIB} MiB fault disk (-g 4096, backup @4096) =="
  rm -f "$FAULT_IMG"
  # -g 4096 makes group 1 (and its backup superblock at block 4096) fit even
  # inside a small image.  Fully supported standard mkfs geometry.
  mkfs.ext4 -q -F -b 4096 -g 4096 -L "$FAULT_DISK" -d "$SEED" \
    "$FAULT_IMG" "${SAMPLE_MIB}M"
  truncate -s "${SAMPLE_MIB}M" "$FAULT_IMG"
  e2fsck -f -y "$FAULT_IMG" >/dev/null
  BACKUP=4096
  SUGGEST_QUOTA=160
fi

# 3) Corrupt only the primary magic bytes and report the known backup number.
python3 "$SCRIPT_DIR/corrupt-superblock.py" --json "$FAULT_IMG" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("magic:", d["primary_magic_before"], "->", d["primary_magic_after"]); print("backups:", [b["block"] for b in d["backup_superblocks"] if b["block_size"]==4096])'

echo
echo "fault sample ready:"
echo "  data dir:      $DATA_DIR"
echo "  fault disk:    $FAULT_DISK ($FAULT_IMG, ${SAMPLE_MIB} MiB)"
echo "  healthy disk:  $OTHER_DISK ($DATA_DIR/$OTHER_DISK.ext4, 32 MiB)"
echo "  backup SB:     $BACKUP"
echo
echo "serve it, for example:"
echo "  DISK_QUOTA_MIB=$SUGGEST_QUOTA DISK_DATA_DIR=$DATA_DIR \\"
echo "  DISK_STATE_DB=$DATA_DIR/state.db python3 scripts/serve.py --port 8000"
echo
echo "then:"
echo "  curl -s localhost:8000/disks | python3 -m json.tool"
echo "  curl -s -X POST localhost:8000/disks/$FAULT_DISK/repair \\"
echo "       -H 'content-type: application/json' \\"
echo "       -d '{\"backup_superblock\": $BACKUP}'"
