"""Fresh-host base-profile discovery and live three-call acceptance gate."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.evidence.real_fixture import controlled_fixture_from_environment

PYTHON = Path(sys.executable)
RUNTIME = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
ARTIFACT_DIR = RUNTIME / "live_profile"
PROFILE_COUNTS = {
    "core": 47,
    "basic_fem": 109,
    "wave_optics": 76,
    "experimental": 97,
    "full": 150,
}
COLD_START_RESPONSE_LIMIT_SECONDS = 5.0
CONTROL_PLANE_READ_LIMIT_SECONDS = 15.0
LIVE_CLEANUP_LIMIT_SECONDS = 30.0


def _controlled_cases() -> tuple[dict[str, Any], ...]:
    fixture = controlled_fixture_from_environment()
    fixture["validation_policy"] = {
        "assumptions": {"passive": True, "port_power_normalized": True},
        "required_evidence": [
            "wavelength_controls",
            "flux_RTA",
            "top_air_region",
            "source_integrity",
        ],
        "tolerances": {
            "closure_abs": 1e-3,
            "quantity_bounds_margin": 1e-3,
            "wavelength_abs_m": 1e-12,
        },
    }
    return (fixture,)


def _decode(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", getattr(result, "isError", False)):
        raise RuntimeError(f"MCP tool returned an error: {result}")
    structured = getattr(
        result,
        "structured_content",
        getattr(result, "structuredContent", None),
    )
    if isinstance(structured, dict):
        value = structured.get("result", structured)
        if isinstance(value, dict):
            return value
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise ValueError(f"MCP result did not contain a JSON object: {result}")


def _server(profile: str) -> StdioServerParameters:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["COMSOL_MCP_PROFILE"] = profile
    env["COMSOL_MCP_RUNTIME_DIR"] = str(RUNTIME)
    return StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "src.server"],
        cwd=RUNTIME,
        env=env,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def _call(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    return await _call_before(session, name, arguments, deadline=None)


async def _call_before(
    session: ClientSession,
    name: str,
    arguments: dict[str, Any],
    *,
    deadline: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    timeout_seconds = 300.0 if deadline is None else deadline - time.monotonic()
    if timeout_seconds <= 0.0:
        raise TimeoutError(f"{name} exceeded its absolute deadline")
    result = await session.call_tool(
        name,
        arguments,
        read_timeout_seconds=timeout_seconds,
    )
    payload = _decode(result)
    return payload, {
        "tool": name,
        "elapsed_seconds": time.perf_counter() - started,
        "response_bytes": len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ),
    }


async def _discover_profile(profile: str) -> dict[str, Any]:
    started = time.perf_counter()
    async with stdio_client(_server(profile)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            capabilities_result = await session.call_tool("capabilities", {})
            capabilities = _decode(capabilities_result)
    expected = PROFILE_COUNTS[profile]
    assert len(names) == expected, (profile, len(names), expected)
    assert capabilities["profile"] == profile, capabilities
    assert capabilities["tool_count"] == expected, capabilities
    identity = capabilities["deployment_identity"]
    assert identity["source_classification"] == "installed_site_package", identity
    assert identity["contains_local_path"] is False, identity
    return {
        "profile": profile,
        "tool_count": len(names),
        "tools": names,
        "capabilities": capabilities,
        "elapsed_seconds": time.perf_counter() - started,
    }


async def _wait_for_comsol(session: ClientSession) -> list[dict[str, Any]]:
    polls: list[dict[str, Any]] = []
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status, timing = await _call_before(session, "comsol_status", {}, deadline=deadline)
        polls.append({"status": status, **timing})
        if status.get("connected"):
            return polls
        if not status.get("starting"):
            raise RuntimeError(f"COMSOL start stopped before connection: {status}")
        await anyio.sleep(2)
    raise TimeoutError("COMSOL did not connect within 120 seconds")


async def _setup_live_session(session: ClientSession, output: dict[str, Any]) -> None:
    start_result, start_timing = await _call_before(
        session,
        "comsol_start",
        {"cores": 8, "version": "6.4"},
        deadline=time.monotonic() + COLD_START_RESPONSE_LIMIT_SECONDS,
    )
    assert start_result.get("success"), start_result
    assert start_timing["elapsed_seconds"] <= COLD_START_RESPONSE_LIMIT_SECONDS, start_timing
    output["setup"]["comsol_start"] = {"result": start_result, **start_timing}
    output["setup"]["status_polls"] = await _wait_for_comsol(session)

    first_disconnect, first_disconnect_timing = await _call_before(
        session,
        "comsol_disconnect",
        {},
        deadline=time.monotonic() + CONTROL_PLANE_READ_LIMIT_SECONDS,
    )
    assert first_disconnect.get("success"), first_disconnect
    assert first_disconnect.get("client_reusable") is True, first_disconnect
    between_status, between_status_timing = await _call_before(
        session,
        "solver_status",
        {},
        deadline=time.monotonic() + CONTROL_PLANE_READ_LIMIT_SECONDS,
    )
    assert between_status.get("lease", {}).get("state") == "absent", between_status
    restart_result, restart_timing = await _call_before(
        session,
        "comsol_start",
        {"cores": 8, "version": "6.4"},
        deadline=time.monotonic() + COLD_START_RESPONSE_LIMIT_SECONDS,
    )
    assert restart_result.get("success"), restart_result
    assert restart_timing["elapsed_seconds"] <= COLD_START_RESPONSE_LIMIT_SECONDS, restart_timing
    restart_polls = await _wait_for_comsol(session)
    output["setup"]["same_host_start_disconnect_start"] = {
        "response_limit_seconds": COLD_START_RESPONSE_LIMIT_SECONDS,
        "first_disconnect": {"result": first_disconnect, **first_disconnect_timing},
        "between_status": {"result": between_status, **between_status_timing},
        "restart": {"result": restart_result, **restart_timing},
        "restart_status_polls": restart_polls,
    }


async def _cleanup_live_session(session: ClientSession, model_names: list[str]) -> dict[str, Any]:
    steps: dict[str, Any] = {}
    passed = True
    deadline = time.monotonic() + LIVE_CLEANUP_LIMIT_SECONDS
    for model_name in reversed(model_names):
        try:
            result, timing = await _call_before(
                session,
                "model_remove",
                {"model_name": model_name},
                deadline=deadline,
            )
            step_passed = result.get("success") is True
            steps[f"model_remove:{model_name}"] = {
                "passed": step_passed,
                "result": result,
                **timing,
            }
            passed = passed and step_passed
        except Exception as exc:
            passed = False
            steps[f"model_remove:{model_name}"] = {
                "passed": False,
                "error_type": type(exc).__name__,
            }
    try:
        result, timing = await _call_before(
            session, "comsol_disconnect", {}, deadline=deadline
        )
        step_passed = result.get("success") is True
        steps["comsol_disconnect"] = {"passed": step_passed, "result": result, **timing}
        passed = passed and step_passed
    except Exception as exc:
        passed = False
        steps["comsol_disconnect"] = {
            "passed": False,
            "error_type": type(exc).__name__,
        }
    return {"passed": passed, "steps": steps}


def _agent_reasoning(case: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    measurement = audit["measurement"]
    power = measurement["power"]
    assessment = audit["assessment"]
    return {
        "raw_power_interpretation": (
            f"R={power.get('R')}, T={power.get('T')}, A={power.get('A')}, "
            f"closure_residual={power.get('closure_residual')}; raw evidence is retained without clamping."
        ),
        "policy_interpretation": (
            f"The caller-declared policy result is {assessment.get('project_verdict')}; "
            "this classification is not inferred from evidence-only defaults."
        ),
        "polarization_interpretation": (
            "Structure total field is diagnostic only. Incident polarization is not claimed "
            "without a matching incident-reference artifact."
        ),
        "project_type": case["name"],
    }


async def _live_three_call_matrix() -> dict[str, Any]:
    started = time.perf_counter()
    cases = _controlled_cases()
    output: dict[str, Any] = {"profile": "wave_optics", "setup": {}, "cases": []}
    loaded_model_names: list[str] = []
    async with stdio_client(_server("wave_optics")) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=300.0) as session:
            await session.initialize()
            try:
                await _setup_live_session(session, output)
                for case in cases:
                    source = case["source"]
                    if not source.is_file():
                        raise FileNotFoundError(source)
                    source_hash = _sha256(source)
                    source_stat = source.stat()
                    loaded, load_timing = await _call(
                        session, "model_load", {"file_path": str(source)}
                    )
                    assert loaded.get("success"), loaded
                    model_name = loaded["model"]["name"]
                    loaded_model_names.append(model_name)

                    calls: list[dict[str, Any]] = []
                    ownership, timing = await _call(session, "solver_status", {})
                    calls.append({"summary": ownership, **timing})
                    assert ownership.get("success") and not ownership.get("collision"), ownership

                    preflight, timing = await _call(
                        session,
                        "wave_optics_preflight",
                        {
                            "model_name": model_name,
                            "expected_component_tag": "comp1",
                            "expected_physics_tag": "ewfd",
                            "expected_study_tag": "std1",
                            "expected_source_path": str(source),
                            "expected_source_sha256": source_hash,
                            "target_wavelength_parameter": "wl",
                        },
                    )
                    calls.append(
                        {
                            "summary": {
                                "success": preflight.get("success"),
                                "inspection_status": preflight.get("inspection_status"),
                                "evidence_codes": {
                                    key: [
                                        item.get("code")
                                        for item in preflight.get("evidence", {}).get(key, [])
                                    ]
                                    for key in (
                                        "observations",
                                        "warnings",
                                        "unknowns",
                                        "integrity_errors",
                                    )
                                },
                            },
                            **timing,
                        }
                    )
                    assert preflight.get("success"), preflight

                    audit, timing = await _call(
                        session,
                        "wave_optics_point_audit",
                        {
                            "model_name": model_name,
                            "component_tag": "comp1",
                            "physics_tag": "ewfd",
                            "study_tag": "std1",
                            "wavelength_value": case["wavelength_um"],
                            "wavelength_unit": "um",
                            "wavelength_parameter": "wl",
                            "study_step_tag": "wl_step",
                            "study_step_property": "plist",
                            "expected_source_sha256": source_hash,
                            "config_id": f"live-profile-{case['name']}",
                            "artifact_dir": str(ARTIFACT_DIR / "audits"),
                            "top_air_domain_ids": case["top_air_domain_ids"],
                            "top_air_coordinate_range": case["top_air_coordinate_range"],
                            "validation_policy": case["validation_policy"],
                        },
                    )
                    calls.append(
                        {
                            "summary": {
                                "success": audit.get("success"),
                                "audit_status": audit.get("audit_status"),
                                "assessment": audit.get("assessment"),
                                "power": audit.get("measurement", {}).get("power"),
                                "wavelength": audit.get("measurement", {}).get("wavelength"),
                                "polarization_evidence_level": audit.get("measurement", {})
                                .get("polarization", {})
                                .get("evidence_level"),
                                "artifacts": audit.get("artifacts"),
                            },
                            **timing,
                        }
                    )
                    assert audit.get("success"), audit
                    assert _sha256(source) == source_hash
                    final_stat = source.stat()
                    assert final_stat.st_mtime_ns == source_stat.st_mtime_ns
                    assert final_stat.st_size == source_stat.st_size
                    output["cases"].append(
                        {
                            "name": case["name"],
                            "source": str(source),
                            "source_sha256": source_hash,
                            "setup_model_load": {"model_name": model_name, **load_timing},
                            "exact_three_calls": calls,
                            "agent_reasoning": _agent_reasoning(case, audit),
                        }
                    )
                    removed, remove_timing = await _call(
                        session, "model_remove", {"model_name": model_name}
                    )
                    assert removed.get("success"), removed
                    loaded_model_names.remove(model_name)
                    output["cases"][-1]["cleanup"] = {"model_remove": removed, **remove_timing}
            finally:
                output["cleanup"] = await _cleanup_live_session(session, loaded_model_names)
            if output["cleanup"]["passed"] is not True:
                raise RuntimeError("live-profile cleanup did not complete")
    output["elapsed_seconds"] = time.perf_counter() - started
    assert len(output["cases"]) == len(cases)
    return output


async def main_async() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    output: dict[str, Any] = {"success": False, "profiles": []}
    result_path = ARTIFACT_DIR / "live_acceptance_result.json"
    try:
        for profile in PROFILE_COUNTS:
            output["profiles"].append(await _discover_profile(profile))
        output["three_call_matrix"] = await _live_three_call_matrix()
        output["success"] = True
    except Exception as exc:
        output["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result_path.write_text(
            json.dumps(output, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    anyio.run(main_async)


if __name__ == "__main__":
    main()
