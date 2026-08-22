"""Independent COMSOL first and explicit RCWA fallback receipt tests."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research.external_validation import normalize_external_validation_receipt


def _receipt(kind: str = "independent_comsol") -> dict:
    fallback = {
        "mode": "primary",
        "prior_backend": None,
        "prior_receipt_sha256": None,
        "authorization_sha256": None,
    }
    if kind == "rcwa":
        fallback = {
            "mode": "explicit_manual_fallback",
            "prior_backend": "independent_comsol",
            "prior_receipt_sha256": "1" * 64,
            "authorization_sha256": "2" * 64,
        }
    return {
        "schema_name": "comsol_mcp.external_fidelity_validation_receipt",
        "schema_version": "1.0.0",
        "validation_id": f"pedot-{kind}",
        "backend": {
            "kind": kind,
            "provider": "COMSOL" if kind == "independent_comsol" else "grcwa",
            "version": "6.4" if kind == "independent_comsol" else "0.1",
            "execution_location": "local",
            "license_authority": "caller_authorized",
        },
        "fallback": fallback,
        "environment_identity_sha256": "3" * 64,
        "geometry_mapping_sha256": "4" * 64,
        "material_mapping_sha256": "5" * 64,
        "excitation_mapping_sha256": "6" * 64,
        "known_non_equivalences": [],
        "sampling_sha256": "7" * 64,
        "discretization_sha256": "8" * 64,
        "convergence_sha256": "9" * 64,
        "raw_artifact_sha256": ["a" * 64],
        "comparison_metrics": {"maximum_absolute_delta": 0.01},
        "comparison_tolerances": {"maximum_absolute_delta": 0.02},
        "disposition": "validated",
        "retention_disposition": "hash_bound_local_only",
    }


def test_independent_comsol_primary_and_explicit_rcwa_fallback_are_idempotent():
    primary = normalize_external_validation_receipt(_receipt())
    fallback = normalize_external_validation_receipt(_receipt("rcwa"))
    assert primary["fallback"]["mode"] == "primary"
    assert fallback["fallback"]["mode"] == "explicit_manual_fallback"
    assert fallback["fallback"]["authorization_sha256"] == "2" * 64
    assert normalize_external_validation_receipt(primary) == primary


def test_rcwa_cannot_be_silent_primary_or_omit_prior_receipt_or_authorization():
    value = _receipt("rcwa")
    value["fallback"] = {
        "mode": "primary",
        "prior_backend": None,
        "prior_receipt_sha256": None,
        "authorization_sha256": None,
    }
    with pytest.raises(ValueError, match="primary backend"):
        normalize_external_validation_receipt(value)
    for field in ("prior_receipt_sha256", "authorization_sha256"):
        value = _receipt("rcwa")
        value["fallback"][field] = None
        with pytest.raises(ValueError, match="manual authorization"):
            normalize_external_validation_receipt(value)


def test_receipt_rejects_nonfinite_metrics_duplicate_artifacts_and_tampering():
    value = _receipt()
    value["comparison_metrics"]["maximum_absolute_delta"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        normalize_external_validation_receipt(value)
    value = _receipt()
    value["raw_artifact_sha256"].append("a" * 64)
    with pytest.raises(ValueError, match="unique"):
        normalize_external_validation_receipt(value)
    normalized = normalize_external_validation_receipt(_receipt())
    tampered = copy.deepcopy(normalized)
    tampered["disposition"] = "disagreed"
    with pytest.raises(ValueError, match="fingerprint"):
        normalize_external_validation_receipt(tampered)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["backend"].update({"kind": ["independent_comsol"]}),
        lambda value: value["fallback"].update({"mode": {"primary": True}}),
        lambda value: value.update({"disposition": ["validated"]}),
    ],
)
def test_untrusted_enum_leaves_reject_unhashable_values_as_value_error(mutation):
    value = _receipt()
    mutation(value)
    with pytest.raises(ValueError):
        normalize_external_validation_receipt(value)
