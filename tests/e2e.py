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


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def corrupt_primary_magic(image: Path) -> None:
    """Zero the two s_magic bytes at 1024 + 0x38 (= 1080)."""
    with image.open("r+b") as fh:
        fh.seek(1024 + 56)
        assert fh.read(2) == b"\x53\xef", "precondition: image has ext4 magic"
        fh.seek(1024 + 56)
        fh.write(b"\x00\x00")
        fh.flush()
        os.fsync(fh.fileno())


def build_custom_image(
    image: Path, seed_dir: Path, mib: int, block_size: int = 4096,
    groupsize: Optional[int] = None, label: str = "disk1",
) -> None:
    """Create a seeded image with explicit geometry (real mkfs.ext4)."""
    cmd = [
        "mkfs.ext4", "-q", "-F", "-b", str(block_size),
        "-L", label, "-d", str(seed_dir),
    ]
    if groupsize is not None:
        cmd += ["-g", str(groupsize)]
    cmd += [str(image), f"{mib}M"]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    subprocess.run(["truncate", "-s", f"{mib}M", str(image)], check=True)
    subprocess.run(["e2fsck", "-f", "-y", str(image)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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


# ---------------------------------------------------------------------------
# Scenario 4: superblock repair -- marking, quota, concurrency, content
# ---------------------------------------------------------------------------


def scenario_superblock_repair(work: Path, seed: Path) -> None:
    print("\n=== scenario 4: primary superblock magic corruption + repair ===")
    data = work / "s4" / "data"
    data.mkdir(parents=True)
    build_images(data, seed)
    d1, d2 = data / "disk1.ext4", data / "disk2.ext4"
    # Default 32 MiB/4k images have one group and no backup SB; rebuild disk1
    # with compact geometry that places a backup at block 4096.
    build_custom_image(d1, seed / "disk1", mib=32, groupsize=4096)
    backup = 4096

    inventory_before = image_snapshot(d1, d2)
    length_before = d1.stat().st_size
    corrupt_primary_magic(d1)
    sha_corrupt = sha256_file(d1)

    q = 160
    svc = Service(
        data,
        work / "s4" / "state" / "state.db",
        quota=q,
        extra_env={"DISK_REPAIR_DELAY_MS": "1500"},
    )
    try:
        wait_ready()  # service must start despite the bad disk

        status, body = http("GET", "/disks")
        by_id = {d["disk"]: d for d in body["disks"]}
        bad, good = by_id["disk1"], by_id["disk2"]
        check(
            "service starts with a bad disk and GET /disks marks the fault",
            bad["healthy"] is False
            and bool(bad["fault"])
            and bad["superblock_magic_ok"] is False
            and bad["corrupted"] is True,
            {k: bad.get(k) for k in ("healthy", "fault", "corrupted", "state")},
        )
        check(
            "healthy disk is reported healthy",
            good["healthy"] is True and good["fault"] is None,
            good.get("fault"),
        )
        check(
            "bad disk is still quota-charged by its file length",
            bad["image_length_mib"] == 32
            and bad["quota_charge_mib"] == 32
            and body["used_mib"] == 64,
            {"disk1_charge": bad["quota_charge_mib"], "used": body["used_mib"]},
        )

        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            repair_future = pool.submit(
                http,
                "POST",
                "/disks/disk1/repair",
                {"backup_superblock": backup},
                60,
            )
            time.sleep(0.5)
            _, during = http("GET", "/disks/disk1")
            check(
                "repair is marked in flight",
                during["repair_in_flight"] is True,
                during["repair_in_flight"],
            )
            busy_status, busy_body = http(
                "POST", "/disks/disk1/resize", {"target_mib": 48}
            )
            check(
                "expansion of the repairing disk is rejected with 409 disk_busy",
                busy_status == 409
                and busy_body["detail"]["error"] == "disk_busy",
                busy_body,
            )
            # The other disk is unaffected: 32 (disk1 length) + 64 <= 160.
            other_status, other_body = http(
                "POST", "/disks/disk2/resize", {"target_mib": 64}, 120
            )
            check(
                "the other disk is quota-admitted and expands while repair runs",
                other_status == 200
                and other_body["status"] == "succeeded"
                and other_body["result"]["ext4_capacity_mib"] == 64,
                {"status": other_status,
                 "body": other_body if other_status != 200 else other_body["status"]},
            )
            rep_status, rep_body = repair_future.result()

        check(
            "repair request succeeds",
            rep_status == 200
            and rep_body["status"] == "succeeded"
            and rep_body["backup_superblock"] == backup
            and rep_body["read_only_primary_check_passed"] is True,
            {"status": rep_status,
             "body": rep_body if rep_status != 200 else rep_body["status"]},
        )
        check(
            "capacity is queryable again under the same disk id and length is unchanged",
            rep_body["result"]["healthy"] is True
            and rep_body["result"]["fault"] is None
            and rep_body["result"]["ext4_capacity_mib"] == 32
            and rep_body["result"]["image_length_bytes"] == length_before,
            rep_body.get("result"),
        )
        _, after = http("GET", "/disks")
        by_id = {d["disk"]: d for d in after["disks"]}
        check(
            "final status: disk1 healthy 32 MiB, disk2 expanded to 64 MiB",
            by_id["disk1"]["healthy"] is True
            and by_id["disk1"]["ext4_capacity_mib"] == 32
            and by_id["disk2"]["ext4_capacity_mib"] == 64,
            {k: (v["healthy"], v["ext4_capacity_mib"]) for k, v in by_id.items()},
        )
        check(
            "original files remain readable and unchanged after repair",
            snapshot_files_equal(inventory_before, image_snapshot(d1, d2)),
            "sha256 of every file compared",
        )
        check(
            "repaired image differs from the corrupted one (replacement happened)",
            sha256_file(d1) != sha_corrupt,
            None,
        )
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Scenario 5: repair guards -- bad backup, non-4k, missing field, busy
# ---------------------------------------------------------------------------


def scenario_repair_guards(work: Path, seed: Path) -> None:
    print("\n=== scenario 5: repair validation and byte preservation ===")
    data = work / "s5" / "data"
    data.mkdir(parents=True)
    build_images(data, seed)
    d1, d2 = data / "disk1.ext4", data / "disk2.ext4"
    build_custom_image(d1, seed / "disk1", mib=32, groupsize=4096)
    corrupt_primary_magic(d1)
    sha_before = sha256_file(d1)

    svc = Service(data, work / "s5" / "state" / "state.db", quota=160)
    try:
        wait_ready()
        st, body = http("POST", "/disks/disk1/repair", {})
        check("missing backup number is rejected 422", st == 422, st)

        st, body = http(
            "POST", "/disks/disk1/repair", {"backup_superblock": 999999}
        )
        check(
            "invalid backup number is rejected 422",
            st == 422 and body["detail"]["error"] == "invalid_backup_superblock",
            {"status": st, "detail": body.get("detail")},
        )
        check(
            "invalid repair leaves the original image byte-for-byte intact",
            sha256_file(d1) == sha_before,
            None,
        )

        # Healthy disk must not be "repaired".
        st, body = http(
            "POST", "/disks/disk2/repair", {"backup_superblock": 32768}
        )
        check(
            "repair of a healthy disk is refused 409 disk_healthy",
            st == 409 and body["detail"]["error"] == "disk_healthy",
            {"status": st, "error": body.get("detail", {}).get("error")},
        )

        # Unknown disk.
        check(
            "repair of unknown disk is 404",
            http("POST", "/disks/nope/repair", {"backup_superblock": 1})[0] == 404,
            None,
        )
    finally:
        svc.stop()

    # Non-4-KiB image: 1 KiB blocks with a real backup at 8193.
    data1k = work / "s5b" / "data"
    data1k.mkdir(parents=True)
    build_images(data1k, seed)
    d1k = data1k / "disk1.ext4"
    build_custom_image(d1k, seed / "disk1", mib=64, block_size=1024)
    corrupt_primary_magic(d1k)
    sha_1k = sha256_file(d1k)
    svc = Service(data1k, work / "s5b" / "state" / "state.db", quota=160)
    try:
        wait_ready()
        st, body = http(
            "POST", "/disks/disk1/repair", {"backup_superblock": 8193}
        )
        check(
            "non-4-KiB image is rejected with unsupported_block_size",
            st == 422
            and body["detail"]["error"] == "unsupported_block_size"
            and body["detail"]["block_size"] == 1024,
            {"status": st, "detail": body.get("detail")},
        )
        check(
            "non-4-KiB image is untouched by the rejected repair",
            sha256_file(d1k) == sha_1k,
            None,
        )
    finally:
        svc.stop()

    # Unfinished expansion blocks repair.
    data_busy = work / "s5c" / "data"
    data_busy.mkdir(parents=True)
    build_images(data_busy, seed)
    svc = Service(
        data_busy,
        work / "s5c" / "state" / "state.db",
        quota=160,
        extra_env={"DISK_EXTEND_DELAY_MS": "1200"},
    )
    try:
        wait_ready()
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            f = pool.submit(http, "POST", "/disks/disk1/resize",
                            {"target_mib": 80}, 60)
            time.sleep(0.4)
            st, body = http(
                "POST", "/disks/disk1/repair", {"backup_superblock": 4096}
            )
            f.result()
        check(
            "repair is refused while an expansion is unfinished (409 disk_busy)",
            st == 409
            and body["detail"]["error"] == "disk_busy"
            and body["detail"].get("reason") == "unfinished_expansion",
            {"status": st, "detail": body.get("detail")},
        )
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Scenario 6: process exit before replacement -> bytes kept -> retry works
# ---------------------------------------------------------------------------


def scenario_repair_crash_retry(work: Path, seed: Path) -> None:
    print("\n=== scenario 6: exit before replace preserves image, retry after restart ===")
    data = work / "s6" / "data"
    data.mkdir(parents=True)
    build_images(data, seed)
    d1 = data / "disk1.ext4"
    build_custom_image(d1, seed / "disk1", mib=32, groupsize=4096)
    inventory_before = image_snapshot(d1)
    corrupt_primary_magic(d1)
    sha_before = sha256_file(d1)
    state = work / "s6" / "state" / "state.db"

    svc = Service(
        data, state, quota=160,
        extra_env={"DISK_CRASH_BEFORE_REPLACE": "disk1"},
    )
    try:
        wait_ready()
        try:
            status, _ = http(
                "POST", "/disks/disk1/repair",
                {"backup_superblock": 4096}, timeout=15,
            )
        except Exception:
            status = 0
        check(
            "service exits before replacing the image (client sees drop)",
            status == 0 and svc.proc.poll() is not None,
            {"http_status": status, "exit": svc.proc.returncode},
        )
    finally:
        svc.stop()

    check(
        "process hard-exited with 137",
        svc.proc.returncode == 137,
        svc.proc.returncode,
    )
    check(
        "original image is byte-for-byte identical after the pre-replace exit",
        sha256_file(d1) == sha_before,
        {"before": sha_before[:16], "after": sha256_file(d1)[:16]},
    )

    # Restart without fault injection and retry the same repair.
    svc = Service(data, state, quota=160)
    try:
        wait_ready()
        st, body = http(
            "POST", "/disks/disk1/repair", {"backup_superblock": 4096}, 60
        )
        check(
            "repair retried after restart succeeds",
            st == 200 and body["status"] == "succeeded"
            and body["result"]["healthy"] is True,
            {"status": st, "body": body if st != 200 else body["status"]},
        )
        check(
            "files are intact after crash + retry",
            snapshot_files_equal(inventory_before, image_snapshot(d1)),
            "sha256 of every file compared",
        )
        repairs = http("GET", "/disks/disk1/repairs")[1]
        check(
            "repair history records the failed attempt and the success",
            len(repairs["repairs"]) >= 2
            and repairs["repairs"][0]["status"] == "succeeded",
            [(j["status"], (j.get("message") or "")[:40]) for j in repairs["repairs"]],
        )
    finally:
        svc.stop()


# ---------------------------------------------------------------------------
# Scenario 7: canonical standard geometry (256 MiB, backup @ 32768)
# ---------------------------------------------------------------------------


def scenario_repair_standard_geometry(work: Path, seed: Path) -> None:
    print("\n=== scenario 7: standard 256 MiB geometry, backup superblock 32768 ===")
    data = work / "s7" / "data"
    data.mkdir(parents=True)
    build_images(data, seed)
    d1 = data / "disk1.ext4"
    build_custom_image(d1, seed / "disk1", mib=256)  # default groups -> SB @32768
    inventory_before = image_snapshot(d1)
    length_before = d1.stat().st_size
    corrupt_primary_magic(d1)

    svc = Service(data, work / "s7" / "state" / "state.db", quota=320)
    try:
        wait_ready()
        st, body = http(
            "POST", "/disks/disk1/repair", {"superblock": 32768}, 120
        )
        check(
            "canonical backup 32768 repairs the standard image",
            st == 200
            and body["status"] == "succeeded"
            and body["result"]["ext4_capacity_mib"] == 256
            and body["result"]["image_length_bytes"] == length_before,
            {"status": st,
             "body": body if st != 200 else body["result"]["ext4_capacity_mib"]},
        )
        check(
            "files survive the standard-geometry repair",
            snapshot_files_equal(inventory_before, image_snapshot(d1)),
            "sha256 of every file compared",
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
        scenario_superblock_repair(work, seed)
        scenario_repair_guards(work, seed)
        scenario_repair_crash_retry(work, seed)
        scenario_repair_standard_geometry(work, seed)
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
