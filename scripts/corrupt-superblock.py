#!/usr/bin/env python3
"""Damage (or inspect) the primary ext4 superblock magic of an image.

This is the *fault sample* generator companion for the offline superblock
repair API.  It never mounts anything and only writes the two magic bytes at
byte offset 1024 + 0x38 (= 1080) in the primary superblock.

Examples
--------
Corrupt the primary magic and list usable 4 KiB backup superblocks::

    python3 scripts/corrupt-superblock.py /data/disk1.ext4

Only list backup superblocks / current magic without writing::

    python3 scripts/corrupt-superblock.py --inspect /data/disk1.ext4

Restore the correct magic again (manual un-corruption)::

    python3 scripts/corrupt-superblock.py --restore /data/disk1.ext4

The JSON form (``--json``) is convenient for scripts and tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional

PRIMARY_SB_OFFSET = 1024
MAGIC_OFF = PRIMARY_SB_OFFSET + 0x38
LOG_BS_OFF_IN_SB = 0x18
MAGIC = b"\x53\xef"
CANDIDATE_BS = (4096, 1024, 2048, 65536)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def primary_magic(path: Path) -> Optional[bytes]:
    with path.open("rb") as fh:
        fh.seek(MAGIC_OFF)
        data = fh.read(2)
    return data if len(data) == 2 else None


def primary_block_size(path: Path) -> Optional[int]:
    with path.open("rb") as fh:
        fh.seek(PRIMARY_SB_OFFSET + LOG_BS_OFF_IN_SB)
        raw = fh.read(4)
    if len(raw) != 4:
        return None
    bs = 1024 << struct.unpack("<I", raw)[0]
    return bs if bs in CANDIDATE_BS else None


def find_backup_superblocks(path: Path) -> List[Dict[str, int]]:
    """Block-align scan for valid ext4 backup superblocks.

    The primary at byte 1024 is reported separately and excluded here.  Scans
    block boundaries for every candidate block size and keeps entries whose
    magic and s_log_block_size are self-consistent.
    """
    size = path.stat().st_size
    found: List[Dict[str, int]] = []
    seen = set()
    with path.open("rb") as fh:
        for bs in CANDIDATE_BS:
            block = 0
            while block * bs + 1024 <= size:
                start = block * bs
                block += 1
                if start == 0:
                    continue
                # For 1 KiB blocks block 1 *is* the primary superblock.
                if bs == 1024 and start == PRIMARY_SB_OFFSET:
                    continue
                fh.seek(start + 0x38)
                if fh.read(2) != MAGIC:
                    continue
                fh.seek(start + LOG_BS_OFF_IN_SB)
                raw = fh.read(4)
                if len(raw) != 4:
                    continue
                log_bs = struct.unpack("<I", raw)[0]
                if (1024 << log_bs) != bs:
                    continue
                key = (bs, start)
                if key in seen:
                    continue
                seen.add(key)
                found.append({"block": start // bs, "block_size": bs, "offset_bytes": start})
    found.sort(key=lambda x: (x["block_size"], x["block"]))
    return found


def write_magic(path: Path, value: bytes) -> None:
    with path.open("r+b") as fh:
        fh.seek(MAGIC_OFF)
        fh.write(value)
        fh.flush()
        import os

        os.fsync(fh.fileno())


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--inspect", action="store_true", help="do not modify the image")
    action.add_argument("--restore", action="store_true", help="write the valid magic back")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    path: Path = args.image
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 2

    before_magic = primary_magic(path)
    sha_before = sha256_of(path)

    changed = False
    if not args.inspect:
        if args.restore:
            if before_magic != MAGIC:
                write_magic(path, MAGIC)
                changed = True
        else:  # corrupt: zero the two magic bytes
            if before_magic == MAGIC:
                write_magic(path, b"\x00\x00")
                changed = True
            else:
                # Already corrupt; still make sure the bytes are zeroed.
                write_magic(path, b"\x00\x00")
                changed = True

    after_magic = primary_magic(path)
    backups = find_backup_superblocks(path)
    bs = primary_block_size(path)
    payload = {
        "image": str(path),
        "sha256_before": sha_before,
        "sha256_after": sha256_of(path),
        "changed": changed,
        "primary_magic_before": before_magic.hex() if before_magic else None,
        "primary_magic_after": after_magic.hex() if after_magic else None,
        "primary_magic_ok": after_magic == MAGIC,
        "primary_block_size": bs,
        "backup_superblocks": backups,
        "known_backup_superblock": next(
            (b["block"] for b in backups if b["block_size"] == 4096), None
        ),
        "repair_request_example": (
            f'curl -X POST <host>/disks/disk1/repair '
            f"-H 'content-type: application/json' "
            f"-d '{{\"backup_superblock\": "
            f"{next((b['block'] for b in backups if b['block_size'] == 4096), 32768)}}}'"
        ),
    }
    if args.as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"image: {path}")
        print(f"primary magic: {payload['primary_magic_before']} -> "
              f"{payload['primary_magic_after']} (ok={payload['primary_magic_ok']})")
        print(f"primary block size field: {bs}")
        print("backup superblocks:")
        for b in backups:
            print(f"  block {b['block']:>6}  block_size={b['block_size']}")
        if payload["known_backup_superblock"] is not None:
            print(f"known 4 KiB backup number: {payload['known_backup_superblock']}")
            print(f"example: {payload['repair_request_example']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
