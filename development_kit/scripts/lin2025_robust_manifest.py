"""Compile and self-validate one licensed Lin2025 robust-shape submission."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from comsol_mcp.jobs.robust_shape_optimization import expand_robust_shape_manifest
from comsol_mcp.jobs.store import atomic_write_json

CAMPAIGN_SCHEMA_NAME = "comsol_mcp.lin2025_robust_campaign_inputs"
CAMPAIGN_SCHEMA_VERSION = "1.0.0"
_MAX_JSON_BYTES = 2 * 1024 * 1024
_CAMPAIGN_FIELDS = {
    "schema_name",
    "schema_version",
    "condition_controls",
    "objective",
    "shape_policy",
    "finalist_validation_policy",
    "gradient_policy",
    "optimizer_policy",
    "native_optimizer",
    "startup_admission",
    "initial_values",
    "cores",
    "version",
    "resource_policy",
    "condition_execution_limit",
    "comsol_temporary_directory",
}


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular JSON file")
    payload = path.read_bytes()
    if len(payload) > _MAX_JSON_BYTES:
        raise ValueError(f"{label} exceeds its byte limit")
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compile_lin2025_robust_submission(
    *,
    source_model: Path,
    fixture_path: Path,
    tree_path: Path,
    support_path: Path,
    pedot_fixture_path: Path,
    campaign_path: Path,
    manifest_path: Path,
    envelope_path: Path,
) -> dict[str, Any]:
    """Write canonical manifest/envelope files only after strict expansion passes."""
    for output, label in ((manifest_path, "manifest"), (envelope_path, "envelope")):
        if not output.is_absolute() or not str(output).isascii() or output.suffix != ".json":
            raise ValueError(f"{label} output must be an absolute ASCII JSON path")
    if manifest_path == envelope_path:
        raise ValueError("manifest and envelope outputs must differ")
    if not source_model.is_file() or source_model.is_symlink():
        raise ValueError("source model must be a regular immutable input")
    source_sha256 = _sha256(source_model)
    fixture = _read_json(fixture_path, "Lin2025 fixture")
    tree = _read_json(tree_path, "Lin2025 tree readback")
    support = _read_json(support_path, "derivative support")
    pedot = _read_json(pedot_fixture_path, "PEDOT fixture")
    campaign = _read_json(campaign_path, "campaign inputs")
    if set(campaign) != _CAMPAIGN_FIELDS:
        raise ValueError("campaign input fields are invalid")
    if (
        campaign["schema_name"] != CAMPAIGN_SCHEMA_NAME
        or campaign["schema_version"] != CAMPAIGN_SCHEMA_VERSION
    ):
        raise ValueError("campaign input schema is unsupported")
    if pedot.get("schema_name") != "comsol_mcp.robust_pedot_fixture_manifest":
        raise ValueError("PEDOT fixture schema is unsupported")
    if "material_tensor_rows" not in pedot:
        raise ValueError("PEDOT fixture lacks material tensor rows")
    body = {
        "schema_name": "comsol_mcp.robust_shape_optimization_manifest",
        "schema_version": "1.1.0",
        "source_model_path": str(source_model.resolve()),
        "source_model_sha256": source_sha256,
        "support": support,
        "condition_table": pedot["condition_table"],
        "objective": campaign["objective"],
        "shape_policy": campaign["shape_policy"],
        "finalist_validation_policy": campaign["finalist_validation_policy"],
        "gradient_policy": campaign["gradient_policy"],
        "optimizer_policy": campaign["optimizer_policy"],
        "native_optimizer": campaign["native_optimizer"],
        "startup_admission": campaign["startup_admission"],
        "initial_values": campaign["initial_values"],
        "synthetic_mode": False,
        "adapter_configuration": {
            "schema_name": "comsol_mcp.robust_shape_adapter_configuration",
            "schema_version": "1.0.0",
            "adapter_id": "lin2025_pedot_cylinder_v1",
            "configuration": {
                "fixture": fixture,
                "tree_readback": tree,
                "material_tensor_rows": pedot["material_tensor_rows"],
                "condition_controls": campaign["condition_controls"],
            },
        },
    }
    envelope = {
        "job_type": "robust_shape_optimization",
        "submission_manifest_path": str(manifest_path),
        "submission_manifest_sha256": "0" * 64,
        "cores": campaign["cores"],
        "version": campaign["version"],
        "resource_policy": campaign["resource_policy"],
        "comsol_temporary_directory": campaign["comsol_temporary_directory"],
    }
    if campaign["condition_execution_limit"] is not None:
        envelope["condition_execution_limit"] = campaign["condition_execution_limit"]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    envelope_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(manifest_path, body)
    envelope["submission_manifest_sha256"] = _sha256(manifest_path)
    try:
        spec = expand_robust_shape_manifest(envelope)
    except Exception:
        manifest_path.unlink(missing_ok=True)
        envelope_path.unlink(missing_ok=True)
        raise
    atomic_write_json(envelope_path, envelope)
    return {
        "manifest_sha256": envelope["submission_manifest_sha256"],
        "spec_fingerprint": spec["spec_fingerprint"],
        "condition_count": len(spec["condition_table"]["conditions"]),
        "adapter_id": spec["adapter_binding"]["adapter_id"],
        "source_model_sha256": source_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-model", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--tree", required=True, type=Path)
    parser.add_argument("--support", required=True, type=Path)
    parser.add_argument("--pedot-fixture", required=True, type=Path)
    parser.add_argument("--campaign", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--envelope", required=True, type=Path)
    args = parser.parse_args()
    receipt = compile_lin2025_robust_submission(
        source_model=args.source_model,
        fixture_path=args.fixture,
        tree_path=args.tree,
        support_path=args.support,
        pedot_fixture_path=args.pedot_fixture,
        campaign_path=args.campaign,
        manifest_path=args.manifest,
        envelope_path=args.envelope,
    )
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
