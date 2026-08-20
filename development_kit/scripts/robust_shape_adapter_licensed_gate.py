"""Run the alpha7.2 trusted robust-shape control gate on a licensed host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

_durable = import_module("comsol_mcp.durable")
_adapters = import_module("comsol_mcp.research.adapters")
_adjoint = import_module("comsol_mcp.research.adjoint_adapter")
_robust = import_module("comsol_mcp.research.robust_shape_adapter")
_ownership = import_module("comsol_mcp.tools.ownership")
atomic_write_json = _durable.atomic_write_json
normalize_structure_adapter_manifest = _adapters.normalize_structure_adapter_manifest
normalize_structure_tree_audit = _adapters.normalize_structure_tree_audit
ClientapiAdjointStudyBackend = _adjoint.ClientapiAdjointStudyBackend
prepare_robust_shape_controls = _robust.prepare_robust_shape_controls
SolverOwnership = _ownership.SolverOwnership

SCHEMA_NAME = "comsol_mcp.robust_shape_adapter_licensed_gate"
SCHEMA_VERSION = "1.0.0"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tree-audit", type=Path, required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--max-elements-per-model", type=int, required=True)
    parser.add_argument("--minimum-element-quality", type=float, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _git_identity() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    revision = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603
        [git, "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return {"revision": revision, "clean": not status.strip()}


def _support(spec: dict[str, Any]) -> dict[str, Any]:
    variables = []
    for index, item in enumerate(spec["manifest"]["mutable_dimensions"]):
        variables.append(
            {
                "variable_id": item["variable_id"],
                "order": index,
                "kind": "continuous",
                "meaning": "periodic MIM patch length",
                "unit": item["unit"],
                "baseline": item["baseline"],
                "lower": item["lower"],
                "upper": item["upper"],
                "scale": item["baseline"],
                "mapping": {
                    "feature_tag": "patch_a71",
                    "feature_type": "PrescribedMeshDisplacement",
                    "property_name": "dx",
                    "property_index": index,
                    "readback_expression": item["variable_id"],
                },
                "dependency_class": "geometry",
                "step_policy": {
                    "relative_steps": [0.01, 0.003, 0.001],
                    "absolute_floor": 1e-15,
                    "central_difference": True,
                    "near_bound_mode": "one_sided",
                },
                "active_bound_semantics": "projected_zero",
            }
        )
    return {
        "schema_name": "comsol_mcp.derivative_support",
        "schema_version": "1.0.0",
        "contract_id": "periodic-mim-robust-shape-v1",
        "comsol_version": "6.4",
        "comsol_build": spec["manifest"]["source_identity"]["comsol_build"],
        "required_products": ["Wave Optics", "Optimization"],
        "adapter_id": spec["manifest"]["adapter_id"],
        "adapter_version": "1.0.0",
        "source_identity": spec["source_sha256"],
        "study_identity": spec["manifest"]["source_identity"]["tree_sha256"],
        "derivative_method": "adjoint",
        "variables": variables,
        "objective": {
            "objective_id": "structural_preflight_only",
            "expression": "comp1.ewfd.Torder_0_0",
            "direction": "maximize",
            "unit": "1",
            "wavelength_um": 1.717657785,
            "study_tag": "std1",
            "solution_tag": "sol2",
            "dataset_tag": "dset2",
            "evidence_paths": ["forward.transmission"],
        },
        "constraints": [],
        "mesh_policy": {
            "topology": "fixed",
            "selection": "preserve",
            "quality_expression": "mesh.minqual",
            "finalist_remesh": True,
        },
        "nondifferentiable_events": ["topology_changed", "branch_switch_unresolved"],
        "result_identity": {
            "study_tag": "std1",
            "solution_tag": "sol2",
            "dataset_tag": "dset2",
            "derivative_expression": "real(fsens(control_variable))",
            "derivative_units": "1/m",
        },
        "support_state": "structurally_supported",
    }


def _shape_policy(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": "comsol_mcp.shape_support_policy",
        "schema_version": "1.0.0",
        "policy_id": "periodic-mim-s3-structural-gate",
        "adapter_id": spec["manifest"]["adapter_id"],
        "minimum_gap": {
            "mode": "not_requested",
            "explicit_value_m": None,
            "geometry_reference_length_m": None,
            "mesh_resolution_m": None,
            "mesh_multiplier": None,
            "geometry_relative_floor": None,
        },
        "geometry_guards": {
            "minimum_thickness_m": None,
            "minimum_radius_m": None,
            "preserve_topology": True,
            "preserve_selections": True,
            "require_positive_dimensions": True,
            "reject_self_intersection": True,
        },
        "mesh_admission": {
            "max_elements_per_model": spec["max_elements_per_model"],
            "minimum_element_quality": spec["minimum_element_quality"],
            "quality_measure": "volcircum",
            "check_after_build": True,
            "check_after_remesh": True,
            "check_before_solve": True,
        },
        "model_retention": {"mode": "finalist_only", "max_retained_models": 1},
    }


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.resolve(strict=True)
    approved = Path("D:/mcp_tests").resolve(strict=False)
    if os.name == "nt" and (root.parent != approved or len(root.name) > 12):
        raise ValueError("test root must be a direct short child of D:/mcp_tests")
    if not str(root).isascii():
        raise ValueError("test root must be ASCII")
    if isinstance(args.cores, bool) or not isinstance(args.cores, int) or args.cores < 1:
        raise ValueError("cores must be an explicit positive integer")
    available = os.cpu_count()
    if not isinstance(available, int) or args.cores > available:
        raise ValueError("cores exceeds live host capacity or capacity is unavailable")
    if (
        isinstance(args.max_elements_per_model, bool)
        or not isinstance(args.max_elements_per_model, int)
        or args.max_elements_per_model < 1
    ):
        raise ValueError("max_elements_per_model must be caller supplied")
    if not 0.0 < args.minimum_element_quality <= 1.0:
        raise ValueError("minimum_element_quality must be caller supplied in (0, 1]")
    source = args.source_model.resolve(strict=True)
    manifest_path = args.manifest.resolve(strict=True)
    audit_path = args.tree_audit.resolve(strict=True)
    manifest = normalize_structure_adapter_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    audit = normalize_structure_tree_audit(
        json.loads(audit_path.read_text(encoding="utf-8")), manifest
    )
    source_hash = _sha(source)
    if (
        source.suffix.casefold() != ".mph"
        or source_hash != manifest["source_identity"]["source_sha256"]
    ):
        raise ValueError("source model differs from the trusted manifest")
    return {
        "root": root,
        "source": source,
        "source_sha256": source_hash,
        "manifest": manifest,
        "audit": audit,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "tree_audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "cores": args.cores,
        "max_elements_per_model": args.max_elements_per_model,
        "minimum_element_quality": float(args.minimum_element_quality),
        "base_copy": root / "base.mph",
        "configured_copy": root / "configured.mph",
        "rollback_copy": root / "rollback.mph",
        "receipt": root / "licensed-receipt.json",
        "private_receipt": root / "licensed-private.json",
    }


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "source_sha256": spec["source_sha256"],
        "manifest_sha256": spec["manifest_sha256"],
        "tree_audit_sha256": spec["tree_audit_sha256"],
        "requested_cores": spec["cores"],
        "max_elements_per_model": spec["max_elements_per_model"],
        "minimum_element_quality": spec["minimum_element_quality"],
        "solver_started": False,
        "filesystem_modified": False,
        "paths_included": False,
    }


class _FailAfterPrepare:
    def __init__(self, backend: Any):
        self.backend = backend

    def snapshot(self) -> Any:
        return self.backend.snapshot()

    def restore(self, snapshot: Any) -> None:
        self.backend.restore(snapshot)

    def prepare_controls(self, support: Any) -> Any:
        self.backend.prepare_controls(support)
        raise ValueError("injected post-prepare failure")


def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["clean"]:
        raise RuntimeError("robust shape adapter licensed gate requires a clean source tree")
    import mph

    source_before = _sha(spec["source"])
    support = _support(spec)
    policy = _shape_policy(spec)
    client = None
    ownership = SolverOwnership()
    lease_acquired = False
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "dry_run": False,
        "source_revision": git["revision"],
        "source_sha256": source_before,
        "manifest_sha256": spec["manifest_sha256"],
        "tree_audit_sha256": spec["tree_audit_sha256"],
        "requested_cores": spec["cores"],
        "max_elements_per_model": spec["max_elements_per_model"],
        "minimum_element_quality": spec["minimum_element_quality"],
        "paths_included": False,
    }
    private = {
        key: str(spec[key]) for key in ("source", "base_copy", "configured_copy", "rollback_copy")
    }
    try:
        lease = ownership.acquire(mode="alpha7.2_s3_licensed_gate", model_path=str(spec["source"]))
        if not lease.get("success") or not lease.get("acquired"):
            raise RuntimeError("exclusive solver ownership could not be acquired")
        lease_acquired = True
        for path in (spec["base_copy"], spec["configured_copy"], spec["rollback_copy"]):
            path.unlink(missing_ok=True)
        client = mph.Client(cores=spec["cores"], version="6.4")
        if not ownership.heartbeat(model_path=str(spec["source"]), refresh_server_processes=True):
            raise RuntimeError("solver ownership heartbeat failed after client startup")
        source_model = client.load(str(spec["source"]))
        source_model.java.save(str(spec["base_copy"]), True)
        client.remove(source_model)

        model = client.load(str(spec["base_copy"]))
        control_receipt = prepare_robust_shape_controls(
            ClientapiAdjointStudyBackend(model),
            spec["manifest"],
            spec["audit"],
            support,
            policy,
        )
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        reloaded = client.load(str(spec["configured_copy"]))
        saved_snapshot = dict(ClientapiAdjointStudyBackend(reloaded).snapshot())
        if "dg_a71" not in saved_snapshot["physics"]:
            raise ValueError("saved robust shape controls are absent after reload")
        client.remove(reloaded)

        rollback_model = client.load(str(spec["base_copy"]))
        rollback_backend = ClientapiAdjointStudyBackend(rollback_model)
        rollback_before = dict(rollback_backend.snapshot())
        try:
            prepare_robust_shape_controls(
                _FailAfterPrepare(rollback_backend),
                spec["manifest"],
                spec["audit"],
                support,
                policy,
            )
        except ValueError as exc:
            if str(exc) != "injected post-prepare failure":
                raise
        else:
            raise RuntimeError("injected robust shape control failure was not observed")
        if dict(rollback_backend.snapshot()) != rollback_before:
            raise ValueError("real robust shape rollback differs from the original snapshot")
        rollback_model.java.save(str(spec["rollback_copy"]), True)
        client.remove(rollback_model)
        rollback_reloaded = client.load(str(spec["rollback_copy"]))
        if dict(ClientapiAdjointStudyBackend(rollback_reloaded).snapshot()) != rollback_before:
            raise ValueError("saved real robust shape rollback differs after reload")
        client.remove(rollback_reloaded)
        receipt.update(
            {
                "success": True,
                "source_unchanged": _sha(spec["source"]) == source_before,
                "configured_copy_sha256": _sha(spec["configured_copy"]),
                "rollback_copy_sha256": _sha(spec["rollback_copy"]),
                "control_receipt_fingerprint": control_receipt["receipt_fingerprint"],
                "save_reload_verified": True,
                "rollback_verified": True,
                "native_solve_executed": False,
            }
        )
        private["control_receipt"] = control_receipt
    except Exception as exc:
        receipt["error"] = {"code": "robust_shape_adapter_failed", "type": type(exc).__name__}
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = {
            "client_clear": False,
            "lease_released": not lease_acquired,
            "source_unchanged": _sha(spec["source"]) == source_before,
        }
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                private["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if lease_acquired:
            released = ownership.release()
            cleanup["lease_released"] = bool(released.get("success") and released.get("released"))
            if not cleanup["lease_released"]:
                private["lease_cleanup_error"] = released
        receipt["cleanup"] = cleanup
        receipt["success"] = receipt.get("success") is True and all(cleanup.values())
    return receipt, private


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec = _spec(args)
    if args.dry_run:
        print(json.dumps(_dry_run(spec), ensure_ascii=False, sort_keys=True))
        return 0
    receipt, private = _run(spec)
    atomic_write_json(spec["receipt"], receipt)
    atomic_write_json(spec["private_receipt"], private)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
