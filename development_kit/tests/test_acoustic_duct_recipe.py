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
