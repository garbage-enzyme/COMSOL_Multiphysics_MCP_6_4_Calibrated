"""Fresh-host H3f profile discovery and live three-call acceptance gate."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from datetime import timedelta

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).parents[2]
PYTHON = Path(sys.executable)
RUNTIME = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
ARTIFACT_DIR = RUNTIME / "H3f"
PROFILE_COUNTS = {
    "core": 38,
    "basic_fem": 71,
    "wave_optics": 47,
    "experimental": 64,
    "full": 100,
}
ITERATIONS = Path(r"C:\Users\陆星\Desktop\iterations")
CASES = (
    {
        "name": "chen_mim_port",
        "source": ITERATIONS / "Chen2023_MIM" / "chen2023_c1_smoke_check.mph",
        "wavelength_um": 4.37,
        "top_air_domain_ids": [3],
        "top_air_coordinate_range": {
            "x": [0.0, 1.35e-6],
            "y": [0.0, 1.35e-6],
            "z": [1.092e-6, 1.39e-6],
        },
        "validation_policy": {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "required_evidence": ["wavelength_controls", "flux_RTA", "top_air_region", "source_integrity"],
            "tolerances": {"closure_abs": 1e-3, "quantity_bounds_margin": 1e-3, "wavelength_abs_m": 1e-12},
        },
    },
    {
        "name": "zhou_qbic_port",
        "source": ITERATIONS / "Zhou2025_QBIC" / "stage2_localmesh.mph",
        "wavelength_um": 4.254,
        "top_air_domain_ids": [3],
        "top_air_coordinate_range": {
            "x": [0.0, 2.771281292110203e-6],
            "y": [0.0, 9.6e-6],
            "z": [1.984e-6, 2.48e-6],
        },
        "validation_policy": {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "required_evidence": ["wavelength_controls", "flux_RTA", "top_air_region", "source_integrity"],
            "tolerances": {"closure_abs": 1e-3, "quantity_bounds_margin": 1e-3, "wavelength_abs_m": 1e-12},
        },
    },
    {
        "name": "sun_flatband_port",
        "source": ITERATIONS / "Sun2024_NatComm_FlatBand" / "stage2_DDS_smoke.mph",
        "wavelength_um": 5.998,
        "top_air_domain_ids": [4],
        "top_air_coordinate_range": {
            "x": [0.0, 4.0e-6],
            "y": [0.0, 2.0e-6],
            "z": [1.66e-6, 2.15e-6],
        },
        "validation_policy": {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "required_evidence": ["wavelength_controls", "flux_RTA", "incident_polarization", "source_integrity"],
            "tolerances": {"closure_abs": 1e-3, "quantity_bounds_margin": 1e-3, "wavelength_abs_m": 1e-12},
            "polarization": {"target_vector": [0, 1, 0], "max_cross_power_fraction": 0.05},
        },
    },
)


def _decode(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool returned an error: {result}")
    structured = getattr(result, "structuredContent", None)
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
    env["COMSOL_MCP_PROFILE"] = profile
    env["COMSOL_MCP_RUNTIME_DIR"] = str(RUNTIME)
    return StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "src.server"],
        cwd=ROOT,
        env=env,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    result = await session.call_tool(
        name,
        arguments,
        read_timeout_seconds=timedelta(minutes=5),
    )
    payload = _decode(result)
    return payload, {
        "tool": name,
        "elapsed_seconds": time.perf_counter() - started,
        "response_bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
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
        status, timing = await _call(session, "comsol_status", {})
        polls.append({"status": status, **timing})
        if status.get("connected"):
            return polls
        if not status.get("starting"):
            raise RuntimeError(f"COMSOL start stopped before connection: {status}")
        await anyio.sleep(2)
    raise TimeoutError("COMSOL did not connect within 120 seconds")


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
    output: dict[str, Any] = {"profile": "wave_optics", "setup": {}, "cases": []}
    async with stdio_client(_server("wave_optics")) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(minutes=5)) as session:
            await session.initialize()
            start_result, start_timing = await _call(session, "comsol_start", {"cores": 8, "version": "6.4"})
            assert start_result.get("success"), start_result
            output["setup"]["comsol_start"] = {"result": start_result, **start_timing}
            output["setup"]["status_polls"] = await _wait_for_comsol(session)

            try:
                for case in CASES:
                    source = case["source"]
                    if not source.is_file():
                        raise FileNotFoundError(source)
                    source_hash = _sha256(source)
                    source_stat = source.stat()
                    loaded, load_timing = await _call(session, "model_load", {"file_path": str(source)})
                    assert loaded.get("success"), loaded
                    model_name = loaded["model"]["name"]

                    calls: list[dict[str, Any]] = []
                    ownership, timing = await _call(session, "solver_status", {})
                    calls.append({"summary": ownership, **timing})
                    assert ownership.get("success") and not ownership.get("collision"), ownership

                    preflight, timing = await _call(session, "wave_optics_preflight", {
                        "model_name": model_name,
                        "expected_component_tag": "comp1",
                        "expected_physics_tag": "ewfd",
                        "expected_study_tag": "std1",
                        "expected_source_path": str(source),
                        "expected_source_sha256": source_hash,
                        "target_wavelength_parameter": "wl",
                    })
                    calls.append({
                        "summary": {
                            "success": preflight.get("success"),
                            "inspection_status": preflight.get("inspection_status"),
                            "evidence_codes": {
                                key: [item.get("code") for item in preflight.get("evidence", {}).get(key, [])]
                                for key in ("observations", "warnings", "unknowns", "integrity_errors")
                            },
                        },
                        **timing,
                    })
                    assert preflight.get("success"), preflight

                    audit, timing = await _call(session, "wave_optics_point_audit", {
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
                        "config_id": f"h3f-{case['name']}",
                        "artifact_dir": str(ARTIFACT_DIR / "audits"),
                        "top_air_domain_ids": case["top_air_domain_ids"],
                        "top_air_coordinate_range": case["top_air_coordinate_range"],
                        "validation_policy": case["validation_policy"],
                    })
                    calls.append({
                        "summary": {
                            "success": audit.get("success"),
                            "audit_status": audit.get("audit_status"),
                            "assessment": audit.get("assessment"),
                            "power": audit.get("measurement", {}).get("power"),
                            "wavelength": audit.get("measurement", {}).get("wavelength"),
                            "polarization_evidence_level": audit.get("measurement", {}).get("polarization", {}).get("evidence_level"),
                            "artifacts": audit.get("artifacts"),
                        },
                        **timing,
                    })
                    assert audit.get("success"), audit
                    assert _sha256(source) == source_hash
                    final_stat = source.stat()
                    assert final_stat.st_mtime_ns == source_stat.st_mtime_ns
                    assert final_stat.st_size == source_stat.st_size
                    output["cases"].append({
                        "name": case["name"],
                        "source": str(source),
                        "source_sha256": source_hash,
                        "setup_model_load": {"model_name": model_name, **load_timing},
                        "exact_three_calls": calls,
                        "agent_reasoning": _agent_reasoning(case, audit),
                    })
                    removed, remove_timing = await _call(session, "model_remove", {"model_name": model_name})
                    assert removed.get("success"), removed
                    output["cases"][-1]["cleanup"] = {"model_remove": removed, **remove_timing}
            finally:
                disconnected, disconnect_timing = await _call(session, "comsol_disconnect", {})
                output["cleanup"] = {"comsol_disconnect": disconnected, **disconnect_timing}
    output["elapsed_seconds"] = time.perf_counter() - started
    assert len(output["cases"]) == len(CASES)
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
