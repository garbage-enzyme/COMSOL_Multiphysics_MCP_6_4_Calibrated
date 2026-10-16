"""Probe the installed console entry point over real MCP stdio transport."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

REMOVED_TOOLS = (
    "spectral_characterize",
    "spectral_model_compare",
    "thermal_kirchhoff_assess",
    "thermal_radiation_evaluate",
    "thermal_material_validate",
    "thermal_material_evaluate",
    "convergence_evaluate",
    "branch_continuation_plan",
    "research_campaign_compile",
    "research_optimizer_advance",
    "research_robustness_plan",
    "robust_shape_plan_preview",
    "robust_shape_job_submit",
    "robust_shape_evidence_inspect",
    "robust_shape_evidence_verify",
)


def _sdk_attribute(value: Any, snake_case: str, legacy_alias: str, default: Any = None) -> Any:
    if hasattr(value, snake_case):
        return getattr(value, snake_case)
    return getattr(value, legacy_alias, default)


def _object_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("result")
    if isinstance(nested, dict):
        return {**nested, **{key: item for key, item in value.items() if key != "result"}}
    return value


def _tool_payload(result: Any) -> dict[str, Any]:
    structured = _sdk_attribute(result, "structured_content", "structuredContent")
    payload = _object_payload(structured)
    if payload is not None:
        return payload
    candidates = []
    for content in getattr(result, "content", []):
        text = getattr(content, "text", None)
        if isinstance(text, str):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            payload = _object_payload(value)
            if payload is not None:
                candidates.append(payload)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RuntimeError("tool result contains multiple JSON object payloads")
    raise RuntimeError("tool result does not contain one JSON object")


def _stdio_environment(workdir: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("COMSOL_MCP_")
    }
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "COMSOL_MCP_PROFILE": "core",
            "COMSOL_MCP_RUNTIME_DIR": str(workdir / "runtime"),
        }
    )
    return environment


def _validate_passive_evidence(capabilities: dict[str, Any], preview: dict[str, Any]) -> bool:
    session_state = capabilities.get("session")
    if not isinstance(session_state, dict):
        raise RuntimeError("installed stdio probe omitted session-state evidence")
    if session_state.get("connected") is not False or session_state.get("starting") is not False:
        raise RuntimeError("installed stdio probe unexpectedly started COMSOL")
    guarantees = preview.get("preview_guarantees", {})
    if guarantees.get("solver_started") is not False:
        raise RuntimeError("installed native probe did not prove solver absence")
    if guarantees.get("filesystem_modified") is not False:
        raise RuntimeError("installed native probe did not prove filesystem passivity")
    return bool(session_state["connected"] or session_state["starting"])


def _preview_arguments() -> dict[str, Any]:
    return {
        "spec": {
            "job_type": "staged_sweep",
            "source_model_path": "fixtures/solver-free.mph",
            "parameter_name": "wl",
            "parameter_values": [5.0],
            "expressions": ["R", "T"],
        }
    }


async def _expect_rejection(
    session: ClientSession,
    *,
    case_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = await session.call_tool(
            tool_name,
            arguments,
            read_timeout_seconds=10.0,
        )
    except MCPError as exc:
        return {
            "case_id": case_id,
            "rejected": True,
            "mode": "protocol_error",
            "exception_type": type(exc).__name__,
        }
    except Exception as exc:
        return {
            "case_id": case_id,
            "rejected": False,
            "mode": "client_or_transport_failure",
            "exception_type": type(exc).__name__,
        }
    rejected = bool(_sdk_attribute(result, "is_error", "isError", False))
    return {
        "case_id": case_id,
        "rejected": rejected,
        "mode": "tool_error_result" if rejected else "unexpected_success",
        "exception_type": None,
    }


async def _verify_removed_tools(session: Any, names: list[str]) -> list[dict[str, Any]]:
    if set(names).intersection(REMOVED_TOOLS):
        raise RuntimeError("installed discovery exposes removed tools")
    results = []
    for name in REMOVED_TOOLS:
        result = await _expect_rejection(session, case_id=name, tool_name=name, arguments={})
        if not result["rejected"]:
            raise RuntimeError(f"removed tool did not fail closed: {name}")
        results.append(result)
    return results


async def _probe(command: Path, workdir: Path, stderr_path: Path) -> dict[str, Any]:
    environment = _stdio_environment(workdir)
    parameters = StdioServerParameters(
        command=str(command),
        args=[],
        cwd=str(workdir),
        env=environment,
    )
    with stderr_path.open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=15.0,
            ) as session:
                initialized = await session.initialize()
                listed = await session.list_tools()
                tool_names = sorted(tool.name for tool in listed.tools)
                removed = await _verify_removed_tools(session, tool_names)
                preflight_started = time.perf_counter()
                preflight_result = await session.call_tool(
                    "solver_preflight",
                    {},
                    read_timeout_seconds=15.0,
                )
                preflight_wall = time.perf_counter() - preflight_started
                if _sdk_attribute(preflight_result, "is_error", "isError", False):
                    raise RuntimeError("installed cold solver_preflight call returned a tool error")
                preflight = _tool_payload(preflight_result)
                preview_started = time.perf_counter()
                preview_result = await session.call_tool(
                    "job_spec_preview",
                    _preview_arguments(),
                    read_timeout_seconds=15.0,
                )
                preview_wall = time.perf_counter() - preview_started
                if _sdk_attribute(preview_result, "is_error", "isError", False):
                    raise RuntimeError("installed cold job_spec_preview call returned a tool error")
                preview = _tool_payload(preview_result)
                if preview.get("success") is not True:
                    raise RuntimeError(
                        f"installed cold job_spec_preview call failed safely: {preview}"
                    )
                capabilities_result = await session.call_tool(
                    "capabilities",
                    {},
                    read_timeout_seconds=15.0,
                )
                if _sdk_attribute(capabilities_result, "is_error", "isError", False):
                    raise RuntimeError("installed capabilities call returned a tool error")
                capabilities = _tool_payload(capabilities_result)
                malformed = [
                    await _expect_rejection(
                        session,
                        case_id="unknown_tool",
                        tool_name="__unknown_tool__",
                        arguments={},
                    ),
                    await _expect_rejection(
                        session,
                        case_id="invalid_job_identifier_type",
                        tool_name="job_status",
                        arguments={"job_id": {"invalid": True}},
                    ),
                    await _expect_rejection(
                        session,
                        case_id="missing_job_identifier",
                        tool_name="job_status",
                        arguments={},
                    ),
                ]
    if not malformed or not all(item["rejected"] for item in malformed):
        raise RuntimeError(f"malformed request matrix did not fail closed: {malformed}")
    if capabilities.get("profile") != "core":
        raise RuntimeError("installed stdio probe did not activate the core profile")
    if os.name == "nt":
        if preflight.get("control_plane", {}).get("operation") != "solver_preflight":
            raise RuntimeError("installed cold solver_preflight call omitted timing evidence")
    elif (
        preflight.get("classification") != "unsupported_platform"
        or preflight.get("reason_code") != "linux_solver_not_enabled"
        or preflight.get("solver_started") is not False
        or preflight.get("filesystem_modified") is not False
    ):
        raise RuntimeError("installed Linux preflight did not refuse before native work")
    comsol_client_started = _validate_passive_evidence(capabilities, preview)
    names_payload = json.dumps(tool_names, separators=(",", ":")).encode("utf-8")
    return {
        "schema_name": "comsol_mcp.installed_stdio_probe",
        "schema_version": "1.1.0",
        "transport": "stdio",
        "initialize": {
            "protocol_version": _sdk_attribute(initialized, "protocol_version", "protocolVersion"),
            "server_name": _sdk_attribute(initialized, "server_info", "serverInfo").name,
            "server_version": _sdk_attribute(initialized, "server_info", "serverInfo").version,
        },
        "tool_count": len(tool_names),
        "tool_names_sha256": hashlib.sha256(names_payload).hexdigest(),
        "capabilities": {
            "profile": capabilities["profile"],
            "package_version": capabilities["deployment_identity"]["package_version"],
            "build_identity_sha256": capabilities["deployment_identity"]["build_identity"][
                "build_identity_sha256"
            ],
            "schema_registry_sha256": capabilities["schema_registry"]["registry_sha256"],
            "catalog_contract_sha256": capabilities["deployment_identity"][
                "catalog_contract_sha256"
            ],
        },
        "cold_solver_preflight": {
            "ready": preflight.get("ready"),
            "classification": preflight.get("classification"),
            "blocker_count": len(preflight.get("blockers", [])),
            "latency_seconds": preflight.get("control_plane", {}).get("latency_seconds"),
            "transport_wall_seconds": preflight_wall,
            "outcome": preflight.get("control_plane", {}).get("outcome"),
        },
        "cold_solver_free_tool_matrix": {
            "job_spec_preview": {
                "success": preview["success"],
                "job_type": preview["job_type"],
                "transport_wall_seconds": preview_wall,
                "solver_started": preview["preview_guarantees"]["solver_started"],
                "filesystem_modified": preview["preview_guarantees"]["filesystem_modified"],
            },
        },
        "malformed_request_matrix": malformed,
        "removed_tool_matrix": removed,
        "comsol_client_started": comsol_client_started,
        "stderr_byte_count": stderr_path.stat().st_size,
        "paths_included": False,
    }


async def _probe_modern(command: Path, workdir: Path) -> dict[str, Any]:
    """Use the current public SDK client and its discover-first core path."""
    parameters = StdioServerParameters(
        command=str(command), args=[], cwd=str(workdir), env=_stdio_environment(workdir)
    )
    async with Client(parameters, mode="auto", read_timeout_seconds=15.0) as client:
        if client.protocol_version != "2026-07-28":
            raise RuntimeError("installed modern client did not select the target revision")
        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        removed = await _verify_removed_tools(client, names)
        capabilities = _tool_payload(await client.call_tool("capabilities", {}))
        preview = _tool_payload(await client.call_tool("job_spec_preview", _preview_arguments()))
        if preview.get("success") is not True:
            raise RuntimeError("installed modern offline preview call failed")
        started = _validate_passive_evidence(capabilities, preview)
        if capabilities["deployment_identity"]["source_classification"] != "installed_site_package":
            raise RuntimeError("installed modern stdio loaded source-tree code")
        rejected = await _expect_rejection(
            client,
            case_id="invalid_modern_job_id",
            tool_name="job_status",
            arguments={"job_id": []},
        )
        if not rejected["rejected"]:
            raise RuntimeError("modern malformed request was not rejected")
        return {
            "protocol_version": client.protocol_version,
            "initialize_handshake": False,
            "tool_count": len(names),
            "tool_names_sha256": hashlib.sha256(
                json.dumps(names, separators=(",", ":")).encode()
            ).hexdigest(),
            "package_version": capabilities["deployment_identity"]["package_version"],
            "source_classification": "installed_site_package",
            "comsol_client_started": started,
            "malformed_request": rejected,
            "removed_tool_matrix": removed,
        }


def _probe_modern_isolated(command: Path, workdir: Path) -> dict[str, Any]:
    """Run the modern client in a fresh child directory on every invocation."""
    with tempfile.TemporaryDirectory(prefix="modern-", dir=workdir) as modern_root:
        return asyncio.run(_probe_modern(command, Path(modern_root)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    command = args.command.resolve(strict=True)
    workdir = args.workdir.resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    stderr_path = workdir / "server-stderr.log"
    result = asyncio.run(_probe(command, workdir, stderr_path))
    result["modern_stdio"] = _probe_modern_isolated(command, workdir)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
