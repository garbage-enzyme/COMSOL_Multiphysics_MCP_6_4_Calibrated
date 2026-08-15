"""Bounded manifest submission and expansion for robust shape jobs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from comsol_mcp.durable import validate_finite_json
from comsol_mcp.jobs.adjoint_optimization import _digest, _manifest_path
from comsol_mcp.jobs.resource_admission import normalize_resource_policy
from comsol_mcp.research.adapters import (
    normalize_structure_adapter_manifest,
    normalize_structure_tree_audit,
)
from comsol_mcp.research.derivative_support import normalize_derivative_support
from comsol_mcp.research.gradient_contracts import normalize_native_optimizer_configuration
from comsol_mcp.research.robust_adapter_configuration import (
    SCHEMA_NAME as ROBUST_ADAPTER_CONFIGURATION_SCHEMA_NAME,
)
from comsol_mcp.research.robust_adapter_configuration import (
    normalize_robust_shape_adapter_configuration,
)
from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.robust_finalist_validation import (
    normalize_robust_finalist_validation_policy,
)
from comsol_mcp.research.robust_gradient_acceptance import normalize_robust_gradient_policy
from comsol_mcp.research.robust_material_tensor_rows import bind_robust_material_tensor_rows
from comsol_mcp.research.robust_objectives import normalize_robust_objective_configuration
from comsol_mcp.research.robust_optimizer_policy import normalize_robust_optimizer_policy
from comsol_mcp.research.robust_startup_admission import normalize_robust_startup_policy
from comsol_mcp.research.shape_support import normalize_shape_support_policy

ROBUST_SHAPE_MANIFEST_SCHEMA_NAME = "comsol_mcp.robust_shape_optimization_manifest"
ROBUST_SHAPE_MANIFEST_SCHEMA_VERSION = "1.1.0"
ROBUST_SHAPE_MANIFEST_LEGACY_SCHEMA_VERSION = "1.0.0"
ROBUST_SHAPE_SUBMISSION_SCHEMA_NAME = "comsol_mcp.robust_shape_optimization_submission"
ROBUST_SHAPE_SUBMISSION_SCHEMA_VERSION = "1.0.0"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def normalize_robust_shape_submission(value: object) -> dict[str, Any]:
    """Normalize the compact public envelope without reading the manifest."""
    if not isinstance(value, dict):
        raise ValueError("robust shape submission must be an object")
    fields = {
        "job_type",
        "submission_manifest_path",
        "submission_manifest_sha256",
        "cores",
        "version",
        "resource_policy",
    }
    if isinstance(value, dict) and "condition_execution_limit" in value:
        fields.add("condition_execution_limit")
    if isinstance(value, dict) and "comsol_temporary_directory" in value:
        fields.add("comsol_temporary_directory")
    if set(value) != fields:
        raise ValueError("robust shape submission fields are invalid")
    if value["job_type"] != "robust_shape_optimization":
        raise ValueError("robust shape submission discriminator is invalid")
    cores = value["cores"]
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= 1024:
        raise ValueError("robust shape cores must be explicitly bounded")
    version = value["version"]
    if not isinstance(version, str) or not version.strip() or len(version) > 32:
        raise ValueError("robust shape version must be bounded")
    resource_policy = normalize_resource_policy(value["resource_policy"])
    if resource_policy is None:
        raise ValueError("robust shape resource_policy is required")
    body = {
        "job_type": "robust_shape_optimization",
        "submission_manifest_path": str(_manifest_path(value["submission_manifest_path"])),
        "submission_manifest_sha256": _digest(
            value["submission_manifest_sha256"], "submission_manifest_sha256"
        ),
        "cores": cores,
        "version": version.strip(),
        "resource_policy": resource_policy,
        "schema_name": ROBUST_SHAPE_SUBMISSION_SCHEMA_NAME,
        "schema_version": ROBUST_SHAPE_SUBMISSION_SCHEMA_VERSION,
    }
    if "condition_execution_limit" in value:
        limit = value["condition_execution_limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 4096:
            raise ValueError("condition_execution_limit must be a bounded positive integer")
        body["condition_execution_limit"] = limit
    if "comsol_temporary_directory" in value:
        temporary_text = value["comsol_temporary_directory"]
        if (
            not isinstance(temporary_text, str)
            or not temporary_text.isascii()
            or any(character.isspace() for character in temporary_text)
        ):
            raise ValueError("COMSOL temporary directory must be an ASCII path without whitespace")
        temporary_directory = Path(temporary_text).expanduser()
        if (
            not temporary_directory.is_absolute()
            or temporary_directory.is_symlink()
            or not temporary_directory.is_dir()
        ):
            raise ValueError("COMSOL temporary directory must be an existing absolute directory")
        body["comsol_temporary_directory"] = str(temporary_directory.resolve())
    return body


def expand_robust_shape_manifest(submission: object) -> dict[str, Any]:
    """Hash-pin and normalize a complete robust shape manifest before startup."""
    envelope = normalize_robust_shape_submission(submission)
    path = Path(envelope["submission_manifest_path"])
    payload = path.read_bytes()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("robust shape manifest exceeds its byte limit")
    if hashlib.sha256(payload).hexdigest() != envelope["submission_manifest_sha256"]:
        raise ValueError("robust shape manifest SHA-256 changed")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("robust shape manifest is not strict UTF-8 JSON") from exc
    common_fields = {
        "schema_name",
        "schema_version",
        "source_model_path",
        "source_model_sha256",
        "support",
        "condition_table",
        "objective",
        "shape_policy",
        "finalist_validation_policy",
        "gradient_policy",
        "optimizer_policy",
        "native_optimizer",
        "startup_admission",
        "initial_values",
        "synthetic_mode",
    }
    if not isinstance(raw, dict):
        raise ValueError("robust shape manifest fields are invalid")
    schema_version = raw.get("schema_version")
    if schema_version == ROBUST_SHAPE_MANIFEST_LEGACY_SCHEMA_VERSION:
        fields = common_fields | {"structure_adapter_manifest", "structure_tree_audit"}
    elif schema_version == ROBUST_SHAPE_MANIFEST_SCHEMA_VERSION:
        fields = common_fields | {"adapter_configuration"}
    else:
        raise ValueError("robust shape manifest schema is unsupported")
    if set(raw) != fields or raw["schema_name"] != ROBUST_SHAPE_MANIFEST_SCHEMA_NAME:
        raise ValueError("robust shape manifest fields are invalid")
    source_text = raw["source_model_path"]
    if not isinstance(source_text, str) or not source_text.isascii():
        raise ValueError("robust shape source path must be ASCII")
    source = Path(source_text).expanduser()
    if (
        not source.is_absolute()
        or source.suffix.casefold() != ".mph"
        or source.is_symlink()
        or not source.is_file()
    ):
        raise ValueError("robust shape source must be a regular absolute MPH file")
    source = source.resolve()
    source_hash = _digest(raw["source_model_sha256"], "source_model_sha256")
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise ValueError("robust shape source SHA-256 changed")

    support = normalize_derivative_support(raw["support"])
    conditions = normalize_optimization_condition_table(raw["condition_table"])
    objective = normalize_robust_objective_configuration(raw["objective"])
    shape_policy = normalize_shape_support_policy(raw["shape_policy"])
    finalist_validation_policy = normalize_robust_finalist_validation_policy(
        raw["finalist_validation_policy"]
    )
    gradient_policy = normalize_robust_gradient_policy(raw["gradient_policy"])
    optimizer_policy = normalize_robust_optimizer_policy(raw["optimizer_policy"])
    native_optimizer = normalize_native_optimizer_configuration(raw["native_optimizer"])
    startup_admission = normalize_robust_startup_policy(raw["startup_admission"])
    if schema_version == ROBUST_SHAPE_MANIFEST_LEGACY_SCHEMA_VERSION:
        structure_manifest = normalize_structure_adapter_manifest(raw["structure_adapter_manifest"])
        structure_tree_audit = normalize_structure_tree_audit(
            raw["structure_tree_audit"], structure_manifest
        )
        adapter_input = {
            "schema_name": ROBUST_ADAPTER_CONFIGURATION_SCHEMA_NAME,
            "schema_version": "1.0.0",
            "adapter_id": support["adapter_id"],
            "configuration": {
                "structure_adapter_manifest": structure_manifest,
                "structure_tree_audit": structure_tree_audit,
            },
        }
    else:
        adapter_input = raw["adapter_configuration"]
        structure_manifest = None
        structure_tree_audit = None
    adapter_configuration = normalize_robust_shape_adapter_configuration(
        adapter_input, support, shape_policy
    )
    adapter_binding = adapter_configuration["binding"]
    material_tensor_binding = None
    if adapter_binding["adapter_id"] == "lin2025_pedot_cylinder_v1":
        material_tensor_binding = bind_robust_material_tensor_rows(
            adapter_configuration["configuration"]["material_tensor_rows"],
            conditions,
            expected_temperature_k=adapter_binding["temperature_k"],
        )
        condition_controls = adapter_configuration["configuration"]["condition_controls"]
        if condition_controls["observable_expression"] != support["objective"]["expression"]:
            raise ValueError(
                "Lin2025 condition observable expression differs from the native objective"
            )
        if (
            condition_controls["out_of_core_value"].casefold() == "on"
            and "comsol_temporary_directory" not in envelope
        ):
            raise ValueError("explicit out-of-core solve requires a COMSOL temporary directory")
    if support["source_identity"] != source_hash:
        raise ValueError("robust shape support source identity differs from manifest source")
    if support["adapter_id"] != shape_policy["adapter_id"]:
        raise ValueError("robust shape adapter identity differs across support policies")
    if (
        finalist_validation_policy["condition_table_fingerprint"]
        != conditions["condition_table_fingerprint"]
    ):
        raise ValueError("finalist validation condition table identity differs from manifest")
    if finalist_validation_policy["shape_policy_fingerprint"] != shape_policy["policy_fingerprint"]:
        raise ValueError("finalist validation shape policy identity differs from manifest")
    table_states = {item["state_id"] for item in conditions["material_states"]}
    if not set(objective["state_ids"]).issubset(table_states):
        raise ValueError("robust objective states are not declared by the condition table")
    if native_optimizer["method"] != optimizer_policy["selected_method"]:
        raise ValueError("native optimizer method differs from manual robust selection")
    if native_optimizer["method"] not in {"gcmma", "mma"}:
        raise ValueError("robust shape native optimizer must be GCMMA or MMA")
    if native_optimizer["budget"]["cores"] != envelope["cores"]:
        raise ValueError("robust shape optimizer budget cores differ from submission cores")
    if support["comsol_version"] != envelope["version"]:
        raise ValueError("robust shape COMSOL version differs from submission version")
    mesh_cap = envelope["resource_policy"]["rules"].get("max_mesh_elements")
    if mesh_cap != shape_policy["mesh_admission"]["max_elements_per_model"]:
        raise ValueError("robust shape mesh cap differs from resource admission policy")
    finalist_mesh = finalist_validation_policy["mesh_convergence"]
    shape_mesh = shape_policy["mesh_admission"]
    if finalist_mesh["max_elements_per_model"] != shape_mesh["max_elements_per_model"]:
        raise ValueError("finalist validation mesh cap differs from shape policy")
    if (
        finalist_mesh["minimum_element_quality"] != shape_mesh["minimum_element_quality"]
        or finalist_mesh["quality_measure"] != shape_mesh["quality_measure"]
    ):
        raise ValueError("finalist validation mesh quality identity differs from shape policy")
    values = raw["initial_values"]
    if not isinstance(values, list) or len(values) != len(support["variables"]):
        raise ValueError("initial_values must match the robust support variable count")
    normalized_values = [float(item) for item in values]
    for item, variable in zip(normalized_values, support["variables"], strict=True):
        if not variable["lower"] <= item <= variable["upper"]:
            raise ValueError("initial_values must remain within robust support bounds")
    if not isinstance(raw["synthetic_mode"], bool):
        raise ValueError("synthetic_mode must be boolean")
    if not raw["synthetic_mode"] and not optimizer_policy["execution_allowed"]:
        raise ValueError("selected robust optimizer lacks accepted execution evidence")
    body = {
        **envelope,
        "schema_name": ROBUST_SHAPE_MANIFEST_SCHEMA_NAME,
        "schema_version": schema_version,
        "source_model_path": str(source),
        "source_model_sha256": source_hash,
        "adapter_configuration": adapter_configuration,
        "adapter_binding": adapter_binding,
        "material_tensor_binding": material_tensor_binding,
        "support": support,
        "condition_table": conditions,
        "objective": objective,
        "shape_policy": shape_policy,
        "finalist_validation_policy": finalist_validation_policy,
        "gradient_policy": gradient_policy,
        "optimizer_policy": optimizer_policy,
        "native_optimizer": native_optimizer,
        "startup_admission": startup_admission,
        "initial_values": normalized_values,
        "synthetic_mode": raw["synthetic_mode"],
    }
    if schema_version == ROBUST_SHAPE_MANIFEST_LEGACY_SCHEMA_VERSION:
        body["structure_adapter_manifest"] = structure_manifest
        body["structure_tree_audit"] = structure_tree_audit
    validate_finite_json(body)
    body["spec_fingerprint"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


__all__ = [
    "ROBUST_SHAPE_MANIFEST_SCHEMA_NAME",
    "ROBUST_SHAPE_MANIFEST_SCHEMA_VERSION",
    "ROBUST_SHAPE_SUBMISSION_SCHEMA_NAME",
    "ROBUST_SHAPE_SUBMISSION_SCHEMA_VERSION",
    "expand_robust_shape_manifest",
    "normalize_robust_shape_submission",
]
