"""Bounded shared-model Save Copy tests (batch D), solver-free.

The fake-volume rows reuse the canonical shared-session harness
(``_manager`` and ``_attach_and_lock`` in ``test_shared_session_lifecycle``)
and the fake volume, so every boundary and cleanup path is deterministic
and needs no elevation. The real diskpart volume has its own admin-gated
tests at the bottom.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from pathlib import Path as _Path

import pytest

from comsol_mcp.shared_session import bounded_save
from comsol_mcp.shared_session.bounded_save import (
    FakeVolume,
    VolumePreparationFailed,
    prepare_bounded_writer,
)

sys.path.insert(0, str(_Path(__file__).resolve().parent))
from test_shared_session_lifecycle import _attach_and_lock, _manager

CAP = 8192  # a multiple of the write granularity, so the bound equals the cap


class WritingJavaModel:
    """A stand-in for Model.java that writes through a byte budget."""

    def __init__(self, tag: str, payload: bytes, *, crash_after_write: bool = False):
        self._tag = tag
        self._payload = payload
        self._crash = crash_after_write
        self.volume: FakeVolume | None = None

    def tag(self) -> str:
        return self._tag

    def save(self, path: str, _copy: bool) -> None:
        if self.volume is None:
            raise AssertionError("the bounded writer must prepare a volume first")
        self.volume.write(self._payload)
        if self._crash:
            raise RuntimeError("server exploded mid-save")


class WritingMphModel:
    def __init__(self, tag: str, payload: bytes, *, crash_after_write: bool = False):
        self.java = WritingJavaModel(tag, payload, crash_after_write=crash_after_write)


class ModelsClient:
    def __init__(self, models):
        self._models = models

    def models(self):
        return self._models


class BoundedProvider:
    """Prepares a FakeVolume-backed writer for one declared cap."""

    def __init__(
        self,
        root: Path,
        volumes: list[FakeVolume],
        *,
        fail: bool = False,
        model: "WritingMphModel | None" = None,
    ):
        self.root = root
        self.volumes = volumes
        self.fail = fail
        self.model = model
        self.caps: list[int] = []

    def __call__(self, cap: int) -> dict:
        self.caps.append(cap)
        if self.fail:
            return {
                "available": False,
                "reason_code": "elevation_required",
                "detail": "attaching the capped volume needs an elevated host",
                "write_time_bound_bytes": bounded_save.write_time_bound(cap),
                "granularity_bytes": bounded_save.CLUSTER_BYTES,
                "bound_rule": bounded_save.BOUND_RULE,
            }

        def factory(local_cap: int, work: Path) -> FakeVolume:
            volume = FakeVolume(local_cap, work)
            self.volumes.append(volume)
            if self.model is not None:
                self.model.java.volume = volume
            return volume

        return prepare_bounded_writer(cap, volume_factory=factory, volume_root=self.root)


def _snapshot(manager, lock, cap):
    return manager.snapshot_model(
        expected_lock_sha256=lock["lock_sha256"],
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=cap,
    )


def _residue_free(report: dict) -> bool:
    assert report["errors"] == [], report
    assert not any(
        report[key] for key in ("image_exists", "mount_point_exists", "work_dir_exists")
    ), report
    return True


# --- the write-time bound never exceeds the caller cap ----------------------


def test_the_write_time_bound_never_exceeds_the_caller_cap():
    # cluster granularity: the bound is the largest cluster multiple below.
    assert bounded_save.write_time_bound(1) == 0
    assert bounded_save.write_time_bound(512) == 512
    assert bounded_save.write_time_bound(513) == 512
    assert bounded_save.write_time_bound(4097) == 4096
    assert bounded_save.write_time_bound(8192) == 8192
    for cap in (1, 511, 512, 4097, 97119, 32 * 1024 * 1024):
        bound = bounded_save.write_time_bound(cap)
        assert bound <= cap
        assert bound % bounded_save.CLUSTER_BYTES == 0
        assert cap - bound < bounded_save.CLUSTER_BYTES


def test_a_cap_below_the_write_granularity_is_refused_without_any_write(tmp_path):
    def forbidden_factory(cap: int, work: Path) -> None:
        pytest.fail("no volume may be prepared")

    result = prepare_bounded_writer(100, volume_factory=forbidden_factory, volume_root=tmp_path)
    assert result["available"] is False
    assert result["reason_code"] == "cap_below_write_granularity"
    assert result["write_time_bound_bytes"] == 0

    manager, _ownership, _client = _manager(
        tmp_path, bounded_provider=BoundedProvider(tmp_path, [])
    )
    lock = _attach_and_lock(manager)
    outcome = _snapshot(manager, lock, 100)
    assert outcome["state"] == "snapshot_write_bound_unavailable"
    assert outcome["reason_code"] == "cap_below_write_granularity"
    assert outcome["write_attempted"] is False
    assert outcome["write_time_bound_bytes"] == 0
    assert list(tmp_path.glob("Model_1-snapshot*")) == []


# --- provider refusals carry zero bytes and their reason --------------------


def test_refused_provider_leaves_zero_bytes_and_reports_the_reason(tmp_path):
    volumes: list[FakeVolume] = []
    provider = BoundedProvider(tmp_path, volumes, fail=True)
    manager, _ownership, _client = _manager(tmp_path, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_write_bound_unavailable"
    assert result["write_attempted"] is False
    assert result["reason_code"] == "elevation_required"
    assert result["required_capability"] == "os_volume_free_space"
    assert result["write_time_bound_bytes"] == CAP
    assert result["cap_bytes"] == CAP
    assert list(tmp_path.glob("Model_1-snapshot*")) == []
    assert volumes == []


def test_provider_crash_is_a_typed_refusal_not_an_unwound_manager(tmp_path):
    def crashing_provider(cap: int) -> dict:
        raise RuntimeError("diskpart exploded")

    manager, _ownership, _client = _manager(tmp_path, bounded_provider=crashing_provider)
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_write_bound_unavailable"
    assert result["reason_code"] == "provider_failed"
    assert result["write_attempted"] is False
    assert list(tmp_path.glob("Model_1-snapshot*")) == []


# --- accepted copies and write-time refusals --------------------------------


def test_exact_cap_copy_is_accepted_with_manifest_hash(tmp_path):
    payload = bytes(CAP)
    volumes: list[FakeVolume] = []
    model = WritingMphModel("Model_1", payload)
    provider = BoundedProvider(tmp_path, volumes, model=model)
    client = ModelsClient([model])
    manager, _ownership, _client = _manager(tmp_path, client=client, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_complete", result
    snapshot_path = Path(result["snapshot_path"])
    assert snapshot_path.read_bytes() == payload
    manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["snapshot"]["size_bytes"] == CAP
    assert manifest["snapshot"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert volumes[-1].teardown_called is True
    assert _residue_free(result["volume_cleanup"])
    assert result["write_time_bound_bytes"] == CAP
    assert result["cleanup_state"] == "complete"
    assert "cleanup_recovery" not in result


# --- the operation completes only when its volume cleanup passes ------------


def _aligned_copy_manager(tmp_path, volumes):
    model = WritingMphModel("Model_1", bytes(CAP))
    provider = BoundedProvider(tmp_path / "volumes", volumes, model=model)
    manager, _ownership, _client = _manager(
        tmp_path, client=ModelsClient([model]), bounded_provider=provider
    )
    return manager, _attach_and_lock(manager)


def _assert_artifacts_kept_and_valid(result):
    snapshot = Path(result["snapshot_path"])
    manifest_path = Path(result["manifest_path"])
    assert snapshot.read_bytes() == bytes(CAP)
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == result["snapshot_sha256"]
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest() == result["manifest_sha256"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["snapshot"]["sha256"] == result["snapshot_sha256"]
    assert manifest["complete"] is True


def test_reported_cleanup_residue_is_not_a_completed_snapshot(tmp_path, monkeypatch):
    original = FakeVolume.teardown

    def teardown_with_residue(self):
        original(self)
        return {
            "residue_free": False,
            "image_exists": True,
            "errors": ["injected detached-state UNKNOWN"],
        }

    monkeypatch.setattr(FakeVolume, "teardown", teardown_with_residue)
    volumes: list[FakeVolume] = []
    manager, lock = _aligned_copy_manager(tmp_path, volumes)

    result = _snapshot(manager, lock, CAP)

    assert result["success"] is False
    assert result["state"] == "snapshot_cleanup_pending"
    assert result["artifact_complete"] is True
    assert result["cleanup_state"] == "residue_reported"
    assert "injected detached-state UNKNOWN" in result["volume_cleanup"]["errors"]
    assert result["cleanup_recovery"]["work_dir"] == str(volumes[-1].work_dir)
    _assert_artifacts_kept_and_valid(result)


def test_a_raising_teardown_is_unverified_and_not_a_completed_snapshot(tmp_path, monkeypatch):
    original = FakeVolume.teardown

    def raising_teardown(self):
        original(self)
        raise OSError("injected detach verification failure")

    monkeypatch.setattr(FakeVolume, "teardown", raising_teardown)
    volumes: list[FakeVolume] = []
    manager, lock = _aligned_copy_manager(tmp_path, volumes)

    result = _snapshot(manager, lock, CAP)

    assert result["success"] is False
    assert result["state"] == "snapshot_cleanup_pending"
    assert result["cleanup_state"] == "unverified"
    cleanup = result["volume_cleanup"]
    assert cleanup["teardown_raised"] is True
    assert any("injected detach verification failure" in error for error in cleanup["errors"])
    assert result["cleanup_recovery"]["work_dir"] == str(volumes[-1].work_dir)
    _assert_artifacts_kept_and_valid(result)


def test_pending_cleanup_is_recorded_durably_for_a_later_verified_cleanup(tmp_path, monkeypatch):
    def teardown_leaving_the_volume(self):
        return {"residue_free": False, "image_exists": True, "errors": ["detach refused"]}

    monkeypatch.setattr(FakeVolume, "teardown", teardown_leaving_the_volume)
    volumes: list[FakeVolume] = []
    manager, lock = _aligned_copy_manager(tmp_path, volumes)

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_cleanup_pending"
    record_path = Path(result["volume_cleanup"]["cleanup_record_path"])
    assert record_path.parent == volumes[-1].work_dir
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["schema_name"] == bounded_save.PENDING_CLEANUP_SCHEMA
    assert record["schema_version"] == bounded_save.PENDING_CLEANUP_VERSION
    assert record["cleanup_state"] == "residue_reported"
    assert record["recovery_identity"] == result["cleanup_recovery"]
    assert record["report"]["errors"] == ["detach refused"]


def test_a_refusal_with_cleanup_residue_reports_its_cleanup_state(tmp_path, monkeypatch):
    def teardown_with_residue(self):
        return {"residue_free": False, "image_exists": True, "errors": ["detach refused"]}

    monkeypatch.setattr(FakeVolume, "teardown", teardown_with_residue)
    volumes: list[FakeVolume] = []
    model = WritingMphModel("Model_1", bytes(CAP + 1))
    provider = BoundedProvider(tmp_path / "volumes", volumes, model=model)
    manager, _ownership, _client = _manager(
        tmp_path, client=ModelsClient([model]), bounded_provider=provider
    )
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, CAP)

    assert result["success"] is False
    assert result["state"] == "snapshot_byte_cap_enforced"
    assert result["cleanup_state"] == "residue_reported"
    assert result["cleanup_recovery"]["work_dir"] == str(volumes[-1].work_dir)
    assert result["bytes_at_final_target"] == 0


def test_only_an_explicit_residue_free_report_counts_as_complete():
    assert bounded_save.cleanup_state_of({"residue_free": True}) == "complete"
    assert bounded_save.cleanup_state_of({"residue_free": False}) == "residue_reported"
    assert bounded_save.cleanup_state_of({}) == "unverified"
    assert bounded_save.cleanup_state_of({"residue_free": "yes"}) == "unverified"
    assert (
        bounded_save.cleanup_state_of({"residue_free": True, "teardown_raised": True})
        == "unverified"
    )


def test_one_byte_overflow_is_refused_at_write_time_with_zero_bytes(tmp_path):
    payload = bytes(CAP + 1)
    volumes: list[FakeVolume] = []
    model = WritingMphModel("Model_1", payload)
    provider = BoundedProvider(tmp_path, volumes, model=model)
    client = ModelsClient([model])
    manager, _ownership, _client = _manager(tmp_path, client=client, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_byte_cap_enforced", result
    assert result["write_attempted"] is True
    assert result["bytes_at_final_target"] == 0
    assert result["enforcement_point"] == "os_volume"
    assert result["write_time_bound_bytes"] == CAP
    # the OS refused mid-write: only bytes within the bound ever existed
    assert volumes[-1].bytes_written <= CAP
    assert list(tmp_path.glob("Model_1-snapshot*")) == []
    assert volumes[-1].teardown_called is True
    assert _residue_free(result["volume_cleanup"])


def test_an_unaligned_cap_refuses_a_copy_above_its_bound_at_write_time(tmp_path):
    cap = 4096 + 100  # not cluster-aligned: the write-time bound is 4096
    payload = bytes(4096 + 1)  # one byte above the bound, still below the cap
    volumes: list[FakeVolume] = []
    model = WritingMphModel("Model_1", payload)
    provider = BoundedProvider(tmp_path, volumes, model=model)
    client = ModelsClient([model])
    manager, _ownership, _client = _manager(tmp_path, client=client, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, cap)

    # the write-time bound (4096) refuses it during the write, not after
    assert result["state"] == "snapshot_byte_cap_enforced", result
    assert result["write_time_bound_bytes"] == 4096
    assert result["cap_bytes"] == cap
    assert result["bytes_at_final_target"] == 0
    assert volumes[-1].bytes_written <= 4096
    assert list(tmp_path.glob("Model_1-snapshot*")) == []


def test_mid_write_crash_is_snapshot_incomplete_with_full_cleanup(tmp_path):
    volumes: list[FakeVolume] = []
    provider = BoundedProvider(tmp_path, volumes)
    client = ModelsClient([WritingMphModel("Model_1", b"partial bytes", crash_after_write=True)])
    manager, _ownership, _client = _manager(tmp_path, client=client, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_incomplete", result
    assert result["artifacts_removed"] is True
    assert list(tmp_path.glob("Model_1-snapshot*")) == []
    assert volumes[-1].teardown_called is True
    assert _residue_free(result["volume_cleanup"])


# --- every unconsumed prepared volume is discarded --------------------------


def test_target_collision_discards_the_prepared_volume(tmp_path):
    volumes: list[FakeVolume] = []
    provider = BoundedProvider(tmp_path, volumes)
    manager, _ownership, _client = _manager(tmp_path, bounded_provider=provider)
    lock = _attach_and_lock(manager)
    existing = tmp_path / "existing.mph"
    existing.write_bytes(b"preserve")
    manager._snapshot_target_factory = lambda tag: existing

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_incomplete", result
    assert result["artifacts_removed"] is True
    assert existing.read_bytes() == b"preserve"
    assert volumes[-1].teardown_called is True
    assert not volumes[-1].directory.exists()
    assert _residue_free(result["volume_cleanup"])


def test_existing_manifest_discards_the_prepared_volume(tmp_path):
    volumes: list[FakeVolume] = []
    provider = BoundedProvider(tmp_path, volumes)
    manager, _ownership, _client = _manager(tmp_path, bounded_provider=provider)
    lock = _attach_and_lock(manager)
    target = tmp_path / "Model_1-new.mph"
    (tmp_path / "Model_1-new.manifest.json").write_text("{}", encoding="utf-8")
    manager._snapshot_target_factory = lambda tag: target

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_incomplete", result
    assert volumes[-1].teardown_called is True
    assert _residue_free(result["volume_cleanup"])


def test_failing_target_factory_discards_the_prepared_volume(tmp_path):
    volumes: list[FakeVolume] = []
    provider = BoundedProvider(tmp_path, volumes)
    manager, _ownership, _client = _manager(tmp_path, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    def broken_factory(_tag: str) -> Path:
        raise RuntimeError("target factory failed")

    manager._snapshot_target_factory = broken_factory

    result = _snapshot(manager, lock, CAP)

    assert result["state"] == "snapshot_incomplete", result
    assert volumes[-1].teardown_called is True
    assert _residue_free(result["volume_cleanup"])


def test_precondition_failure_discards_the_prepared_volume(tmp_path):
    volumes: list[FakeVolume] = []
    provider = BoundedProvider(tmp_path, volumes)
    manager, _ownership, _client = _manager(tmp_path, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    result = manager.snapshot_model(
        expected_lock_sha256="0" * 64,  # wrong lock: the precondition fails
        expected_revision_sha256=lock["revision"]["revision_sha256"],
        max_snapshot_bytes=CAP,
    )

    assert result["state"] == "snapshot_precondition_failed", result
    assert volumes[-1].teardown_called is True
    assert _residue_free(result["volume_cleanup"])
    assert list(tmp_path.glob("Model_1-snapshot*")) == []


# --- volume initialization failure is failure-atomic ------------------------


def test_volume_initialization_failure_rolls_the_volume_back(monkeypatch):
    work = _ascii_work_dir()
    if work is None:
        pytest.skip("no ASCII work directory for the diskpart constructor")
    detached: list[Path] = []

    def fake_run(command, *, what):
        # the creation step "succeeds" and leaves the attached-image file
        (work / bounded_save.IMAGE_NAME).write_bytes(b"fake attached resource")
        return ""

    def fake_detach(self, errors):
        detached.append(self.vhd)
        return True

    monkeypatch.setattr(bounded_save, "_run", fake_run)
    monkeypatch.setattr(
        bounded_save.DiskpartVolume,
        "_format_and_mount",
        lambda self: (_ for _ in ()).throw(RuntimeError("injected format failure")),
    )
    monkeypatch.setattr(bounded_save.DiskpartVolume, "_detach", fake_detach)

    with pytest.raises(VolumePreparationFailed) as failure:
        bounded_save.DiskpartVolume(8192, work)

    assert failure.value.cleanup["residue_free"] is True
    assert not (work / bounded_save.IMAGE_NAME).exists()
    assert not work.exists()
    assert detached == [work / bounded_save.IMAGE_NAME], (
        "rollback must detach the created image before deleting it"
    )


def test_preparation_refusal_reports_the_rollback(tmp_path, monkeypatch):
    def failing_factory(cap: int, work: Path) -> None:
        # a factory that starts but cannot finish (no rollback of its own)
        (work / bounded_save.IMAGE_NAME).write_bytes(b"partial")
        raise RuntimeError("attach failed halfway")

    result = prepare_bounded_writer(
        CAP, volume_factory=failing_factory, volume_root=tmp_path / "root"
    )

    assert result["available"] is False
    assert result["reason_code"] == "volume_preparation_failed"
    assert "attach failed halfway" in result["detail"]
    cleanup = result["cleanup"]
    # a non-atomic factory's leftovers are reported, not guessed away
    assert cleanup["work_dir_exists"] is True
    assert cleanup["residue_free"] is False
    assert list((tmp_path / "root").iterdir()) != []


def test_the_production_factory_refuses_before_any_diskpart_call(tmp_path, monkeypatch):
    import os
    import shutil

    def forbidden_run(command, *, what):
        pytest.fail(f"no volume command may run: {what}")

    monkeypatch.setattr(bounded_save, "_run", forbidden_run)
    result = prepare_bounded_writer(
        CAP,
        volume_factory=bounded_save.windows_bounded_volume_factory,
        volume_root=Path(str(tmp_path) + "-非ASCII"),
    )

    assert result["available"] is False
    if os.name != "nt":
        expected = "unsupported_platform"
    elif shutil.which("diskpart.exe") is None or shutil.which("pwsh.exe") is None:
        expected = "tooling_missing"
    else:
        expected = "volume_root_not_ascii"
    assert result["reason_code"] == expected
    assert result["cleanup"]["residue_free"] is True


# --- copy-out and teardown helpers ------------------------------------------


def test_save_copy_into_volume_hashes_the_copy_out(tmp_path):
    payload = b"A" * 100
    volume = FakeVolume(4096, tmp_path / "v")
    model = WritingMphModel("m", payload)
    model.java.volume = volume
    client = ModelsClient([model])
    target = tmp_path / "out.mph"
    size = bounded_save.save_copy_into_volume(client, "m", target, cap=4096, volume=volume)
    assert size == 100
    assert target.read_bytes() == payload
    report = volume.teardown()
    assert volume.teardown_called is True
    assert not volume.directory.exists()
    assert _residue_free(report)


# --- real diskpart volume (admin only, still solver-free) -------------------


def _admin_available() -> bool:
    import os

    if os.name != "nt":
        return False
    import ctypes

    try:
        shell32 = getattr(ctypes, "WinDLL")("shell32")
        shell32.IsUserAnAdmin.restype = ctypes.c_int  # type: ignore[attr-defined]
        return bool(shell32.IsUserAnAdmin())
    except OSError, AttributeError:
        return False


def _ascii_work_dir():
    """One fresh ASCII work directory for the real diskpart volume.

    diskpart reads its script in the system codepage, so the work directory
    must stay ASCII even when the pytest temp tree carries a user name that
    is not. ``SystemRoot`` is the one ASCII base every Windows host has; its
    Temp folder is admin-writable, which these tests already require.
    """
    import os
    import uuid

    base = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp"
    work = base / f"comsol-mcp-capped-volume-{uuid.uuid4().hex}"
    try:
        work.mkdir(parents=True)
    except OSError:
        return None
    if not str(work).isascii():
        return None
    return work


def _ascii_work_dir_available() -> bool:
    import os

    base = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp"
    return str(base).isascii() and base.is_dir() and os.access(base, os.W_OK)


@pytest.mark.skipif(not _admin_available(), reason="the capped volume needs an elevated runner")
@pytest.mark.skipif(
    not _ascii_work_dir_available(), reason="no ASCII admin-writable temp directory"
)
class TestRealDiskpartVolume:
    CAP_BYTES = 8 * 1024 * 1024  # a multiple of the write granularity

    def _volume(self, cap_bytes: int | None = None):
        work = _ascii_work_dir()
        assert work is not None
        return bounded_save.DiskpartVolume(cap_bytes or self.CAP_BYTES, work)

    def test_free_equals_bound_and_teardown_leaves_no_residue(self):
        import shutil

        volume = self._volume()
        try:
            assert volume.bound == self.CAP_BYTES
            assert shutil.disk_usage(volume.mount).free == self.CAP_BYTES
            probe = volume.mount / "probe.bin"
            with probe.open("wb") as handle:
                handle.truncate(self.CAP_BYTES)
            assert probe.stat().st_size == self.CAP_BYTES
        finally:
            report = volume.teardown()
        assert _residue_free(report)
        assert not volume.vhd.exists()
        assert not volume.work_dir.exists()

    def test_one_byte_over_the_bound_fails_at_write_time(self):
        volume = self._volume()
        try:
            probe = volume.mount / "probe.bin"
            with pytest.raises(OSError) as error:
                with probe.open("wb", buffering=0) as handle:
                    handle.write(b"z" * (self.CAP_BYTES + 1))
            assert (
                getattr(error.value, "winerror", None) in bounded_save.DISK_FULL_WINERRORS
                or error.value.errno == bounded_save.DISK_FULL_ERRNO
            )
            assert probe.stat().st_size <= self.CAP_BYTES
        finally:
            report = volume.teardown()
        assert _residue_free(report)

    def test_an_unaligned_cap_fills_to_its_floor_bound(self):
        import shutil

        cap = self.CAP_BYTES + 100  # unaligned: the bound is the floor multiple
        volume = self._volume(cap)
        try:
            assert volume.bound == self.CAP_BYTES
            assert shutil.disk_usage(volume.mount).free == volume.bound
        finally:
            report = volume.teardown()
        assert _residue_free(report)

    def test_teardown_is_idempotent_and_removes_the_mount_point(self):
        import os

        volume = self._volume()
        first = volume.teardown()
        second = volume.teardown()
        assert first == second
        assert _residue_free(first)
        assert not os.path.lexists(volume.mount)
        assert not volume.work_dir.exists()
