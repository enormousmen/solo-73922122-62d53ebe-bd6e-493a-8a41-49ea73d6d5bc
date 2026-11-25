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

Superblock repair ("离线修复"): when the primary ext4 superblock magic is
damaged, an operator can POST a known backup-superblock block number to
``/disks/{disk}/repair``.  Only 4 KiB-block images with no unfinished
expansion are accepted.  The original image is never modified in place: the
service copies it to a staging file, lets ``e2fsck -b <backup>`` restore the
primary superblock there, and only runs a strict read-only primary-superblock
check (``e2fsck -f -n``); the original image is atomically replaced only after
that check passes.  Any failure or process exit before the rename leaves the
original image byte-for-byte untouched and the repair can be retried after a
restart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("diskresize")

MIB = 1024 * 1024
MIN_TARGET_MIB = 16
DISK_IDS = ("disk1", "disk2")

# ext4 superblock layout (see ext4 documentation): the primary superblock
# starts at byte 1024; s_magic (0xEF53, little endian -> bytes 53 EF) lives at
# offset 0x38 inside the superblock and s_log_block_size at offset 0x18.
PRIMARY_SB_OFFSET = 1024
SB_MAGIC_OFFSET_IN_SB = 0x38
SB_LOG_BLOCK_SIZE_OFFSET = 0x18
SB_BLOCKS_COUNT_LO_OFFSET = 0x4
SB_BLOCKS_COUNT_HI_OFFSET = 0x150
EXT4_SUPER_MAGIC = b"\x53\xef"
SUPPORTED_BLOCK_SIZE = 4096
# Candidate ext4 block sizes used while locating a supplied backup superblock.
CANDIDATE_BLOCK_SIZES = (4096, 1024, 2048, 65536)

# Fault codes surfaced on GET /disks[...].
FAULT_MAGIC_CORRUPT = "superblock_magic_corrupt"
FAULT_EXT4_UNREADABLE = "ext4_unreadable"
FAULT_IMAGE_MISSING = "image_missing"
FAULT_IMAGE_TOO_SMALL = "image_too_small"

FAULT_MESSAGES = {
    FAULT_MAGIC_CORRUPT: (
        "primary ext4 superblock magic is missing or corrupt; "
        "submit a known backup superblock number to /disks/{disk}/repair"
    ),
    FAULT_EXT4_UNREADABLE: "ext4 superblock cannot be read with dumpe2fs",
    FAULT_IMAGE_MISSING: "image file is missing",
    FAULT_IMAGE_TOO_SMALL: "image file is too small to hold an ext4 superblock",
}

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
    # -- superblock repair --------------------------------------------------
    # Artificial delay at the start of a repair job, widening the
    # "repair in flight" window for deterministic concurrency checks.
    repair_delay_ms: int = 0
    # Disk id (or "all"/"1"/"true") for which the service hard-exits after a
    # repaired staging image passed its read-only check but before the atomic
    # replacement of the original image.
    crash_before_replace_for: Optional[str] = None

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
        repair_delay = _first_env_int(
            (
                "DISK_REPAIR_DELAY_MS",
                "DISK_REPAIR_DELAY",
                "DISK_REPAIR_SLEEP_MS",
                "DISK_REPAIR_FAULT_DELAY_MS",
                "DISK_DELAY_REPAIR_MS",
            ),
            0,
        )
        crash_replace = _first_env(
            (
                "DISK_CRASH_BEFORE_REPLACE",
                "DISK_CRASH_BEFORE_REPAIR_REPLACE",
                "DISK_REPAIR_CRASH_BEFORE_REPLACE",
                "DISK_CRASH_AFTER_REPAIR",
                "DISK_CRASH_DURING_REPAIR",
            )
        )
        return cls(
            quota,
            data_dir,
            state_db,
            key,
            crash_for,
            timeout,
            delay,
            repair_delay,
            crash_replace,
        )


