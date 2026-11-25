#!/usr/bin/env python3
"""Run the API from a harness process and stop it without signals.

Signals are awkward in some sandboxes, so this launcher's lifetime is
controlled two ways:

  * write ``stop`` on stdin, or
  * let the launching process exit (``--exit-with-parent``).

A SIGTERM/SIGINT still works where they are permitted.

    python3 scripts/serve.py --port 8080 --exit-with-parent
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn

# Load the app factory from the project root, also in isolated mode (-I)
# where neither cwd nor the script directory is on sys.path.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def _stdin_watcher(server: uvicorn.Server) -> None:
    """Stop the server on an explicit "stop" line.

    EOF is ignored on purpose: the service often runs with stdin on
    /dev/null, and that must not shut it down immediately.
    """
    try:
        for line in sys.stdin:
            if line.strip() in ("stop", "quit"):
                server.should_exit = True
                return
    except (OSError, ValueError):
        pass


def _parent_watcher(server: uvicorn.Server, original: int, interval: float = 0.5) -> None:
    while not server.should_exit:
        if os.getppid() != original:
            server.should_exit = True
            return
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--no-stdin-control", action="store_true")
    parser.add_argument(
        "--exit-with-parent",
        action="store_true",
        help="shut down when the launching process exits (test/demo helper)",
    )
    args = parser.parse_args()

    config = uvicorn.Config(
        "app.main:create_app",
        host=args.host,
        port=args.port,
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        lifespan="on",
        factory=True,
    )
    server = uvicorn.Server(config)
    if not args.no_stdin_control:
        threading.Thread(target=_stdin_watcher, args=(server,), daemon=True).start()
    if args.exit_with_parent:
        threading.Thread(
            target=_parent_watcher, args=(server, os.getppid()), daemon=True
        ).start()
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
