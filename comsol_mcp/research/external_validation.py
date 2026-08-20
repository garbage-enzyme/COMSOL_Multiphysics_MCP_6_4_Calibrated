"""Backend-neutral independent validation receipt contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _sha256, _text

EXTERNAL_VALIDATION_RECEIPT_SCHEMA_NAME = "comsol_mcp.external_fidelity_validation_receipt"
EXTERNAL_VALIDATION_RECEIPT_SCHEMA_VERSION = "1.0.0"

_BACKENDS = {"independent_comsol", "rcwa"}
_FALLBACK_MODES = {"primary", "explicit_manual_fallback"}
_DISPOSITIONS = {"validated", "disagreed", "inconclusive", "failed"}


def _optional_sha256(value: object, name: str) -> str | None:
    return None if value is None else _sha256(value, name)


def _finite_mapping(value: object, name: str, *, maximum: int = 64) -> dict[str, float]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    if not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must be a bounded nonempty object")
    return {
        _identifier(key, f"{name} key"): _finite(item, f"{name}.{key}")
        for key, item in sorted(value.items())
    }


def normalize_external_validation_receipt(value: object) -> dict[str, Any]:
    """Normalize independent COMSOL evidence or an explicitly authorized RCWA fallback."""
    bounded = _bounded_json(value, "external validation receipt", 512 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "receipt_fingerprint" in bounded:
        supplied = bounded.pop("receipt_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "validation_id",
            "backend",
            "fallback",
            "environment_identity_sha256",
            "geometry_mapping_sha256",
            "material_mapping_sha256",
            "excitation_mapping_sha256",
            "known_non_equivalences",
            "sampling_sha256",
            "discretization_sha256",
            "convergence_sha256",
            "raw_artifact_sha256",
            "comparison_metrics",
            "comparison_tolerances",
            "disposition",
            "retention_disposition",
        },
        "external validation receipt",
    )
    if (
        raw["schema_name"] != EXTERNAL_VALIDATION_RECEIPT_SCHEMA_NAME
        or raw["schema_version"] != EXTERNAL_VALIDATION_RECEIPT_SCHEMA_VERSION
    ):
        raise ValueError("external validation receipt schema identity is unsupported")
    backend = _object(
        raw["backend"],
        {"kind", "provider", "version", "execution_location", "license_authority"},
        "backend",
    )
    backend_kind = backend["kind"]
    if backend_kind not in _BACKENDS:
        raise ValueError("external validation backend is unsupported")
    normalized_backend = {
        "kind": backend_kind,
        "provider": _text(backend["provider"], "backend.provider", maximum=128),
        "version": _text(backend["version"], "backend.version", maximum=128),
        "execution_location": _identifier(
            backend["execution_location"], "backend.execution_location"
        ),
        "license_authority": _identifier(backend["license_authority"], "backend.license_authority"),
    }
    fallback = _object(
        raw["fallback"],
        {"mode", "prior_backend", "prior_receipt_sha256", "authorization_sha256"},
        "fallback",
    )
    mode = fallback["mode"]
    if mode not in _FALLBACK_MODES:
        raise ValueError("external validation fallback mode is unsupported")
    prior_backend = fallback["prior_backend"]
    prior_receipt = _optional_sha256(fallback["prior_receipt_sha256"], "prior_receipt_sha256")
    authorization = _optional_sha256(fallback["authorization_sha256"], "authorization_sha256")
    if mode == "primary":
        if backend_kind != "independent_comsol":
            raise ValueError("external validation primary backend must be independent COMSOL")
        if prior_backend is not None or prior_receipt is not None or authorization is not None:
            raise ValueError("primary validation must not declare fallback evidence")
        normalized_prior = None
    else:
        if (
            backend_kind != "rcwa"
            or prior_backend != "independent_comsol"
            or prior_receipt is None
            or authorization is None
        ):
            raise ValueError("RCWA fallback requires prior COMSOL receipt and manual authorization")
        normalized_prior = "independent_comsol"
    non_equivalences = raw["known_non_equivalences"]
    if not isinstance(non_equivalences, list) or len(non_equivalences) > 64:
        raise ValueError("known_non_equivalences must be a bounded list")
    normalized_non_equivalences = [
        _text(item, "known_non_equivalences", maximum=512) for item in non_equivalences
    ]
    artifacts = raw["raw_artifact_sha256"]
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 256:
        raise ValueError("raw_artifact_sha256 must be a bounded nonempty list")
    normalized_artifacts = [_sha256(item, "raw_artifact_sha256") for item in artifacts]
    if len(normalized_artifacts) != len(set(normalized_artifacts)):
        raise ValueError("raw artifact identities must be unique")
    disposition = raw["disposition"]
    if disposition not in _DISPOSITIONS:
        raise ValueError("external validation disposition is unsupported")
    body = {
        "schema_name": EXTERNAL_VALIDATION_RECEIPT_SCHEMA_NAME,
        "schema_version": EXTERNAL_VALIDATION_RECEIPT_SCHEMA_VERSION,
        "validation_id": _identifier(raw["validation_id"], "validation_id"),
        "backend": normalized_backend,
        "fallback": {
            "mode": mode,
            "prior_backend": normalized_prior,
            "prior_receipt_sha256": prior_receipt,
            "authorization_sha256": authorization,
        },
        "environment_identity_sha256": _sha256(
            raw["environment_identity_sha256"], "environment_identity_sha256"
        ),
        "geometry_mapping_sha256": _sha256(
            raw["geometry_mapping_sha256"], "geometry_mapping_sha256"
        ),
        "material_mapping_sha256": _sha256(
            raw["material_mapping_sha256"], "material_mapping_sha256"
        ),
        "excitation_mapping_sha256": _sha256(
            raw["excitation_mapping_sha256"], "excitation_mapping_sha256"
        ),
        "known_non_equivalences": normalized_non_equivalences,
        "sampling_sha256": _sha256(raw["sampling_sha256"], "sampling_sha256"),
        "discretization_sha256": _sha256(raw["discretization_sha256"], "discretization_sha256"),
        "convergence_sha256": _sha256(raw["convergence_sha256"], "convergence_sha256"),
        "raw_artifact_sha256": normalized_artifacts,
        "comparison_metrics": _finite_mapping(raw["comparison_metrics"], "comparison_metrics"),
        "comparison_tolerances": _finite_mapping(
            raw["comparison_tolerances"], "comparison_tolerances"
        ),
        "disposition": disposition,
        "retention_disposition": _identifier(raw["retention_disposition"], "retention_disposition"),
    }
    body["receipt_fingerprint"] = domain_sha256_v2(EXTERNAL_VALIDATION_RECEIPT_SCHEMA_NAME, body)
    if supplied is not None and supplied != body["receipt_fingerprint"]:
        raise ValueError("external validation receipt fingerprint is invalid")
    return body


__all__ = [
    "EXTERNAL_VALIDATION_RECEIPT_SCHEMA_NAME",
    "EXTERNAL_VALIDATION_RECEIPT_SCHEMA_VERSION",
    "normalize_external_validation_receipt",
]
