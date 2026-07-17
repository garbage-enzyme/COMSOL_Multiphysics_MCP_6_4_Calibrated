"""Solver-independent orchestration for durable convergence campaigns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from src.evidence.convergence_evaluation import (
    build_convergence_ladder,
    evaluate_convergence,
)

from .convergence_campaign_rows import (
    append_convergence_campaign_level,
    read_convergence_campaign_levels,
)
from .store import atomic_write_json, read_json


CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_NAME = "comsol_mcp.convergence_campaign_summary"
CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_VERSION = "1.0.0"


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


def _level_input(root: Path, spec: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    level = spec["levels"][row["ordinal"]]
    artifacts = row["artifacts"]
    bundle = read_json(root / artifacts["spectral_bundle"]["relative_path"])
    decision = read_json(root / artifacts["spectral_decision"]["relative_path"])
    characterization = read_json(
        root / artifacts["spectral_characterization"]["relative_path"]
    )
    return {
        "level_id": level["level_id"],
        "ordinal": level["ordinal"],
        "declared_predecessor_level_id": level["declared_predecessor_level_id"],
        "source_model_sha256": row["source_model_sha256"],
        "configuration_sha256": row["configuration_sha256"],
        "mesh_counts": row["mesh_counts"],
        "material_identity_sha256": row["material_identity_sha256"],
        "incidence_identity_sha256": row["incidence_identity_sha256"],
        "spectral_bundle": bundle,
        "analysis_decision": decision,
        "candidate_measurements": characterization,
        "optional_field_metrics": {},
        "fixed_reference_diagnostics": {},
    }


def build_convergence_campaign_progress(
    spec: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    *,
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Recompute the next campaign action only from verified completed levels."""
    root = Path(artifact_root).resolve()
    completed = len(rows)
    total = len(spec["levels"])
    if completed == 0:
        return {
            "action": "schedule_next_level",
            "next_level": spec["levels"][0],
            "completed_level_count": 0,
            "declared_level_count": total,
            "ladder": None,
            "evaluation": None,
        }
    ladder = None
    evaluation = None
    if completed >= 2:
        ladder = build_convergence_ladder(
            ladder_id=spec["campaign_id"],
            levels=[_level_input(root, spec, row) for row in rows],
        )
        policy = {
            **spec["convergence_policy"],
            "declared_cap_reached": (
                bool(spec["convergence_policy"]["declared_cap_reached"])
                if completed == total
                else False
            ),
        }
        evaluation = evaluate_convergence(ladder, policy)
    last = rows[-1]
    if last["scientific_disposition"] in {"unresolved_at_declared_cap", "invalid_evidence"}:
        return {
            "action": "complete",
            "scientific_disposition": last["scientific_disposition"],
            "reason_code": "level_spectrum_" + last["reason_code"],
            "completed_level_count": completed,
            "declared_level_count": total,
            "declared_cap_reached": last["scientific_disposition"] == "unresolved_at_declared_cap",
            "ladder": ladder,
            "evaluation": evaluation,
        }
    early = (
        evaluation is not None
        and spec["stop_policy"]["allow_early_acceptance"]
        and completed >= spec["stop_policy"]["minimum_completed_levels"]
        and evaluation["scientific_disposition"] == "accepted"
    )
    if early or completed == total:
        if evaluation is None:
            raise RuntimeError("a completed convergence campaign requires an evaluation")
        return {
            "action": "complete",
            "scientific_disposition": evaluation["scientific_disposition"],
            "reason_code": (
                "early_acceptance_allowed" if early else evaluation["reason_code"]
            ),
            "completed_level_count": completed,
            "declared_level_count": total,
            "declared_cap_reached": evaluation["convergence_policy"]["declared_cap_reached"],
            "ladder": ladder,
            "evaluation": evaluation,
        }
    return {
        "action": "schedule_next_level",
        "next_level": spec["levels"][completed],
        "completed_level_count": completed,
        "declared_level_count": total,
        "ladder": ladder,
        "evaluation": evaluation,
    }


