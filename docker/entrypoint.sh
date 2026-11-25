#!/usr/bin/env bash
# Seed the two ext4 images on first start, then exec the API.
# Existing images are never overwritten, so restarts keep the current state.
set -euo pipefail

DATA_DIR="${DISK_DATA_DIR:-/data}"
SEED_DIR="${DISK_SEED_DIR:-/srv/seed}"
TARGET="${DISK_IMAGE_MIB:-32}"

mkdir -p "$DATA_DIR"
for disk in disk1 disk2; do
  img="$DATA_DIR/$disk.ext4"
  if [[ ! -f "$img" ]]; then
    echo "seeding $img (${TARGET} MiB)"
    mkfs.ext4 -q -F -b 4096 -L "$disk" -d "$SEED_DIR/$disk" "$img" "${TARGET}M"
    truncate -s "${TARGET}M" "$img"
    e2fsck -f -y "$img" >/dev/null
  else
    echo "keeping existing $img"
  fi
done

exec "$@"
