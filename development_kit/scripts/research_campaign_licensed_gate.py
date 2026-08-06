"""Run strict feasible or impossible alpha7 research-campaign acceptance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from comsol_mcp.durable import domain_sha256_v2  # noqa: E402
from comsol_mcp.evidence.spectral_characterization import (  # noqa: E402
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
)
from comsol_mcp.research.adaptive_acquisition import (  # noqa: E402
    GaussianProcessExpectedImprovementOptimizer,
)
from development_kit.scripts.research_adapter_licensed_gate import (  # noqa: E402
    _atomic_write_json,
    _canonical_bytes,
    _git_identity,
    _settings,
    _sha256,
    _stdio_environment,
    _tool_payload,
)

SCHEMA_NAME = "comsol_mcp.research_campaign_licensed_gate"
SCHEMA_VERSION = "1.0.0"
SERVER = REPOSITORY_ROOT / "development_kit/scripts/research_adapter_gate_server.py"
WAVELENGTHS_M = tuple(1.5e-6 + index * 6.25e-8 for index in range(17))
PEAK_TOLERANCE_M = 1.5e-7
Q_TOLERANCE = 1.0
PASSIVITY_ABS_TOLERANCE = 5.0e-2
IMPOSSIBLE_TARGET = {"peak_wavelength_m": 5.0e-6, "quality_factor": 50.0}
HIDDEN_CANDIDATE = {"patch_length_x": 9.0e-7, "patch_length_y": 8.0e-7}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tree-audit", type=Path, required=True)
    parser.add_argument("--mode", choices=("feasible", "impossible"), required=True)
    parser.add_argument("--budget", type=int, default=32)
    parser.add_argument("--cores", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.resolve(strict=True)
    approved = Path("D:/mcp_tests").resolve(strict=False)
    if os.name == "nt" and (root.parent != approved or len(root.name) > 12):
        raise ValueError("test root must be a direct short child of D:/mcp_tests")
    if not str(root).isascii():
        raise ValueError("test root must be ASCII")
    if isinstance(args.budget, bool) or not 1 <= args.budget <= 32:
        raise ValueError("budget must be from 1 through 32")
    if isinstance(args.cores, bool) or not 1 <= args.cores <= 64:
        raise ValueError("cores must be from 1 through 64")
    source = args.source_model.resolve(strict=True)
    manifest_path = args.manifest.resolve(strict=True)
    audit_path = args.tree_audit.resolve(strict=True)
    for path in (manifest_path, audit_path):
        path.relative_to(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    dimensions = {item["variable_id"]: item for item in manifest["mutable_dimensions"]}
    if set(dimensions) != {"patch_length_x", "patch_length_y"}:
        raise ValueError("manifest does not expose the frozen two-variable space")
    return {
        "root": root,
        "runtime": root / "runtime",
        "artifacts": root / "artifacts",
        "settings": root / "settings.json",
        "stderr": root / "server-stderr.log",
        "receipt": root / "licensed-campaign-receipt.json",
        "private": root / "licensed-campaign-private.json",
        "state": root / "campaign-state.json",
        "source": source,
        "source_sha256": _sha256(source),
        "manifest": manifest,
        "manifest_sha256": hashlib.sha256(_canonical_bytes(manifest)).hexdigest(),
        "audit": audit,
        "audit_sha256": hashlib.sha256(_canonical_bytes(audit)).hexdigest(),
        "mode": args.mode,
        "budget": args.budget,
        "cores": args.cores,
        "design_space": {
            "schema_name": "comsol_mcp.design_space",
            "schema_version": "1.0.0",
            "space_id": "licensed-periodic-mim-patch-v1",
            "structure_family": manifest["structure_family"],
            "template_identity": manifest["source_identity"],
            "variables": [
                {
                    "variable_id": name,
                    "kind": "continuous",
                    "unit": "m",
                    "baseline": float(dimensions[name]["baseline"]),
                    "lower": float(dimensions[name]["lower"]),
                    "upper": float(dimensions[name]["upper"]),
                    "allowed_values": None,
                    "dependency_class": "geometry",
                    "adapter_path": f"geom.{name}",
                }
                for name in ("patch_length_x", "patch_length_y")
            ],
            "constraints": [],
            "canonicalization": {"float_digits": 15, "relative_tolerance": 1.0e-12},
            "adapter_mappings": [
                {"variable_id": name, "adapter_path": f"geom.{name}", "unit": "m"}
                for name in ("patch_length_x", "patch_length_y")
            ],
        },
    }


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "mode": spec["mode"],
        "candidate_evaluation_budget": spec["budget"],
        "wavelength_solve_count_per_candidate": len(WAVELENGTHS_M),
        "wavelength_grid_m": list(WAVELENGTHS_M),
        "objective_tolerances": {
            "peak_wavelength_m": PEAK_TOLERANCE_M,
            "quality_factor": Q_TOLERANCE,
        },
        "solver_started": False,
        "filesystem_modified": False,
        "paths_included": False,
    }


def _characterize(
    rows: list[dict[str, Any]], candidate: dict[str, float], source_sha256: str
) -> dict[str, Any]:
    configuration = domain_sha256_v2(
        "comsol_mcp.licensed_spectrum_configuration",
        {"candidate": candidate, "wavelengths_m": list(WAVELENGTHS_M)},
    )
    bound_rows = []
    for index, row in enumerate(rows):
        raw_hash = domain_sha256_v2("comsol_mcp.licensed_spectrum_row", row)
        bound_rows.append(
            {
                "row_id": f"point-{index:03d}",
                "raw_row_sha256": raw_hash,
                "configuration_sha256": configuration,
                "requested_wavelength_m": row["requested_wavelength_m"],
                "evaluated_wavelength_m": row["evaluated_wavelength_m"],
                "frequency_wavelength_m": row["solved_frequency_wavelength_m"],
                "R": row["R"],
                "T": row["T"],
                "A": row["A"],
            }
        )
    bundle = build_spectral_point_bundle(
        bundle_id="licensed-candidate-spectrum",
        source_model={"relative_identity": "private/source.mph", "sha256": source_sha256},
        configuration_sha256=configuration,
        parameter_state=candidate,
        wavelength_convention={
            "unit": "m",
            "requested_field": "requested_wavelength_m",
            "evaluated_field": "evaluated_wavelength_m",
            "frequency_derived_field": "frequency_wavelength_m",
            "frequency_relation": "c_const/frequency",
        },
        expressions={"R": "ewfd.Rtotal", "T": "ewfd.Ttotal", "A": "ewfd.Atotal"},
        rows=bound_rows,
    )
    decision = build_spectral_analysis_decision(
        bundle,
        {
            "response_quantity": "R",
            "candidate_polarity": "maximum",
            "passivity_abs_tolerance": PASSIVITY_ABS_TOLERANCE,
            "closure_abs_tolerance": 1.0e-9,
            "wavelength_sync_abs_m": 1.0e-15,
            "flat_response_abs_tolerance": 1.0e-6,
            "minimum_point_count": 7,
        },
    )
    measurement = build_spectral_characterization(
        bundle,
        decision,
        {
            "peak_method": "quadratic_interpolation",
            "baseline_rule": "local_prominence",
            "baseline_response_value": None,
            "fwhm_definition": "half_prominence",
            "fit_support_points": None,
            "fit_support_sensitivity_points": [],
            "local_polynomial_degree": None,
            "fit_max_evaluations": None,
            "fit_quality_policy": None,
        },
    )
    candidate_measurement = measurement.get("candidate")
    if measurement.get("measurement_state") != "measured" or not isinstance(
        candidate_measurement, dict
    ):
        raise ValueError(f"spectrum is not measurable: {measurement.get('reason_code')}")
    q = candidate_measurement["quality_factor"]
    if (
        q.get("state") != "computed_from_bracketed_fwhm"
        or not isinstance(q.get("value"), (int, float))
        or not math.isfinite(float(q["value"]))
        or float(q["value"]) <= 0.0
    ):
        raise ValueError(f"spectrum does not bracket a finite quality factor: {q}")
    return {
        "peak_wavelength_m": float(candidate_measurement["peak"]["wavelength_m"]),
        "quality_factor": float(q["value"]),
        "bundle_sha256": bundle["bundle_sha256"],
        "decision_sha256": decision["decision_sha256"],
        "characterization_sha256": measurement["characterization_sha256"],
        "maximum_closure_abs": max(float(row["closure_abs"]) for row in rows),
        "maximum_wavelength_sync_abs_m": max(float(row["wavelength_sync_abs_m"]) for row in rows),
    }


def _score(measurement: dict[str, Any], target: dict[str, float]) -> dict[str, Any]:
    peak_loss = (
        abs(measurement["peak_wavelength_m"] - target["peak_wavelength_m"]) / PEAK_TOLERANCE_M
    )
    q_loss = abs(measurement["quality_factor"] - target["quality_factor"]) / Q_TOLERANCE
    return {
        "peak_loss": peak_loss,
        "q_loss": q_loss,
        "total_loss": peak_loss + q_loss,
        "success": peak_loss <= 1.0 and q_loss <= 1.0,
    }


async def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["success"] or not git["worktree_clean"]:
        raise RuntimeError("licensed gate requires a clean source revision")
    source_stat = spec["source"].stat()
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "mode": spec["mode"],
        "source_revision": git["commit"],
        "source_sha256": spec["source_sha256"],
        "manifest_sha256": spec["manifest_sha256"],
        "audit_sha256": spec["audit_sha256"],
        "candidate_evaluation_budget": spec["budget"],
        "wavelength_solve_count_per_candidate": len(WAVELENGTHS_M),
        "paths_included": False,
        "cleanup": {"passed": False, "steps": {}},
    }
    private: dict[str, Any] = {"calls": [], "evaluations": []}
    optimizer = GaussianProcessExpectedImprovementOptimizer(
        spec["design_space"],
        seed=17001 if spec["mode"] == "feasible" else 17002,
        warmup_count=8,
        candidate_pool_count=256,
    )
    model_names: list[str] = []
    transport = True
    started = False

    async def call(session, name, arguments, timeout=600.0):
        nonlocal transport
        begun = time.perf_counter()
        try:
            payload = _tool_payload(
                await session.call_tool(name, arguments, read_timeout_seconds=timeout)
            )
        except Exception:
            transport = False
            raise
        private["calls"].append(
            {"tool": name, "elapsed_seconds": time.perf_counter() - begun, "payload": payload}
        )
        if payload.get("success") is False:
            raise RuntimeError(f"{name} failed: {payload.get('error_type', 'unknown')}")
        return payload

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER)],
        cwd=REPOSITORY_ROOT,
        env=_stdio_environment(spec["settings"]),
    )
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
                    await call(session, "comsol_start", {"cores": spec["cores"], "version": "6.4"})
                    while True:
                        status = await call(session, "comsol_status", {})
                        if status.get("connected") is True:
                            break
                        if status.get("starting") is not True:
                            raise RuntimeError("COMSOL did not reach connected state")
                        await asyncio.sleep(2)
                    loaded = await call(session, "model_load", {"file_path": str(spec["source"])})
                    source_name = loaded["model"]["name"]
                    model_names.append(source_name)
                    clone = await call(
                        session,
                        "research_adapter_gate_clone",
                        {"source_model_name": source_name, "new_name": "CampaignCandidate"},
                    )
                    model_name = clone["model_name"]
                    model_names.append(model_name)

                    async def evaluate(values: dict[str, float]) -> dict[str, Any]:
                        state = await call(
                            session,
                            "research_adapter_gate_state",
                            {
                                "model_name": model_name,
                                "derived_model_id": clone["derived_model_id"],
                                "manifest": spec["manifest"],
                            },
                        )
                        applied = await call(
                            session,
                            "research_adapter_gate_apply",
                            {
                                "model_name": model_name,
                                "derived_model_id": clone["derived_model_id"],
                                "manifest": spec["manifest"],
                                "tree_audit": spec["audit"],
                                "candidate": values,
                                "expected_state_sha256": state["state_sha256"],
                            },
                        )
                        if applied.get("success") is not True:
                            raise RuntimeError("candidate application failed")
                        spectrum = await call(
                            session,
                            "research_adapter_gate_spectrum",
                            {
                                "model_name": model_name,
                                "wavelengths_m": list(WAVELENGTHS_M),
                                "wavelength_sync_abs_m": 1.0e-15,
                            },
                            3600.0,
                        )
                        return _characterize(spectrum["rows"], values, spec["source_sha256"])

                    if spec["mode"] == "feasible":
                        target_measurement = await evaluate(HIDDEN_CANDIDATE)
                        target = {
                            "peak_wavelength_m": target_measurement["peak_wavelength_m"],
                            "quality_factor": target_measurement["quality_factor"],
                        }
                        receipt["hidden_target_evaluation"] = {
                            "candidate_withheld_from_optimizer": True,
                            "measurement": target_measurement,
                        }
                    else:
                        target = dict(IMPOSSIBLE_TARGET)
                    target_fingerprint = domain_sha256_v2(
                        "comsol_mcp.licensed_campaign_target",
                        {
                            "target": target,
                            "tolerances": {
                                "peak_wavelength_m": PEAK_TOLERANCE_M,
                                "quality_factor": Q_TOLERANCE,
                            },
                            "wavelengths_m": list(WAVELENGTHS_M),
                        },
                    )
                    receipt["target"] = target
                    receipt["target_fingerprint"] = target_fingerprint
                    receipt["objective_tolerances"] = {
                        "peak_wavelength_m": PEAK_TOLERANCE_M,
                        "quality_factor": Q_TOLERANCE,
                    }
                    best = None
                    stop_reason = "budget_exhausted"
                    for index in range(spec["budget"]):
                        proposal = optimizer.ask()
                        values = {name: float(value) for name, value in proposal["values"].items()}
                        measurement = await evaluate(values)
                        score = _score(measurement, target)
                        candidate_fingerprint = domain_sha256_v2(
                            "comsol_mcp.licensed_campaign_candidate", values
                        )
                        score_fingerprint = domain_sha256_v2(
                            "comsol_mcp.licensed_campaign_score", score
                        )
                        optimizer.tell(
                            proposal,
                            candidate_fingerprint=candidate_fingerprint,
                            status="completed",
                            score_fingerprint=score_fingerprint,
                            losses={"peak": score["peak_loss"], "q": score["q_loss"]},
                        )
                        row = {
                            "evaluation_index": index,
                            "proposal": proposal,
                            "candidate_fingerprint": candidate_fingerprint,
                            "measurement": measurement,
                            "score": score,
                            "score_fingerprint": score_fingerprint,
                        }
                        private["evaluations"].append(row)
                        if best is None or (score["total_loss"], candidate_fingerprint) < (
                            best["score"]["total_loss"],
                            best["candidate_fingerprint"],
                        ):
                            best = row
                        _atomic_write_json(
                            spec["state"],
                            {
                                "schema_name": "comsol_mcp.licensed_campaign_state",
                                "schema_version": "1.0.0",
                                "mode": spec["mode"],
                                "target_fingerprint": target_fingerprint,
                                "completed_candidate_evaluations": index + 1,
                                "optimizer_checkpoint": optimizer.checkpoint(),
                                "best": best,
                            },
                            maximum_bytes=4 * 1024 * 1024,
                        )
                        if spec["mode"] == "feasible" and score["success"]:
                            stop_reason = "target_met"
                            break
                    completed = len(private["evaluations"])
                    receipt["completed_candidate_evaluations"] = completed
                    receipt["started_wavelength_solves"] = completed * len(WAVELENGTHS_M) + (
                        len(WAVELENGTHS_M) if spec["mode"] == "feasible" else 0
                    )
                    receipt["stop_reason"] = stop_reason
                    receipt["best"] = best
                    receipt["scientific_outcome"] = (
                        "target_met"
                        if spec["mode"] == "feasible" and stop_reason == "target_met"
                        else "target_unmet"
                    )
                    receipt["success"] = (
                        stop_reason == "target_met"
                        if spec["mode"] == "feasible"
                        else stop_reason == "budget_exhausted" and completed == spec["budget"]
                    )
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
        _atomic_write_json(spec["private"], private, maximum_bytes=32 * 1024 * 1024)
        _atomic_write_json(spec["receipt"], receipt, maximum_bytes=4 * 1024 * 1024)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt["success"] else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        _parser().error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
