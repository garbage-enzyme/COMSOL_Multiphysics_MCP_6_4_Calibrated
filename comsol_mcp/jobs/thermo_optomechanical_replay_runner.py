"""Solver-independent five-stage thermo-optomechanical replay orchestration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .store import atomic_write_json, read_json
from .thermo_optomechanical_replay import THERMO_OPTOMECHANICAL_STAGES
from .thermo_optomechanical_replay_rows import (
    append_thermo_optomechanical_stage_row,
    build_stage_evidence,
    read_thermo_optomechanical_stage_rows,
)

THERMO_OPTOMECHANICAL_SUMMARY_SCHEMA_NAME = "comsol_mcp.thermo_optomechanical_summary"
THERMO_OPTOMECHANICAL_SUMMARY_SCHEMA_VERSION = "1.0.0"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _descriptor(path: Path, root: Path) -> dict[str, Any]:
    return {
        "relative_path": path.resolve().relative_to(root.resolve()).as_posix(),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _evidence_payload(root: Path, stage_id: str) -> dict[str, Any]:
    value = read_json(root / stage_id / "evidence.json")
    payload = value.get("payload") if isinstance(value, Mapping) else None
    if not isinstance(payload, Mapping):
        raise ValueError("thermo-optomechanical evidence payload is unavailable")
    return dict(payload)


def build_thermo_optomechanical_summary(
    spec: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], *, artifact_root: str | Path
) -> dict[str, Any]:
    """Evaluate the declared acceptance thresholds from verified stage artifacts."""
    if [row["stage_id"] for row in rows] != list(THERMO_OPTOMECHANICAL_STAGES):
        raise ValueError("all exact thermo-optomechanical stages are required")
    root = Path(artifact_root).resolve()
    thermal = _evidence_payload(root, "thermal_structural_solve")
    state = _evidence_payload(root, "state_evidence")
    optical = _evidence_payload(root, "optical_replay")
    policy = spec["acceptance_policy"]
    reasons = []
    if abs(float(thermal["expansion"]["relative_error"])) > policy["expansion_relative_tolerance"]:
        reasons.append("expansion_tolerance_exceeded")
    if (
        abs(float(thermal["energy_balance"]["relative_residual"]))
        > policy["energy_relative_tolerance"]
    ):
        reasons.append("thermal_energy_closure_failed")
    if float(state["mesh"]["minimum_quality"]) < policy["minimum_mesh_quality"]:
        reasons.append("minimum_mesh_quality_failed")
    if float(state["displacement_to_length"]) > policy["maximum_displacement_to_length"]:
        reasons.append("deformation_scale_failed")
    for row in optical["rows"]:
        for name in ("baseline_rta", "deformed_rta"):
            if abs(float(row[name]["closure_residual"])) > policy["rta_closure_absolute_tolerance"]:
                reasons.append("optical_power_closure_failed")
                break
    if any(item["passed"] is not True for item in optical["control_results"]):
        reasons.append("control_matrix_failed")
    body = {
        "schema_name": THERMO_OPTOMECHANICAL_SUMMARY_SCHEMA_NAME,
        "schema_version": THERMO_OPTOMECHANICAL_SUMMARY_SCHEMA_VERSION,
        "spec_fingerprint": spec["spec_fingerprint"],
        "execution_state": "completed",
        "scientific_disposition": "accepted" if not reasons else "rejected",
        "reason_codes": (
            sorted(set(reasons)) if reasons else ["all_thermo_optomechanical_gates_passed"]
        ),
        "completed_stage_count": len(rows),
        "completed_stage_ids": [row["stage_id"] for row in rows],
        "last_stage_row_sha256": rows[-1]["row_sha256"],
        "source_model_sha256": spec["source_model_sha256"],
        "material_ledger_sha256": spec["material_ledger_sha256"],
        "optical_configuration_sha256": spec["optical_configuration_sha256"],
        "thermal_extrema_K": thermal["temperature"],
        "maximum_displacement_m": thermal["displacement"]["maximum_m"],
        "maximum_stress_abs_Pa": thermal["stress"]["maximum_abs_Pa"],
        "minimum_mesh_quality": state["mesh"]["minimum_quality"],
        "optical_row_count": len(optical["rows"]),
        "control_count": len(optical["control_results"]),
        "source_unchanged": optical["source_unchanged"],
    }
    return {**body, "summary_sha256": _fingerprint(body)}


def _control_action(
    hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    payload: Mapping[str, Any],
) -> str:
    if hook is None:
        return "continue"
    result = hook(dict(payload))
    if not isinstance(result, Mapping) or result.get("action", "continue") not in {
        "continue",
        "stop",
        "cancel",
    }:
        raise ValueError("thermo-optomechanical control hook returned an unsupported action")
    return str(result.get("action", "continue"))


def run_thermo_optomechanical_replay(
    spec: Mapping[str, Any],
    artifact_root: str | Path,
    *,
    attempt: int,
    stage_executor: Callable[[str, Path, Mapping[str, Any]], Mapping[str, Any]],
    control_hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    on_durable_stage: Callable[[Mapping[str, Any]], None] | None = None,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Execute each stage once and resume only from verified durable rows."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("attempt must be a positive integer")
    root = Path(artifact_root).resolve()
    journal = root / "thermo_optomechanical_stages.jsonl"
    rows = read_thermo_optomechanical_stage_rows(journal, spec, artifact_root=root)
    skipped = len(rows)
    executed = 0
    while len(rows) < len(THERMO_OPTOMECHANICAL_STAGES):
        ordinal = len(rows)
        stage_id = THERMO_OPTOMECHANICAL_STAGES[ordinal]
        action = _control_action(
            control_hook,
            {
                "phase": "before_stage",
                "attempt": attempt,
                "ordinal": ordinal,
                "stage_id": stage_id,
                "completed_stages": len(rows),
            },
        )
        if action != "continue":
            return {
                "completed": False,
                "stop_reason": f"before_stage_{action}",
                "executed_this_attempt": executed,
                "skipped_complete": skipped,
            }
        stage_dir = root / stage_id
        evidence_path = stage_dir / "evidence.json"
        if evidence_path.is_file():
            row = append_thermo_optomechanical_stage_row(
                journal, spec, attempt=attempt, artifact_root=root
            )
        else:
            if fault_hook is not None:
                fault_hook("before_stage", {"stage_id": stage_id, "ordinal": ordinal})
            payload = stage_executor(stage_id, stage_dir, spec)
            if not isinstance(payload, Mapping):
                raise RuntimeError("thermo-optomechanical stage executor returned invalid evidence")
            evidence = build_stage_evidence(spec, stage_id, payload)
            atomic_write_json(evidence_path, evidence)
            if read_json(evidence_path) != evidence:
                raise RuntimeError("thermo-optomechanical stage evidence did not replay")
            if fault_hook is not None:
                fault_hook("after_stage_evidence", evidence)
            row = append_thermo_optomechanical_stage_row(
                journal, spec, attempt=attempt, artifact_root=root
            )
            executed += 1
        rows.append(row)
        if on_durable_stage is not None:
            on_durable_stage(dict(row))
        if fault_hook is not None:
            fault_hook("after_stage_row", row)
    summary = build_thermo_optomechanical_summary(spec, rows, artifact_root=root)
    summary_path = root / "analysis" / "summary.json"
    atomic_write_json(summary_path, summary)
    if read_json(summary_path) != summary:
        raise RuntimeError("thermo-optomechanical summary did not replay")
    return {
        "completed": True,
        "stop_reason": "thermo_optomechanical_replay_complete",
        "executed_this_attempt": executed,
        "skipped_complete": skipped,
        "summary": summary,
        "summary_artifact": _descriptor(summary_path, root),
    }


__all__ = [
    "THERMO_OPTOMECHANICAL_SUMMARY_SCHEMA_NAME",
    "THERMO_OPTOMECHANICAL_SUMMARY_SCHEMA_VERSION",
    "build_thermo_optomechanical_summary",
    "run_thermo_optomechanical_replay",
]