def write_convergence_campaign_summary(
    spec: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    progress: Mapping[str, Any],
    *,
    artifact_root: str | Path,
) -> dict[str, Any]:
    if progress.get("action") != "complete":
        raise ValueError("completed convergence progress is required")
    root = Path(artifact_root).resolve()
    analysis = root / "analysis"
    artifacts: dict[str, dict[str, Any]] = {}
    for name, value in (
        ("convergence_ladder", progress.get("ladder")),
        ("convergence_evaluation", progress.get("evaluation")),
    ):
        if value is not None:
            path = analysis / f"{name}.json"
            atomic_write_json(path, dict(value))
            if read_json(path) != value:
                raise RuntimeError(f"{name} did not replay after atomic write")
            artifacts[name] = _descriptor(path, root)
    body = {
        "schema_name": CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_NAME,
        "schema_version": CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_VERSION,
        "spec_fingerprint": spec["spec_fingerprint"],
        "campaign_id": spec["campaign_id"],
        "execution_state": "completed",
        "scientific_disposition": progress["scientific_disposition"],
        "reason_code": progress["reason_code"],
        "declared_cap_reached": progress["declared_cap_reached"],
        "declared_level_count": progress["declared_level_count"],
        "completed_level_count": progress["completed_level_count"],
        "completed_level_ids": [row["level_id"] for row in rows],
        "last_level_row_sha256": rows[-1]["row_sha256"],
        "artifacts": artifacts,
    }
    summary = {**body, "summary_sha256": _fingerprint(body)}
    path = analysis / "summary.json"
    atomic_write_json(path, summary)
    if read_json(path) != summary:
        raise RuntimeError("convergence campaign summary did not replay")
    return {"summary": summary, "summary_artifact": _descriptor(path, root)}


def _control_action(
    hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    payload: Mapping[str, Any],
) -> str:
    if hook is None:
        return "continue"
    result = hook(dict(payload))
    if not isinstance(result, Mapping) or result.get("action", "continue") not in {
        "continue", "stop", "cancel"
    }:
        raise ValueError("campaign control hook returned an unsupported action")
    return str(result.get("action", "continue"))


def run_convergence_campaign(
    spec: Mapping[str, Any],
    artifact_root: str | Path,
    *,
    attempt: int,
    level_executor: Callable[[Mapping[str, Any], Path], Mapping[str, Any]],
    control_hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    on_durable_level: Callable[[Mapping[str, Any]], None] | None = None,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Execute each declared spectral level once and resume from verified rows."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("attempt must be a positive integer")
    root = Path(artifact_root).resolve()
    journal = root / "convergence_levels.jsonl"
    solved_this_attempt = 0
    skipped_complete = len(
        read_convergence_campaign_levels(journal, spec, artifact_root=root)
    )
    while True:
        rows = read_convergence_campaign_levels(journal, spec, artifact_root=root)
        progress = build_convergence_campaign_progress(spec, rows, artifact_root=root)
        if progress["action"] == "complete":
            if fault_hook is not None:
                fault_hook("during_summary_write", {"completed_levels": len(rows)})
            receipt = write_convergence_campaign_summary(
                spec, rows, progress, artifact_root=root
            )
            return {
                "completed": True,
                "stop_reason": "convergence_campaign_complete",
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
                **receipt,
            }
        level = progress["next_level"]
        action = _control_action(
            control_hook,
            {
                "phase": "before_level",
                "attempt": attempt,
                "level_id": level["level_id"],
                "ordinal": level["ordinal"],
                "completed_levels": len(rows),
            },
        )
        if action != "continue":
            return {
                "completed": False,
                "stop_reason": f"before_level_{action}",
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
            }
        if fault_hook is not None:
            fault_hook("before_level", {"level_id": level["level_id"]})
        level_dir = root / "levels" / f"{level['ordinal']:02d}-{level['level_id']}"
        result = level_executor(level, level_dir)
        if not isinstance(result, Mapping) or result.get("completed") is not True:
            raise RuntimeError("spectral level executor did not complete the declared level")
        row = append_convergence_campaign_level(
            journal,
            spec,
            attempt=attempt,
            level_dir=level_dir,
            artifact_root=root,
        )
        solved_this_attempt += 1
        if on_durable_level is not None:
            on_durable_level(dict(row))
        if fault_hook is not None:
            fault_hook("after_level_row", row)


__all__ = [
    "CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_NAME",
    "CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_VERSION",
    "build_convergence_campaign_progress",
    "run_convergence_campaign",
    "write_convergence_campaign_summary",
]
