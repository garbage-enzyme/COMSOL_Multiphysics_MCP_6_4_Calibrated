"""Run the alpha7.1 trusted native-adjoint structural gate on a licensed host."""

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
_adjoint_adapter = import_module("comsol_mcp.research.adjoint_adapter")
_gradient_contracts = import_module("comsol_mcp.research.gradient_contracts")
atomic_write_json = _durable.atomic_write_json
normalize_structure_adapter_manifest = _adapters.normalize_structure_adapter_manifest
normalize_structure_tree_audit = _adapters.normalize_structure_tree_audit
ClientapiAdjointStudyBackend = _adjoint_adapter.ClientapiAdjointStudyBackend
configure_native_adjoint = _adjoint_adapter.configure_native_adjoint
normalize_native_optimizer_configuration = (
    _gradient_contracts.normalize_native_optimizer_configuration
)

SCHEMA_NAME = "comsol_mcp.native_adjoint_licensed_gate"
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
    parser.add_argument("--optimizer-method", choices=("gcmma", "mma", "ipopt"), required=True)
    parser.add_argument("--max-solves", type=int, required=True)
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--max-commit-fraction", type=float, required=True)
    parser.add_argument("--max-disk-bytes", type=int, required=True)
    parser.add_argument("--max-review-items", type=int, required=True)
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
    if source.suffix.casefold() != ".mph":
        raise ValueError("source model must be an MPH file")
    if isinstance(args.cores, bool) or not isinstance(args.cores, int) or args.cores < 1:
        raise ValueError("cores must be an explicit positive integer")
    available = os.cpu_count()
    if not isinstance(available, int) or available < 1 or args.cores > available:
        raise ValueError("cores exceeds live host capacity or capacity is unavailable")
    manifest = normalize_structure_adapter_manifest(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    audit = normalize_structure_tree_audit(
        json.loads(audit_path.read_text(encoding="utf-8")), manifest
    )
    source_hash = _sha(source)
    if manifest["source_identity"]["source_sha256"] != source_hash:
        raise ValueError("source model hash differs from the trusted manifest")
    if audit["source_identity"] != manifest["source_identity"]:
        raise ValueError("tree audit source identity differs from the trusted manifest")
    spec = {
        "root": root,
        "source": source,
        "source_sha256": source_hash,
        "manifest": manifest,
        "audit": audit,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "tree_audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "cores": args.cores,
        "base_copy": root / "base.mph",
        "configured_copy": root / "configured.mph",
        "receipt": root / "licensed-receipt.json",
        "private_receipt": root / "licensed-private.json",
    }
    spec["optimizer"] = normalize_native_optimizer_configuration(
        {
            "schema_name": "comsol_mcp.native_optimizer_configuration",
            "schema_version": "1.0.0",
            "optimizer_id": f"trusted-structural-{args.optimizer_method}-probe",
            "backend": "comsol_native",
            "method": args.optimizer_method,
            "move_limit": 0.1,
            "optimality_tolerance": 1e-3,
            "constraint_tolerance": 1e-3,
            "budget": {
                "cores": args.cores,
                "max_solves": args.max_solves,
                "max_iterations": args.max_iterations,
                "max_wall_time_seconds": args.max_wall_time_seconds,
                "max_commit_fraction": args.max_commit_fraction,
                "max_disk_bytes": args.max_disk_bytes,
                "max_review_items": args.max_review_items,
            },
            "checkpoint_policy": {
                "every_accepted_iteration": True,
                "save_copy": True,
                "exact_native_resume_required": False,
            },
            "deterministic_seed": 71004,
        }
    )
    return spec


def _support(spec: dict[str, Any]) -> dict[str, Any]:
    variables = []
    for item in spec["manifest"]["mutable_dimensions"]:
        variables.append(
            {
                "variable_id": item["variable_id"],
                "order": len(variables),
                "kind": "continuous",
                "meaning": "periodic MIM patch length",
                "unit": item["unit"],
                "baseline": item["baseline"],
                "lower": item["lower"],
                "upper": item["upper"],
                "scale": item["baseline"],
                "mapping": {
                    "feature_tag": spec["manifest"]["patch_feature"]["tag_path"][0],
                    "feature_type": spec["manifest"]["patch_feature"]["feature_types"][0],
                    "property_name": spec["manifest"]["patch_feature"]["size_property"],
                    "property_index": item["property_index"],
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
        "contract_id": "periodic-mim-adjoint-v1",
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
            "objective_id": "fixed_transmission",
            "expression": "ewfd.Ttotal",
            "direction": "maximize",
            "unit": "1",
            "wavelength_um": 1.717657785,
            "study_tag": "std1",
            "solution_tag": "sol1",
            "dataset_tag": "dset1",
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
            "solution_tag": "sol1",
            "dataset_tag": "dset1",
            "derivative_expression": "dJ/dpatch_length",
            "derivative_units": "1/m",
        },
        "support_state": "structurally_supported",
    }


def _optimizer(spec: dict[str, Any]) -> dict[str, Any]:
    return dict(spec["optimizer"])


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
        "optimizer_method": spec["optimizer"]["method"],
        "optimizer_fingerprint": spec["optimizer"]["optimizer_fingerprint"],
        "budget": spec["optimizer"]["budget"],
        "solver_started": False,
        "filesystem_modified": False,
        "paths_included": False,
    }


def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["clean"]:
        raise RuntimeError("native adjoint licensed gate requires a clean source tree")
    import mph

    source_before = _sha(spec["source"])
    for path in (spec["base_copy"], spec["configured_copy"]):
        path.unlink(missing_ok=True)
    client = None
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
        "optimizer_method": spec["optimizer"]["method"],
        "optimizer_fingerprint": spec["optimizer"]["optimizer_fingerprint"],
        "budget": spec["optimizer"]["budget"],
        "paths_included": False,
    }
    private = {
        "source_model": str(spec["source"]),
        "base_copy": str(spec["base_copy"]),
        "configured_copy": str(spec["configured_copy"]),
    }
    try:
        client = mph.Client(cores=spec["cores"], version="6.4")
        source_model = client.load(str(spec["source"]))
        source_model.java.save(str(spec["base_copy"]), True)
        client.remove(source_model)
        model = client.load(str(spec["base_copy"]))
        adapter_receipt = configure_native_adjoint(
            ClientapiAdjointStudyBackend(model), _support(spec), _optimizer(spec)
        )
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        reloaded = client.load(str(spec["configured_copy"]))
        sensitivity = reloaded.java.study("std1").feature("sens_a71")
        optimization = reloaded.java.study("std2").feature("opt_a71")
        readback = {
            "sensitivity_type": str(sensitivity.getType()),
            "gradient_method": str(sensitivity.getString("gradientMethod")),
            "variable_order": [str(item) for item in list(sensitivity.getStringArray("pname"))],
            "objective_expression": [
                str(item) for item in list(sensitivity.getStringArray("optobj"))
            ],
            "optimization_type": str(optimization.getType()),
            "optimizer_method": str(optimization.getString("optmethod")),
        }
        receipt.update(
            {
                "success": True,
                "source_unchanged": _sha(spec["source"]) == source_before,
                "configured_copy_sha256": _sha(spec["configured_copy"]),
                "adapter_receipt_fingerprint": adapter_receipt["receipt_fingerprint"],
                "readback": readback,
                "native_solve_executed": False,
            }
        )
        private["adapter_receipt"] = adapter_receipt
    except Exception as exc:
        receipt["error"] = {
            "code": "native_configuration_failed",
            "type": type(exc).__name__,
        }
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = {"client_clear": False, "source_unchanged": _sha(spec["source"]) == source_before}
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                private["cleanup_error"] = f"{type(exc).__name__}: {exc}"
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
