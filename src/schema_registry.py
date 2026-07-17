"""Version support registry for named public and durable artifact schemas."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from src import __version__
from src.evidence.branch_continuation import (
    BRANCH_CONTINUATION_SCHEMA_VERSION,
    BRANCH_CONTINUATION_STATES_SCHEMA,
)
from src.evidence.convergence_evaluation import (
    CONVERGENCE_EVALUATION_SCHEMA,
    CONVERGENCE_LADDER_SCHEMA,
    CONVERGENCE_SCHEMA_VERSION,
)
from src.evidence.reference_power_acceptance import (
    REFERENCE_POWER_CONTRACT_SCHEMA,
    REFERENCE_POWER_EXECUTION_SCHEMA,
)
from src.evidence.spectral_characterization import (
    SPECTRAL_BUNDLE_SCHEMA,
    SPECTRAL_CHARACTERIZATION_SCHEMA,
    SPECTRAL_DECISION_SCHEMA,
    SPECTRAL_SCHEMA_VERSION,
)


_REGISTRY_SCHEMA = "comsol_mcp.schema_registry"
_REGISTRY_VERSION = "1.0.0"
_REFERENCE_POWER_DRY_RUN_SCHEMA = REFERENCE_POWER_EXECUTION_SCHEMA.replace(
    "execution_spec", "dry_run_receipt"
)


def _entry(
    schema_name: str,
    version: str,
    producer: str,
    *,
    artifact_kind: str = "public_artifact",
    writable: bool = True,
    readable_versions: tuple[str, ...] | None = None,
    migration_sources: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "schema_name": schema_name,
        "artifact_kind": artifact_kind,
        "producer": producer,
        "producer_version": __version__,
        "readable_versions": list(readable_versions or (version,)),
        "writable_version": version if writable else None,
        "migration": {
            "available": bool(migration_sources),
            "source_schema_names": list(migration_sources),
            "rewrites_source_in_place": False,
        },
    }


def _entries() -> list[dict[str, Any]]:
    legacy_point_audit = "comsol_mcp.wave_optics_point_audit"
    entries = [
        _entry("comsol_mcp.artifact_chain", "1.0.0", "src.artifact_chain"),
        _entry("comsol_mcp.artifact_chain_verification", "1.0.0", "src.artifact_chain"),
        _entry("comsol_mcp.build_identity", "1.0.0", "src.build_identity"),
        _entry(
            BRANCH_CONTINUATION_STATES_SCHEMA,
            BRANCH_CONTINUATION_SCHEMA_VERSION,
            "src.evidence.branch_continuation",
        ),
        _entry(
            CONVERGENCE_LADDER_SCHEMA,
            CONVERGENCE_SCHEMA_VERSION,
            "src.evidence.convergence_evaluation",
        ),
        _entry(
            CONVERGENCE_EVALUATION_SCHEMA,
            CONVERGENCE_SCHEMA_VERSION,
            "src.evidence.convergence_evaluation",
        ),
        _entry(
            "comsol_mcp.deployment_identity",
            "1.1.0",
            "src.tools.capabilities",
            readable_versions=("1.0.0", "1.1.0"),
        ),
        _entry("comsol_mcp.environment_identity", "1.0.0", "src.environment_identity"),
        _entry(
            "comsol_mcp.execution_evidence_outcome",
            "1.0.0",
            "src.evidence.outcome_contract",
        ),
        _entry("comsol_mcp.field_dataset_discovery", "1.0.0", "src.evidence.field_discovery"),
        _entry("comsol_mcp.field_evidence_manifest", "1.0.0", "src.evidence.field_manifest"),
        _entry("comsol_mcp.field_evidence_request", "1.1.0", "src.evidence.field_bundle"),
        _entry(_REFERENCE_POWER_DRY_RUN_SCHEMA, "1.0.0", "src.evidence.reference_power_acceptance"),
        _entry(REFERENCE_POWER_EXECUTION_SCHEMA, "1.0.0", "src.evidence.reference_power_acceptance"),
        _entry(REFERENCE_POWER_CONTRACT_SCHEMA, "1.0.0", "src.evidence.reference_power_acceptance"),
        _entry("comsol_mcp.periodic_mesh_audit", "1.0.0", "src.tools.periodic_mesh_audit"),
        _entry("comsol_mcp.periodic_mesh_smoke", "1.0.0", "src.tools.periodic_mesh_audit"),
        _entry(
            "comsol_mcp.physical_evidence",
            "1.1.0",
            "src.evidence.contracts",
            readable_versions=("1.0.0", "1.1.0"),
            migration_sources=(legacy_point_audit,),
        ),
        _entry(
            "comsol_mcp.portfolio_evidence_request",
            "1.0.0",
            "src.evidence.portfolio_verifier",
        ),
        _entry(
            "comsol_mcp.portfolio_evidence_verification",
            "1.0.0",
            "src.evidence.portfolio_verifier",
        ),
        _entry(
            SPECTRAL_DECISION_SCHEMA,
            SPECTRAL_SCHEMA_VERSION,
            "src.evidence.spectral_characterization",
        ),
        _entry(
            SPECTRAL_BUNDLE_SCHEMA,
            SPECTRAL_SCHEMA_VERSION,
            "src.evidence.spectral_characterization",
        ),
        _entry(
            SPECTRAL_CHARACTERIZATION_SCHEMA,
            SPECTRAL_SCHEMA_VERSION,
            "src.evidence.spectral_characterization",
        ),
        _entry("comsol_mcp.resource_calibration_report", "1.0.0", "src.jobs.resource_admission"),
        _entry("comsol_mcp.resource_journal_entry", "1.0.0", "src.jobs.resource_admission"),
        _entry("comsol_mcp.resource_journal_replay", "1.0.0", "src.jobs.resource_admission"),
        _entry("comsol_mcp.resource_policy", "1.0.0", "src.jobs.resource_admission"),
        _entry("comsol_mcp.resource_telemetry_sample", "1.0.0", "src.jobs.resource_admission"),
        _entry("comsol_mcp.runtime_compatibility", "1.0.0", "src.compatibility"),
        _entry(_REGISTRY_SCHEMA, _REGISTRY_VERSION, "src.schema_registry"),
        _entry("comsol_mcp.validation_matrix_collector", "1.0.0", "src.jobs.validation_collectors"),
        _entry("comsol_mcp.validation_matrix_field_collector", "1.0.0", "src.jobs.validation_collectors"),
        _entry("comsol_mcp.validation_matrix_field_review", "1.0.0", "src.jobs.field_review"),
        _entry("comsol_mcp.validation_policy", "1.0.0", "src.evidence.contracts"),
        _entry("comsol_mcp.visual_dual_review", "1.0.0", "src.evidence.visual_review"),
        _entry("comsol_mcp.visual_review_receipt", "1.0.0", "src.evidence.visual_review"),
        _entry("comsol_mcp.visual_review_request", "1.0.0", "src.evidence.visual_review"),
        _entry("comsol_mcp.visual_reviewer_capability", "1.0.0", "src.evidence.visual_review"),
        _entry(
            legacy_point_audit,
            "1",
            "src.tools.wave_optics_audit",
            artifact_kind="legacy_artifact",
            writable=False,
        ),
    ]
    return sorted(entries, key=lambda item: item["schema_name"])


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_schema_registry() -> dict[str, Any]:
    """Return the complete deterministic schema support registry."""
    entries = _entries()
    body = {
        "schema_name": _REGISTRY_SCHEMA,
        "schema_version": _REGISTRY_VERSION,
        "producer": {"package": "comsol-mcp", "version": __version__},
        "entries": entries,
        "entry_count": len(entries),
    }
    return deepcopy({**body, "registry_sha256": _canonical_sha256(body)})


def check_schema_support(
    schema_name: object,
    schema_version: object,
    *,
    for_write: bool = False,
) -> dict[str, Any]:
    """Return a machine-readable support result without modifying an artifact."""
    if not isinstance(schema_name, str) or not schema_name:
        return {"supported": False, "reason_code": "invalid_schema_name"}
    if not isinstance(schema_version, str) or not schema_version:
        return {"supported": False, "reason_code": "invalid_schema_version"}
    by_name = {item["schema_name"]: item for item in _entries()}
    entry = by_name.get(schema_name)
    if entry is None:
        return {
            "supported": False,
            "reason_code": "unknown_schema_name",
            "schema_name": schema_name,
            "schema_version": schema_version,
        }
    supported_versions = (
        [entry["writable_version"]] if for_write and entry["writable_version"] else []
    ) if for_write else entry["readable_versions"]
    if schema_version not in supported_versions:
        return {
            "supported": False,
            "reason_code": "unsupported_schema_version",
            "schema_name": schema_name,
            "schema_version": schema_version,
            "supported_versions": supported_versions,
            "migration_available": entry["migration"]["available"],
        }
    return {
        "supported": True,
        "reason_code": "supported",
        "schema_name": schema_name,
        "schema_version": schema_version,
        "access": "write" if for_write else "read",
        "producer": entry["producer"],
        "producer_version": entry["producer_version"],
    }


__all__ = ["check_schema_support", "get_schema_registry"]
