"""Probe the installed console entry point over real MCP stdio transport."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError


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


def _validate_passive_evidence(capabilities: dict[str, Any], spectral: dict[str, Any]) -> bool:
    session_state = capabilities.get("session")
    if not isinstance(session_state, dict):
        raise RuntimeError("installed stdio probe omitted session-state evidence")
    if session_state.get("connected") is not False or session_state.get("starting") is not False:
        raise RuntimeError("installed stdio probe unexpectedly started COMSOL")
    if spectral.get("solver_started") is not False:
        raise RuntimeError("installed native probe did not prove solver absence")
    if spectral.get("filesystem_modified") is not False:
        raise RuntimeError("installed native probe did not prove filesystem passivity")
    return bool(session_state["connected"] or session_state["starting"])


def _spectral_arguments() -> dict[str, Any]:
    configuration_sha256 = "a" * 64
    absorptions = (0.1, 0.5, 0.9, 0.5, 0.1)
    rows = []
    for index, absorption in enumerate(absorptions):
        wavelength = 4.8e-6 + index * 0.1e-6
        rows.append(
            {
                "row_id": f"point-{index:03d}",
                "raw_row_sha256": hashlib.sha256(
                    f"installed-stdio-native-{index}".encode()
                ).hexdigest(),
                "configuration_sha256": configuration_sha256,
                "requested_wavelength_m": wavelength,
                "evaluated_wavelength_m": wavelength,
                "frequency_wavelength_m": wavelength,
                "R": 0.95 - absorption,
                "T": 0.05,
                "A": absorption,
            }
        )
    return {
        "bundle_spec": {
            "bundle_id": "installed-stdio-native-matrix",
            "source_model": {
                "relative_identity": "fixtures/solver-free.mph",
                "sha256": "b" * 64,
            },
            "configuration_sha256": configuration_sha256,
            "parameter_state": {"probe": "native-runtime"},
            "wavelength_convention": {
                "unit": "m",
                "requested_field": "requested_wavelength_m",
                "evaluated_field": "evaluated_wavelength_m",
                "frequency_derived_field": "frequency_wavelength_m",
                "frequency_relation": "c_const/frequency",
            },
            "expressions": {"R": "R", "T": "T", "A": "1-R-T"},
            "rows": rows,
        },
        "analysis_policy": {
            "response_quantity": "A",
            "candidate_polarity": "maximum",
            "passivity_abs_tolerance": 1.0e-12,
            "closure_abs_tolerance": 1.0e-12,
            "wavelength_sync_abs_m": 1.0e-15,
            "flat_response_abs_tolerance": 1.0e-12,
            "minimum_point_count": 5,
        },
        "measurement_configuration": {
            "peak_method": "measured_grid",
            "baseline_rule": "local_prominence",
            "baseline_response_value": None,
            "fwhm_definition": "half_prominence",
            "fit_support_points": None,
            "fit_support_sensitivity_points": [],
            "local_polynomial_degree": None,
            "fit_max_evaluations": None,
        },
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
                spectral_started = time.perf_counter()
                spectral_result = await session.call_tool(
                    "spectral_characterize",
                    _spectral_arguments(),
                    read_timeout_seconds=15.0,
                )
                spectral_wall = time.perf_counter() - spectral_started
                if _sdk_attribute(spectral_result, "is_error", "isError", False):
                    raise RuntimeError(
                        "installed cold spectral_characterize call returned a tool error"
                    )
                spectral = _tool_payload(spectral_result)
                if spectral.get("success") is not True:
                    raise RuntimeError(
                        f"installed cold spectral_characterize call failed safely: {spectral}"
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
    if preflight.get("control_plane", {}).get("operation") != "solver_preflight":
        raise RuntimeError("installed cold solver_preflight call omitted timing evidence")
    comsol_client_started = _validate_passive_evidence(capabilities, spectral)
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
            "blocker_count": len(preflight.get("blockers", [])),
            "latency_seconds": preflight["control_plane"]["latency_seconds"],
            "transport_wall_seconds": preflight_wall,
            "outcome": preflight["control_plane"]["outcome"],
        },
        "cold_native_tool_matrix": {
            "spectral_characterize": {
                "success": spectral["success"],
                "classification": spectral["classification"],
                "transport_wall_seconds": spectral_wall,
                "solver_started": spectral["solver_started"],
                "filesystem_modified": spectral["filesystem_modified"],
            },
        },
        "malformed_request_matrix": malformed,
        "comsol_client_started": comsol_client_started,
        "stderr_byte_count": stderr_path.stat().st_size,
        "paths_included": False,
    }


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
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
