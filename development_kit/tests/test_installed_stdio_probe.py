"""Installed stdio probe result decoding tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from development_kit.scripts.installed_stdio_probe import (
    _expect_rejection,
    _spectral_arguments,
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

    structured_with_metadata = SimpleNamespace(
        structuredContent={"result": {"value": 4}, "request_id": "request-1"}
    )
    assert _tool_payload(structured_with_metadata) == {"value": 4}


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


def test_stdio_environment_preserves_launcher_environment_and_forces_probe_scope(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("INSTALLED_STDIO_SENTINEL", "preserved")
    monkeypatch.setenv("PYTHONPATH", "untrusted-import-root")
    monkeypatch.setenv("COMSOL_MCP_PROFILE", "wave_optics")
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", "stale-runtime")

    environment = _stdio_environment(tmp_path)

    assert environment["INSTALLED_STDIO_SENTINEL"] == "preserved"
    assert environment["COMSOL_MCP_PROFILE"] == "core"
    assert environment["COMSOL_MCP_RUNTIME_DIR"] == str(tmp_path / "runtime")
    assert "PYTHONPATH" not in environment
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
            raise McpError(ErrorData(code=-32602, message="invalid parameters"))

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
        "exception_type": "McpError",
    }


@pytest.mark.parametrize(
    ("session", "spectral", "message"),
    [
        ({"connected": True, "starting": False}, {}, "started COMSOL"),
        ({"connected": False, "starting": True}, {}, "started COMSOL"),
        ({"connected": False, "starting": False}, {"solver_started": True}, "solver absence"),
        (
            {"connected": False, "starting": False},
            {"solver_started": False, "filesystem_modified": True},
            "filesystem passivity",
        ),
    ],
)
def test_passive_evidence_rejects_active_or_incomplete_results(session, spectral, message):
    with pytest.raises(RuntimeError, match=message):
        _validate_passive_evidence({"session": session}, spectral)


def test_passive_evidence_requires_exact_negative_observations():
    assert (
        _validate_passive_evidence(
            {"session": {"connected": False, "starting": False}},
            {"solver_started": False, "filesystem_modified": False},
        )
        is False
    )


def test_installed_spectral_probe_arguments_are_bounded_and_passive():
    arguments = _spectral_arguments()
    rows = arguments["bundle_spec"]["rows"]

    assert len(rows) == 5
    assert len({row["raw_row_sha256"] for row in rows}) == 5
    assert all(abs(row["R"] + row["T"] + row["A"] - 1.0) < 1.0e-12 for row in rows)
    assert arguments["measurement_configuration"]["peak_method"] == "measured_grid"
