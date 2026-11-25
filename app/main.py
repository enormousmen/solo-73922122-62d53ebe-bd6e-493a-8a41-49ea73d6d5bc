"""Offline ext4 image expansion API.

Two pre-built, unmounted ext4 images share one configurable quota.  A resize
request states only a whole-MiB target length for a disk.  The API:

  * grows the image file (sparse, via truncate) and then runs
    ``e2fsck -f -y`` + ``resize2fs`` on it, so the ext4 capacity follows;
  * counts an in-flight (accepted but not yet completed) disk at its *target*
    length against the quota, and every other disk at its *actual* file length;
  * accepts at most one job per disk at a time, but different disks run
    concurrently whenever the quota allows it;
  * never accepts a request that would exceed the quota, and leaves the image
    length and ext4 capacity untouched when it refuses;
  * responds successfully only after resize2fs has completed and the ext4
    capacity has been re-read from the image.

Crash recovery: the pending job and its target length are persisted (SQLite)
before the image is touched.  If the process stops after the file was
lengthened but before ext4 was resized, a restart still charges the target
length for that disk (the on-disk file is already that long), and the same
target can be resubmitted to finish the job ("续做").  If the ext4 resize had
already happened, recovery only reconciles bookkeeping.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("diskresize")

MIB = 1024 * 1024
MIN_TARGET_MIB = 16
DISK_IDS = ("disk1", "disk2")

# e2fsprogs does not lock image files, and dumpe2fs must not read a
# superblock while resize2fs is rewriting it.  Tool calls on the same image
# are serialised through a per-path (reentrant) lock; different disks stay
# independent so both can be expanded at the same time.
_PATH_LOCKS: Dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    quota_mib: int
    data_dir: Path
    state_db: Path
    api_key: Optional[str]
    crash_after_extend_for: Optional[str]  # disk id, only for the interruption demo
    command_timeout: int
    extend_delay_ms: int  # widens the in-flight window for demos/tests

    @classmethod
    def from_env(cls) -> "Settings":
        quota = _env_int("DISK_QUOTA_MIB", 256)
        if quota <= 0:
            raise RuntimeError("DISK_QUOTA_MIB must be positive")
        data_dir = Path(os.environ.get("DISK_DATA_DIR", "/data")).resolve()
        state_db = Path(
            os.environ.get("DISK_STATE_DB", str(data_dir / "state" / "state.db"))
        ).resolve()
        key = os.environ.get("DISK_API_KEY") or None
        crash_for = os.environ.get("DISK_CRASH_AFTER_EXTEND") or None
        timeout = _env_int("DISK_COMMAND_TIMEOUT_SEC", 300)
        delay = _env_int("DISK_EXTEND_DELAY_MS", 0)
        return cls(quota, data_dir, state_db, key, crash_for, timeout, delay)


# ---------------------------------------------------------------------------
# ext4 / image helpers (regular files only -- nothing is ever mounted)
# ---------------------------------------------------------------------------


class CommandError(RuntimeError):
    def __init__(self, cmd: List[str], rc: int, output: str):
        super().__init__(f"{' '.join(cmd)} failed with rc={rc}: {output.strip()[-2000:]}")
        self.cmd = cmd
        self.rc = rc
        self.output = output


def _run(cmd: List[str], timeout: int) -> subprocess.CompletedProcess:
    log.info("exec: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        check=False,
    )


def _check_tool(proc: subprocess.CompletedProcess, cmd: List[str]) -> str:
    # e2fsck exit bit 1 means "errors corrected" and is still usable here;
    # anything above 1 is a hard failure, and resize2fs must return 0.
    if proc.returncode > 1 or (cmd[0] != "e2fsck" and proc.returncode != 0):
        raise CommandError(cmd, proc.returncode, proc.stdout)
    return proc.stdout


def file_length(path: Path) -> int:
    return path.stat().st_size


def ext4_geometry(path: Path) -> tuple[int, int]:
    """Return (block_count, block_size) for the ext4 filesystem in ``path``."""
    with lock_for(path):
        cmd = ["dumpe2fs", "-h", str(path)]
        proc = _run(cmd, timeout=60)
    if proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout)
    values: Dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            values[k.strip().lower()] = v.strip()
    try:
        return int(values["block count"]), int(values["block size"])
    except (KeyError, ValueError) as exc:
        raise CommandError(cmd, 0, proc.stdout) from exc


def ext4_capacity_bytes(path: Path) -> int:
    """Bytes addressable by the filesystem = block count * block size."""
    block_count, block_size = ext4_geometry(path)
    return block_count * block_size


def has_ext4_superblock(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            fh.seek(1024 + 56)  # s_magic at offset 0x38 in the superblock
            return fh.read(2) == b"\x53\xef"
    except OSError:
        return False


def fsck_and_resize(path: Path, target_bytes: int, timeout: int) -> str:
    """Grow the ext4 filesystem inside ``path`` to ``target_bytes``.

    The caller must already have lengthened the regular file.  e2fsck runs
    first because resize2fs wants a consistent filesystem for offline growth.
    resize2fs takes a *block count*, not a byte count, so the byte target is
    converted with the filesystem's own block size.
    """
    fsck_cmd = ["e2fsck", "-f", "-y", str(path)]
    resize_cmd = ["resize2fs", str(path), ""]  # filled in under the lock
    with lock_for(path):
        fsck_out = _check_tool(_run(fsck_cmd, timeout), fsck_cmd)
        _, block_size = ext4_geometry(path)
        blocks = target_bytes // block_size
        resize_cmd[-1] = str(blocks)
        resize_out = _check_tool(_run(resize_cmd, timeout), resize_cmd)
    return fsck_out + "\n" + resize_out


def extend_image(path: Path, target_bytes: int) -> None:
    """Lengthen the image file to ``target_bytes`` in place and fsync it.

    Only the file length changes; the ext4 superblock still describes the old
    capacity until resize2fs runs.
    """
    with lock_for(path):
        with path.open("r+b") as fh:
            fh.truncate(target_bytes)
            fh.flush()
            os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------


class Store:
    """SQLite-backed state: per-disk bookkeeping plus one row per job."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS disk (
                    disk_id TEXT PRIMARY KEY,
                    pending_job_id TEXT,
                    pending_target_bytes INTEGER,
                    pending_started_at REAL
                );
                CREATE TABLE IF NOT EXISTS job (
                    job_id TEXT PRIMARY KEY,
                    disk_id TEXT NOT NULL,
                    target_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    message TEXT
                );
                """
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def ensure_disk(self, disk_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT OR IGNORE INTO disk(disk_id) VALUES (?)", (disk_id,))

    def disk_row(self, disk_id: str) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute("SELECT * FROM disk WHERE disk_id=?", (disk_id,)).fetchone()
        if row is None:
            raise KeyError(disk_id)
        return row

    def all_disks(self) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute("SELECT * FROM disk ORDER BY disk_id"))

    def begin_pending(self, disk_id: str, job_id: str, target: int) -> None:
        """Persist the in-flight intent *before* the image is touched."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO job(job_id,disk_id,target_bytes,status,created_at,updated_at)"
                " VALUES (?,?,?,'pending',?,?)",
                (job_id, disk_id, target, now, now),
            )
            self._conn.execute(
                "UPDATE disk SET pending_job_id=?, pending_target_bytes=?, pending_started_at=?"
                " WHERE disk_id=?",
                (job_id, target, now, disk_id),
            )

    def finish_pending(self, disk_id: str, job_id: str, status: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE disk SET pending_job_id=NULL, pending_target_bytes=NULL,"
                " pending_started_at=NULL WHERE disk_id=? AND pending_job_id=?",
                (disk_id, job_id),
            )
            self._conn.execute(
                "UPDATE job SET status=?, updated_at=?, message=? WHERE job_id=?",
                (status, time.time(), message, job_id),
            )

    def mark_job(self, job_id: str, status: str, message: str) -> None:
        """Update only the job row, keeping the quota reservation intact."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE job SET status=?, updated_at=?, message=? WHERE job_id=?",
                (status, time.time(), message, job_id),
            )

    def job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def jobs_for(self, disk_id: str, limit: int = 10) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM job WHERE disk_id=? ORDER BY created_at DESC LIMIT ?",
                (disk_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class DiskState:
    disk_id: str
    path: Path
    length: int
    ext4_bytes: int
    pending_job_id: Optional[str]
    pending_target: Optional[int]
    charging: int  # bytes counted against the quota

    def as_dict(self) -> dict:
        return {
            "disk": self.disk_id,
            "image_length_bytes": self.length,
            "image_length_mib": self.length // MIB,
            "ext4_capacity_bytes": self.ext4_bytes,
            "ext4_capacity_mib": self.ext4_bytes // MIB,
            "pending_job_id": self.pending_job_id,
            "pending_target_mib": (self.pending_target // MIB) if self.pending_target else None,
            "quota_charge_bytes": self.charging,
            "quota_charge_mib": self.charging // MIB,
            "in_flight": self.pending_job_id is not None,
        }


class DiskResizeService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.state_db)
        self.lock = asyncio.Lock()  # serialises admission + quota accounting
        self.disks: Dict[str, Path] = {}
        self.tasks: Dict[str, asyncio.Task] = {}

    # -- setup -------------------------------------------------------------

    def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        for disk_id in DISK_IDS:
            path = self.settings.data_dir / f"{disk_id}.ext4"
            if not path.exists():
                raise RuntimeError(
                    f"missing image {path}; run scripts/make-images.sh or let Docker seed it"
                )
            if not has_ext4_superblock(path):
                raise RuntimeError(f"{path} does not look like an ext4 image")
            self.disks[disk_id] = path
            self.store.ensure_disk(disk_id)
        self._recover()

    def _recover(self) -> None:
        """Reconcile persisted pending jobs with what is actually on disk.

        Nothing is rolled back: a lengthened image stays lengthened and keeps
        being charged at its target length, so a crash mid-job cannot hand out
        free quota.  A pending marker is kept whenever the file is already at
        the target, so the same target can be resubmitted to finish the job.
        """
        for row in self.store.all_disks():
            disk_id = row["disk_id"]
            job_id = row["pending_job_id"]
            if disk_id not in self.disks or job_id is None:
                continue
            path = self.disks[disk_id]
            length = file_length(path)
            target = row["pending_target_bytes"]
            try:
                ext4 = ext4_capacity_bytes(path)
            except Exception:  # pragma: no cover - damaged image
                log.exception("cannot read ext4 capacity of %s during recovery", path)
                ext4 = 0
            log.warning(
                "recovery: %s job=%s target=%s length=%s ext4=%s",
                disk_id, job_id, target, length, ext4,
            )
            if target is not None and ext4 >= target:
                self.store.finish_pending(disk_id, job_id, "succeeded", "recovered")
            elif target is not None and length < target:
                # Died before the file was lengthened: nothing was applied.
                self.store.finish_pending(disk_id, job_id, "failed", "interrupted before extend")
            # Else: lengthened but not resized -> keep pending, charge target.

    # -- state / accounting --------------------------------------------------

    def _snapshot(self) -> dict:
        disks: List[DiskState] = []
        for disk_id in DISK_IDS:
            path = self.disks[disk_id]
            row = self.store.disk_row(disk_id)
            length = file_length(path)
            try:
                ext4 = ext4_capacity_bytes(path)
            except Exception:
                ext4 = 0
            pending = row["pending_job_id"]
            target = row["pending_target_bytes"]
            if pending is not None:
                # In-flight disk: charge the target (the file is at or about
                # to reach that length).
                charging = max(target or 0, length)
            else:
                charging = length
            disks.append(
                DiskState(
                    disk_id=disk_id,
                    path=path,
                    length=length,
                    ext4_bytes=ext4,
                    pending_job_id=pending,
                    pending_target=target,
                    charging=charging,
                )
            )
        used = sum(d.charging for d in disks)
        quota = self.settings.quota_mib * MIB
        return {
            "quota_bytes": quota,
            "quota_mib": self.settings.quota_mib,
            "used_bytes": used,
            "used_mib": used / MIB,
            "free_bytes": max(quota - used, 0),
            "free_mib": max(quota - used, 0) / MIB,
            "disks": disks,
        }

    def status(self) -> dict:
        """Blocking snapshot; call via a thread from request handlers."""
        snap = self._snapshot()
        return {
            "quota_bytes": snap["quota_bytes"],
            "quota_mib": snap["quota_mib"],
            "used_bytes": snap["used_bytes"],
            "used_mib": snap["used_mib"],
            "free_bytes": snap["free_bytes"],
            "free_mib": snap["free_mib"],
            "disks": [d.as_dict() for d in snap["disks"]],
        }

    def disk_status(self, disk_id: str) -> dict:
        for entry in self.status()["disks"]:
            if entry["disk"] == disk_id:
                return entry
        raise KeyError(disk_id)

    # -- admission -----------------------------------------------------------

    async def request_resize(self, disk_id: str, target_mib: int) -> tuple[dict, asyncio.Task]:
        """Admit (or refuse) a resize and return its payload plus worker task."""
        target = target_mib * MIB
        async with self.lock:
            # dumpe2fs runs in a worker thread: it may wait on a per-image
            # lock while the other disk is being resized, and must not stall
            # the event loop for that long.
            snap = await asyncio.to_thread(self._snapshot)
            disk = next(d for d in snap["disks"] if d.disk_id == disk_id)

            if disk.pending_job_id is not None:
                running = self.tasks.get(disk_id)
                if running is not None and not running.done():
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error": "disk_busy",
                            "message": f"{disk_id} already has job {disk.pending_job_id}"
                            " in flight",
                            "pending_target_mib": (
                                disk.pending_target // MIB if disk.pending_target else None
                            ),
                        },
                    )
                if disk.pending_target == target:
                    # The previous service instance died mid-job; the same
                    # target resumes it.
                    job_id = disk.pending_job_id
                    task = self._spawn(disk_id, job_id, target, resumed=True)
                    return {
                        "job_id": job_id,
                        "disk": disk_id,
                        "status": "accepted",
                        "resumed": True,
                        "target_mib": target_mib,
                    }, task
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "disk_busy",
                        "message": f"{disk_id} has unfinished job {disk.pending_job_id}",
                        "pending_target_mib": (
                            disk.pending_target // MIB if disk.pending_target else None
                        ),
                    },
                )

            if target <= disk.length or target <= disk.ext4_bytes:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "not_an_expansion",
                        "message": "target must exceed the current image length"
                        " and ext4 capacity",
                        "current_image_length_mib": disk.length // MIB,
                        "ext4_capacity_mib": disk.ext4_bytes // MIB,
                    },
                )

            # Every disk except this one is charged as it really is; this disk
            # is charged at the requested target if accepted.
            other_used = snap["used_bytes"] - disk.charging
            if other_used + target > snap["quota_bytes"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "quota_exceeded",
                        "message": "request would exceed the shared quota",
                        "quota_mib": snap["quota_mib"],
                        "used_mib": snap["used_mib"],
                        "requested_target_mib": target_mib,
                        "available_mib": (snap["quota_bytes"] - other_used) // MIB,
                    },
                )

            job_id = uuid.uuid4().hex
            self.store.begin_pending(disk_id, job_id, target)  # durable intent first
            task = self._spawn(disk_id, job_id, target, resumed=False)
            return {
                "job_id": job_id,
                "disk": disk_id,
                "status": "accepted",
                "resumed": False,
                "target_mib": target_mib,
            }, task

    def _spawn(self, disk_id: str, job_id: str, target: int, resumed: bool) -> asyncio.Task:
        previous = self.tasks.get(disk_id)
        if previous is not None and not previous.done():
            raise HTTPException(
                status_code=409,
                detail={"error": "disk_busy", "message": f"{disk_id} worker still running"},
            )
        task = asyncio.create_task(
            self._run_job(disk_id, job_id, target, resumed),
            name=f"resize-{disk_id}-{job_id}",
        )
        self.tasks[disk_id] = task
        return task

    # -- worker --------------------------------------------------------------

    async def _run_job(self, disk_id: str, job_id: str, target: int, resumed: bool) -> None:
        path = self.disks[disk_id]
        try:
            if self.settings.extend_delay_ms:
                # Widens the accepted-but-not-yet-extended window so that the
                # in-flight accounting can be observed deterministically.
                await asyncio.sleep(self.settings.extend_delay_ms / 1000)
            if file_length(path) < target:
                await asyncio.to_thread(extend_image, path, target)
                if self.settings.crash_after_extend_for == disk_id and not resumed:
                    log.error(
                        "fault injection: %s extended to %s bytes, exiting before resize2fs",
                        disk_id,
                        target,
                    )
                    # os._exit skips cleanup, like a kill -9 during the job.
                    os._exit(137)
            ext4 = await asyncio.to_thread(ext4_capacity_bytes, path)
            if ext4 < target:
                out = await asyncio.to_thread(
                    fsck_and_resize, path, target, self.settings.command_timeout
                )
                log.info("resize2fs on %s: %s", disk_id, out[-2000:])
            ext4 = await asyncio.to_thread(ext4_capacity_bytes, path)
            if ext4 < target:
                raise RuntimeError(f"ext4 capacity {ext4} below target {target} after resize2fs")
            if file_length(path) < target:
                raise RuntimeError(f"image length below target {target}")
        except Exception as exc:  # noqa: BLE001 - surfaced through job state
            log.exception("resize job %s for %s failed", job_id, disk_id)
            try:
                length = file_length(path)
            except OSError:
                length = 0
            if length >= target:
                # File is already long: keep the reservation so quota stays
                # correct and the same target stays resumable.
                self.store.mark_job(job_id, "pending", f"interrupted: {exc}")
            else:
                self.store.finish_pending(disk_id, job_id, "failed", str(exc))
            return
        self.store.finish_pending(disk_id, job_id, "succeeded", "resize2fs completed")

    async def drain(self) -> None:
        pending = [t for t in self.tasks.values() if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class ResizeRequest(BaseModel):
    target_mib: int = Field(..., description="whole-MiB target image length")

    @field_validator("target_mib")
    @classmethod
    def _whole_mib(cls, v: int) -> int:
        if v < MIN_TARGET_MIB:
            raise ValueError(f"target_mib must be >= {MIN_TARGET_MIB}")
        return v


def create_app(
    settings: Optional[Settings] = None,
    service: Optional[DiskResizeService] = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or DiskResizeService(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, level_name, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        await asyncio.to_thread(service.initialize)
        app.state.service = service
        try:
            yield
        finally:
            await service.drain()
            service.store.close()

    app = FastAPI(title="Offline ext4 Disk Expansion", version="1.0.0", lifespan=lifespan)

    @app.middleware("http")
    async def _auth(request, call_next):
        if settings.api_key and request.url.path != "/healthz":
            if request.headers.get("x-api-key") != settings.api_key:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    def _check_disk(disk: str) -> None:
        if disk not in DISK_IDS:
            raise HTTPException(status_code=404, detail=f"unknown disk {disk!r}")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/disks")
    async def list_disks() -> dict:
        # dumpe2fs is blocking and may wait on the tool lock, so keep it out
        # of the event loop.
        return await asyncio.to_thread(service.status)

    @app.get("/disks/{disk}")
    async def get_disk(disk: str) -> dict:
        _check_disk(disk)
        return await asyncio.to_thread(service.disk_status, disk)

    @app.get("/disks/{disk}/jobs")
    async def get_jobs(disk: str) -> dict:
        _check_disk(disk)
        return {"disk": disk, "jobs": service.store.jobs_for(disk)}

    @app.post("/disks/{disk}/resize")
    async def post_resize(disk: str, body: ResizeRequest) -> dict:
        """Accept, perform and report one resize.  Success is only returned
        after resize2fs has finished and the new ext4 capacity was re-read."""
        _check_disk(disk)
        admission, task = await service.request_resize(disk, body.target_mib)
        # shield: if the client disconnects, the resize itself keeps running and
        # its outcome is recorded; the pending marker covers a hard crash.
        await asyncio.shield(task)
        job = service.store.job(admission["job_id"]) or {}
        if job.get("status") != "succeeded":
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "resize_incomplete",
                    "job_id": admission["job_id"],
                    "status": job.get("status"),
                    "message": job.get("message"),
                    "resumable": job.get("status") == "pending",
                    "disk": service.disk_status(disk),
                },
            )
        return {
            "job_id": admission["job_id"],
            "disk": disk,
            "status": "succeeded",
            "resumed": admission["resumed"],
            "target_mib": body.target_mib,
            "result": service.disk_status(disk),
        }

    return app


# Uvicorn is pointed at ``app.main:create_app`` with ``--factory`` so that
# importing this module has no side effects (no directories, no state DB).
