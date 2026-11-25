#!/usr/bin/env python3
"""End-to-end checks for the offline ext4 expansion API.

Runs the real service in a child process (scripts/serve.py) against freshly
built images in a temporary directory, then drives it over HTTP.  Two
scenarios are covered:

  1. quota competition -- two disks race for a shared quota while both have
     jobs in flight, plus a refusal that must leave both capacities untouched;
  2. crash + resume -- the service is killed after the image was lengthened
     but before resize2fs; on restart the disk is still charged at its target
     length and resubmitting the same target finishes the job with the original
     files intact.

Usage: python3 -I tests/e2e.py [--keep]
Exit code 0 means everything passed; a compact report is printed either way.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from importlib import util as _importlib_util  # noqa: E402

_spec = _importlib_util.spec_from_file_location("inspect_image", ROOT / "scripts" / "inspect-image.py")
inspect_image = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(inspect_image)  # type: ignore[union-attr]

MIB = 1024 * 1024
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"


def pick_free_port() -> int:
    """Use an ephemeral port so a leftover process cannot break the run."""
    global PORT, BASE
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        PORT = s.getsockname()[1]
    BASE = f"http://127.0.0.1:{PORT}"
    return PORT

results: List[Dict[str, Any]] = []


def check(name: str, condition: bool, detail: Any = None) -> None:
    results.append({"name": name, "ok": bool(condition), "detail": detail})
    status = "PASS" if condition else "FAIL"
    extra = f"  {detail}" if detail is not None else ""
    print(f"[{status}] {name}{extra}")


def http(method: str, path: str, body: Optional[dict] = None, timeout: float = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload or b"null")
        except json.JSONDecodeError:
            parsed = payload.decode(errors="replace")
        return exc.code, parsed


def wait_ready(timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = http("GET", "/healthz", timeout=1)
            if status == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("service did not become ready")


def build_images(data_dir: Path, seed_dir: Path) -> None:
    env = dict(os.environ, DATA_DIR=str(data_dir), SEED_DIR=str(seed_dir), FORCE="1")
    subprocess.run(
        ["bash", str(ROOT / "scripts" / "make-images.sh")],
        check=True,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class Service:
    """Child-process wrapper with stdin-triggered shutdown."""

    def __init__(self, data_dir: Path, state_db: Path, quota: int, extra_env: Optional[dict] = None):
        env = dict(
            os.environ,
            DISK_QUOTA_MIB=str(quota),
            DISK_DATA_DIR=str(data_dir),
            DISK_STATE_DB=str(state_db),
            LOG_LEVEL="warning",
            PYTHONPATH=str(ROOT),
        )
        if extra_env:
            env.update(extra_env)
        self.log_path = data_dir.parent / f"service-{time.time_ns()}.log"
        self.log_file = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(ROOT / "scripts" / "serve.py"),
                "--port",
                str(PORT),
                "--exit-with-parent",
            ],
            stdin=subprocess.PIPE,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(ROOT),
            start_new_session=True,
        )

    def stop(self) -> None:
        if self.proc.poll() is not None:
            self.log_file.close()
            return
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(b"stop\n")
            self.proc.stdin.flush()
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # Signals are not always permitted in the sandbox; report instead
            # of masking the real problem.
            print(f"WARNING: service pid {self.proc.pid} did not stop on its own")
        self.log_file.close()

    def log_tail(self, n: int = 30) -> str:
        try:
            self.log_file.flush()
            return self.log_path.read_text(errors="replace").splitlines()[-n:]
        except Exception:
            return []


def image_snapshot(*images: Path) -> Dict[str, dict]:
    return {str(p): inspect_image.walk(str(p)) for p in images}


def lengths(*images: Path) -> Dict[str, int]:
    return {str(p): p.stat().st_size for p in images}


def capacities(*images: Path) -> Dict[str, int]:
    out = {}
    for p in images:
        proc = subprocess.run(
            ["dumpe2fs", "-h", str(p)], capture_output=True, text=True, check=True
        )
        values = {}
        for line in proc.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                values[k.strip().lower()] = v.strip()
        out[str(p)] = int(values["block count"]) * int(values["block size"])
    return out


# ---------------------------------------------------------------------------
# Scenario 1: two disks competing for the quota
# ---------------------------------------------------------------------------


def scenario_quota_competition(work: Path, seed: Path) -> None:
    print("\n=== scenario 1: two disks competing for the shared quota ===")
    data = work / "s1" / "data"
    data.mkdir(parents=True)
    build_images(data, seed)
    d1, d2 = data / "disk1.ext4", data / "disk2.ext4"

    before = image_snapshot(d1, d2)
    q = 160  # 32 + 32 used at rest
    svc = Service(data, work / "s1" / "state" / "state.db", quota=q)
    try:
        wait_ready()
        status, body = http("GET", "/disks")
        check("initial quota accounting", body["used_mib"] == 64, body["used_mib"])
        check(
            "both disks report image length and ext4 capacity",
            all(
                d["image_length_mib"] == 32 and d["ext4_capacity_mib"] == 32
                for d in body["disks"]
            ),
            [(d["disk"], d["image_length_mib"], d["ext4_capacity_mib"]) for d in body["disks"]],
        )

        # Slow the extend phase so the in-flight window is observable, then
        # submit both disks without waiting for the first to finish.
        # (The delay is per service instance; restart with it set.)
        svc.stop()
        svc = Service(
            data,
            work / "s1" / "state" / "state.db",
            quota=q,
            extra_env={"DISK_EXTEND_DELAY_MS": "1500"},
        )
        wait_ready()

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(http, "POST", "/disks/disk1/resize", {"target_mib": 80})
            # Let disk1's admission + pending commit land first.
            time.sleep(0.3)
            _, during = http("GET", "/disks")
            # Same disk, second request while the first job is in flight.
            busy_status, busy_body = http("POST", "/disks/disk1/resize", {"target_mib": 80})
            # 80 (disk1 target) + 96 > 160, so this one must be refused.
            rej_status, rej_body = http("POST", "/disks/disk2/resize", {"target_mib": 96})
            d2_after_reject = http("GET", "/disks/disk2")[1]
            # 80 + 64 <= 160, so disk2 is accepted while disk1 is still busy.
            f2 = pool.submit(http, "POST", "/disks/disk2/resize", {"target_mib": 64})
            time.sleep(0.3)
            _, both = http("GET", "/disks")
            r1, r2 = f1.result(), f2.result()

        disk1_inflight = next(d for d in during["disks"] if d["disk"] == "disk1")
        check(
            "in-flight disk is charged at its target length",
            disk1_inflight["in_flight"] is True
            and disk1_inflight["quota_charge_mib"] == 80
            and disk1_inflight["image_length_mib"] == 32,
            {
                "charge_mib": disk1_inflight["quota_charge_mib"],
                "length_mib": disk1_inflight["image_length_mib"],
            },
        )
        check(
            "second request for the same disk is refused while a job runs",
            busy_status == 409 and busy_body["detail"]["error"] == "disk_busy",
            busy_body,
        )
        check(
            "competing request is rejected when quota is insufficient",
            rej_status == 409 and rej_body["detail"]["error"] == "quota_exceeded",
            rej_body,
        )
        check(
            "rejected disk keeps image length and ext4 capacity unchanged",
            d2_after_reject["image_length_mib"] == 32
            and d2_after_reject["ext4_capacity_mib"] == 32
            and d2_after_reject["pending_job_id"] is None,
            d2_after_reject,
        )
        check(
            "both disks are in flight at the same time",
            all(d["in_flight"] for d in both["disks"]),
            [(d["disk"], d["in_flight"], d["quota_charge_mib"]) for d in both["disks"]],
        )
        check(
            "both in-flight jobs are charged at their targets",
            both["used_mib"] == 80 + 64,
            both["used_mib"],
        )
        check(
            "both concurrent requests succeed",
            r1[0] == 200
            and r1[1]["status"] == "succeeded"
            and r2[0] == 200
            and r2[1]["status"] == "succeeded",
            {"disk1": (r1[0], r1[1].get("status")), "disk2": (r2[0], r2[1].get("status"))},
        )

        _, after_both = http("GET", "/disks")
        by_id = {d["disk"]: d for d in after_both["disks"]}
        check(
            "each disk reached its own target for both length and ext4 capacity",
            by_id["disk1"]["image_length_mib"] == 80
            and by_id["disk1"]["ext4_capacity_mib"] == 80
            and by_id["disk2"]["image_length_mib"] == 64
            and by_id["disk2"]["ext4_capacity_mib"] == 64,
            {k: (v["image_length_mib"], v["ext4_capacity_mib"]) for k, v in by_id.items()},
        )

        # Now 160 - 80 - 64 = 16 MiB free: 96 is refused without touching
        # disk2, 80 fits exactly and is accepted.
        status, refused = http("POST", "/disks/disk2/resize", {"target_mib": 96})
        detail = refused.get("detail") if isinstance(refused, dict) else None
        check(
            "too-large retry is refused with both capacities intact",
            status == 409
            and lengths(d2)[str(d2)] == 64 * MIB
            and capacities(d2)[str(d2)] == 64 * MIB,
            {"status": status, "detail": detail},
        )
        status, ok = http("POST", "/disks/disk2/resize", {"target_mib": 80})
        check(
            "fits-now retry succeeds",
            status == 200 and ok["status"] == "succeeded" and ok["result"]["image_length_mib"] == 80,
            {"status": status, "target": 80},
        )

        status, final = http("GET", "/disks")
        check("final quota fully accounted", final["used_mib"] == 160, final["used_mib"])
        check(
            "original files survived both resizes",
            snapshot_files_equal(before, image_snapshot(d1, d2)),
            "sha256 of every file compared",
        )
    finally:
        svc.stop()


def snapshot_files_equal(a: Dict[str, dict], b: Dict[str, dict]) -> bool:
    for image, files in a.items():
        if image not in b:
            return False
        if files != b[image]:
            return False
    return True


# ---------------------------------------------------------------------------
# Scenario 2: crash between extend and resize2fs, then resume
# ---------------------------------------------------------------------------


def scenario_crash_resume(work: Path, seed: Path) -> None:
    print("\n=== scenario 2: interruption between file extend and ext4 resize ===")
    data = work / "s2" / "data"
    data.mkdir(parents=True)
    build_images(data, seed)
    d1, d2 = data / "disk1.ext4", data / "disk2.ext4"
    state = work / "s2" / "state" / "state.db"

    before = image_snapshot(d1, d2)
    q = 160

    svc = Service(
        data,
        state,
        quota=q,
        extra_env={"DISK_CRASH_AFTER_EXTEND": "disk1"},
    )
    try:
        wait_ready()
        t0 = time.time()
        status, body = http("POST", "/disks/disk1/resize", {"target_mib": 96}, timeout=10)
        elapsed = time.time() - t0
        check(
            "service died during the resize (client sees a broken connection)",
            status == 0,
            {"status_returned": status, "elapsed_s": round(elapsed, 2)},
        )
    except Exception as exc:  # connection reset is the expected outcome
        check(
            "service died during the resize (connection dropped)",
            True,
            type(exc).__name__,
        )
    finally:
        svc.stop()
        time.sleep(0.3)

    check("process exited during the fault", svc.proc.poll() is not None, svc.proc.returncode)
    check(
        "image was lengthened before the crash",
        lengths(d1)[str(d1)] == 96 * MIB and capacities(d1)[str(d1)] == 32 * MIB,
        {"length_mib": lengths(d1)[str(d1)] // MIB, "ext4_mib": capacities(d1)[str(d1)] // MIB},
    )

    # Restart without the fault injection.
    svc = Service(data, state, quota=q)
    try:
        wait_ready()
        status, after_restart = http("GET", "/disks")
        d1_state = next(d for d in after_restart["disks"] if d["disk"] == "disk1")
        check(
            "after restart the disk is still charged at its target length",
            d1_state["quota_charge_mib"] == 96
            and d1_state["ext4_capacity_mib"] == 32
            and d1_state["pending_job_id"] is not None,
            d1_state,
        )
        check(
            "quota reflects the interrupted target, not the old length",
            after_restart["used_mib"] == 96 + 32,
            after_restart["used_mib"],
        )
        status, refused = http("POST", "/disks/disk1/resize", {"target_mib": 112})
        check(
            "a different target for the unfinished disk is refused",
            status == 409 and refused["detail"]["error"] == "disk_busy",
            status,
        )
        status, resumed = http("POST", "/disks/disk1/resize", {"target_mib": 96})
        check(
            "the same target resumes the interrupted job and succeeds",
            status == 200
            and resumed["status"] == "succeeded"
            and resumed["resumed"] is True
            and resumed["result"]["ext4_capacity_mib"] == 96
            and resumed["result"]["image_length_mib"] == 96
            and resumed["result"]["pending_job_id"] is None,
            {"status": status, "body": resumed if status != 200 else resumed["status"]},
        )

        status, jobs = http("GET", "/disks/disk1/jobs")
        check(
            "the job history shows one succeeded job (same job id resumed)",
            len(jobs["jobs"]) == 1 and jobs["jobs"][0]["status"] == "succeeded",
            [(j["job_id"][:8], j["status"], j["target_bytes"] // MIB) for j in jobs["jobs"]],
        )
        check(
            "original files survived the crash and resume",
            snapshot_files_equal(before, image_snapshot(d1, d2)),
            "sha256 of every file compared",
        )
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Extra: validation of the "whole MiB" and unknown-disk behaviour
# ---------------------------------------------------------------------------


def scenario_validation(work: Path, seed: Path) -> None:
    print("\n=== scenario 3: request validation ===")
    data = work / "s3" / "data"
    data.mkdir(parents=True)
    build_images(data, seed)
    svc = Service(data, work / "s3" / "state" / "state.db", quota=128)
    try:
        wait_ready()
        check("missing target is rejected", http("POST", "/disks/disk1/resize", {})[0] == 422)
        check(
            "non-integer target is rejected",
            http("POST", "/disks/disk1/resize", {"target_mib": 48.5})[0] == 422,
        )
        check(
            "shrink is rejected", http("POST", "/disks/disk1/resize", {"target_mib": 16})[0] == 422
        )
        check("unknown disk is 404", http("POST", "/disks/nope/resize", {"target_mib": 48})[0] == 404)
        check("unknown disk status is 404", http("GET", "/disks/nope")[0] == 404)
        check(
            "unknown disk listing is 404", http("GET", "/disks/nope/jobs")[0] == 404
        )
    finally:
        svc.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="ext4-e2e-"))
    seed = ROOT / "seed"
    print(f"workdir: {work}")
    print(f"port: {pick_free_port()}")
    try:
        scenario_quota_competition(work, seed)
        scenario_crash_resume(work, seed)
        scenario_validation(work, seed)
    except Exception:
        traceback.print_exc()
        results.append({"name": "harness crashed", "ok": False, "detail": traceback.format_exc()})
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]
    print(f"\n{passed}/{len(results)} checks passed")
    if failed:
        for r in failed:
            print(f"  FAILED: {r['name']} -> {r['detail']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
