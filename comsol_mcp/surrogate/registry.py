"""Immutable surrogate registry, lifecycle, drift, and out-of-domain policy.

This module never imports COMSOL, Java, MPh, or network clients.  Registry
entries are append-only and sealed by canonical hash; a transition never
rewrites an accepted entry, and a surrogate prediction never upgrades a FEM
evidence state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"

# Forward lifecycle.  ``retired`` is terminal.
LIFECYCLE_STATES = (
    "draft",
    "data_validated",
    "configured",
    "training",
    "trained",
    "tested",
    "calibrated",
    "accepted",
    "retired",
)

# States that end a lineage without acceptance.
TERMINAL_ERROR_STATES = (
    "data_rejected",
    "training_failed",
    "nonfinite_loss",
    "resource_stopped",
    "test_failed",
    "ood_policy_failed",
    "artifact_invalid",
    "corrupt",
    "cancelled",
)

# Allowed forward transitions only.  No state may be skipped and no accepted
# entry may be reopened.
_ALLOWED_TRANSITIONS = {
    "draft": ("data_validated", "data_rejected", "cancelled"),
    "data_validated": ("configured", "data_rejected", "cancelled"),
    "configured": ("training", "cancelled"),
    "training": (
        "trained",
        "training_failed",
        "nonfinite_loss",
        "resource_stopped",
        "cancelled",
    ),
    "trained": ("tested", "test_failed", "artifact_invalid", "cancelled"),
    "tested": ("calibrated", "ood_policy_failed", "artifact_invalid"),
    "calibrated": ("accepted", "artifact_invalid"),
    "accepted": ("retired",),
    "retired": (),
}
for _terminal in TERMINAL_ERROR_STATES:
    _ALLOWED_TRANSITIONS[_terminal] = ()

# Out-of-domain prediction states.  Only ``in_domain`` avoids FEM escalation.
OOD_STATES = (
    "in_domain",
    "edge",
    "out_of_domain",
    "unsupported_identity",
    "uncalibrated",
)
ESCALATION_REQUIRED_STATES = ("edge", "out_of_domain", "unsupported_identity", "uncalibrated")

# Identity fields that constitute contract drift when they change.
DRIFT_IDENTITY_FIELDS = (
    "dataset_manifest_sha256",
    "split_manifest_sha256",
    "field_schema_sha256",
    "transforms_sha256",
    "architecture_sha256",
    "comsol_build",
    "objective",
)

# Scientific disposition is deliberately separate from execution state.
SCIENTIFIC_STATES = (
    "predicted",
    "fem_verified",
    "not_requested",
    "unknown",
)


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_hex64(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-character hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a 64-character hex digest") from exc
    return value.lower()


def _require_finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _finalize(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "entry_sha256": canonical_sha256_v1(body)}


def build_model_card(
    *,
    model_id: str,
    lineage_id: str,
    identities: Mapping[str, Any],
    metrics: Mapping[str, Any],
    baselines: Mapping[str, Any] | None = None,
    seed_policy: Mapping[str, Any] | None = None,
    trained_chksum: str | None = None,
    intended_uses: Sequence[str] = (),
    prohibited_uses: Sequence[str] = (),
) -> dict[str, Any]:
    """Build one immutable model card binding every identity and metric."""
    if not isinstance(identities, Mapping) or not identities:
        raise ValueError("identities must be a non-empty mapping")
    normalized_identities: dict[str, Any] = {}
    for key, value in identities.items():
        key = _require_str("identity key", key, max_len=64)
        if key.endswith("_sha256"):
            normalized_identities[key] = _require_hex64(f"identities.{key}", value)
        else:
            normalized_identities[key] = _require_str(f"identities.{key}", value, max_len=128)

    if not isinstance(metrics, Mapping) or not metrics:
        raise ValueError("metrics must be a non-empty mapping")
    normalized_metrics = {
        _require_str("metric name", key, max_len=64): _require_finite(f"metrics.{key}", value)
        for key, value in metrics.items()
    }

    body: dict[str, Any] = {
        "schema": "comsol_mcp.surrogate_model_card",
        "schema_version": SCHEMA_VERSION,
        "model_id": _require_str("model_id", model_id),
        "lineage_id": _require_str("lineage_id", lineage_id),
        "identities": dict(sorted(normalized_identities.items())),
        "metrics": dict(sorted(normalized_metrics.items())),
        "baselines": dict(sorted((baselines or {}).items())),
        "seed_policy": dict(sorted((seed_policy or {}).items())),
        "intended_uses": [_require_str("intended_use", item) for item in intended_uses],
        "prohibited_uses": [_require_str("prohibited_use", item) for item in prohibited_uses],
        "trained_chksum": (
            _require_hex64("trained_chksum", trained_chksum) if trained_chksum else None
        ),
        "scientific_disposition": "predicted",
        "never_upgrades_fem_evidence": True,
    }
    return _finalize(body)


def validate_model_card(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("model card must be a mapping")
    required = (
        "schema",
        "schema_version",
        "model_id",
        "lineage_id",
        "identities",
        "metrics",
        "baselines",
        "seed_policy",
        "intended_uses",
        "prohibited_uses",
        "trained_chksum",
        "scientific_disposition",
        "never_upgrades_fem_evidence",
    )
    if set(value) != set(required) | {"entry_sha256"}:
        raise ValueError("model card keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_model_card":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    body = {key: value[key] for key in required}
    if value["entry_sha256"] != canonical_sha256_v1(body):
        raise ValueError("entry_sha256 mismatch")
    if value["never_upgrades_fem_evidence"] is not True:
        raise ValueError("a surrogate model card must never upgrade FEM evidence")
    if value["scientific_disposition"] not in SCIENTIFIC_STATES:
        raise ValueError("unknown scientific disposition")
    # A model card describes a surrogate.  It may never assert that the model
    # itself is FEM-verified: that disposition belongs to a fresh FEM run and
    # is recorded on the candidate, not on the surrogate.
    if value["scientific_disposition"] == "fem_verified":
        raise ValueError("a surrogate model card cannot declare fem_verified")
    for key in body["identities"]:
        if key.endswith("_sha256"):
            _require_hex64(f"identities.{key}", body["identities"][key])
    for key, metric in body["metrics"].items():
        _require_finite(f"metrics.{key}", metric)
    return dict(value)


def build_registry_entry(
    *,
    model_id: str,
    lineage_id: str,
    state: str,
    model_card_sha256: str,
    artifacts: Mapping[str, str],
) -> dict[str, Any]:
    """Build one sealed registry entry in a declared lifecycle state."""
    if state not in LIFECYCLE_STATES:
        raise ValueError(f"unknown lifecycle state: {state}")
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise ValueError("artifacts must be a non-empty mapping")
    normalized_artifacts = {
        _require_str("artifact name", key, max_len=128): _require_hex64(
            f"artifacts.{key}", value
        )
        for key, value in artifacts.items()
    }
    body = {
        "schema": "comsol_mcp.surrogate_registry_entry",
        "schema_version": SCHEMA_VERSION,
        "model_id": _require_str("model_id", model_id),
        "lineage_id": _require_str("lineage_id", lineage_id),
        "state": state,
        "model_card_sha256": _require_hex64("model_card_sha256", model_card_sha256),
        "artifacts": dict(sorted(normalized_artifacts.items())),
        "transition_count": 1,
        "history": [{"from": None, "to": state}],
        "immutable": state in {"accepted", "retired"},
    }
    return _finalize(body)


def validate_registry_entry(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("registry entry must be a mapping")
    required = (
        "schema",
        "schema_version",
        "model_id",
        "lineage_id",
        "state",
        "model_card_sha256",
        "artifacts",
        "transition_count",
        "history",
        "immutable",
    )
    if set(value) != set(required) | {"entry_sha256"}:
        raise ValueError("registry entry keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_registry_entry":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if value["state"] not in LIFECYCLE_STATES + TERMINAL_ERROR_STATES:
        raise ValueError("unknown lifecycle state")
    body = {key: value[key] for key in required}
    if value["entry_sha256"] != canonical_sha256_v1(body):
        raise ValueError("entry_sha256 mismatch")
    if not isinstance(value["history"], list) or not value["history"]:
        raise ValueError("history must be a non-empty list")
    if value["transition_count"] != len(value["history"]):
        raise ValueError("transition_count must match history length")
    return dict(value)


def advance_registry_entry(entry: Mapping[str, Any], *, to_state: str, **evidence: Any) -> dict[str, Any]:
    """Advance a registry entry by exactly one permitted transition.

    An accepted or retired entry is immutable: further transitions are refused
    rather than silently rewriting history.
    """
    current = validate_registry_entry(entry)
    if current["immutable"] is True or current["state"] in {"accepted", "retired"}:
        raise ValueError("accepted or retired registry entries are immutable")
    allowed = _ALLOWED_TRANSITIONS.get(current["state"], ())
    if to_state not in allowed:
        raise ValueError(
            f"transition {current['state']} -> {to_state} is not permitted"
        )
    body = {
        key: current[key]
        for key in (
            "schema",
            "schema_version",
            "model_id",
            "lineage_id",
            "model_card_sha256",
            "artifacts",
        )
    }
    body["state"] = to_state
    body["transition_count"] = current["transition_count"] + 1
    body["history"] = [*current["history"], {"from": current["state"], "to": to_state}]
    if evidence:
        body["history"][-1]["evidence_sha256"] = canonical_sha256_v1(
            {key: str(value) for key, value in sorted(evidence.items())}
        )
    body["immutable"] = to_state in {"accepted", "retired"}
    return _finalize(body)


def detect_drift(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare two model-card identity sets for contract drift.

    A material, mesh, physics, adapter, or COMSOL identity change is contract
    drift even when numeric inputs stay inside bounds.
    """
    before = validate_model_card(previous)["identities"]
    after = validate_model_card(current)["identities"]
    changed = sorted(
        key
        for key in set(before) | set(after)
        if before.get(key) != after.get(key)
    )
    material = [key for key in changed if key in DRIFT_IDENTITY_FIELDS]
    return {
        "drift_detected": bool(changed),
        "changed_fields": changed,
        "contract_drift_fields": material,
        "requires_new_lineage": bool(material),
        "automatic_retraining": False,
    }


