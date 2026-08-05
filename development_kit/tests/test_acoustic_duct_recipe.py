"""Solver-free lifecycle checks for the minimal acoustic duct recipe."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RECIPE = Path(__file__).parents[2] / "recipes" / "acoustic_duct_2d.py"


def _namespace(monkeypatch):
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Model=object))
    loaded = runpy.run_path(str(RECIPE), run_name="acoustic_recipe_test")
    return loaded["main"].__globals__


def test_staged_model_is_published_only_after_client_and_lease_cleanup(tmp_path, monkeypatch):
    namespace = _namespace(monkeypatch)
    output = tmp_path / "duct.mph"
    receipt = tmp_path / "receipt.json"
    staging = tmp_path / ".duct.staging.mph"
    events = []
    model = SimpleNamespace(java=object())

    class Ownership:
        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def heartbeat(self, **_kwargs):
            return None

        def release(self):
            events.append("release")
            return {"success": True}

    class Client:
        port = None

        def create(self, _name):
            return model

        def clear(self):
            events.append("clear")

    namespace["parse_args"] = lambda: SimpleNamespace(
        output_model=output,
        receipt=receipt,
        frequency_hz=100.0,
        maximum_relative_error=0.02,
        solve=True,
        overwrite_output=False,
    )
    namespace["SolverOwnership"] = lambda owner: Ownership()
    namespace["mph"] = SimpleNamespace(Client=lambda version: Client())
    namespace["build_acoustic_duct"] = lambda *_args: {"built": True}
    namespace["validate_solution"] = lambda *_args: {"status": "verified"}

    def save(_java, _output):
        staging.write_bytes(b"model")
        events.append("save")
        return staging

    def publish(source, destination, *, overwrite):
        assert events == ["save", "clear", "release"]
        events.append("publish")
        source.replace(destination)

    namespace["save_staged_model"] = save
    namespace["publish_staged_model"] = publish
    namespace["_atomic_json"] = lambda *_args: events.append("receipt")

    namespace["main"]()

    assert events == ["save", "clear", "release", "publish", "receipt"]
    assert output.read_bytes() == b"model"


def test_build_failure_remains_primary_when_cleanup_also_fails(tmp_path, monkeypatch):
    namespace = _namespace(monkeypatch)

    class Ownership:
        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def heartbeat(self, **_kwargs):
            return None

        def release(self):
            return {"success": False, "error": "release failed"}

    class Client:
        port = None

        def create(self, _name):
            return SimpleNamespace(java=object())

        def clear(self):
            raise OSError("clear failed")

    namespace["parse_args"] = lambda: SimpleNamespace(
        output_model=tmp_path / "duct.mph",
        receipt=tmp_path / "receipt.json",
        frequency_hz=100.0,
        maximum_relative_error=0.02,
        solve=False,
        overwrite_output=False,
    )
    namespace["SolverOwnership"] = lambda owner: Ownership()
    namespace["mph"] = SimpleNamespace(Client=lambda version: Client())
    namespace["build_acoustic_duct"] = lambda *_args: (_ for _ in ()).throw(
        ValueError("build failed")
    )

    with pytest.raises(ValueError, match="build failed") as caught:
        namespace["main"]()

    assert any("cleanup was incomplete" in note for note in caught.value.__notes__)


def test_publish_never_overwrites_competing_output(tmp_path, monkeypatch):
    namespace = _namespace(monkeypatch)
    staging = tmp_path / ".duct.staging.mph"
    output = tmp_path / "duct.mph"
    staging.write_bytes(b"ours")
    output.write_bytes(b"competitor")

    with pytest.raises(FileExistsError):
        namespace["publish_staged_model"](staging, output, overwrite=False)

    assert output.read_bytes() == b"competitor"
    assert not staging.exists()


def test_publish_retries_transient_windows_file_lock(tmp_path, monkeypatch):
    namespace = _namespace(monkeypatch)
    staging = tmp_path / ".duct.staging.mph"
    output = tmp_path / "duct.mph"
    staging.write_bytes(b"model")
    real_replace = namespace["os"].replace
    attempts = []

    def transient_replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) < 3:
            raise PermissionError(32, "simulated sharing violation", str(source))
        real_replace(source, destination)

    monkeypatch.setattr(namespace["os"], "replace", transient_replace)
    monkeypatch.setattr(namespace["time"], "sleep", lambda _seconds: None)

    namespace["publish_staged_model"](staging, output, overwrite=True)

    assert len(attempts) == 3
    assert output.read_bytes() == b"model"


def test_publish_succeeds_when_second_cleanup_removes_published_hardlink_source(
    tmp_path, monkeypatch
):
    namespace = _namespace(monkeypatch)
    staging = tmp_path / ".duct.staging.mph"
    output = tmp_path / "duct.mph"
    staging.write_bytes(b"model")
    unlink_attempts = 0

    def retry(operation, **_kwargs):
        nonlocal unlink_attempts
        if getattr(operation, "__name__", "") == "unlink":
            unlink_attempts += 1
            if unlink_attempts == 1:
                raise PermissionError(32, "simulated exhausted sharing retry")
        operation()

    namespace["_retry_permission_error"] = retry

    namespace["publish_staged_model"](staging, output, overwrite=False)

    assert unlink_attempts == 2
    assert output.read_bytes() == b"model"
    assert not staging.exists()


def test_save_failure_preserves_primary_error_when_staging_retry_fails(tmp_path, monkeypatch):
    namespace = _namespace(monkeypatch)

    class Java:
        def save(self, staging):
            Path(staging).write_bytes(b"partial")
            raise RuntimeError("save failed")

    namespace["_retry_permission_error"] = lambda *_args, **_kwargs: (
        _ for _ in ()
    ).throw(PermissionError("sharing violation"))

    with pytest.raises(RuntimeError, match="save failed") as caught:
        namespace["save_staged_model"](Java(), tmp_path / "duct.mph")
    assert any("staging cleanup failed" in note for note in caught.value.__notes__)


def test_release_exception_does_not_mask_active_build_failure(tmp_path, monkeypatch):
    namespace = _namespace(monkeypatch)

    class Ownership:
        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def release(self):
            raise OSError("release failed")

    namespace["parse_args"] = lambda: SimpleNamespace(
        output_model=tmp_path / "duct.mph",
        receipt=tmp_path / "receipt.json",
        frequency_hz=100.0,
        maximum_relative_error=0.02,
        solve=False,
        overwrite_output=False,
    )
    namespace["SolverOwnership"] = lambda owner: Ownership()
    namespace["mph"] = SimpleNamespace(
        Client=lambda version: (_ for _ in ()).throw(ValueError("build failed"))
    )

    with pytest.raises(ValueError, match="build failed") as caught:
        namespace["main"]()

    assert any("lease.release: OSError" in note for note in caught.value.__notes__)
