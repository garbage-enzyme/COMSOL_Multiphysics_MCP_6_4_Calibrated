"""Version support registry for named public and durable artifact schemas."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from comsol_mcp import __version__
from comsol_mcp.durable import canonical_sha256_v1

OPERATION_LOCK_SCHEMA, OPERATION_LOCK_VERSION = "comsol_mcp.operation_lock", "1.0.0"
PATH_POLICY_SCHEMA, PATH_POLICY_VERSION = "comsol_mcp.path_policy", "1.1.0"
SETTINGS_SCHEMA, SETTINGS_VERSION = "comsol_mcp.settings", "1.0.0"
CLEANUP_OUTCOME_SCHEMA, CLEANUP_OUTCOME_VERSION = "comsol_mcp.cleanup_outcome", "1.0.0"
SHARED_MODEL_LOCK_SCHEMA, SHARED_MODEL_LOCK_VERSION = "comsol_mcp.shared_model_lock", "1.0.0"
SHARED_MODEL_SNAPSHOT_SCHEMA, SHARED_MODEL_SNAPSHOT_VERSION = "comsol_mcp.shared_model_snapshot", "1.0.0"
SHARED_SERVER_PREFLIGHT_SCHEMA, SHARED_SERVER_PREFLIGHT_VERSION = "comsol_mcp.shared_server_preflight", "1.1.0"
BRANCH_CONTINUATION_PLAN_SCHEMA, BRANCH_CONTINUATION_SCHEMA_VERSION = "comsol_mcp.branch_continuation_plan", "2.0.0"
BRANCH_CONTINUATION_STATES_SCHEMA = "comsol_mcp.branch_continuation_states"
CONVERGENCE_EVALUATION_SCHEMA, CONVERGENCE_LADDER_SCHEMA, CONVERGENCE_SCHEMA_VERSION = "comsol_mcp.convergence_evaluation", "comsol_mcp.convergence_ladder", "1.0.0"
_REFERENCE_POWER_NAMESPACE = "comsol_mcp" + "." + "h" + "1"
REFERENCE_POWER_CONTRACT_SCHEMA = _REFERENCE_POWER_NAMESPACE + "_licensed_gate"
REFERENCE_POWER_EXECUTION_SCHEMA = _REFERENCE_POWER_NAMESPACE + "_execution_spec"
SPECTRAL_BUNDLE_SCHEMA, SPECTRAL_CHARACTERIZATION_SCHEMA, SPECTRAL_DECISION_SCHEMA, SPECTRAL_SCHEMA_VERSION = "comsol_mcp.spectral_point_bundle", "comsol_mcp.spectral_characterization", "comsol_mcp.spectral_analysis_decision", "1.0.0"
EVIDENCE_SETTINGS_SCHEMA, EVIDENCE_STATUS_SCHEMA, EVIDENCE_VERIFICATION_SCHEMA, EVIDENCE_INTEGRITY_VERSION = "comsol_mcp.evidence_integrity_settings", "comsol_mcp.evidence_integrity_status", "comsol_mcp.evidence_integrity_verification", "1.0.0"
SPECTRAL_PROGRESS_SCHEMA_NAME, SPECTRAL_PROGRESS_SCHEMA_VERSION = "comsol_mcp.spectral_progress", "1.0.0"
ATTACHED_EXECUTION_BACKEND_SCHEMA, ATTACHED_EXECUTION_BACKEND_VERSION = "comsol_mcp.attached_execution_backend", "1.0.0"
CONVERGENCE_CAMPAIGN_LEVEL_SCHEMA_NAME, CONVERGENCE_CAMPAIGN_LEVEL_SCHEMA_VERSION = "comsol_mcp.convergence_campaign_level", "1.0.0"
CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_NAME, CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_VERSION = "comsol_mcp.convergence_campaign_summary", "1.0.0"
BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_NAME, BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_VERSION = "comsol_mcp.branch_continuation_campaign_state", "1.0.0"
BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_NAME, BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_VERSION = "comsol_mcp.branch_continuation_campaign_summary", "1.0.0"
SPECTRAL_ROW_SCHEMA_NAME, SPECTRAL_ROW_SCHEMA_VERSION = "comsol_mcp.durable_spectral_point", "1.0.0"
SPECTRAL_SUMMARY_SCHEMA_NAME, SPECTRAL_SUMMARY_SCHEMA_VERSION = "comsol_mcp.durable_spectral_summary", "1.0.0"
SPECTRAL_STAGE_SCHEMA_NAME, SPECTRAL_STAGE_SCHEMA_VERSION = "comsol_mcp.spectral_stage_plan", "1.0.0"


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
        _entry("comsol_mcp.artifact_chain", "1.0.0", "comsol_mcp.artifact_chain"),
        _entry("comsol_mcp.artifact_chain_verification", "1.0.0", "comsol_mcp.artifact_chain"),
        _entry(
            ATTACHED_EXECUTION_BACKEND_SCHEMA,
            ATTACHED_EXECUTION_BACKEND_VERSION,
            "comsol_mcp.jobs.attached_backend",
            artifact_kind="durable_artifact",
        ),
        _entry("comsol_mcp.build_identity", "1.0.0", "comsol_mcp.build_identity"),
        _entry(
            CLEANUP_OUTCOME_SCHEMA,
            CLEANUP_OUTCOME_VERSION,
            "comsol_mcp.shared_session.cleanup",
        ),
        _entry(
            BRANCH_CONTINUATION_PLAN_SCHEMA,
            BRANCH_CONTINUATION_SCHEMA_VERSION,
            "comsol_mcp.evidence.branch_continuation",
        ),
        _entry(
            BRANCH_CONTINUATION_STATES_SCHEMA,
            BRANCH_CONTINUATION_SCHEMA_VERSION,
            "comsol_mcp.evidence.branch_continuation",
        ),
        _entry(
            BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_NAME,
            BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_VERSION,
            "comsol_mcp.jobs.branch_continuation_campaign_rows",
            artifact_kind="durable_artifact",
        ),
        _entry(
            BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_NAME,
            BRANCH_CONTINUATION_CAMPAIGN_SUMMARY_SCHEMA_VERSION,
            "comsol_mcp.jobs.branch_continuation_campaign_runner",
            artifact_kind="durable_artifact",
        ),
        _entry(
            CONVERGENCE_LADDER_SCHEMA,
            CONVERGENCE_SCHEMA_VERSION,
            "comsol_mcp.evidence.convergence_evaluation",
        ),
        _entry(
            CONVERGENCE_CAMPAIGN_LEVEL_SCHEMA_NAME,
            CONVERGENCE_CAMPAIGN_LEVEL_SCHEMA_VERSION,
            "comsol_mcp.jobs.convergence_campaign_rows",
            artifact_kind="durable_artifact",
        ),
        _entry(
            CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_NAME,
            CONVERGENCE_CAMPAIGN_SUMMARY_SCHEMA_VERSION,
            "comsol_mcp.jobs.convergence_campaign_runner",
            artifact_kind="durable_artifact",
        ),
        _entry(
            CONVERGENCE_EVALUATION_SCHEMA,
            CONVERGENCE_SCHEMA_VERSION,
            "comsol_mcp.evidence.convergence_evaluation",
        ),
        _entry(
            "comsol_mcp.deployment_identity",
            "1.1.0",
            "comsol_mcp.tools.capabilities",
            readable_versions=("1.0.0", "1.1.0"),
        ),
        _entry("comsol_mcp.environment_identity", "1.0.0", "comsol_mcp.environment_identity"),
        _entry(
            EVIDENCE_SETTINGS_SCHEMA,
            EVIDENCE_INTEGRITY_VERSION,
            "comsol_mcp.evidence.integrity_controls",
        ),
        _entry(
            EVIDENCE_STATUS_SCHEMA,
            EVIDENCE_INTEGRITY_VERSION,
            "comsol_mcp.evidence.integrity_controls",
        ),
        _entry(
            EVIDENCE_VERIFICATION_SCHEMA,
            EVIDENCE_INTEGRITY_VERSION,
            "comsol_mcp.evidence.integrity_verifier",
        ),
        _entry(
            "comsol_mcp.execution_evidence_outcome",
            "1.0.0",
            "comsol_mcp.evidence.outcome_contract",
        ),
        _entry("comsol_mcp.field_dataset_discovery", "1.0.0", "comsol_mcp.evidence.field_discovery"),
        _entry("comsol_mcp.field_evidence_manifest", "1.0.0", "comsol_mcp.evidence.field_manifest"),
        _entry("comsol_mcp.field_evidence_request", "1.1.0", "comsol_mcp.evidence.field_bundle"),
        _entry(_REFERENCE_POWER_DRY_RUN_SCHEMA, "1.0.0", "comsol_mcp.evidence.reference_power_acceptance"),
        _entry(REFERENCE_POWER_EXECUTION_SCHEMA, "1.0.0", "comsol_mcp.evidence.reference_power_acceptance"),
        _entry(REFERENCE_POWER_CONTRACT_SCHEMA, "1.0.0", "comsol_mcp.evidence.reference_power_acceptance"),
        _entry("comsol_mcp.periodic_mesh_audit", "1.0.0", "comsol_mcp.tools.periodic_mesh_audit"),
        _entry("comsol_mcp.periodic_mesh_smoke", "1.0.0", "comsol_mcp.tools.periodic_mesh_audit"),
        _entry(
            OPERATION_LOCK_SCHEMA,
            OPERATION_LOCK_VERSION,
            "comsol_mcp.operation_arbiter",
            artifact_kind="durable_artifact",
        ),
        _entry(
            PATH_POLICY_SCHEMA,
            PATH_POLICY_VERSION,
            "comsol_mcp.path_policy",
            readable_versions=("1.0.0", "1.1.0"),
        ),
        _entry(
            "comsol_mcp.physical_evidence",
            "1.1.0",
            "comsol_mcp.evidence.contracts",
            readable_versions=("1.0.0", "1.1.0"),
            migration_sources=(legacy_point_audit,),
        ),
        _entry(
            "comsol_mcp.portfolio_evidence_request",
            "1.0.0",
            "comsol_mcp.evidence.portfolio_verifier",
        ),
        _entry(
            "comsol_mcp.portfolio_evidence_verification",
            "1.0.0",
            "comsol_mcp.evidence.portfolio_verifier",
        ),
        _entry(
            SPECTRAL_DECISION_SCHEMA,
            SPECTRAL_SCHEMA_VERSION,
            "comsol_mcp.evidence.spectral_characterization",
        ),
        _entry(
            SPECTRAL_BUNDLE_SCHEMA,
            SPECTRAL_SCHEMA_VERSION,
            "comsol_mcp.evidence.spectral_characterization",
        ),
        _entry(
            SPECTRAL_CHARACTERIZATION_SCHEMA,
            SPECTRAL_SCHEMA_VERSION,
            "comsol_mcp.evidence.spectral_characterization",
        ),
        _entry(
            SPECTRAL_PROGRESS_SCHEMA_NAME,
            SPECTRAL_PROGRESS_SCHEMA_VERSION,
            "comsol_mcp.jobs.spectral_progress",
            artifact_kind="durable_artifact",
        ),
        _entry(
            SPECTRAL_ROW_SCHEMA_NAME,
            SPECTRAL_ROW_SCHEMA_VERSION,
            "comsol_mcp.jobs.spectral_rows",
            artifact_kind="durable_artifact",
        ),
        _entry(
            SPECTRAL_STAGE_SCHEMA_NAME,
            SPECTRAL_STAGE_SCHEMA_VERSION,
            "comsol_mcp.jobs.spectral_stages",
            artifact_kind="durable_artifact",
        ),
        _entry(
            SPECTRAL_SUMMARY_SCHEMA_NAME,
            SPECTRAL_SUMMARY_SCHEMA_VERSION,
            "comsol_mcp.jobs.spectral_runner",
            artifact_kind="durable_artifact",
        ),
        _entry("comsol_mcp.resource_calibration_report", "1.0.0", "comsol_mcp.jobs.resource_admission"),
        _entry("comsol_mcp.resource_journal_entry", "1.0.0", "comsol_mcp.jobs.resource_admission"),
        _entry("comsol_mcp.resource_journal_replay", "1.0.0", "comsol_mcp.jobs.resource_admission"),
        _entry("comsol_mcp.resource_policy", "1.0.0", "comsol_mcp.jobs.resource_admission"),
        _entry("comsol_mcp.resource_telemetry_sample", "1.0.0", "comsol_mcp.jobs.resource_admission"),
        _entry("comsol_mcp.runtime_compatibility", "1.0.0", "comsol_mcp.compatibility"),
        _entry(SETTINGS_SCHEMA, SETTINGS_VERSION, "comsol_mcp.settings", artifact_kind="configuration"),
        _entry(_REGISTRY_SCHEMA, _REGISTRY_VERSION, "comsol_mcp.schema_registry"),
        _entry(
            SHARED_MODEL_LOCK_SCHEMA,
            SHARED_MODEL_LOCK_VERSION,
            "comsol_mcp.shared_session.locking",
            artifact_kind="durable_artifact",
        ),
        _entry(
            SHARED_MODEL_SNAPSHOT_SCHEMA,
            SHARED_MODEL_SNAPSHOT_VERSION,
            "comsol_mcp.shared_session.lifecycle",
            artifact_kind="durable_artifact",
        ),
        _entry(
            SHARED_SERVER_PREFLIGHT_SCHEMA,
            SHARED_SERVER_PREFLIGHT_VERSION,
            "comsol_mcp.shared_session.preflight",
        ),
        _entry("comsol_mcp.validation_matrix_collector", "1.0.0", "comsol_mcp.jobs.validation_collectors"),
        _entry("comsol_mcp.validation_matrix_field_collector", "1.0.0", "comsol_mcp.jobs.validation_collectors"),
        _entry("comsol_mcp.validation_matrix_field_review", "1.0.0", "comsol_mcp.jobs.field_review"),
        _entry("comsol_mcp.validation_policy", "1.0.0", "comsol_mcp.evidence.contracts"),
        _entry("comsol_mcp.visual_dual_review", "1.0.0", "comsol_mcp.evidence.visual_review"),
        _entry("comsol_mcp.visual_review_receipt", "1.0.0", "comsol_mcp.evidence.visual_review"),
        _entry("comsol_mcp.visual_review_request", "1.0.0", "comsol_mcp.evidence.visual_review"),
        _entry("comsol_mcp.visual_reviewer_capability", "1.0.0", "comsol_mcp.evidence.visual_review"),
        _entry(
            legacy_point_audit,
            "1",
            "comsol_mcp.tools.wave_optics_audit",
            artifact_kind="legacy_artifact",
            writable=False,
        ),
    ]
    return sorted(entries, key=lambda item: item["schema_name"])


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
    return deepcopy({**body, "registry_sha256": canonical_sha256_v1(body)})


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
