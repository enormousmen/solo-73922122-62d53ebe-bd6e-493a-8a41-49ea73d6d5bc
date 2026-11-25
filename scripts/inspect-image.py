#!/usr/bin/env python3
"""Recursively list a (never mounted) ext4 image and hash every file in it.

Usage:
    inspect-image.py IMAGE [IMAGE ...]            # human readable
    inspect-image.py --json IMAGE                 # JSON to stdout

Uses only debugfs, so it works without root or loop devices.  The JSON form is
what the tests compare before and after a resize to prove the original files
are byte-for-byte intact.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List


def _debugfs(image: str, request: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["debugfs", "-R", request, image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def _parse_ls(output: str) -> List[tuple[str, str, int]]:
    """Parse `ls -l -p` output into (mode, name, size) tuples."""
    entries = []
    for line in output.splitlines():
        # /inode/mode/uid/gid/name/size/
        parts = line.split("/")
        if len(parts) < 8 or not parts[0] == "":
            continue
        try:
            mode, name = parts[2], parts[5]
            size = int(parts[6]) if parts[6] else 0
        except (IndexError, ValueError):
            continue
        if name in (".", ".."):
            continue
        entries.append((mode, name, size))
    return entries


def walk(image: str, directory: str = "/") -> Dict[str, dict]:
    result: Dict[str, dict] = {}
    stack = [directory]
    visited = set()
    while stack:
        current = stack.pop()
        if current in visited:
            continue
        visited.add(current)
        out = _debugfs(image, f"ls -l -p {current}").stdout
        for mode, name, size in _parse_ls(out):
            path = current.rstrip("/") + "/" + name
            if mode.startswith("04"):
                stack.append(path)
            elif mode.startswith("10"):
                with tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / "file"
                    dump = _debugfs(image, f"dump -p {path} {target}")
                    if dump.returncode != 0 or not target.exists():
                        raise RuntimeError(f"cannot read {path} from {image}")
                    digest = hashlib.sha256(target.read_bytes()).hexdigest()
                result[path] = {"size": size, "sha256": digest}
    return result


def main(argv: List[str]) -> int:
    as_json = "--json" in argv
    images = [a for a in argv if a != "--json"]
    if not images:
        print(__doc__, file=sys.stderr)
        return 2
    if as_json:
        payload = {img: walk(img) for img in images}
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0
    for img in images:
        print(f"--- {img}")
        for path, info in sorted(walk(img).items()):
            print(f"  {path}  {info['size']} bytes  {info['sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
