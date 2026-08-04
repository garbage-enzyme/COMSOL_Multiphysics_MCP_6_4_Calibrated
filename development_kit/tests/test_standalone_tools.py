"""Public MCP dispatch and containment tests for standalone campaigns."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import comsol_mcp.standalone.builder as builder_module
import comsol_mcp.tools.standalone as standalone_tools_module
from comsol_mcp.durable.io import atomic_write_json
from comsol_mcp.path_policy import ARTIFACT_WRITE_ROOT_ENV
from comsol_mcp.server import create_server
from comsol_mcp.tools.catalog import TOOL_METADATA
from development_kit.tests.mcp_test_support import decode_tool_result


def _workstation_build_available() -> bool:
    try:
        builder_module._validate_build_host(builder_module._default_csc_path())
    except builder_module.PlatformError, FileNotFoundError:
        return False
    return True


def _call(server, name: str, arguments: dict) -> dict:
    return decode_tool_result(asyncio.run(server.call_tool(name, arguments)))


def test_standalone_tool_metadata_is_explicit() -> None:
    expected = {
        "standalone_build": ("filesystem_write", "solver_free", False),
        "standalone_start": ("solver_execution", "comsol_bound", True),
        "standalone_status": ("read_only", "control_plane", False),
        "standalone_pause": ("job_control", "control_plane", False),
        "standalone_resume": ("solver_execution", "comsol_bound", True),
        "standalone_tail": ("read_only", "solver_free", False),
        "standalone_results": ("read_only", "solver_free", False),
    }
    for name, values in expected.items():
        metadata = TOOL_METADATA[name]
        assert (
            metadata.side_effect_class,
            metadata.concurrency_class,
            metadata.starts_solver,
        ) == values
        assert {"basic_fem", "experimental", "full"} <= set(metadata.intended_profiles)
        assert metadata.requires_model_revision is False


def test_standalone_comsol_root_explicit_value_has_precedence(monkeypatch) -> None:
    monkeypatch.setattr(
        standalone_tools_module,
        "load_settings",
        lambda: {"comsol": {"installation_root": "C:/configured"}},
    )

    assert standalone_tools_module._resolve_comsol_root("D:/explicit") == "D:/explicit"
    assert standalone_tools_module._resolve_comsol_root(None) == "C:/configured"


def test_standalone_comsol_root_requires_one_source(monkeypatch) -> None:
    monkeypatch.setattr(
        standalone_tools_module,
        "load_settings",
        lambda: {"comsol": {"installation_root": None}},
    )

    with pytest.raises(ValueError, match="configure it in settings"):
        standalone_tools_module._resolve_comsol_root(None)


@pytest.mark.skipif(
    not _workstation_build_available(),
    reason="requires a supported Windows 10/11 x64 workstation build host",
)
def test_public_build_and_status_dispatch_are_solver_free_and_contained(
    ascii_tmp_path: Path, monkeypatch
) -> None:
    owned = ascii_tmp_path / "owned"
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(owned))
    server = create_server("standalone-dispatch", profile="basic_fem")
    deployment = owned / "campaign"

    capabilities = _call(server, "capabilities", {})
    standalone = capabilities["standalone_executable"]
    assert standalone["profile_active"] is True
    assert standalone["target_python_required"] is False
    assert standalone["local_licensed_comsol_required"] is True
    assert standalone["comsol_runtime_bundled"] is False
    assert standalone["windows_inbox_dotnet_framework_required"] is True
    assert standalone["separate_dotnet_runtime_required"] is False
    assert standalone["separate_dotnet_sdk_required"] is False
    assert standalone["visual_studio_required"] is False
    assert standalone["network_download_required"] is False
    assert standalone["build_compiler"] == "%WINDIR%/Microsoft.NET/Framework64/v4.0.30319/csc.exe"

    built = _call(
        server,
        "standalone_build",
        {"output_directory": str(deployment)},
    )

    assert built["success"] is True
    assert built["python_required_at_runtime"] is False
    assert built["local_comsol_installation_required"] is True
    status = {
        "schema_name": "comsol_mcp.standalone_status",
        "schema_version": "1.0.0",
        "status": "paused",
        "attempt_id": "a" * 32,
        "completed": 1,
        "total": 3,
        "phase": "terminal",
        "updated_at_utc": "2026-08-01T00:00:00.0000000Z",
        "launcher_sha256": built["launcher"]["sha256"],
        "campaign_spec_sha256": "b" * 64,
        "comsol_version": "6.4.0.293",
        "comsol_compile_sha256": "c" * 64,
        "comsol_batch_sha256": "d" * 64,
    }
    state = deployment / "assets" / "state"
    state.mkdir(parents=True)
    (deployment / "assets" / "locks").mkdir()
    atomic_write_json(state / "status.json", status)

    observed = _call(
        server,
        "standalone_status",
        {"deployment_directory": str(deployment)},
    )

    assert observed["success"] is True
    assert observed["status"] == "paused"
    assert observed["owner_active"] is False
    assert observed["effective_status"] == "paused"
    assert observed["path_policy"]["validated_kinds"] == ["artifact_read_root"]


def test_public_standalone_paths_cannot_escape_owned_artifacts(
    ascii_tmp_path: Path, monkeypatch
) -> None:
    owned = ascii_tmp_path / "owned"
    outside = ascii_tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(owned))
    server = create_server("standalone-containment", profile="basic_fem")

    build = _call(
        server,
        "standalone_build",
        {"output_directory": str(outside / "build")},
    )
    status = _call(
        server,
        "standalone_status",
        {"deployment_directory": str(outside)},
    )

    assert build["success"] is False
    assert build["path_policy"]["accepted"] is False
    assert status["success"] is False
    assert status["path_policy"]["accepted"] is False
    assert not (outside / "build").exists()
