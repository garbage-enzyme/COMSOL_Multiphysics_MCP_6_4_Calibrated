"""Solver-free contract tests for the licensed shared interactive gate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from development_kit.scripts import shared_interactive_licensed_gate as gate

SCRIPT = Path(__file__).parents[1] / "scripts" / "shared_interactive_licensed_gate.py"
_ASCII_ROOT = Path(
    "D:/comsol_runtime_test"
    if os.name == "nt"
    else os.getenv("RUNNER_TEMP", os.path.join(os.sep, "tmp"))
) / (f"pytest-shared-interactive-{uuid.uuid4().hex}")


@pytest.fixture(scope="module", autouse=True)
def _isolated_ascii_gate_root():
    _ASCII_ROOT.mkdir(parents=True, exist_ok=False)
    try:
        yield
    finally:
        shutil.rmtree(_ASCII_ROOT, ignore_errors=True)


def _ascii_receipt() -> str:
    return str(_ASCII_ROOT / "receipt.json")


def _ascii_source() -> str:
    return str(_ASCII_ROOT / "source.mph")


def _ascii_working_model() -> str:
    return str(_ASCII_ROOT / "working.mph")


def test_shared_interactive_gate_dry_run_is_solver_free():
    receipt = Path(_ascii_receipt())
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "prepare",
            "--model-tag",
            "Model1",
            "--expected-label",
            "Untitled.mph",
            "--receipt",
            str(receipt),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["spec"]["selector"] == {
        "tag": "Model1",
        "expected_label": "Untitled.mph",
        "expected_unsaved": True,
    }
    assert result["spec"]["solver_gate"]["publication_claim"] is False
    assert not receipt.exists()


def test_shared_interactive_readback_requires_declared_desktop_value():
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "readback",
            "--model-tag",
            "Model1",
            "--expected-label",
            "Untitled.mph",
            "--receipt",
            _ascii_receipt(),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 2
    assert "requires --expected-desktop-value" in completed.stderr


def test_shared_interactive_saved_mode_binds_exact_source_path():
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "saved",
            "--model-tag",
            "Model1",
            "--expected-label",
            "existing_model_source.mph",
            "--expected-desktop-value",
            "29[mm]",
            "--expected-file-path",
            _ascii_working_model(),
            "--immutable-source-path",
            _ascii_source(),
            "--receipt",
            _ascii_receipt(),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["spec"]["selector"] == {
        "tag": "Model1",
        "expected_label": "existing_model_source.mph",
        "expected_file_path": str(Path(_ascii_working_model())),
    }
    assert result["spec"]["saved_model_parameter"] == {
        "name": "saved_model_agent_value",
        "value": "31[mm]",
    }
    assert result["spec"]["immutable_source_path"] == str(Path(_ascii_source()))


def test_saved_readback_mode_uses_distinct_source_and_working_paths():
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "saved_readback",
            "--model-tag",
            "Model1",
            "--expected-label",
            "existing_model_working.mph",
            "--expected-desktop-value",
            "29[mm]",
            "--expected-file-path",
            _ascii_working_model(),
            "--immutable-source-path",
            _ascii_source(),
            "--receipt",
            _ascii_receipt(),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    source = Path(result["spec"]["immutable_source_path"])
    working = Path(result["spec"]["expected_file_path"])
    assert result["spec"]["mode"] == "saved_readback"
    assert source.is_absolute()
    assert working.is_absolute()
    assert source == Path(_ascii_source()).resolve(strict=False)
    assert working == Path(_ascii_working_model()).resolve(strict=False)
    assert result["spec"]["selector"]["expected_file_path"] == str(working)
    assert source != working
    assert os.path.normcase(str(source)) != os.path.normcase(str(working))


def test_saved_model_readback_requires_exact_shared_edit_value(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_parameter_expressions",
        lambda _model: {
            gate.MCP_PARAMETER: gate.MCP_PARAMETER_VALUE,
            gate.DESKTOP_PARAMETER: "29[mm]",
            gate.SAVED_MODEL_PARAMETER: "wrong",
        },
    )

    with pytest.raises(RuntimeError, match="shared edit parameter"):
        gate._saved_model_readback(object(), "29[mm]")


def test_saved_mode_rejects_lexically_distinct_aliases_of_one_path():
    source = Path(_ascii_source())
    aliased_working = source.parent / "unused" / ".." / source.name
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--mode",
            "saved",
            "--model-tag",
            "Model1",
            "--expected-label",
            source.name,
            "--expected-desktop-value",
            "29[mm]",
            "--expected-file-path",
            str(aliased_working),
            "--immutable-source-path",
            str(source),
            "--receipt",
            _ascii_receipt(),
            "--dry-run",
        ],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 2
    assert "distinct absolute ASCII path" in completed.stderr


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (FileNotFoundError("git missing"), "FileNotFoundError"),
        (subprocess.TimeoutExpired(["git"], 10), "TimeoutExpired"),
    ],
)
def test_git_probe_contains_missing_or_hung_git(monkeypatch, failure, error_type):
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    result = gate._git_head()

    assert result == {
        "success": False,
        "commit": None,
        "error_type": error_type,
        "returncode": None,
    }


def test_git_probe_failure_returns_structured_gate_result(monkeypatch):
    monkeypatch.setattr(
        gate,
        "_git_head",
        lambda: {
            "success": False,
            "commit": None,
            "error_type": "TimeoutExpired",
            "returncode": None,
        },
    )
    monkeypatch.setattr(gate, "SharedSessionManager", lambda: object())
    args = gate._parser().parse_args(
        [
            "--mode",
            "prepare",
            "--model-tag",
            "Model1",
            "--expected-label",
            "Untitled.mph",
            "--receipt",
            _ascii_receipt(),
        ]
    )

    result = gate._run(args)

    assert result["success"] is False
    assert result["schema_version"] == "1.1.0"
    assert result["source_revision"] is None
    assert result["source_revision_probe"]["error_type"] == "TimeoutExpired"
    assert result["error"] == "RuntimeError: source revision probe failed"
    assert result["cleanup"]["passed"] is True


def test_operation_ownership_is_required_before_shared_attach(monkeypatch):
    calls = []

    class Manager:
        def attach(self, *_args, **_kwargs):
            calls.append("attach")
            raise AssertionError("attach must not run before operation ownership")

    class Arbiter:
        def try_acquire(self, **_kwargs):
            calls.append("acquire")
            return None, {"acquired": False}

    monkeypatch.setattr(
        gate,
        "_git_head",
        lambda: {
            "success": True,
            "commit": "a" * 40,
            "error_type": None,
            "returncode": 0,
        },
    )
    monkeypatch.setattr(gate, "SharedSessionManager", Manager)
    monkeypatch.setattr(gate, "get_operation_arbiter", lambda: Arbiter())
    args = gate._parser().parse_args(
        [
            "--mode",
            "prepare",
            "--model-tag",
            "Model1",
            "--expected-label",
            "Untitled.mph",
            "--receipt",
            _ascii_receipt(),
        ]
    )

    result = gate._run(args)

    assert calls == ["acquire"]
    assert result["success"] is False
    assert result["operation_acquisition"] == {"acquired": False}
    assert "could not acquire operation ownership" in result["error"]


def test_cleanup_steps_continue_independently_and_fail_the_result(monkeypatch):
    calls = []

    class Manager:
        def unlock_model(self, **_kwargs):
            calls.append("unlock")
            raise OSError("injected unlock failure")

        def detach(self):
            calls.append("detach")
            return {"success": True}

    class Arbiter:
        def release(self, _claim):
            calls.append("release")
            return {"verified": False}

    monkeypatch.setattr(gate, "get_operation_arbiter", lambda: Arbiter())

    cleanup = gate._cleanup_shared_resources(
        Manager(),
        active_lock_sha256="a" * 64,
        operation_claim=object(),
        attached=True,
    )

    assert calls == ["unlock", "detach", "release"]
    assert cleanup["steps"] == {
        "unlock": {"passed": False, "error_type": "OSError"},
        "detach": {"passed": True},
        "operation_release": {"passed": False},
    }
    assert cleanup["passed"] is False
