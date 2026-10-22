"""Collect one explicit native condition under the matrix worker's ownership."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.adapter.condition_backend import (
    FORWARD_SHAPE_CONTROLS_VERSION,
    ClientapiLin2025ConditionBackend,
)
from comsol_mcp.contracts.native_condition import (
    NATIVE_CONDITION_COLLECTOR,
    NATIVE_CONDITION_COLLECTORS,
    bind_native_condition_point,
)
from comsol_mcp.durable import atomic_write_json, domain_sha256_v2
from comsol_mcp.durable.io import atomic_write_json_exclusive

from .native_condition_gradients import collect_native_condition_gradients
from .native_condition_receipts import _normalize_receipt, condition_tensor_expressions
from .store import read_json

NATIVE_CONDITION_MANIFEST_SCHEMA_NAME = "comsol_mcp.native_condition_gradient_manifest"
NATIVE_FORWARD_MANIFEST_SCHEMA_NAME = "comsol_mcp.native_condition_forward_manifest"
NATIVE_CONDITION_MANIFEST_SCHEMA_VERSION = "1.0.0"


def execute_native_condition_collector(
    point: Mapping[str, Any],
    collector: Mapping[str, Any],
    artifact_dir: str | Path,
    *,
    model: Any,
    client: Any,
    expected_source_sha256: str,
    cancel_requested: Callable[[], bool],
    backend_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Measure the declared condition on an owned clone and preserve raw receipts."""
    if collector.get("name") not in NATIVE_CONDITION_COLLECTORS:
        raise ValueError("native condition collector is unsupported")
    gradients = collector["name"] == NATIVE_CONDITION_COLLECTOR
    spec = bind_native_condition_point(
        collector["inputs"], point, expected_source_sha256, gradients=gradients
    )
    root = Path(artifact_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    clone_path = root / "native-derived.mph"
    if clone_path.exists():
        raise ValueError("native condition clone destination already exists")
    clone = None
    cleanup = {"clone_created": False, "clone_removed": False}

    def check_cancel() -> None:
        if cancel_requested():
            raise InterruptedError("native condition collection was cancelled")

    try:
        check_cancel()
        model.java.save(str(clone_path), True)
        clone = client.load(str(clone_path))
        cleanup["clone_created"] = True
        factory = backend_factory or ClientapiLin2025ConditionBackend
        backend = factory(clone, spec, working_model_path=clone_path)
        check_cancel()
        controls = backend.prepare(spec["initial_values"])
        clone.java.save(str(clone_path))
        shape_application = None
        if backend.controls["schema_version"] == FORWARD_SHAPE_CONTROLS_VERSION:
            check_cancel()
            shape_application = backend.run_shape_application()
        check_cancel()
        condition = spec["condition_table"]["conditions"][0]
        raw = backend.evaluate_condition(condition, condition_tensor_expressions(spec, condition))
        atomic_write_json(root / "forward-raw.json", dict(raw))
        forward = _normalize_receipt(condition, raw, backend.controls, spec["mesh_admission"])
        atomic_write_json(root / "forward-receipt.json", dict(forward))
        if forward["disposition"] != "measured":
            # The finite raw values and the gate verdict are already durable
            # (forward-raw.json and forward-receipt.json); refuse here so a
            # gate-failed raw forward measurement is never promoted into
            # observation or acceptance evidence.
            raise ValueError("robust condition scientific measurement gate failed")
        observations = [
            {
                "condition_id": condition["condition_id"],
                "observable_id": condition["observable_id"],
                "value": forward["observable_value"],
                "evidence_sha256": forward["receipt_fingerprint"],
                "disposition": "measured",
            }
        ]
        check_cancel()
        gradient = None
        if gradients:
            collect_native_condition_gradients(
                spec,
                root,
                backend=backend,
                conditions=[condition],
                observations=observations,
                cancel_requested=cancel_requested,
            )
            check_cancel()
            gradient = read_json(root / f"condition-gradient-{condition['order']:04d}.json")
    finally:
        try:
            if clone is not None:
                client.remove(clone)
                cleanup["clone_removed"] = True
        finally:
            atomic_write_json(root / "clone-cleanup.json", cleanup)
    manifest = {
        "schema_name": (
            NATIVE_CONDITION_MANIFEST_SCHEMA_NAME
            if gradients
            else NATIVE_FORWARD_MANIFEST_SCHEMA_NAME
        ),
        "schema_version": NATIVE_CONDITION_MANIFEST_SCHEMA_VERSION,
        "source_model_sha256": expected_source_sha256,
        "point_fingerprint": point["point_fingerprint"],
        "request_fingerprint": spec["request_fingerprint"],
        "forward": forward,
        "gradient": gradient,
        "control_receipt": controls,
        "shape_application": shape_application,
        "cleanup": cleanup,
        "evidence_state": "native_unchecked",
        "scientific_disposition": "UNVALIDATED",
    }
    manifest["manifest_fingerprint"] = domain_sha256_v2(manifest["schema_name"], manifest)
    payload_path = root / "native-condition.json"
    atomic_write_json_exclusive(payload_path, manifest)
    wrapper = {
        "schema_name": "comsol_mcp.validation_matrix_collector",
        "schema_version": "1.0.0",
        "collector": collector["name"],
        "point": {
            key: point[key]
            for key in (
                "point_id",
                "point_fingerprint",
                "configuration_sha256",
                "wavelength",
                "incidence",
            )
        },
        "source_model_sha256": expected_source_sha256,
        "audit_status": "measurement_complete",
        "inner_manifest": {
            "relative_path": payload_path.name,
            "sha256": hashlib.sha256(payload_path.read_bytes()).hexdigest(),
            "size_bytes": payload_path.stat().st_size,
        },
    }
    wrapper_path = root / "matrix_collector.json"
    atomic_write_json_exclusive(wrapper_path, wrapper)
    return {
        "success": True,
        "audit_status": "measurement_complete",
        "artifacts": {"manifest": str(wrapper_path)},
    }
