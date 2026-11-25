#!/usr/bin/env python3
"""Pretty-print GET /disks for the demo scripts.

    curl -s localhost:8000/disks | python3 scripts/show-status.py
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    data = json.load(sys.stdin)
    print(
        f"  quota {data['quota_mib']} MiB, used {data['used_mib']} MiB,"
        f" free {data['free_mib']} MiB"
    )
    for disk in data["disks"]:
        print(
            f"  {disk['disk']}: image {disk['image_length_mib']} MiB,"
            f" ext4 {disk['ext4_capacity_mib']} MiB,"
            f" charged {disk['quota_charge_mib']} MiB,"
            f" in_flight {disk['in_flight']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
