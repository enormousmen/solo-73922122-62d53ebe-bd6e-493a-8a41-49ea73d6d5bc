"""Unit tests for quota accounting that do not need e2fsprogs.

Run with:  python3 -m pytest tests/unit -q      (or python3 -I tests/unit.py)

The service imports the real app module; the ext4 helpers it calls are
monkey-patched with fakes backed by plain files, so the admission logic,
in-flight charging, per-disk locking and restart recovery are exercised
without touching a single real image.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.main as main  # noqa: E402

MIB = main.MIB


class FakeFS:
    """Minimal stand-in for ext4_geometry/ext4_capacity_bytes.

    The fake ext4 capacity lives in a sidecar file, so a simulated restart
    (a second service object on the same directory) sees the same state.
    """

    def __init__(self, images: dict[str, Path]):
        self.images = images
        self.state = images["disk1"].parent / ".fake-ext4"

    def name_of(self, path: Path) -> str:
        for name, p in self.images.items():
            if str(p) == str(path):
                return name
        raise KeyError(path)

    def _load(self) -> dict[str, int]:
        if not self.state.exists():
            return {name: 32 * MIB for name in self.images}
        return {
            line.split("=")[0]: int(line.split("=")[1])
            for line in self.state.read_text().splitlines()
            if line
        }

    def _save(self, values: dict[str, int]) -> None:
        self.state.write_text("".join(f"{k}={v}\n" for k, v in values.items()))

    def geometry(self, path: Path):
        capacity = self._load()[self.name_of(path)]
        return capacity // 4096, 4096

    def resize(self, path: Path, target: int, timeout: int) -> str:
        assert path.stat().st_size >= target
        values = self._load()
        values[self.name_of(path)] = target
        self._save(values)
        return "fake resize2fs"

    def capacity_of(self, path: Path) -> int:
        return self._load()[self.name_of(path)]


def make_service(tmp: Path, quota: int = 160, crash_for: str | None = None, delay_ms: int = 0):
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    images = {}
    for disk in main.DISK_IDS:
        path = data / f"{disk}.ext4"
        if not path.exists():  # keep images from an earlier "process"
            path.write_bytes(b"\0" * (32 * MIB))
            # give the fake superblock a magic so initialize() accepts it
            with path.open("r+b") as fh:
                fh.seek(1024 + 56)
                fh.write(b"\x53\xef")
        images[disk] = path
    settings = main.Settings(
        quota_mib=quota,
        data_dir=data,
        state_db=tmp / "state" / "state.db",
        api_key=None,
        crash_after_extend_for=crash_for,
        command_timeout=30,
        extend_delay_ms=delay_ms,
    )
    fs = FakeFS(images)
    # fake ext4 capacity is already set up for a fresh directory; on restart
    # the sidecar file carries it forward.
    main.ext4_geometry = fs.geometry
    main.ext4_capacity_bytes = fs.capacity_of
    main.fsck_and_resize = fs.resize
    svc = main.DiskResizeService(settings)
    svc.initialize()
    return svc, fs

def test_in_flight_charged_at_target(tmp: Path) -> None:
    async def body():
        svc, fs = make_service(tmp, quota=160, delay_ms=200)
        payload, task = await svc.request_resize("disk1", 80)
        assert payload["resumed"] is False
        status = svc.status()
        disk1 = next(d for d in status["disks"] if d["disk"] == "disk1")
        # Not yet extended, but already charged at the target.
        assert disk1["image_length_mib"] == 32
        assert disk1["quota_charge_mib"] == 80
        assert status["used_mib"] == 80 + 32
        await asyncio.gather(task)
        status = svc.status()
        assert status["used_mib"] == 80 + 32  # disk1 at its new length, disk2 untouched
        assert svc.disk_status("disk1")["ext4_capacity_mib"] == 80
        assert svc.disk_status("disk1")["pending_job_id"] is None
        svc.store.close()

    asyncio.run(body())


def test_refusal_does_not_touch_image(tmp: Path) -> None:
    async def body():
        svc, fs = make_service(tmp, quota=160, delay_ms=100)
        try:
            _, t1 = await svc.request_resize("disk1", 80)
            with_raise = None
            try:
                await svc.request_resize("disk2", 96)
            except main.HTTPException as exc:
                with_raise = exc
            assert with_raise is not None and with_raise.status_code == 409
            assert with_raise.detail["error"] == "quota_exceeded"
            d2 = svc.disk_status("disk2")
            assert d2["image_length_mib"] == 32
            assert d2["ext4_capacity_mib"] == 32
            assert d2["pending_job_id"] is None
            await asyncio.gather(t1)
        finally:
            await svc.drain()

    asyncio.run(body())


def test_recovery_keeps_target_charge(tmp: Path) -> None:
    async def body():
        svc, fs = make_service(tmp, quota=160, crash_for=None)
        # Simulate a crash after extend: file is long, ext4 is still small,
        # and the pending row is durable.
        d1 = svc.disks["disk1"]
        with d1.open("r+b") as fh:
            fh.truncate(96 * MIB)
        svc.store.begin_pending("disk1", "job-crash", 96 * MIB)
        svc.store.close()

        svc2, fs2 = make_service(tmp, quota=160)
        status = svc2.status()
        disk1 = next(d for d in status["disks"] if d["disk"] == "disk1")
        assert disk1["quota_charge_mib"] == 96
        assert disk1["ext4_capacity_mib"] == 32
        assert disk1["pending_job_id"] == "job-crash"
        assert status["used_mib"] == 96 + 32

        # Different target is refused; the same target resumes and succeeds.
        try:
            await svc2.request_resize("disk1", 120)
            raise AssertionError("expected 409")
        except main.HTTPException as exc:
            assert exc.status_code == 409
        payload, task = await svc2.request_resize("disk1", 96)
        assert payload["resumed"] is True and payload["job_id"] == "job-crash"
        await asyncio.gather(task)
        d1 = svc2.disk_status("disk1")
        assert d1["image_length_mib"] == 96 and d1["ext4_capacity_mib"] == 96
        assert d1["pending_job_id"] is None
        assert len(svc2.store.jobs_for("disk1")) == 1
        svc2.store.close()

    asyncio.run(body())


def test_recovery_after_extend_but_before_resize_is_not_rolled_back(tmp: Path) -> None:
    async def body():
        svc, fs = make_service(tmp, quota=160)
        d1 = svc.disks["disk1"]
        with d1.open("r+b") as fh:
            fh.truncate(64 * MIB)
        svc.store.begin_pending("disk1", "job-x", 64 * MIB)
        svc.store.close()
        svc2, _ = make_service(tmp, quota=160)
        # Capacity was never touched, so the file length is charged as-is
        # (64 MiB) even though the ext4 resize never happened.
        status = svc2.status()
        d1s = next(d for d in status["disks"] if d["disk"] == "disk1")
        assert d1s["image_length_mib"] == 64
        assert d1s["quota_charge_mib"] == 64
        svc2.store.close()

    asyncio.run(body())


def test_crash_before_extend_releases_reservation(tmp: Path) -> None:
    async def body():
        svc, fs = make_service(tmp, quota=160)
        svc.store.begin_pending("disk1", "job-y", 80 * MIB)
        svc.store.close()
        svc2, _ = make_service(tmp, quota=160)
        d1 = svc2.disk_status("disk1")
        assert d1["pending_job_id"] is None
        assert d1["quota_charge_mib"] == 32
        svc2.store.close()

    asyncio.run(body())


def test_per_disk_requests_are_independent(tmp: Path) -> None:
    async def body():
        svc, fs = make_service(tmp, quota=160, delay_ms=0)
        (_, t1), (_, t2) = await asyncio.gather(
            svc.request_resize("disk1", 80), svc.request_resize("disk2", 64)
        )
        await asyncio.gather(t1, t2)
        assert svc.disk_status("disk1")["ext4_capacity_mib"] == 80
        assert svc.disk_status("disk2")["ext4_capacity_mib"] == 64
        svc.store.close()

    asyncio.run(body())


def test_validation(tmp: Path) -> None:
    async def body():
        svc, fs = make_service(tmp, quota=160)
        for bad in ({"target_mib": 0}, {"target_mib": -8}, {}):
            try:
                main.ResizeRequest(**bad)
                raise AssertionError(f"accepted {bad}")
            except Exception:
                pass
        try:
            await svc.request_resize("disk1", 32)
            raise AssertionError("shrink accepted")
        except main.HTTPException as exc:
            assert exc.status_code == 422
        svc.store.close()

    asyncio.run(body())


TESTS = [
    test_in_flight_charged_at_target,
    test_refusal_does_not_touch_image,
    test_recovery_keeps_target_charge,
    test_recovery_after_extend_but_before_resize_is_not_rolled_back,
    test_crash_before_extend_releases_reservation,
    test_per_disk_requests_are_independent,
    test_validation,
]


def main_entry() -> int:
    import tempfile
    import traceback

    failed = 0
    for test in TESTS:
        tmp = Path(tempfile.mkdtemp(prefix="unit-"))
        try:
            test(tmp)
            print(f"[PASS] {test.__name__}")
        except Exception:
            failed += 1
            print(f"[FAIL] {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} unit tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_entry())
