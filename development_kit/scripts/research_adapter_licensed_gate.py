"""Run the S4 derived-copy mutation and one-point gate over isolated MCP stdio."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from development_kit.scripts.research_adapter_template_probe import (
    CALL_SECONDS,
    REPOSITORY_ROOT,
    START_RESPONSE_SECONDS,
    STARTUP_SECONDS,
    _atomic_write_json,
    _canonical_bytes,
    _git_identity,
    _sha256,
    _stdio_environment,
    _tool_payload,
)

SCHEMA_NAME = "comsol_mcp.research_adapter_licensed_gate"
SCHEMA_VERSION = "1.0.0"
SERVER = REPOSITORY_ROOT / "development_kit" / "scripts" / "research_adapter_gate_server.py"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tree-audit", type=Path, required=True)
    parser.add_argument("--patch-length-x", type=float, required=True)
    parser.add_argument("--patch-length-y", type=float, required=True)
    parser.add_argument("--wavelength-um", type=float, default=5.0)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.resolve(strict=True)
    approved = Path("D:/mcp_tests").resolve(strict=False)
    if os.name == "nt" and (root.parent != approved or len(root.name) > 12):
        raise ValueError("test root must be a direct short child of D:/mcp_tests")
    if not str(root).isascii():
        raise ValueError("test root must be ASCII")
    source = args.source_model.resolve(strict=True)
    manifest_path = args.manifest.resolve(strict=True)
    audit_path = args.tree_audit.resolve(strict=True)
    for path in (manifest_path, audit_path):
        path.relative_to(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if isinstance(args.cores, bool) or not 1 <= args.cores <= 64:
        raise ValueError("cores must be from 1 through 64")
    candidate = {
        "patch_length_x": float(args.patch_length_x),
        "patch_length_y": float(args.patch_length_y),
    }
    if not all(value > 0.0 for value in (*candidate.values(), args.wavelength_um)):
        raise ValueError("candidate and wavelength values must be positive")
    return {
        "root": root,
        "runtime": root / "runtime",
        "artifacts": root / "artifacts",
        "settings": root / "settings.json",
        "stderr": root / "server-stderr.log",
        "receipt": root / "licensed-receipt.json",
        "private": root / "licensed-private.json",
        "source": source,
        "source_sha256": _sha256(source),
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(_canonical_bytes(manifest)).hexdigest(),
        "audit": audit,
        "audit_sha256": hashlib.sha256(_canonical_bytes(audit)).hexdigest(),
        "candidate": candidate,
        "wavelength_um": float(args.wavelength_um),
        "cores": args.cores,
    }


def _settings(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": "comsol_mcp.settings",
        "schema_version": "1.3.0",
        "profile": {"name": "full"},
        "runtime": {"directory": str(spec["runtime"]), "jobs_directory": None},
        "paths": {
            "model_read_roots": [str(spec["source"].parent)],
            "artifact_write_root": str(spec["artifacts"]),
        },
        "shared_server": {"enabled": False},
        "evidence_integrity": {
            "checks": {
                "outcome_contract_validation": True,
                "artifact_chain_verification": True,
                "summary_claim_verification": True,
                "producer_driver_compatibility": True,
            }
        },
        "manuals": {"root": None},
        "lexical_docs": {"enabled": False, "index_path": None},
        "semantic_docs": {"enabled": False, "root": None, "model_path": None},
        "ownership": {"owner": "research-adapter-licensed-gate"},
        "java": {"java_home": None, "jdk_home": None},
        "comsol": {"installation_root": None},
        "gui": {"language": "en", "scale": "system"},
    }


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "source_sha256": spec["source_sha256"],
        "manifest_sha256": spec["manifest_sha256"],
        "audit_sha256": spec["audit_sha256"],
        "candidate": spec["candidate"],
        "wavelength_um": spec["wavelength_um"],
        "solver_started": False,
        "filesystem_modified": False,
        "paths_included": False,
    }


async def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["success"] or not git["worktree_clean"]:
        raise RuntimeError("licensed gate requires a clean source revision")
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "source_revision": git["commit"],
        "source_sha256": spec["source_sha256"],
        "manifest_sha256": spec["manifest_sha256"],
        "audit_sha256": spec["audit_sha256"],
        "candidate": spec["candidate"],
        "paths_included": False,
        "cleanup": {"passed": False, "steps": {}},
    }
    private: dict[str, Any] = {"calls": []}
    model_names: list[str] = []
    started = False
    transport = True

    async def call(session, name, arguments, timeout=CALL_SECONDS):
        nonlocal transport
        begun = time.perf_counter()
        try:
            result = await session.call_tool(name, arguments, read_timeout_seconds=timeout)
        except Exception:
            transport = False
            raise
        payload = _tool_payload(result)
        private["calls"].append(
            {"tool": name, "elapsed_seconds": time.perf_counter() - begun, "payload": payload}
        )
        return payload

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        cwd=REPOSITORY_ROOT,
        env=_stdio_environment(spec["settings"]),
    )
    source_stat = spec["source"].stat()
    with spec["stderr"].open("w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                try:
                    capabilities = await call(session, "capabilities", {})
                    if (
                        capabilities["deployment_identity"]["source_classification"]
                        != "source_tree"
                    ):
                        raise RuntimeError("gate server is not using the source tree")
                    cold = await call(session, "comsol_status", {})
                    if cold.get("connected") or cold.get("starting"):
                        raise RuntimeError("gate server is not cold")
                    ownership = await call(session, "solver_status", {})
                    if ownership.get("collision") is True:
                        raise RuntimeError("solver collision detected")
                    preflight = await call(
                        session,
                        "solver_preflight",
                        {"model_path": str(spec["source"]), "requested_version": "6.4"},
                    )
                    if preflight.get("ready") is not True:
                        raise RuntimeError("solver preflight rejected the gate")
                    started = True
                    start = await call(
                        session,
                        "comsol_start",
                        {"cores": spec["cores"], "version": "6.4"},
                        START_RESPONSE_SECONDS,
                    )
                    if start.get("success") is False:
                        raise RuntimeError("COMSOL start was rejected")
                    deadline = time.monotonic() + STARTUP_SECONDS
                    while True:
                        status = await call(session, "comsol_status", {})
                        if status.get("connected") is True:
                            break
                        if status.get("starting") is not True or time.monotonic() >= deadline:
                            raise RuntimeError("COMSOL did not reach connected state")
                        await asyncio.sleep(2)
                    loaded = await call(session, "model_load", {"file_path": str(spec["source"])})
                    source_name = loaded["model"]["name"]
                    model_names.append(source_name)
                    clone = await call(
                        session,
                        "geometry_derived_clone",
                        {"source_model_name": source_name, "new_name": "S4Candidate"},
                    )
                    if clone.get("success") is not True:
                        raise RuntimeError("derived clone failed")
                    derived_name = clone["model_name"]
                    model_names.append(derived_name)
                    state = await call(
                        session,
                        "research_adapter_gate_state",
                        {
                            "model_name": derived_name,
                            "derived_model_id": clone["derived_model_id"],
                            "manifest": spec["manifest"],
                        },
                    )
                    if state.get("success") is not True:
                        raise RuntimeError("trusted adapter state failed")
                    applied = await call(
                        session,
                        "research_adapter_gate_apply",
                        {
                            "model_name": derived_name,
                            "derived_model_id": clone["derived_model_id"],
                            "manifest": spec["manifest"],
                            "tree_audit": spec["audit"],
                            "candidate": spec["candidate"],
                            "expected_state_sha256": state["state_sha256"],
                        },
                    )
                    if applied.get("success") is not True:
                        raise RuntimeError("trusted adapter application failed")
                    audit = await call(
                        session,
                        "wave_optics_point_audit",
                        {
                            "model_name": derived_name,
                            "component_tag": "comp1",
                            "physics_tag": "ewfd",
                            "study_tag": "std1",
                            "wavelength_value": spec["wavelength_um"],
                            "wavelength_unit": "um",
                            "wavelength_parameter": "wl",
                            "study_step_tag": "step1",
                            "study_step_property": "plist",
                            "expected_source_sha256": spec["source_sha256"],
                            "config_id": "alpha70-s4-one-point",
                            "artifact_dir": str(spec["artifacts"] / "point"),
                            "top_air_domain_ids": [2],
                            "top_air_coordinate_range": {
                                "x": [1.0e-7, 1.25e-6],
                                "y": [1.0e-7, 1.25e-6],
                                "z": [8.0e-7, 1.2e-6],
                            },
                        },
                        600.0,
                    )
                    if audit.get("success") is not True:
                        raise RuntimeError("one-point evidence collection failed")
                    measurement = audit.get("measurement", {})
                    receipt["application"] = {
                        key: applied.get(key)
                        for key in (
                            "success",
                            "pre_state_sha256",
                            "post_state_sha256",
                            "rollback_proved",
                            "derived_model_dirty",
                            "snapshot",
                        )
                    }
                    receipt["measurement"] = {
                        "power": measurement.get("power"),
                        "wavelength": measurement.get("wavelength"),
                        "audit_status": audit.get("audit_status"),
                        "assessment": audit.get("assessment"),
                        "artifacts": audit.get("artifacts"),
                    }
                    receipt["success"] = True
                except Exception as exc:
                    receipt["error"] = {"type": type(exc).__name__, "message": str(exc)[:512]}
                finally:
                    steps = receipt["cleanup"]["steps"]
                    for name in reversed(model_names):
                        if not transport:
                            steps[f"remove:{name}"] = {"passed": False}
                            continue
                        try:
                            removed = await call(session, "model_remove", {"model_name": name})
                            steps[f"remove:{name}"] = {"passed": removed.get("success") is True}
                        except Exception:
                            steps[f"remove:{name}"] = {"passed": False}
                    if transport and started:
                        try:
                            disconnected = await call(session, "comsol_disconnect", {})
                            steps["disconnect"] = {"passed": disconnected.get("success") is True}
                        except Exception:
                            steps["disconnect"] = {"passed": False}
                    if transport:
                        try:
                            final = await call(session, "solver_status", {})
                            steps["lease_absent"] = {
                                "passed": final.get("lease", {}).get("state") == "absent"
                            }
                        except Exception:
                            steps["lease_absent"] = {"passed": False}
    unchanged = (
        _sha256(spec["source"]) == spec["source_sha256"]
        and spec["source"].stat().st_mtime_ns == source_stat.st_mtime_ns
        and spec["source"].stat().st_size == source_stat.st_size
    )
    receipt["cleanup"]["steps"]["source_unchanged"] = {"passed": unchanged}
    receipt["cleanup"]["passed"] = all(
        item.get("passed") is True for item in receipt["cleanup"]["steps"].values()
    )
    receipt["success"] = receipt["success"] is True and receipt["cleanup"]["passed"] is True
    private["stderr_sha256"] = _sha256(spec["stderr"])
    return receipt, private


def main() -> int:
    args = _parser().parse_args()
    try:
        spec = _spec(args)
        if args.dry_run:
            print(json.dumps(_dry_run(spec), sort_keys=True))
            return 0
        spec["runtime"].mkdir(exist_ok=False)
        spec["artifacts"].mkdir(exist_ok=False)
        _atomic_write_json(spec["settings"], _settings(spec), maximum_bytes=64 * 1024)
        receipt, private = asyncio.run(_run(spec))
        _atomic_write_json(spec["private"], private, maximum_bytes=8 * 1024 * 1024)
        _atomic_write_json(spec["receipt"], receipt, maximum_bytes=1024 * 1024)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt["success"] else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        _parser().error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
