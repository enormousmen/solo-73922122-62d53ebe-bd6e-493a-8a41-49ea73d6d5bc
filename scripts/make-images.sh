#!/usr/bin/env bash
# Create the two seeded ext4 images.  Safe to re-run: images are rebuilt from
# scratch, but only if they do not exist yet unless FORCE=1 is set.
#
#   IMAGE_MIB   initial image length in MiB      (default 32)
#   SEED_DIR    directory with files to copy in  (default ./seed)
#   DATA_DIR    where disk1.ext4/disk2.ext4 go   (default ./data)
#   FORCE=1     overwrite existing images
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_MIB="${IMAGE_MIB:-32}"
SEED_DIR="${SEED_DIR:-$ROOT_DIR/seed}"
DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"
FORCE="${FORCE:-0}"

mkdir -p "$DATA_DIR"

for spec in "disk1:disk1 seed files" "disk2:disk2 seed files"; do
  disk="${spec%%:*}"
  img="$DATA_DIR/$disk.ext4"
  seed="$SEED_DIR/$disk"
  if [[ -f "$img" && "$FORCE" != "1" ]]; then
    echo "keep existing $img"
    continue
  fi
  if [[ ! -d "$seed" ]]; then
    echo "missing seed directory $seed" >&2
    exit 1
  fi
  rm -f "$img"
  # -d copies the seed directory contents into the new filesystem; -F forces
  # creation in a regular file; -q keeps the output small.
  mkfs.ext4 -q -F -b 4096 -L "$disk" -d "$seed" "$img" "${IMAGE_MIB}M"
  # Make sure the file is exactly IMAGE_MIB and pass a clean fsck.
  truncate -s "${IMAGE_MIB}M" "$img"
  e2fsck -f -y "$img" >/dev/null
  echo "created $img (${IMAGE_MIB} MiB) from $seed"
done

# Inventory of what is inside, without mounting.
for img in "$DATA_DIR"/disk*.ext4; do
  echo "--- $img"
  python3 "$SCRIPT_DIR/inspect-image.py" "$img" | tail -n +2
done
