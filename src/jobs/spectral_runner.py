"""Solver-independent point loop for durable adaptive spectral jobs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .spectral_audit import build_spectral_audit_point, extract_spectral_audit_result
from .spectral_progress import build_spectral_progress
from .spectral_rows import append_spectral_row, read_spectral_rows
from .spectral_stages import read_spectral_stage_plans, write_spectral_stage_plan
from .store import atomic_write_json, read_json


SPECTRAL_SUMMARY_SCHEMA_NAME = "comsol_mcp.durable_spectral_summary"
SPECTRAL_SUMMARY_SCHEMA_VERSION = "1.0.0"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _invoke_hook(
    hook: Callable[[str, Mapping[str, Any]], Any] | None,
    phase: str,
    payload: Mapping[str, Any],
) -> Any:
    return None if hook is None else hook(phase, dict(payload))


def _artifact_descriptor(path: Path, root: Path) -> dict[str, Any]:
    return {
        "relative_path": path.relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def write_spectral_summary(
    job_dir: str | Path,
    spec: Mapping[str, Any],
    progress: Mapping[str, Any],
    *,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Atomically persist the complete solver-free bundle and final citations."""
    if progress.get("action") != "complete" or not isinstance(progress.get("analysis"), Mapping):
        raise ValueError("a completed analyzed progress artifact is required")
    root = Path(job_dir).resolve()
    analysis_root = root / "analysis"
    artifacts = progress["analysis"]
    paths = {
        "spectral_bundle": analysis_root / "spectral_point_bundle.json",
        "spectral_decision": analysis_root / "spectral_analysis_decision.json",
        "spectral_characterization": analysis_root / "spectral_characterization.json",
        "spectral_progress": analysis_root / "spectral_progress.json",
    }
    values = {
        "spectral_bundle": artifacts["bundle"],
        "spectral_decision": artifacts["decision"],
        "spectral_characterization": artifacts["characterization"],
        "spectral_progress": dict(progress),
    }
    for name in ("spectral_bundle", "spectral_decision", "spectral_characterization"):
        atomic_write_json(paths[name], values[name])
        if read_json(paths[name]) != values[name]:
            raise RuntimeError(f"{name} did not replay after atomic write")
    _invoke_hook(
        fault_hook,
        "during_summary_write",
        {
            "written_artifacts": [
                paths[name].relative_to(root).as_posix()
                for name in ("spectral_bundle", "spectral_decision", "spectral_characterization")
            ]
        },
    )
    atomic_write_json(paths["spectral_progress"], values["spectral_progress"])
    descriptors = {
        name: _artifact_descriptor(path, root) for name, path in paths.items()
    }
    body = {
        "schema_name": SPECTRAL_SUMMARY_SCHEMA_NAME,
        "schema_version": SPECTRAL_SUMMARY_SCHEMA_VERSION,
        "spec_fingerprint": spec["spec_fingerprint"],
        "source_model_sha256": spec["source_model_sha256"],
        "configuration_sha256": spec["configuration_sha256"],
        "execution_state": "completed",
        "scientific_disposition": progress["scientific_disposition"],
        "reason_code": progress["reason_code"],
        "declared_cap_reached": progress["declared_cap_reached"],
        "stage_count": progress["stage_count"],
        "row_count": progress["row_count"],
        "last_stage_sha256": progress["last_stage_sha256"],
        "last_row_sha256": progress["last_row_sha256"],
        "artifacts": descriptors,
    }
    summary = {**body, "summary_sha256": _fingerprint(body)}
    summary_path = analysis_root / "summary.json"
    atomic_write_json(summary_path, summary)
    if read_json(summary_path) != summary:
        raise RuntimeError("spectral summary did not replay after atomic write")
    return {
        "summary": summary,
        "summary_artifact": _artifact_descriptor(summary_path, root),
    }


def _hook_action(
    hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if hook is None:
        return {"action": "continue"}
    result = hook(dict(payload))
    if not isinstance(result, Mapping):
        raise ValueError("control hook must return an object")
    action = result.get("action", "continue")
    if action not in {"continue", "stop", "cancel"}:
        raise ValueError("control hook action is unsupported")
    return dict(result)


def run_spectral_characterization(
    spec: Mapping[str, Any],
    job_dir: str | Path,
    *,
    attempt: int,
    point_executor: Callable[[Mapping[str, Any], Path], Mapping[str, Any]],
    control_hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    after_durable_row_hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    on_durable_row: Callable[[Mapping[str, Any]], None] | None = None,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Solve at most one wavelength at a time and resume only verified exact rows."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("attempt must be a positive integer")
    root = Path(job_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    rows_path = root / "spectral_rows.jsonl"
    solved_this_attempt = 0
    skipped_complete = len(
        read_spectral_rows(rows_path, spec, artifact_root=root)
    )
    while True:
        plans = read_spectral_stage_plans(root, spec)
        rows = read_spectral_rows(rows_path, spec, artifact_root=root)
        progress = build_spectral_progress(spec, plans, rows)
        if progress["action"] == "schedule_next_stage":
            next_plan = progress["next_stage_plan"]
            phase = (
                "during_refinement_planning"
                if next_plan["stage_kind"] == "refinement"
                else "during_stage_planning"
            )
            _invoke_hook(
                fault_hook,
                phase,
                {
                    "stage_index": next_plan["stage_index"],
                    "stage_kind": next_plan["stage_kind"],
                    "stage_sha256": next_plan["stage_sha256"],
                },
            )
            write_spectral_stage_plan(root, spec, next_plan)
            continue
        if progress["action"] == "complete":
            receipt = write_spectral_summary(
                root,
                spec,
                progress,
                fault_hook=fault_hook,
            )
            return {
                "completed": True,
                "stop_reason": "spectral_characterization_complete",
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
                **receipt,
            }
        pending = progress["pending_points"]
        if not pending:
            raise RuntimeError("spectral progress requested solving without a pending point")
        target = pending[0]
        before = _hook_action(
            control_hook,
            {
                "phase": "before_solve",
                "attempt": attempt,
                "point": target,
                "completed_rows": len(rows),
            },
        )
        if before["action"] != "continue":
            return {
                "completed": False,
                "stop_reason": f"before_solve_{before['action']}",
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
            }
        _invoke_hook(fault_hook, "before_solve", target)
        point = build_spectral_audit_point(spec, target["requested_wavelength_m"])
        if point["point_fingerprint"] != target["point_fingerprint"]:
            raise RuntimeError("frozen pending point identity changed before solve")
        artifact_dir = root / "point_artifacts" / point["point_fingerprint"]
        result = point_executor(point, artifact_dir)
        projection = extract_spectral_audit_result(
            job_dir=root,
            artifact_dir=artifact_dir,
            spec=spec,
            point=point,
            result=result,
        )
        row = append_spectral_row(
            rows_path,
            spec,
            attempt=attempt,
            stage_index=plans[-1]["stage_index"],
            stage_kind=plans[-1]["stage_kind"],
            artifact_root=root,
            **projection,
        )
        solved_this_attempt += 1
        if on_durable_row is not None:
            on_durable_row(dict(row))
        _invoke_hook(fault_hook, "after_raw_row", row)
        after = _hook_action(after_durable_row_hook, row)
        if after["action"] != "continue":
            return {
                "completed": False,
                "stop_reason": f"after_durable_row_{after['action']}",
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
            }


__all__ = [
    "SPECTRAL_SUMMARY_SCHEMA_NAME",
    "SPECTRAL_SUMMARY_SCHEMA_VERSION",
    "run_spectral_characterization",
    "write_spectral_summary",
]
