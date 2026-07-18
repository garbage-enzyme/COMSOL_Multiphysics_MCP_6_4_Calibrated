"""Solver-independent point loop for durable validation matrices."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from .validation_rows import append_validation_row, completed_point_fingerprints


MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_HOOK_ACTIONS = frozenset(
    {"start_point", "skip_completed", "await_confirmation", "checkpoint_no_start"}
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _contained_file(value: object, root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("collector result must declare one manifest path")
    path = Path(value).resolve()
    resolved_root = root.resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("collector manifest escapes its expected artifact directory") from exc
    if not path.is_file():
        raise ValueError("collector manifest does not exist")
    return path


def summarize_collector_result(
    result: object,
    *,
    collector_name: str,
    artifact_id: str,
    artifact_directory: str | Path,
    job_directory: str | Path,
) -> dict[str, Any]:
    """Validate one collector artifact and return only its bounded identity."""
    if not isinstance(result, Mapping):
        raise ValueError("collector result must be an object")
    if result.get("success") is not True:
        raise ValueError(f"collector failed: {str(result.get('error', 'unknown error'))[:1000]}")
    audit_status = result.get("audit_status")
    if audit_status not in {"measurement_complete", "policy_evaluated"}:
        raise ValueError(f"collector audit_status is incomplete: {audit_status}")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("collector result does not contain artifact identities")
    artifact_root = Path(artifact_directory).resolve()
    manifest = _contained_file(artifacts.get("manifest"), artifact_root)
    size = manifest.stat().st_size
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise ValueError("collector manifest violates its size bound")
    job_root = Path(job_directory).resolve()
    try:
        relative = manifest.relative_to(job_root).as_posix()
    except ValueError as exc:
        raise ValueError("collector manifest escapes the durable job directory") from exc
    return {
        "collector": collector_name,
        "artifact_id": artifact_id,
        "audit_status": audit_status,
        "manifest_relative_path": relative,
        "manifest_sha256": _sha256_file(manifest),
        "manifest_size_bytes": size,
    }


def _run_point_hook(
    hook: Callable[[dict[str, Any]], Mapping[str, Any]] | None,
    *,
    stage: str,
    spec: Mapping[str, Any],
    point: Mapping[str, Any],
) -> dict[str, Any]:
    if hook is None:
        return {"action": "start_point", "start_authorized": True}
    result = hook(
        {
            "stage": stage,
            "config_id": spec["spec_fingerprint"],
            "point_id": point["point_fingerprint"],
            "declared_point_id": point["point_id"],
        }
    )
    if not isinstance(result, Mapping):
        raise ValueError(f"{stage} hook must return an object")
    action = result.get("action")
    if action not in _HOOK_ACTIONS:
        raise ValueError(f"{stage} hook returned an unsupported action")
    authorized = result.get("start_authorized")
    if not isinstance(authorized, bool) or authorized != (action == "start_point"):
        raise ValueError(f"{stage} hook returned inconsistent authorization")
    if result.get("point_id") not in (None, point["point_fingerprint"]):
        raise ValueError(f"{stage} hook returned a mismatched point_id")
    return dict(result)


def run_pending_validation_points(
    spec: Mapping[str, Any],
    job_directory: str | Path,
    *,
    attempt: int,
    collector_executor: Callable[[dict[str, Any], dict[str, Any], Path], Mapping[str, Any]],
    should_stop: Callable[[], bool] | None = None,
    on_durable_row: Callable[[dict[str, Any]], None] | None = None,
    before_point_hook: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    after_durable_row_hook: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run exact pending points and persist one complete or error row per attempt."""
    directory = Path(job_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    rows_path = directory / "matrix_rows.jsonl"
    completed = completed_point_fingerprints(rows_path, spec)
    points = spec.get("points")
    if not isinstance(points, list) or not points:
        raise ValueError("validation_matrix points are unavailable")
    if not callable(collector_executor):
        raise ValueError("collector_executor must be callable")
    stop = should_stop or (lambda: False)
    processed = 0
    skipped = 0
    errors = 0
    last_row_sha256: str | None = None

    for point_value in points:
        if not isinstance(point_value, dict):
            raise ValueError("validation_matrix point must be an object")
        point = dict(point_value)
        if point["point_fingerprint"] in completed:
            skipped += 1
            continue
        if stop():
            return {
                "success": True,
                "stop_reason": "control_request",
                "processed": processed,
                "skipped_completed": skipped,
                "errors": errors,
                "remaining": len(points) - len(completed),
                "last_row_sha256": last_row_sha256,
            }
        before = _run_point_hook(
            before_point_hook,
            stage="pre_solve",
            spec=spec,
            point=point,
        )
        if before["action"] != "start_point":
            return {
                "success": True,
                "stop_reason": f"before_point_{before['action']}",
                "processed": processed,
                "skipped_completed": skipped,
                "errors": errors,
                "remaining": len(points) - len(completed),
                "last_row_sha256": last_row_sha256,
                "resource_gate": before,
            }
        summaries: list[dict[str, Any]] = []
        point_id = str(point["point_id"])
        try:
            collectors = point["collectors"]
            artifact_ids = point["expected_artifact_ids"]
            for index, collector_value in enumerate(collectors):
                if stop():
                    raise InterruptedError("matching control request observed between collectors")
                collector = dict(collector_value)
                artifact_id = str(artifact_ids[index])
                artifact_directory = (
                    directory
                    / "artifacts"
                    / artifact_id
                    / f"attempt-{attempt}"
                )
                if artifact_directory.exists():
                    if any(artifact_directory.iterdir()):
                        raise ValueError("attempt artifact directory is not empty")
                    artifact_directory.rmdir()
                artifact_directory.mkdir(parents=True, exist_ok=False)
                result = collector_executor(point, collector, artifact_directory)
                summaries.append(
                    summarize_collector_result(
                        result,
                        collector_name=str(collector["name"]),
                        artifact_id=artifact_id,
                        artifact_directory=artifact_directory,
                        job_directory=directory,
                    )
                )
            row = append_validation_row(
                rows_path,
                spec,
                attempt=attempt,
                point_id=point_id,
                status="ok",
                collector_summaries=summaries,
            )
            completed.add(point["point_fingerprint"])
        except InterruptedError:
            return {
                "success": True,
                "stop_reason": "control_request",
                "processed": processed,
                "skipped_completed": skipped,
                "errors": errors,
                "remaining": len(points) - len(completed),
                "last_row_sha256": last_row_sha256,
            }
        except Exception as exc:
            errors += 1
            row = append_validation_row(
                rows_path,
                spec,
                attempt=attempt,
                point_id=point_id,
                status="error",
                collector_summaries=summaries,
                error={"type": type(exc).__name__, "message": str(exc)[:2000]},
            )
        processed += 1
        last_row_sha256 = row["row_sha256"]
        if on_durable_row is not None:
            on_durable_row(row)
        after = _run_point_hook(
            after_durable_row_hook,
            stage="post_solve",
            spec=spec,
            point=point,
        )
        if after["action"] in {"await_confirmation", "checkpoint_no_start"}:
            return {
                "success": row["status"] == "ok",
                "stop_reason": f"after_durable_row_{after['action']}",
                "processed": processed,
                "skipped_completed": skipped,
                "errors": errors,
                "remaining": len(points) - len(completed),
                "last_row_sha256": last_row_sha256,
                "resource_gate": after,
            }
        if row["status"] == "error" and not spec.get("continue_on_error", False):
            return {
                "success": False,
                "stop_reason": "point_error",
                "processed": processed,
                "skipped_completed": skipped,
                "errors": errors,
                "remaining": len(points) - len(completed),
                "last_row_sha256": last_row_sha256,
            }

    return {
        "success": errors == 0,
        "stop_reason": None,
        "processed": processed,
        "skipped_completed": skipped,
        "errors": errors,
        "remaining": len(points) - len(completed),
        "last_row_sha256": last_row_sha256,
    }


__all__ = [
    "MAX_MANIFEST_BYTES",
    "run_pending_validation_points",
    "summarize_collector_result",
]