def assert_no_contract_drift(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed when a continuation would cross a contract identity."""
    report = detect_drift(previous, current)
    if report["requires_new_lineage"]:
        raise ValueError(
            "drift_requires_new_lineage: " + ", ".join(report["contract_drift_fields"])
        )
    return report


def evaluate_ood(
    *,
    query: Sequence[float],
    train_rows: Sequence[Sequence[float]],
    bounds: Mapping[str, Sequence[float]],
    calibration: Mapping[str, Any] | None = None,
    edge_fraction: float = 0.05,
) -> dict[str, Any]:
    """Classify one query against hard bounds and normalized support distance.

    Inputs are never clipped back into the domain; an out-of-domain or
    unreliable query is refused or escalated instead.
    """
    if edge_fraction <= 0 or edge_fraction >= 1:
        raise ValueError("edge_fraction must be strictly between 0 and 1")
    point = [_require_finite("query", item) for item in query]
    if not point:
        raise ValueError("query must be non-empty")
    lower = [_require_finite("bounds.lower", item) for item in bounds.get("lower", ())]
    upper = [_require_finite("bounds.upper", item) for item in bounds.get("upper", ())]
    if len(lower) != len(point) or len(upper) != len(point):
        raise ValueError("bounds must match the query dimension")
    for index, (low, high) in enumerate(zip(lower, upper)):
        if not low < high:
            raise ValueError(f"bounds[{index}] requires lower < upper")

    if calibration is None:
        reason = "uncalibrated"
        state = "uncalibrated"
    else:
        reason = None
        state = None

    if state is None:
        for index, value in enumerate(point):
            if not lower[index] <= value <= upper[index]:
                state, reason = "out_of_domain", f"outside_hard_bound_{index}"
                break

    if state is None:
        rows = [[_require_finite("train_row", item) for item in row] for row in train_rows]
        if not rows:
            raise ValueError("train_rows must be non-empty")
        for index, row in enumerate(rows):
            if len(row) != len(point):
                raise ValueError(f"train_rows[{index}] dimension mismatch")
        # Normalize by the declared bounds, then take the nearest-row distance.
        worst_ratio = 0.0
        nearest = None
        for row in rows:
            ratios = [
                abs(point[i] - row[i]) / (upper[i] - lower[i]) for i in range(len(point))
            ]
            distance = max(ratios)
            if nearest is None or distance < nearest:
                nearest = distance
        worst_ratio = float(nearest or 0.0)
        if worst_ratio <= edge_fraction:
            state = "in_domain"
        elif worst_ratio <= 2 * edge_fraction:
            state, reason = "edge", "near_support_boundary"
        else:
            state, reason = "out_of_domain", "far_from_training_support"
    else:
        worst_ratio = None

    return {
        "state": state,
        "reason": reason,
        "fem_escalation_required": state in ESCALATION_REQUIRED_STATES,
        "input_clipped": False,
        "nearest_support_ratio": worst_ratio,
        "calibration_present": calibration is not None,
        "scientific_disposition": "predicted",
    }


def assert_prediction_not_evidence(prediction: Mapping[str, Any]) -> dict[str, Any]:
    """Refuse any prediction row that claims to satisfy FEM evidence fields."""
    forbidden = ("fem_evidence", "verified", "converged", "physical_validation")
    present = sorted(key for key in forbidden if prediction.get(key))
    if present:
        raise ValueError(
            "a surrogate prediction cannot carry FEM evidence fields: "
            + ", ".join(present)
        )
    disposition = prediction.get("scientific_disposition", "predicted")
    if disposition not in SCIENTIFIC_STATES:
        raise ValueError("unknown scientific disposition")
    if disposition == "fem_verified":
        raise ValueError("a surrogate prediction cannot be fem_verified")
    return {
        "accepted": True,
        "scientific_disposition": disposition,
        "never_upgrades_fem_evidence": True,
    }


__all__ = [
    "DRIFT_IDENTITY_FIELDS",
    "ESCALATION_REQUIRED_STATES",
    "LIFECYCLE_STATES",
    "OOD_STATES",
    "SCHEMA_VERSION",
    "SCIENTIFIC_STATES",
    "TERMINAL_ERROR_STATES",
    "advance_registry_entry",
    "assert_no_contract_drift",
    "assert_prediction_not_evidence",
    "build_model_card",
    "build_registry_entry",
    "detect_drift",
    "evaluate_ood",
    "validate_model_card",
    "validate_registry_entry",
]
