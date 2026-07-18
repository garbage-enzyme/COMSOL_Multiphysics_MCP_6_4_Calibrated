"""Solver-independent orchestration for durable branch-continuation campaigns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp.evidence.branch_continuation import (
    build_continuation_states,
    plan_branch_continuation,
)

from .branch_continuation_campaign_rows import (
    append_branch_continuation_campaign_state,
    read_branch_continuation_campaign_states,
)
from .store import atomic_write_json, read_json


BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_NAME = (
    "comsol_mcp.branch_continuation_campaign_summary"
)
BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_VERSION = "1.0.0"


def branch_continuation_state_directory(root: str | Path, ordinal: int) -> Path:
    """Keep nested point-audit paths below the Windows legacy path budget."""
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0 or ordinal > 99:
        raise ValueError("branch-continuation state ordinal is out of bounds")
    return Path(root).resolve() / f"s{ordinal}"


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


def _state_input(
    root: Path,
    spec: Mapping[str, Any],
    row: Mapping[str, Any],
) -> dict[str, Any]:
    state = spec["states"][row["ordinal"]]
    artifacts = row["artifacts"]
    return {
        "state_id": state["state_id"],
        "ordinal": state["ordinal"],
        "declared_predecessor_state_id": state["declared_predecessor_state_id"],
        "coordinate_name": state["coordinate"]["name"],
        "coordinate_value": state["coordinate"]["value"],
        "coordinate_unit": state["coordinate"]["unit"],
        "coordinate_identity_sha256": row["coordinate_identity_sha256"],
        "polarization": row["polarization"],
        "source_model_sha256": row["source_model_sha256"],
        "configuration_sha256": row["configuration_sha256"],
        "material_identity_sha256": row["material_identity_sha256"],
        "search_window_m": row["search_window_m"],
        "spectral_bundle": read_json(root / artifacts["spectral_bundle"]["relative_path"]),
        "analysis_decision": read_json(root / artifacts["spectral_decision"]["relative_path"]),
        "candidate_measurements": read_json(
            root / artifacts["spectral_characterization"]["relative_path"]
        ),
        "optional_field_metrics": {},
    }


def _planner_policy(
    spec: Mapping[str, Any],
    *,
    declared_cap_reached: bool,
    observed_expansion_count: int,
) -> dict[str, Any]:
    return {
        **spec["continuation_policy"],
        "max_expansions": max(
            0,
            spec["continuation_policy"]["max_expansions"]
            - observed_expansion_count,
        ),
        "point_budget": spec["maximum_total_points"],
        "continuity_evidence": [],
        "declared_cap_reached": declared_cap_reached,
    }


def build_branch_continuation_campaign_progress(
    spec: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    *,
    artifact_root: str | Path,
) -> dict[str, Any]:
    """Recompute the next action only from verified completed state spectra."""
    root = Path(artifact_root).resolve()
    completed = len(rows)
    total = len(spec["states"])
    if completed == 0:
        return {
            "action": "schedule_next_state",
            "next_state": spec["states"][0],
            "completed_state_count": 0,
            "declared_state_count": total,
            "continuation_states": None,
            "continuation_plan": None,
        }
    if completed == 1:
        last = rows[-1]
        if last["scientific_disposition"] != "accepted":
            return {
                "action": "complete",
                "scientific_disposition": last["scientific_disposition"],
                "reason_code": "initial_state_spectrum_" + last["reason_code"],
                "declared_cap_reached": (
                    last["scientific_disposition"] == "unresolved_at_declared_cap"
                ),
                "completed_state_count": completed,
                "declared_state_count": total,
                "continuation_states": None,
                "continuation_plan": None,
            }
        return {
            "action": "schedule_next_state",
            "next_state": spec["states"][1],
            "completed_state_count": completed,
            "declared_state_count": total,
            "continuation_states": None,
            "continuation_plan": None,
        }

    states = build_continuation_states(
        states_id=spec["campaign_id"],
        states=[_state_input(root, spec, row) for row in rows],
    )
    last = rows[-1]
    observed_expansion_count = sum(row["expansion_count"] for row in rows)
    declared_cap_reached = completed == total or last["scientific_disposition"] in {
        "unresolved_at_declared_cap", "invalid_evidence"
    }
    plan = plan_branch_continuation(
        states,
        _planner_policy(
            spec,
            declared_cap_reached=declared_cap_reached,
            observed_expansion_count=observed_expansion_count,
        ),
    )
    stop_on_unresolved = (
        spec["continuation_policy"]["stop_policy"] == "stop_at_first_unresolved"
        and plan["scientific_disposition"] != "accepted"
    )
    terminal_child = last["scientific_disposition"] in {
        "unresolved_at_declared_cap", "invalid_evidence"
    }
    if completed == total or terminal_child or stop_on_unresolved:
        return {
            "action": "complete",
            "scientific_disposition": plan["scientific_disposition"],
            "reason_code": plan["reason_code"],
            "declared_cap_reached": declared_cap_reached,
            "completed_state_count": completed,
            "declared_state_count": total,
            "declared_expansion_count": spec["continuation_policy"]["max_expansions"],
            "observed_expansion_count": observed_expansion_count,
            "remaining_expansion_count": max(
                0,
                spec["continuation_policy"]["max_expansions"]
                - observed_expansion_count,
            ),
            "continuation_states": states,
            "continuation_plan": plan,
        }
    return {
        "action": "schedule_next_state",
        "next_state": spec["states"][completed],
        "completed_state_count": completed,
        "declared_state_count": total,
        "declared_expansion_count": spec["continuation_policy"]["max_expansions"],
        "observed_expansion_count": observed_expansion_count,
        "remaining_expansion_count": max(
            0,
            spec["continuation_policy"]["max_expansions"]
            - observed_expansion_count,
        ),
        "continuation_states": states,
        "continuation_plan": plan,
    }


def write_branch_continuation_campaign_summary(
    spec: Mapping[str, Any],
    rows: list[Mapping[str, Any]],
    progress: Mapping[str, Any],
    *,
    artifact_root: str | Path,
) -> dict[str, Any]:
    if progress.get("action") != "complete":
        raise ValueError("completed branch-continuation progress is required")
    root = Path(artifact_root).resolve()
    analysis = root / "analysis"
    artifacts: dict[str, dict[str, Any]] = {}
    for name, value in (
        ("continuation_states", progress.get("continuation_states")),
        ("branch_continuation_plan", progress.get("continuation_plan")),
    ):
        if value is not None:
            path = analysis / f"{name}.json"
            atomic_write_json(path, dict(value))
            if read_json(path) != value:
                raise RuntimeError(f"{name} did not replay after atomic write")
            artifacts[name] = _descriptor(path, root)
    body = {
        "schema_name": BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_NAME,
        "schema_version": BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_VERSION,
        "spec_fingerprint": spec["spec_fingerprint"],
        "campaign_id": spec["campaign_id"],
        "execution_state": "completed",
        "scientific_disposition": progress["scientific_disposition"],
        "reason_code": progress["reason_code"],
        "declared_cap_reached": progress["declared_cap_reached"],
        "declared_state_count": progress["declared_state_count"],
        "completed_state_count": progress["completed_state_count"],
        "declared_expansion_count": progress.get("declared_expansion_count", 0),
        "observed_expansion_count": progress.get("observed_expansion_count", 0),
        "remaining_expansion_count": progress.get("remaining_expansion_count", 0),
        "completed_state_ids": [row["state_id"] for row in rows],
        "last_state_row_sha256": rows[-1]["row_sha256"],
        "branch_disappearance_claimed": False,
        "undeclared_coordinate_started": False,
        "artifacts": artifacts,
    }
    summary = {**body, "summary_sha256": _fingerprint(body)}
    path = analysis / "summary.json"
    atomic_write_json(path, summary)
    if read_json(path) != summary:
        raise RuntimeError("branch-continuation campaign summary did not replay")
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


def run_branch_continuation_campaign(
    spec: Mapping[str, Any],
    artifact_root: str | Path,
    *,
    attempt: int,
    state_executor: Callable[[Mapping[str, Any], Path], Mapping[str, Any]],
    control_hook: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    on_durable_state: Callable[[Mapping[str, Any]], None] | None = None,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Execute each declared state spectrum once and resume from verified rows."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("attempt must be a positive integer")
    root = Path(artifact_root).resolve()
    journal = root / "continuation_states.jsonl"
    solved_this_attempt = 0
    skipped_complete = len(
        read_branch_continuation_campaign_states(journal, spec, artifact_root=root)
    )
    while True:
        rows = read_branch_continuation_campaign_states(journal, spec, artifact_root=root)
        progress = build_branch_continuation_campaign_progress(
            spec, rows, artifact_root=root
        )
        if progress["action"] == "complete":
            if fault_hook is not None:
                fault_hook("during_summary_write", {"completed_states": len(rows)})
            receipt = write_branch_continuation_campaign_summary(
                spec, rows, progress, artifact_root=root
            )
            return {
                "completed": True,
                "stop_reason": "branch_continuation_campaign_complete",
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
                **receipt,
            }
        state = progress["next_state"]
        action = _control_action(
            control_hook,
            {
                "phase": "before_state",
                "attempt": attempt,
                "state_id": state["state_id"],
                "ordinal": state["ordinal"],
                "completed_states": len(rows),
            },
        )
        if action != "continue":
            return {
                "completed": False,
                "stop_reason": f"before_state_{action}",
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
            }
        if fault_hook is not None:
            fault_hook("before_state", {"state_id": state["state_id"]})
        state_dir = branch_continuation_state_directory(root, state["ordinal"])
        result = state_executor(state, state_dir)
        if not isinstance(result, Mapping):
            raise RuntimeError("spectral state executor returned an invalid result")
        if result.get("completed") is not True:
            return {
                "completed": False,
                "stop_reason": str(result.get("stop_reason") or "spectral_state_incomplete"),
                "solved_this_attempt": solved_this_attempt,
                "skipped_complete": skipped_complete,
                "progress": progress,
                "state_result": dict(result),
            }
        row = append_branch_continuation_campaign_state(
            journal,
            spec,
            attempt=attempt,
            state_dir=state_dir,
            artifact_root=root,
        )
        solved_this_attempt += 1
        if on_durable_state is not None:
            on_durable_state(dict(row))
        if fault_hook is not None:
            fault_hook("after_state_row", row)


__all__ = [
    "BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_NAME",
    "BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_VERSION",
    "branch_continuation_state_directory",
    "build_branch_continuation_campaign_progress",
    "run_branch_continuation_campaign",
    "write_branch_continuation_campaign_summary",
]
