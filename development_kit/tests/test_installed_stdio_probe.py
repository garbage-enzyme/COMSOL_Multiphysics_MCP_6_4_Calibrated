"""Installed stdio probe result decoding tests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from mcp.shared.exceptions import MCPError

from development_kit.scripts.installed_stdio_probe import (
    _expect_rejection,
    _preview_arguments,
    _probe_modern_isolated,
    _stdio_environment,
    _tool_payload,
    _validate_passive_evidence,
)


def test_tool_payload_accepts_structured_or_text_json_objects():
    assert _tool_payload(SimpleNamespace(structuredContent={"value": 1})) == {"value": 1}
    wrapped = SimpleNamespace(structuredContent={"result": {"value": 2}})
    assert _tool_payload(wrapped) == {"value": 2}
    text = SimpleNamespace(
        structuredContent=None,
        content=[SimpleNamespace(text=json.dumps({"value": 3}))],
    )
    assert _tool_payload(text) == {"value": 3}
    wrapped_text = SimpleNamespace(
        structuredContent=None,
        content=[SimpleNamespace(text=json.dumps({"result": {"value": 3}}))],
    )
    assert _tool_payload(wrapped_text) == {"value": 3}

    structured_with_metadata = SimpleNamespace(
        structuredContent={"result": {"value": 4}, "request_id": "request-1"}
    )
    assert _tool_payload(structured_with_metadata) == {
        "value": 4,
        "request_id": "request-1",
    }


def test_structured_payload_precedes_text_and_result_wrappers_are_unwrapped():
    result = SimpleNamespace(
        structuredContent={"result": {"value": "structured"}},
        content=[SimpleNamespace(text=json.dumps({"value": "text"}))],
    )

    assert _tool_payload(result) == {"value": "structured"}


def test_tool_payload_rejects_non_object_results():
    result = SimpleNamespace(
        structuredContent=None,
        content=[SimpleNamespace(text="[]")],
    )
    with pytest.raises(RuntimeError, match="JSON object"):
        _tool_payload(result)


def test_tool_payload_rejects_ambiguous_text_objects():
    result = SimpleNamespace(
        structuredContent=None,
        content=[
            SimpleNamespace(text=json.dumps({"value": 1})),
            SimpleNamespace(text=json.dumps({"value": 2})),
        ],
    )

    with pytest.raises(RuntimeError, match="multiple"):
        _tool_payload(result)


@pytest.mark.parametrize(
    "content",
    [
        [SimpleNamespace(text="{")],
        [SimpleNamespace()],
        [SimpleNamespace(text=None)],
    ],
)
def test_tool_payload_rejects_malformed_or_missing_text_content(content):
    result = SimpleNamespace(structuredContent=None, content=content)

    with pytest.raises(RuntimeError, match="does not contain one JSON object"):
        _tool_payload(result)


def test_stdio_environment_preserves_launcher_environment_and_forces_probe_scope(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("INSTALLED_STDIO_SENTINEL", "preserved")
    monkeypatch.setenv("PYTHONPATH", "untrusted-import-root")
    monkeypatch.setenv("COMSOL_MCP_PROFILE", "wave_optics")
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", "stale-runtime")
    monkeypatch.setenv("COMSOL_MCP_UNREVIEWED_OVERRIDE", "must-not-leak")

    environment = _stdio_environment(tmp_path)

    assert environment["INSTALLED_STDIO_SENTINEL"] == "preserved"
    assert environment["COMSOL_MCP_PROFILE"] == "core"
    assert environment["COMSOL_MCP_RUNTIME_DIR"] == str(tmp_path / "runtime")
    assert "PYTHONPATH" not in environment
    assert "COMSOL_MCP_UNREVIEWED_OVERRIDE" not in environment
    assert environment.get("PATH") == os.environ.get("PATH")


def test_client_or_transport_failure_is_not_accepted_as_protocol_rejection():
    class BrokenSession:
        async def call_tool(self, *_args, **_kwargs):
            raise ConnectionError("injected transport failure")

    result = asyncio.run(
        _expect_rejection(
            BrokenSession(),
            case_id="transport_failure",
            tool_name="job_status",
            arguments={},
        )
    )

    assert result == {
        "case_id": "transport_failure",
        "rejected": False,
        "mode": "client_or_transport_failure",
        "exception_type": "ConnectionError",
    }


def test_explicit_protocol_error_is_accepted_as_rejection():
    class RejectingSession:
        async def call_tool(self, *_args, **_kwargs):
            raise MCPError(code=-32602, message="invalid parameters")

    result = asyncio.run(
        _expect_rejection(
            RejectingSession(),
            case_id="invalid_parameters",
            tool_name="job_status",
            arguments={},
        )
    )

    assert result == {
        "case_id": "invalid_parameters",
        "rejected": True,
        "mode": "protocol_error",
        "exception_type": "MCPError",
    }


@pytest.mark.parametrize(
    ("session", "preview", "message"),
    [
        ({"connected": True, "starting": False}, {}, "started COMSOL"),
        ({"connected": False, "starting": True}, {}, "started COMSOL"),
        (
            {"connected": False, "starting": False},
            {"preview_guarantees": {"solver_started": True}},
            "solver absence",
        ),
        (
            {"connected": False, "starting": False},
            {"preview_guarantees": {"solver_started": False, "filesystem_modified": True}},
            "filesystem passivity",
        ),
    ],
)
def test_passive_evidence_rejects_active_or_incomplete_results(session, preview, message):
    with pytest.raises(RuntimeError, match=message):
        _validate_passive_evidence({"session": session}, preview)


def test_passive_evidence_requires_exact_negative_observations():
    assert (
        _validate_passive_evidence(
            {"session": {"connected": False, "starting": False}},
            {"preview_guarantees": {"solver_started": False, "filesystem_modified": False}},
        )
        is False
    )


def test_installed_preview_arguments_are_bounded_and_solver_free():
    arguments = _preview_arguments()
    spec = arguments["spec"]
    assert spec["job_type"] == "staged_sweep"
    assert spec["parameter_values"] == [5.0]
    assert spec["expressions"] == ["R", "T"]


def test_modern_probe_uses_a_fresh_temporary_root_on_repeated_runs(tmp_path, monkeypatch):
    observed = []

    async def fake_probe(_command, workdir):
        observed.append(workdir)
        (workdir / "runtime").mkdir()
        return {"status": "pass"}

    monkeypatch.setattr("development_kit.scripts.installed_stdio_probe._probe_modern", fake_probe)

    assert _probe_modern_isolated(Path("python"), tmp_path) == {"status": "pass"}
    assert _probe_modern_isolated(Path("python"), tmp_path) == {"status": "pass"}

    assert len(observed) == 2
    assert observed[0] != observed[1]
    assert all(not path.exists() for path in observed)


def test_study_mesh_probe_fails_when_both_client_cleanup_steps_fail(monkeypatch):
    monkeypatch.setenv("COMSOL_MCP_ACCEPTANCE_CORES", "1")

    class Client:
        port = 2036

        def create(self, _name):
            java = MagicMock()
            component = java.component.return_value.create.return_value
            geometry = component.geom.return_value.create.return_value
            geometry.getSDim.return_value = "3"
            geometry.getNDomains.return_value = 1
            geometry.getNBoundaries.return_value = 6
            component.mesh.return_value.create.return_value.getNumElem.return_value = 10
            return SimpleNamespace(java=java)

        def clear(self):
            raise OSError("clear failed")

        def disconnect(self):
            raise OSError("disconnect failed")

    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Client=lambda **_kwargs: Client()))
    monkeypatch.setitem(sys.modules, "jpype", SimpleNamespace())

    probe_path = Path(__file__).parent / "integration" / "probes" / "study_mesh.py"
    spec = importlib.util.spec_from_file_location("study_mesh_cleanup_probe", probe_path)
    assert spec is not None and spec.loader is not None
    study_mesh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study_mesh)

    with pytest.raises(RuntimeError, match="cleanup failed: clear, disconnect"):
        study_mesh.main()
