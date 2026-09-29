"""Solver-free surrogate export, consistency, and registry-integration tests."""

from __future__ import annotations

import pytest

from comsol_mcp.durable.canonical import canonical_sha256_v1
from comsol_mcp.surrogate.export import (
    EXPORT_FORMATS,
    build_export_manifest,
    check_prediction_consistency,
    evaluate_export_availability,
    hash_export_artifact,
    integrate_export_into_registry,
    validate_export_manifest,
)

SOLVER_FREE_BANNED = (
    "import mph",
    "from mph",
    "import jpype",
    "from jpype",
    "import comsol",
    "from comsol.",
    "import onnx",
    "import torch",
    "import tensorflow",
    "import numpy",
)

SHA_A = "a" * 64


def _artifact(fmt: str = "onnx", size: int = 2048, sha: str = SHA_A) -> dict:
    return {
        "export_format": fmt,
        "artifact_name": f"model.{fmt}",
        "size_bytes": size,
        "sha256": sha,
    }


def _manifest(**overrides) -> dict:
    kwargs = {
        "export_id": "export-1",
        "trained_chksum": "-8938453606443985335",
        "architecture_sha256": SHA_A,
        "artifacts": [_artifact()],
    }
    kwargs.update(overrides)
    return build_export_manifest(**kwargs)


# --------------------------------------------------------------------------
# Artifact hashing
# --------------------------------------------------------------------------


def test_hash_export_artifact_streams_the_real_file(tmp_path) -> None:
    target = tmp_path / "surrogate.bin"
    payload = b"surrogate-export-payload" * 512
    target.write_bytes(payload)
    record = hash_export_artifact(path=target, export_format="onnx")
    assert record["export_format"] == "onnx"
    assert record["size_bytes"] == len(payload)
    assert record["sha256"] == __import__("hashlib").sha256(payload).hexdigest()
    # The unreadable COMSOL property is explicitly not trusted.
    assert record["trusted_property_filename"] is False


def test_hash_export_artifact_rejects_missing_empty_and_bad_format(tmp_path) -> None:
    with pytest.raises(ValueError, match="not found"):
        hash_export_artifact(path=tmp_path / "absent.bin", export_format="onnx")
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        hash_export_artifact(path=empty, export_format="onnx")
    real = tmp_path / "real.bin"
    real.write_bytes(b"x")
    with pytest.raises(ValueError, match="export_format must be one of"):
        hash_export_artifact(path=real, export_format="pickle")
    with pytest.raises(ValueError, match="chunk_bytes"):
        hash_export_artifact(path=real, export_format="onnx", chunk_bytes=16)


# --------------------------------------------------------------------------
# Export manifest
# --------------------------------------------------------------------------


def test_manifest_binds_the_trained_identity_and_hashes() -> None:
    manifest = _manifest()
    assert manifest["trained_chksum"] == "-8938453606443985335"
    assert manifest["onnx_available"] is True
    assert manifest["unavailable_formats"] == []
    assert manifest["is_surrogate_prediction"] is False
    assert manifest["upgrades_fem_evidence"] is False
    validate_export_manifest(manifest)


def test_onnx_is_the_required_export_format() -> None:
    """COMSOL's export(path) writes ONNX regardless of extension, so ONNX is
    the required path rather than an optional extra."""
    manifest = _manifest()
    assert manifest["required_formats"] == ["onnx"]
    assert manifest["unavailable_is_failure"] is False


def test_missing_onnx_is_refused() -> None:
    with pytest.raises(ValueError, match="artifacts must be non-empty"):
        _manifest(artifacts=[])
    with pytest.raises(ValueError, match="required format is not supported"):
        _manifest(required_formats=["pickle"])


def test_manifest_rejects_duplicates_and_bad_artifacts() -> None:
    with pytest.raises(ValueError, match="duplicate export format"):
        _manifest(artifacts=[_artifact(), _artifact()])
    with pytest.raises(ValueError, match="unsupported format"):
        _manifest(artifacts=[_artifact("pickle")])
    with pytest.raises(ValueError, match="non-empty"):
        _manifest(artifacts=[_artifact(size=0)])
    with pytest.raises(ValueError, match="hex digest"):
        _manifest(artifacts=[_artifact(sha="short")])
    with pytest.raises(ValueError, match="non-empty"):
        _manifest(artifacts=[])


def test_manifest_is_tamper_evident() -> None:
    manifest = _manifest()
    tampered = dict(manifest)
    tampered["trained_chksum"] = "0"
    with pytest.raises(ValueError, match="manifest_sha256 mismatch"):
        validate_export_manifest(tampered)


