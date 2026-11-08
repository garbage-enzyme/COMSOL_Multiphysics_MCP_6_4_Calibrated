"""Bounded shared-model Save Copy tests (batch D), solver-free.

These reuse the canonical shared-session harness (``_manager`` and
``_attach_and_lock`` in ``test_shared_session_lifecycle``); the volume is
faked so every boundary and cleanup path is deterministic and needs no
elevation. The real diskpart volume has its own admin-gated tests at the
bottom.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from pathlib import Path as _Path

import pytest

from comsol_mcp.shared_session import bounded_save
from comsol_mcp.shared_session.bounded_save import FakeVolume, prepare_bounded_writer

sys.path.insert(0, str(_Path(__file__).resolve().parent))
from test_shared_session_lifecycle import _attach_and_lock, _manager

CAP = 8192  # cluster-aligned


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
    assert list(tmp_path.glob("Model_1-snapshot*")) == []
    assert volumes == []


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


def test_one_byte_overflow_is_refused_with_zero_bytes_at_target(tmp_path):
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
    assert list(tmp_path.glob("Model_1-snapshot*")) == []
    assert volumes[-1].teardown_called is True


def test_within_one_cluster_overflow_is_refused_by_the_byte_check(tmp_path):
    cap = 4096 + 100  # not cluster-aligned
    payload = bytes(4096 + 101)  # fits the cluster bound, exceeds the byte cap
    volumes: list[FakeVolume] = []
    model = WritingMphModel("Model_1", payload)
    provider = BoundedProvider(tmp_path, volumes, model=model)
    client = ModelsClient([model])
    manager, _ownership, _client = _manager(tmp_path, client=client, bounded_provider=provider)
    lock = _attach_and_lock(manager)

    result = _snapshot(manager, lock, cap)

    assert result["state"] == "snapshot_byte_cap_enforced", result
    assert result["enforcement_point"] == "post_write_byte_check"
    assert result["bytes_at_final_target"] == 0
    assert list(tmp_path.glob("Model_1-snapshot*")) == []
    assert volumes[-1].teardown_called is True


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
    assert list(tmp_path.glob("Model_1-snapshot*")) == []


def test_aligned_cap_rounds_up_to_the_cluster():
    assert bounded_save.aligned_cap(1) == 4096
    assert bounded_save.aligned_cap(4096) == 4096
    assert bounded_save.aligned_cap(4097) == 8192


def test_save_copy_into_volume_hashes_the_copy_out(tmp_path):
    payload = b"A" * 100
    volume = FakeVolume(4096, tmp_path / "v")
    model = WritingMphModel("m", payload)
    model.java.volume = volume
    client = ModelsClient([model])
    target = tmp_path / "out.mph"
    size = bounded_save.save_copy_into_volume(client, "m", target, cap=128, volume=volume)
    assert size == 100
    assert target.read_bytes() == payload
    assert volume.teardown_called is True
    assert not volume.directory.exists()


# --- real diskpart volume (admin only, still solver-free) -------------------


def _admin_available() -> bool:
    if os.name != "nt":
        return False
    import ctypes
    import ctypes.wintypes as wt

    try:
        shell32 = ctypes.WinDLL("shell32")
        shell32.IsUserAnAdmin.restype = wt.BOOL
        return bool(shell32.IsUserAnAdmin())
    except OSError, AttributeError:
        return False


@pytest.mark.skipif(not _admin_available(), reason="the capped volume needs an elevated runner")
class TestRealDiskpartVolume:
    CAP_BYTES = 8 * 1024 * 1024  # cluster-aligned

    def test_free_equals_cap_and_teardown_removes_everything(self, tmp_path):
        import shutil

        volume = bounded_save.DiskpartVolume(self.CAP_BYTES, tmp_path)
        try:
            assert shutil.disk_usage(volume.mount).free == self.CAP_BYTES
            probe = volume.mount / "probe.bin"
            with probe.open("wb") as handle:
                handle.truncate(self.CAP_BYTES)
            assert probe.stat().st_size == self.CAP_BYTES
            probe.unlink()
        finally:
            volume.teardown()
        assert not volume.vhd.exists()

    def test_one_byte_over_the_cluster_bound_fails_at_write_time(self, tmp_path):
        volume = bounded_save.DiskpartVolume(self.CAP_BYTES, tmp_path)
        try:
            probe = volume.mount / "probe.bin"
            with pytest.raises(OSError) as error:
                with probe.open("wb") as handle:
                    handle.truncate(self.CAP_BYTES + 1)
            assert getattr(error.value, "winerror", None) == 112 or error.value.errno == 28
        finally:
            volume.teardown()