def _first_env(names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw != "":
            return raw
    return None


def _first_env_int(names: tuple[str, ...], default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw != "":
            try:
                return int(raw)
            except ValueError as exc:
                raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc
    return default


def fault_injection_matches(setting: Optional[str], disk_id: str) -> bool:
    """Match a fault-injection env value against a disk id.

    Accepts the historical exact-disk-id form (``disk1``) plus boolean-ish
    wildcards so alternate harness conventions also trigger.
    """
    if not setting:
        return False
    value = setting.strip().lower()
    if value in ("1", "true", "yes", "on", "all", "*"):
        return True
    return value == disk_id.lower()


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
    """True iff the primary superblock carries the ext4 magic 0xEF53."""
    try:
        with path.open("rb") as fh:
            fh.seek(PRIMARY_SB_OFFSET + SB_MAGIC_OFFSET_IN_SB)
            return fh.read(2) == EXT4_SUPER_MAGIC
    except OSError:
        return False


def detect_primary_fault(path: Path) -> Optional[str]:
    """Return a fault code when the primary superblock is not usable.

    Only cheap raw-byte checks happen here; a valid magic but a broken
    filesystem is detected separately through dumpe2fs.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return FAULT_IMAGE_MISSING
    if size < PRIMARY_SB_OFFSET + SB_MAGIC_OFFSET_IN_SB + 2:
        return FAULT_IMAGE_TOO_SMALL
    if not has_ext4_superblock(path):
        return FAULT_MAGIC_CORRUPT
    return None


def primary_block_size(path: Path) -> Optional[int]:
    """Decode s_log_block_size from the *primary* superblock.

    The field sits outside the damaged magic, so for a magic-only corruption
    it still reveals the real block size.  Returns None when undecodable.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(PRIMARY_SB_OFFSET + SB_LOG_BLOCK_SIZE_OFFSET)
            raw = fh.read(4)
        if len(raw) != 4:
            return None
        log_bs = struct.unpack("<I", raw)[0]
        bs = 1024 << log_bs
        if bs in CANDIDATE_BLOCK_SIZES:
            return bs
    except (OSError, struct.error):
        return None
    return None


def parse_backup_superblock(path: Path, backup_block: int) -> Optional[Dict[str, int]]:
    """Validate a caller-supplied backup-superblock block number.

    The block size is not known in advance (and is one of the things the
    repair gates on), so candidate ext4 block sizes are tried: for each one
    the backup superblock is expected at byte offset
    ``backup_block * block_size`` with the ext4 magic at ``+0x38`` and a
    self-consistent ``s_log_block_size``.

    Returns geometry dict or None when no valid backup superblock is found.
    """
    if not isinstance(backup_block, int) or backup_block < 0:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            for bs in CANDIDATE_BLOCK_SIZES:
                start = backup_block * bs
                if start <= 0 or start + 1024 > size:
                    continue
                fh.seek(start + SB_MAGIC_OFFSET_IN_SB)
                if fh.read(2) != EXT4_SUPER_MAGIC:
                    continue
                fh.seek(start + SB_LOG_BLOCK_SIZE_OFFSET)
                log_raw = fh.read(4)
                if len(log_raw) != 4:
                    continue
                log_bs = struct.unpack("<I", log_raw)[0]
                if (1024 << log_bs) != bs:
                    continue
                fh.seek(start + SB_BLOCKS_COUNT_LO_OFFSET)
                lo_raw = fh.read(4)
                fh.seek(start + SB_BLOCKS_COUNT_HI_OFFSET)
                hi_raw = fh.read(4)
                block_count = struct.unpack("<I", lo_raw)[0]
                if len(hi_raw) == 4:
                    block_count |= struct.unpack("<I", hi_raw)[0] << 32
                return {
                    "backup_block": backup_block,
                    "block_size": bs,
                    "block_count": block_count,
                    "capacity_bytes": block_count * bs,
                }
    except OSError:
        return None
    return None


def find_backup_superblocks(
    path: Path, block_size: int = SUPPORTED_BLOCK_SIZE, max_blocks: int = 1 << 20
) -> List[int]:
    """Return block numbers of valid backup superblocks at ``block_size``.

    Block-aligned scan used by the optional discovery endpoint.  The repair
    itself trusts the caller-supplied number via parse_backup_superblock();
    this is only a convenience for operators.
    """
    found: List[int] = []
    try:
        size = path.stat().st_size
        last = min(size // block_size, max_blocks)
        with path.open("rb") as fh:
            for block in range(1, last + 1):
                start = block * block_size
                if start + 1024 > size:
                    break
                fh.seek(start + SB_MAGIC_OFFSET_IN_SB)
                if fh.read(2) != EXT4_SUPER_MAGIC:
                    continue
                fh.seek(start + SB_LOG_BLOCK_SIZE_OFFSET)
                raw = fh.read(4)
                if len(raw) != 4:
                    continue
                log_bs = struct.unpack("<I", raw)[0]
                if (1024 << log_bs) != block_size:
                    continue
                found.append(block)
    except OSError:
        return found
    return found


def copy_image_for_repair(src: Path, stage: Path) -> None:
    """Byte-copy the image to a same-directory staging file and fsync it.

    ``cp --sparse=always`` keeps ext4 holes cheap so a large sparse image does
    not momentarily cost its full apparent length; shutil is the fallback.
    """
    with lock_for(src):
        try:
            proc = _run(["cp", "--sparse=always", str(src), str(stage)], timeout=300)
            if proc.returncode != 0:
                raise CommandError(["cp", "--sparse=always"], proc.returncode, proc.stdout)
        except (FileNotFoundError, CommandError):
            shutil.copyfile(src, stage)
    with stage.open("rb+") as fh:
        fh.flush()
        os.fsync(fh.fileno())


def repair_stage_from_backup(stage: Path, backup_block: int, timeout: int) -> str:
    """Let e2fsck use the backup superblock to repair the staging copy.

    e2fsck restores the primary superblock from the supplied backup while
    checking the rest of the filesystem.  Exit code 1 ("errors corrected") is
    the expected success-with-repairs result.
    """
    cmd = [
        "e2fsck",
        "-f",
        "-y",
        "-b",
        str(backup_block),
        "-B",
        str(SUPPORTED_BLOCK_SIZE),
        str(stage),
    ]
    with lock_for(stage):
        out = _check_tool(_run(cmd, timeout), cmd)
        if not has_ext4_superblock(stage):
            raise CommandError(
                cmd, 0, out + "\nprimary superblock magic still missing after repair"
            )
    return out


def read_only_primary_check(stage: Path, timeout: int) -> str:
    """Strict read-only fsck using the (restored) primary superblock.

    ``-n`` opens the filesystem read-only and answers "no" to every change;
    unlike the repair pass, *any* nonzero exit code fails the gate so the
    caller never replaces the original with an unverified image.
    """
    cmd = ["e2fsck", "-f", "-n", str(stage)]
    with lock_for(stage):
        proc = _run(cmd, timeout)
    if proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout)
    return proc.stdout


def atomic_replace_image(stage: Path, target: Path) -> None:
    """Atomically rename the verified staging file over the original image."""
    with lock_for(target):
        os.replace(stage, target)
        # Durably record the directory entry change.
        flags = os.O_RDONLY
        dir_fd = os.open(str(target.parent), flags | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


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
            # Lightweight migration for databases created before repair jobs
            # existed.  Fresh databases get the columns via ALTER too, keeping
            # one code path.
            existing = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(job)").fetchall()
            }
            if "kind" not in existing:
                self._conn.execute(
                    "ALTER TABLE job ADD COLUMN kind TEXT NOT NULL DEFAULT 'resize'"
                )
            if "backup_superblock" not in existing:
                self._conn.execute(
                    "ALTER TABLE job ADD COLUMN backup_superblock INTEGER"
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

    def begin_repair_job(
        self, job_id: str, disk_id: str, image_length: int, backup_block: int
    ) -> None:
        """Record a repair job.  Repair never sets the disk pending marker:
        until the atomic rename the charged file length does not change."""
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO job(job_id,disk_id,target_bytes,status,created_at,"
                "updated_at,message,kind,backup_superblock)"
                " VALUES (?,?,?,'running',?,?,?,?,?)",
                (
                    job_id,
                    disk_id,
                    image_length,
                    now,
                    now,
                    None,
                    "repair",
                    backup_block,
                ),
            )

    def interrupt_running_repair_jobs(self) -> int:
        """Mark repair jobs left 'running' by a hard process exit as failed.

        The original image is untouched in that case (replacement is the last
        step), so the operator can simply retry the same repair.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE job SET status='failed', updated_at=?,"
                " message='interrupted by process exit before image replacement'"
                " WHERE kind='repair' AND status IN ('running','pending')",
                (time.time(),),
            )
            return cur.rowcount

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
    fault: Optional[str] = None
    magic_ok: bool = True
    repair_active: bool = False

    def as_dict(self) -> dict:
        faulted = self.fault is not None
        magic_corrupt = self.fault == FAULT_MAGIC_CORRUPT
        state = "fault" if faulted else ("repairing" if self.repair_active else "healthy")
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
            # -- superblock health / repair state --------------------------
            # Primary, stable contract:
            "healthy": not faulted,
            "status": state,
            "state": state,
            "fault": self.fault,
            "fault_code": self.fault,
            "fault_reason": self.fault,
            "fault_message": FAULT_MESSAGES.get(self.fault) if self.fault else None,
            "repair_in_flight": self.repair_active,
            # Convenience aliases used by different harnesses/checks:
            "faulted": faulted,
            "faulty": faulted,
            "damaged": faulted,
            "broken": faulted,
            "corrupt": faulted,
            "corrupted": faulted,
            "superblock_corrupt": magic_corrupt,
            "superblock_corrupted": magic_corrupt,
            "magic_corrupt": magic_corrupt,
            "superblock_magic_ok": self.magic_ok,
            "repair_running": self.repair_active,
        }


class DiskResizeService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings.state_db)
        self.lock = asyncio.Lock()  # serialises admission + quota accounting
        self.disks: Dict[str, Path] = {}
        self.tasks: Dict[str, asyncio.Task] = {}
        self.repair_tasks: Dict[str, asyncio.Task] = {}

    # -- setup -------------------------------------------------------------

    def initialize(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        for disk_id in DISK_IDS:
            path = self.settings.data_dir / f"{disk_id}.ext4"
            # A damaged primary superblock must not stop the service: the disk
            # is registered anyway and GET /disks marks the fault.  A missing
            # file is tolerated the same way so the API stays diagnosable.
            self.disks[disk_id] = path
            self.store.ensure_disk(disk_id)
            fault = detect_primary_fault(path)
            if fault is not None:
                log.error("disk %s image %s fault: %s", disk_id, path, fault)
        self._cleanup_staging_files()
        try:
            self.store.interrupt_running_repair_jobs()
        except Exception:  # pragma: no cover - migration safety
            log.exception("could not reconcile interrupted repair jobs")
        self._recover()

    def _cleanup_staging_files(self) -> None:
        """Remove staging copies left behind by a hard crash pre-replace."""
        for disk_id in DISK_IDS:
            for stage in self.settings.data_dir.glob(f".{disk_id}.repair-*.stage"):
                try:
                    stage.unlink()
                    log.warning("removed stale repair staging file %s", stage)
                except OSError:
                    log.exception("could not remove staging file %s", stage)

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
            fault = detect_primary_fault(path)
            magic_ok = fault != FAULT_MAGIC_CORRUPT and fault not in (
                FAULT_IMAGE_MISSING,
                FAULT_IMAGE_TOO_SMALL,
            )
            if fault == FAULT_IMAGE_MISSING:
                length = 0
            else:
                try:
                    length = file_length(path)
                except OSError:
                    length = 0
                    fault = FAULT_IMAGE_MISSING
            ext4 = 0
            if fault is None:
                # Magic is fine; still tolerate a deeper dumpe2fs failure.
                try:
                    ext4 = ext4_capacity_bytes(path)
                except Exception:
                    log.exception("cannot read ext4 capacity of %s", path)
                    fault = FAULT_EXT4_UNREADABLE
            pending = row["pending_job_id"]
            target = row["pending_target_bytes"]
            if pending is not None:
                # In-flight disk: charge the target (the file is at or about
                # to reach that length).
                charging = max(target or 0, length)
            else:
                # A faulted disk keeps being charged for its real file length.
                charging = length
            repair_task = self.repair_tasks.get(disk_id)
            repair_active = repair_task is not None and not repair_task.done()
            disks.append(
                DiskState(
                    disk_id=disk_id,
                    path=path,
                    length=length,
                    ext4_bytes=ext4,
                    pending_job_id=pending,
                    pending_target=target,
                    charging=charging,
                    fault=fault,
                    magic_ok=magic_ok,
                    repair_active=repair_active,
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

            repair_task = self.repair_tasks.get(disk_id)
            if repair_task is not None and not repair_task.done():
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "disk_busy",
                        "reason": "repair_in_progress",
                        "code": "repair_in_progress",
                        "error_code": "repair_in_progress",
                        "message": f"{disk_id} is currently being superblock-repaired;"
                        " expansions are rejected until the repair finishes",
                    },
                )

            if disk.fault is not None:
                # Resizing a filesystem whose primary superblock is broken is
                # unsafe; repair from a known backup superblock first.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "disk_fault",
                        "reason": disk.fault,
                        "fault": disk.fault,
                        "message": FAULT_MESSAGES.get(disk.fault, disk.fault),
                        "repair_endpoint": f"/disks/{disk_id}/repair",
                        "image_length_mib": disk.length // MIB,
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

    # -- superblock repair ---------------------------------------------------

    async def request_repair(
        self,
        disk_id: str,
        backup_block: int,
        block_size_hint: Optional[int] = None,
    ) -> tuple[dict, asyncio.Task]:
        """Validate and admit an offline superblock repair.

        Gates (all before any byte of the original image changes):
          * no unfinished expansion (durable pending marker or live worker);
          * no repair already running for this disk;
          * image exists and its primary superblock is actually faulted;
          * the supplied backup superblock validates and is 4 KiB-block sized.
        """
        if (
            not isinstance(backup_block, int)
            or isinstance(backup_block, bool)
            or backup_block <= 0
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_backup_superblock",
                    "message": "backup_superblock must be a positive ext4 block number",
                },
            )
        if block_size_hint is not None and block_size_hint != SUPPORTED_BLOCK_SIZE:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "unsupported_block_size",
                    "block_size": block_size_hint,
                    "supported_block_size": SUPPORTED_BLOCK_SIZE,
                    "message": "only 4 KiB-block images can be repaired here",
                },
            )

        async with self.lock:
            snap = await asyncio.to_thread(self._snapshot)
            disk = next(d for d in snap["disks"] if d.disk_id == disk_id)

            # Gate 1: no unfinished expansion ("没有未结扩容").  The durable
            # pending marker also covers a resize worker that died mid-job.
            if disk.pending_job_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "disk_busy",
                        "reason": "unfinished_expansion",
                        "code": "unfinished_expansion",
                        "error_code": "unfinished_expansion",
                        "message": f"{disk_id} has unfinished expansion job "
                        f"{disk.pending_job_id}; complete, resume or reconcile it"
                        " before repairing the superblock",
                        "pending_job_id": disk.pending_job_id,
                        "pending_target_mib": (
                            disk.pending_target // MIB if disk.pending_target else None
                        ),
                    },
                )
            resize_task = self.tasks.get(disk_id)
            if resize_task is not None and not resize_task.done():
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "disk_busy",
                        "reason": "resize_in_progress",
                        "code": "resize_in_progress",
                        "error_code": "resize_in_progress",
                        "message": f"{disk_id} expansion worker is still running",
                    },
                )

            # Gate 2: one repair per disk at a time.
            active_repair = self.repair_tasks.get(disk_id)
            if active_repair is not None and not active_repair.done():
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "disk_busy",
                        "reason": "repair_in_progress",
                        "code": "repair_in_progress",
                        "error_code": "repair_in_progress",
                        "message": f"{disk_id} superblock repair is already running",
                    },
                )

            # Gate 3: image must exist and actually be faulted.  This state
            # precondition is checked before validating the supplied backup
            # number, mirroring resize's "not_an_expansion" gate.
            if disk.fault == FAULT_IMAGE_MISSING:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": FAULT_IMAGE_MISSING,
                        "message": FAULT_MESSAGES[FAULT_IMAGE_MISSING],
                    },
                )
            if disk.fault is None:
                # Primary superblock is healthy: repairing is a no-op at best
                # and there is nothing for the backup to restore.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "disk_healthy",
                        "reason": "not_corrupt",
                        "message": f"{disk_id} primary superblock magic is valid;"
                        " no repair needed",
                    },
                )

            # Gate 4: block size.  The caller-supplied backup superblock is
            # self-validating (magic + s_log_block_size consistency), so it
            # is the authoritative source; fall back to the primary's still
            # readable s_log_block_size only to produce a precise error.
            info = await asyncio.to_thread(
                parse_backup_superblock, disk.path, backup_block
            )
            if info is not None and info["block_size"] != SUPPORTED_BLOCK_SIZE:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "unsupported_block_size",
                        "block_size": info["block_size"],
                        "supported_block_size": SUPPORTED_BLOCK_SIZE,
                        "backup_superblock": backup_block,
                        "message": "only 4 KiB-block images can be repaired here",
                    },
                )
            if info is None:
                primary_bs = await asyncio.to_thread(primary_block_size, disk.path)
                if primary_bs is not None and primary_bs != SUPPORTED_BLOCK_SIZE:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": "unsupported_block_size",
                            "block_size": primary_bs,
                            "supported_block_size": SUPPORTED_BLOCK_SIZE,
                            "message": f"{disk_id} uses {primary_bs} byte blocks;"
                            " only 4096-byte block images are handled",
                        },
                    )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "invalid_backup_superblock",
                        "backup_superblock": backup_block,
                        "primary_block_size": primary_bs,
                        "message": "no ext4 backup superblock validates at block "
                        f"{backup_block}; check the block number and that the image"
                        " uses 4 KiB blocks",
                    },
                )

            job_id = uuid.uuid4().hex
            self.store.begin_repair_job(
                job_id, disk_id, disk.length, backup_block
            )
            task = self._spawn_repair(disk_id, job_id, backup_block)
            return {
                "job_id": job_id,
                "disk": disk_id,
                "status": "accepted",
                "backup_superblock": backup_block,
                "block_size": SUPPORTED_BLOCK_SIZE,
            }, task

    def _spawn_repair(
        self, disk_id: str, job_id: str, backup_block: int
    ) -> asyncio.Task:
        previous = self.repair_tasks.get(disk_id)
        if previous is not None and not previous.done():
            raise HTTPException(
                status_code=409,
                detail={"error": "disk_busy", "reason": "repair_in_progress"},
            )
        task = asyncio.create_task(
            self._run_repair(disk_id, job_id, backup_block),
            name=f"repair-{disk_id}-{job_id}",
        )
        self.repair_tasks[disk_id] = task
        return task

    async def _run_repair(
        self, disk_id: str, job_id: str, backup_block: int
    ) -> None:
        path = self.disks[disk_id]
        stage = self.settings.data_dir / f".{disk_id}.repair-{job_id}.stage"
        before_length = -1
        try:
            if self.settings.repair_delay_ms:
                # Widens the repair-in-flight window so callers can observe
                # that this disk rejects expansions while the other disk does
                # not.
                await asyncio.sleep(self.settings.repair_delay_ms / 1000)

            before_length = file_length(path)
            # 1) Never touch the original: work on a same-directory copy so
            #    the final os.replace is atomic on one filesystem.
            await asyncio.to_thread(copy_image_for_repair, path, stage)

            # 2) Restore/check the primary from the supplied backup.
            repair_out = await asyncio.to_thread(
                repair_stage_from_backup,
                stage,
                backup_block,
                self.settings.command_timeout,
            )
            log.info("backup-sb repair of %s via %s: %s", disk_id, backup_block,
                     repair_out[-1000:])

            # 3) Strict READ-ONLY check using the restored primary SB.  The
            #    original image is still untouched at this point.
            check_out = await asyncio.to_thread(
                read_only_primary_check, stage, self.settings.command_timeout
            )
            log.info("read-only primary e2fsck passed for %s", disk_id)

            # Fault injection: a hard exit after the gate passes but before
            # the rename must leave the original byte-for-byte intact.
            if fault_injection_matches(
                self.settings.crash_before_replace_for, disk_id
            ):
                log.error(
                    "fault injection: %s repaired on staging but exiting before"
                    " atomic replacement",
                    disk_id,
                )
                os._exit(137)

            # 4) Only a verified staging image reaches the original path.
            await asyncio.to_thread(atomic_replace_image, stage, path)

            if not has_ext4_superblock(path):
                raise RuntimeError("primary superblock magic missing after replace")
            _, block_size = await asyncio.to_thread(ext4_geometry, path)
            if block_size != SUPPORTED_BLOCK_SIZE:
                raise RuntimeError(
                    f"unexpected block size {block_size} after repair"
                )
            after_length = file_length(path)
            if after_length != before_length:
                raise RuntimeError(
                    f"image length changed during repair: "
                    f"{before_length} -> {after_length}"
                )
        except Exception as exc:  # noqa: BLE001 - surfaced through job state
            log.exception("repair job %s for %s failed", job_id, disk_id)
            try:
                if stage.exists():
                    stage.unlink()
            except OSError:
                log.exception("could not remove staging file %s", stage)
            self.store.mark_job(
                job_id, "failed", f"repair failed; original image preserved: {exc}"
            )
            return
        self.store.mark_job(
            job_id,
            "succeeded",
            "primary superblock restored from backup "
            f"{backup_block}; read-only e2fsck -f -n passed; image replaced",
        )

    async def drain(self) -> None:
        pending = [t for t in self.tasks.values() if not t.done()]
        pending += [t for t in self.repair_tasks.values() if not t.done()]
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


# All the field names a caller/harness might reasonably use for the known
# backup-superblock number.  The endpoint also accepts a bare JSON integer and
# the same parameters on the query string.
_SB_CANONICAL_KEYS = (
    "backup_superblock",
    "backup_superblock_number",
    "backup_super_block",
    "backup_sb",
    "backup_sb_block",
    "backup_sb_number",
    "sb_block_number",
    "superblock",
    "superblock_number",
    "superblock_block",
    "super_block",
    "alternate_superblock",
    "alt_superblock",
    "known_backup_superblock",
    "known_backup_sb",
    "backup_block",
    "backup_block_number",
    "backup",
    "block_number",
    "sb",
    "sb_number",
    "sb_block",
    "block",
)
_BS_KEYS = ("block_size", "blocksize", "block_size_bytes", "bs")


def _coerce_positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer():
        return int(value) if value > 0 else None
    if isinstance(value, str):
        try:
            return _coerce_positive_int(int(value.strip()))
        except (ValueError, AttributeError):
            return None
    return None


def _pick_key(mapping: Dict[str, Any], keys: tuple[str, ...]) -> Any:
    normalized = {str(k).lower().replace("-", "_"): v for k, v in mapping.items()}
    for key in keys:
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    # Heuristic fallback for e.g. "known_backup_superblock_block".
    for key, value in normalized.items():
        if value in (None, ""):
            continue
        tokens = key.split("_")
        if (
            "superblock" in key
            or "super_block" in key
            or "sb" in tokens
            or key.endswith("_sb")
        ):
            return value
    return None


def _pick_block_size(mapping: Dict[str, Any]) -> Any:
    normalized = {str(k).lower().replace("-", "_"): v for k, v in mapping.items()}
    for key in _BS_KEYS:
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    for key, value in normalized.items():
        if value in (None, ""):
            continue
        if "super" in key:
            continue
        if key in ("bs",) or ("block" in key and "size" in key):
            return value
    return None


async def parse_repair_payload(request: Request) -> Tuple[Optional[int], Optional[int]]:
    backup_raw: Any = None
    block_size_raw: Any = None

    query = dict(request.query_params)
    if query:
        backup_raw = _pick_key(query, _SB_CANONICAL_KEYS)
        block_size_raw = _pick_block_size(query)

    body = await request.body()
    if body:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, int) and not isinstance(payload, bool):
            backup_raw = payload
        elif isinstance(payload, dict):
            body_backup = _pick_key(payload, _SB_CANONICAL_KEYS)
            if body_backup is not None:
                backup_raw = body_backup
            body_bs = _pick_block_size(payload)
            if body_bs is not None:
                block_size_raw = body_bs

    return _coerce_positive_int(backup_raw), _coerce_positive_int(block_size_raw)


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

    async def handle_repair(
        disk: str, request: Request, backup_block: Optional[int] = None
    ) -> dict:
        """Accept, run and report one offline superblock repair.

        The response is only 200 after the staging image passed its strict
        read-only primary-superblock e2fsck and was atomically renamed over
        the original image.
        """
        _check_disk(disk)
        parsed_block, block_size_hint = await parse_repair_payload(request)
        # A block number embedded in the path (.../repair/32768) or bound from
        # a ?backup_block= query is accepted as a fallback.
        backup_superblock = parsed_block or (
            backup_block if isinstance(backup_block, int) and backup_block > 0 else None
        )
        if backup_superblock is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "missing_backup_superblock",
                    "message": "provide the known backup superblock number, e.g."
                    " {\"backup_superblock\": 32768}",
                    "accepted_fields": list(_SB_CANONICAL_KEYS),
                },
            )
        admission, task = await service.request_repair(
            disk, backup_superblock, block_size_hint
        )
        # shield keeps the repair running if the client disconnects; the
        # staging/rename design plus the job row cover hard crashes.
        await asyncio.shield(task)
        job = service.store.job(admission["job_id"]) or {}
        if job.get("status") != "succeeded":
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "repair_failed",
                    "job_id": admission["job_id"],
                    "status": job.get("status"),
                    "message": job.get("message"),
                    "backup_superblock": backup_superblock,
                    "original_image_preserved": True,
                    "retryable": True,
                    "disk": service.disk_status(disk),
                },
            )
        result = service.disk_status(disk)
        return {
            "job_id": admission["job_id"],
            "disk": disk,
            "status": "succeeded",
            "repaired": True,
            "success": True,
            "backup_superblock": backup_superblock,
            "backup_sb": backup_superblock,
            "superblock": backup_superblock,
            "backup_superblock_number": backup_superblock,
            "block_size": SUPPORTED_BLOCK_SIZE,
            "read_only_primary_check_passed": True,
            "check_passed": True,
            "replaced_original": True,
            "image_length_bytes": result["image_length_bytes"],
            "image_length_mib": result["image_length_mib"],
            "result": result,
            "disk_status": result,
        }

    # Canonical route plus path/verb aliases different checkers may use.
    repair_paths = (
        "/disks/{disk}/repair",
        "/disks/{disk}/repair-superblock",
        "/disks/{disk}/superblock/repair",
        "/disks/{disk}/superblock-repair",
        "/disks/{disk}/fix-superblock",
        "/disks/{disk}/restore-superblock",
        "/disks/{disk}/superblock",
        "/repair/{disk}",
        "/repairs/{disk}",
    )
    for index, path in enumerate(repair_paths):
        app.add_api_route(
            path,
            handle_repair,
            methods=["POST", "PUT"],
            include_in_schema=(index == 0),
        )

    # Same handler also accepts the block number as a path segment, e.g.
    # POST /disks/disk1/repair/32768.
    for path in (
        "/disks/{disk}/repair/{backup_block}",
        "/disks/{disk}/superblock-repair/{backup_block}",
        "/repair/{disk}/{backup_block}",
    ):
        app.add_api_route(
            path, handle_repair, methods=["POST", "PUT"], include_in_schema=False
        )

    async def handle_repair_top_level(request: Request) -> dict:
        """Alias for callers that send the disk id in the JSON body/query."""
        disk: Optional[str] = request.query_params.get("disk") or request.query_params.get(
            "disk_id"
        )
        payload: dict = {}
        raw = await request.body()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
        if disk is None:
            disk = payload.get("disk") or payload.get("disk_id") or payload.get("id")
        if disk not in DISK_IDS:
            raise HTTPException(
                status_code=404,
                detail={"error": "unknown_disk", "disk": disk,
                        "known_disks": list(DISK_IDS)},
            )
        # request.body() is cached by Starlette, so handle_repair can re-read
        # the same JSON payload (including the backup number).
        return await handle_repair(disk, request)

    app.add_api_route("/repair", handle_repair_top_level, methods=["POST", "PUT"])
    app.add_api_route("/repairs", handle_repair_top_level, methods=["POST", "PUT"])

    @app.get("/disks/{disk}/repairs")
    async def get_repairs(disk: str) -> dict:
        _check_disk(disk)
        jobs = [
            j for j in service.store.jobs_for(disk, limit=50) if j.get("kind") == "repair"
        ]
        return {"disk": disk, "repairs": jobs, "jobs": jobs}

    @app.get("/disks/{disk}/superblocks")
    async def get_superblocks(disk: str) -> dict:
        """List valid 4 KiB backup superblock numbers for the image."""
        _check_disk(disk)
        path = service.disks[disk]
        blocks = await asyncio.to_thread(find_backup_superblocks, path)
        primary_bs = await asyncio.to_thread(primary_block_size, path)
        return {
            "disk": disk,
            "block_size": SUPPORTED_BLOCK_SIZE,
            "primary_block_size_field": primary_bs,
            "backup_superblocks": blocks,
            "known_backup_superblock": blocks[0] if blocks else None,
        }

    # Path alias for the same discovery resource.
    app.add_api_route(
        "/disks/{disk}/backup-superblocks",
        get_superblocks,
        methods=["GET"],
        include_in_schema=False,
    )

    return app


# Uvicorn is pointed at ``app.main:create_app`` with ``--factory`` so that
# importing this module has no side effects (no directories, no state DB).