def test_manifest_rejects_a_lie_about_onnx() -> None:
    manifest = _manifest()
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    body["onnx_available"] = False  # an onnx artifact does exist
    resealed = {**body, "manifest_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="onnx_available disagrees"):
        validate_export_manifest(resealed)


def test_manifest_cannot_claim_fem_evidence() -> None:
    manifest = _manifest()
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    body["upgrades_fem_evidence"] = True
    resealed = {**body, "manifest_sha256": canonical_sha256_v1(body)}
    with pytest.raises(ValueError, match="never upgrades FEM evidence"):
        validate_export_manifest(resealed)


# --------------------------------------------------------------------------
# Prediction consistency
# --------------------------------------------------------------------------


def test_identical_predictions_are_consistent() -> None:
    report = check_prediction_consistency(
        in_model_predictions=[[1.0, 2.0], [3.0, 4.0]],
        reimported_predictions=[[1.0, 2.0], [3.0, 4.0]],
        absolute_tolerance=1e-9,
    )
    assert report["state"] == "consistent"
    assert report["registration_allowed"] is True
    assert report["worst_absolute_difference"] == 0.0


def test_small_difference_within_tolerance_is_consistent() -> None:
    report = check_prediction_consistency(
        in_model_predictions=[[1.0]],
        reimported_predictions=[[1.0 + 1e-12]],
        absolute_tolerance=1e-9,
    )
    assert report["state"] == "consistent"


def test_materially_different_reimport_is_inconsistent() -> None:
    report = check_prediction_consistency(
        in_model_predictions=[[1.0], [2.0]],
        reimported_predictions=[[1.0], [9.0]],
        absolute_tolerance=1e-6,
    )
    assert report["state"] == "inconsistent"
    assert report["consistent"] is False
    assert report["registration_allowed"] is False
    assert report["violating_rows"] == [1]
    assert report["is_surrogate_prediction"] is True
    assert report["upgrades_fem_evidence"] is False


def test_relative_tolerance_can_admit_a_scaled_agreement() -> None:
    report = check_prediction_consistency(
        in_model_predictions=[[1.0e6]],
        reimported_predictions=[[1.0e6 + 1.0]],
        absolute_tolerance=1e-9,
        relative_tolerance=1e-4,
    )
    assert report["state"] == "consistent"


def test_consistency_rejects_misaligned_and_bad_tolerances() -> None:
    with pytest.raises(ValueError, match="same row count"):
        check_prediction_consistency(
            in_model_predictions=[[1.0]],
            reimported_predictions=[[1.0], [2.0]],
            absolute_tolerance=1e-9,
        )
    with pytest.raises(ValueError, match="tolerances must be non-negative"):
        check_prediction_consistency(
            in_model_predictions=[[1.0]],
            reimported_predictions=[[1.0]],
            absolute_tolerance=-1.0,
        )
    with pytest.raises(ValueError, match="finite"):
        check_prediction_consistency(
            in_model_predictions=[[1.0]],
            reimported_predictions=[[float("nan")]],
            absolute_tolerance=1e-9,
        )


# --------------------------------------------------------------------------
# ONNX availability recording
# --------------------------------------------------------------------------


def test_onnx_availability_is_recorded_without_overclaiming() -> None:
    available = evaluate_export_availability(onnx_export_error=None)
    assert available["state"] == "available"
    assert available["required"] is True
    unavailable = evaluate_export_availability(
        onnx_export_error="FlException: export not supported"
    )
    assert unavailable["state"] == "unavailable"
    assert unavailable["blocks_registration"] is True
    assert unavailable["recorded_as_unavailable_not_passed"] is True
    assert "export not supported" in unavailable["error"]


# --------------------------------------------------------------------------
# Registry integration
# --------------------------------------------------------------------------


def test_consistent_in_domain_export_registers_without_escalation() -> None:
    report = integrate_export_into_registry(
        export_manifest=_manifest(),
        consistency={"state": "consistent"},
        ood_state="in_domain",
        escalation_required=False,
    )
    assert report["registered"] is True
    assert report["escalation_required"] is False
    assert report["upgrades_fem_evidence"] is False


def test_inconsistent_export_is_refused_and_escalates() -> None:
    report = integrate_export_into_registry(
        export_manifest=_manifest(),
        consistency={"state": "inconsistent"},
        ood_state="in_domain",
        escalation_required=False,
    )
    assert report["registered"] is False
    assert report["reason_code"] == "export_inconsistent_with_model"
    assert report["escalation_required"] is True


def test_unavailable_consistency_blocks_registration() -> None:
    report = integrate_export_into_registry(
        export_manifest=_manifest(),
        consistency={"state": "unavailable"},
        ood_state="in_domain",
        escalation_required=False,
    )
    assert report["registered"] is False
    assert report["reason_code"] == "consistency_unavailable"
    assert report["escalation_required"] is True


def test_out_of_domain_always_requires_fresh_fem_escalation() -> None:
    for state in ("edge", "out_of_domain", "uncalibrated"):
        report = integrate_export_into_registry(
            export_manifest=_manifest(),
            consistency={"state": "consistent"},
            ood_state=state,
            escalation_required=False,
        )
        assert report["registered"] is True, state
        assert report["escalation_required"] is True, state


def test_registry_integration_rejects_unknown_states() -> None:
    with pytest.raises(ValueError, match="consistency state"):
        integrate_export_into_registry(
            export_manifest=_manifest(),
            consistency={"state": "probably_fine"},
            ood_state="in_domain",
            escalation_required=False,
        )
    with pytest.raises(ValueError, match="unknown ood_state"):
        integrate_export_into_registry(
            export_manifest=_manifest(),
            consistency={"state": "consistent"},
            ood_state="probably_in_domain",
            escalation_required=False,
        )


# --------------------------------------------------------------------------
# Solver-free guard
# --------------------------------------------------------------------------


def test_export_module_is_solver_free() -> None:
    import comsol_mcp.surrogate.export as module

    source = open(module.__file__, encoding="utf-8").read().lower()
    for banned in SOLVER_FREE_BANNED:
        assert banned not in source, f"export references {banned}"


def test_export_formats_are_the_declared_closed_set() -> None:
    """ONNX is the only format COMSOL's DNN export produces."""
    assert EXPORT_FORMATS == ("onnx",)
