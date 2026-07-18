"""Execute one accepted spectral job against an already loaded owned model."""

from __future__ import annotations

from pathlib import Path
import os
import time
from typing import Any, Callable, Mapping

from .resource_admission import ResourceStageAdapter, collect_resource_telemetry
from .spectral_rows import completed_spectral_point_fingerprints
from .spectral_runner import run_spectral_characterization
from .validation_collectors import execute_validation_collector


def execute_loaded_spectral_level(
    *,
    store: Any,
    job_id: str,
    spec: Mapping[str, Any],
    directory: str | Path,
    attempt: int,
    model: Any,
    client: Any,
    model_name: str,
    ownership: Any,
    preflight: Mapping[str, Any],
    worker_started: float,
    should_stop: Callable[[], bool],
    on_durable_row: Callable[[Mapping[str, Any]], None],
    collector_executor: Callable[[dict[str, Any], dict[str, Any], Path], Mapping[str, Any]] | None = None,
    telemetry_provider: Callable[[str, str, Any, Path, float], dict[str, Any]] | None = None,
    fault_hook: Callable[[str, Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Compose resource admission, point audit, and the accepted spectral runner."""
    root = Path(directory).resolve()
    rows_path = root / "spectral_rows.jsonl"

    def completed_ids() -> set[str]:
        return completed_spectral_point_fingerprints(rows_path, spec, artifact_root=root)

    def sample(stage: str, point_id: str) -> dict[str, Any]:
        if telemetry_provider is not None:
            return telemetry_provider(
                stage, point_id, model, root, time.monotonic() - worker_started
            )
        mesh_elements = None
        try:
            from comsol_mcp.tools.mesh import get_mesh_info

            mesh = get_mesh_info(model)
            if mesh.get("success"):
                mesh_elements = mesh.get("mesh", {}).get("num_elements")
        except Exception:
            pass
        return collect_resource_telemetry(
            stage=stage,
            runtime_path=store.root,
            process_id=os.getpid(),
            mesh_elements=mesh_elements,
            elapsed_wall_seconds=time.monotonic() - worker_started,
            durable_result_epoch=rows_path.stat().st_mtime if rows_path.is_file() else None,
        )

    resource = ResourceStageAdapter(
        store=store,
        job_id=job_id,
        attempt=attempt,
        policy=spec["resource_policy"],
        telemetry_provider=sample,
        completed_point_ids_provider=completed_ids,
    )
    latest_resource_decision: dict[str, Any] | None = None

    def resource_control(context: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal latest_resource_decision
        if should_stop():
            return {"action": "cancel"}
        point = context.get("point")
        if not isinstance(point, Mapping):
            raise ValueError("resource control point is unavailable")
        decision = resource.evaluate(stage="pre_solve", point_id=str(point["point_id"]))
        latest_resource_decision = decision
        return {"action": "continue" if decision["action"] == "start_point" else "stop"}

    def resource_after(row: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal latest_resource_decision
        decision = resource.evaluate(stage="post_solve", point_id=str(row["point_id"]))
        latest_resource_decision = decision
        return {
            "action": "continue"
            if decision["action"] in {"start_point", "skip_completed"}
            else "stop"
        }

    def execute(point: dict[str, Any], artifact_dir: Path) -> Mapping[str, Any]:
        collector = spec["collector"]
        if collector_executor is not None:
            return collector_executor(point, collector, artifact_dir)
        return execute_validation_collector(
            point,
            collector,
            artifact_dir,
            model=model,
            client=client,
            model_name=model_name,
            job_id=job_id,
            expected_source_sha256=spec["source_model_sha256"],
            session_state={"connected": True},
            ownership_preflight=preflight,
        )

    def persisted(row: Mapping[str, Any]) -> None:
        on_durable_row(row)
        ownership.heartbeat(
            model_path=spec["source_model_path"], refresh_server_processes=True
        )

    result = run_spectral_characterization(
        spec,
        root,
        attempt=attempt,
        point_executor=execute,
        control_hook=resource_control,
        after_durable_row_hook=resource_after,
        on_durable_row=persisted,
        fault_hook=fault_hook,
    )
    return {"result": result, "latest_resource_decision": latest_resource_decision}


__all__ = ["execute_loaded_spectral_level"]
