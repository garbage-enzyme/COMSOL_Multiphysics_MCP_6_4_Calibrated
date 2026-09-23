"""Solver-free tests for the bounded public surrogate tools.

These tests cover the alpha7.5 S10 public surface: the five bounded read-only
surrogate tools must validate real documents, refuse malformed or tampered ones,
never claim FEM evidence, never start a solver, and never load a heavy optional
import on a cold interpreter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from comsol_mcp.contracts.surrogate import (
    SOURCE_KINDS,
    SurrogateDatasetValidateInput,
    SurrogateModelInspectInput,
    SurrogateModelVerifyInput,
    SurrogatePredictionValidateInput,
    SurrogateReadLimits,
    SurrogateTrainingPreviewInput,
    parse_dbmodel_uri,
    validate_source_reference,
)
from comsol_mcp.evidence.surrogate_evidence import (
    SurrogateEvidenceError,
    inspect_surrogate_document,
    preview_training_configuration,
    validate_dataset_document,
    validate_export_manifest_document,
    validate_model_card_document,
    validate_prediction_document,
    validate_registry_entry_document,
    verify_surrogate_document,
)
from comsol_mcp.surrogate.export import build_export_manifest
from comsol_mcp.surrogate.registry import build_model_card, build_registry_entry
from development_kit.tests.mcp_test_support import decode_tool_result
from src.server import create_server

ROOT = Path(__file__).parents[2]

SURROGATE_TOOLS = {
    "surrogate_dataset_validate",
    "surrogate_training_preview",
    "surrogate_model_inspect",
    "surrogate_model_verify",
    "surrogate_prediction_validate",
}

ALL_PROFILES = (
    "core",
    "basic_fem",
    "wave_optics",
    "electro_chemistry",
    "experimental",
    "full",
)


# ---------------------------------------------------------------------------
# Fixtures: real documents produced by the real builders
# ---------------------------------------------------------------------------


def _model_card() -> dict:
    return build_model_card(
        model_id="m1",
        lineage_id="l1",
        identities={
            "dataset_manifest_sha256": "a" * 64,
            "split_manifest_sha256": "b" * 64,
            "architecture_sha256": "c" * 64,
        },
        metrics={"rmse": 0.02, "mae": 0.01},
        trained_chksum="1" * 64,
    )


def _registry_entry(card_sha256: str) -> dict:
    return build_registry_entry(
        model_id="m1",
        lineage_id="l1",
        state="trained",
        model_card_sha256=card_sha256,
        artifacts={"onnx": "d" * 64},
    )


def _export_manifest() -> dict:
    return build_export_manifest(
        export_id="e1",
        trained_chksum="1" * 64,
        architecture_sha256="c" * 64,
        artifacts=[
            {
                "export_format": "onnx",
                "artifact_name": "m.onnx",
                "size_bytes": 100,
                "sha256": "e" * 64,
            }
        ],
    )


def _write(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture()
def owned_root(tmp_path_factory, monkeypatch):
    """An ASCII owned artifact root, which the path policy requires."""
    root = Path("D:/mcp_tests/a75s10t")
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COMSOL_MCP_ARTIFACT_WRITE_ROOT", str(root))
    monkeypatch.setenv("COMSOL_MCP_MODEL_READ_ROOTS", str(root))
    return root


# ---------------------------------------------------------------------------
# Typed contracts
# ---------------------------------------------------------------------------


def test_read_limits_are_bounded() -> None:
    limits = SurrogateReadLimits()
    assert limits.max_document_bytes > 0
    assert limits.max_rows <= 4096
    with pytest.raises(ValueError):
        SurrogateReadLimits(max_rows=0)
    with pytest.raises(ValueError):
        SurrogateReadLimits(max_rows=10_000)
    with pytest.raises(ValueError):
        SurrogateReadLimits(unexpected_field=1)


def test_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValueError):
        SurrogateDatasetValidateInput(dataset_path="x", surprise=1)
    with pytest.raises(ValueError):
        SurrogateModelInspectInput(document_path="x", surprise=1)
    with pytest.raises(ValueError):
        SurrogateModelVerifyInput(document_path="x", surprise=1)
    with pytest.raises(ValueError):
        SurrogatePredictionValidateInput(prediction_path="x", surprise=1)
    with pytest.raises(ValueError):
        SurrogateTrainingPreviewInput(configuration={}, surprise=1)


def test_dataset_contract_rejects_overlapping_names() -> None:
    with pytest.raises(ValueError, match="both a feature and a target"):
        SurrogateDatasetValidateInput(
            dataset_path="x.csv",
            expected_feature_names=["a", "b"],
            expected_target_names=["b"],
        )


def test_dataset_contract_rejects_duplicate_and_empty_names() -> None:
    with pytest.raises(ValueError, match="unique"):
        SurrogateDatasetValidateInput(
            dataset_path="x.csv", expected_feature_names=["a", "a"]
        )
    with pytest.raises(ValueError, match="non-empty"):
        SurrogateDatasetValidateInput(
            dataset_path="x.csv", expected_feature_names=["a", ""]
        )
    with pytest.raises(ValueError, match="non-empty when declared"):
        SurrogateDatasetValidateInput(dataset_path="x.csv", expected_feature_names=[])


def test_document_kind_literal_is_closed() -> None:
    with pytest.raises(ValueError):
        SurrogateModelInspectInput(document_path="x", document_kind="registry")
    assert (
        SurrogateModelInspectInput(document_path="x", document_kind="auto").document_kind
        == "auto"
    )


# ---------------------------------------------------------------------------
# The frozen dbmodel:// source kind
# ---------------------------------------------------------------------------


def test_dbmodel_uri_is_validated_as_syntax_only() -> None:
    components = parse_dbmodel_uri("dbmodel://library/models/coated_unit_cell")
    assert components["authority"] == "library"
    assert components["resource"] == "models/coated_unit_cell"
    assert components["sha256"] is None
    with_digest = parse_dbmodel_uri(
        f"dbmodel://library/models/cell?sha256={'a' * 64}"
    )
    assert with_digest["sha256"] == "a" * 64


def test_dbmodel_uri_refuses_traversal_and_non_ascii() -> None:
    with pytest.raises(ValueError, match="without traversal"):
        parse_dbmodel_uri("dbmodel://library/../secrets")
    with pytest.raises(ValueError, match="ASCII"):
        parse_dbmodel_uri("dbmodel://library/模型")
    with pytest.raises(ValueError, match="does not match"):
        parse_dbmodel_uri("https://example.com/model")
    with pytest.raises(ValueError, match="does not match"):
        parse_dbmodel_uri("dbmodel://library")
    with pytest.raises(ValueError, match="length"):
        parse_dbmodel_uri("dbmodel://library/" + "a" * 2000)


def test_dbmodel_source_cannot_declare_a_local_path() -> None:
    with pytest.raises(ValueError, match="must not declare a local path"):
        validate_source_reference(
            source_kind="dbmodel",
            source_path="D:/models/cell.mph",
            source_uri="dbmodel://library/cell",
        )
    with pytest.raises(ValueError, match="requires a source_uri"):
        validate_source_reference(
            source_kind="dbmodel", source_path=None, source_uri=None
        )


def test_file_source_cannot_declare_a_uri() -> None:
    with pytest.raises(ValueError, match="only a dbmodel source"):
        validate_source_reference(
            source_kind="file", source_path="a.csv", source_uri="dbmodel://x/y"
        )
    with pytest.raises(ValueError, match="requires a source_path"):
        validate_source_reference(source_kind="file", source_path=None, source_uri=None)


def test_dbmodel_reference_never_claims_filesystem_or_live_access() -> None:
    report = validate_source_reference(
        source_kind="dbmodel", source_path=None, source_uri="dbmodel://library/cell"
    )
    assert report["filesystem_access"] is False
    assert report["live_model_manager_access"] is False
    assert report["readable"] is False


def test_source_kind_set_is_frozen() -> None:
    assert SOURCE_KINDS == ("file", "directory", "dbmodel")
    with pytest.raises(ValueError, match="source_kind must be one of"):
        validate_source_reference(
            source_kind="network", source_path="x", source_uri=None
        )


# ---------------------------------------------------------------------------
# Document inspection
# ---------------------------------------------------------------------------


def test_inspect_validates_each_real_document_kind(tmp_path, owned_root) -> None:
    card_path = _write(owned_root / "card.json", _model_card())
    result = inspect_surrogate_document(card_path, max_bytes=1_000_000)
    assert result["success"] is True
    assert result["summary"]["document_kind"] == "model_card"
    assert result["summary"]["hash_verified"] is True
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False

    entry = _registry_entry(_model_card()["entry_sha256"])
    entry_path = _write(owned_root / "entry.json", entry)
    result = inspect_surrogate_document(entry_path, max_bytes=1_000_000)
    assert result["summary"]["document_kind"] == "registry_entry"
    assert result["summary"]["hash_verified"] is True

    manifest_path = _write(owned_root / "manifest.json", _export_manifest())
    result = inspect_surrogate_document(manifest_path, max_bytes=1_000_000)
    assert result["summary"]["document_kind"] == "export_manifest"
    assert result["summary"]["onnx_available"] is True


def test_inspect_refuses_a_tampered_document(tmp_path, owned_root) -> None:
    card = _model_card()
    card["trained_chksum"] = "9" * 64
    path = _write(owned_root / "tampered.json", card)
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(path, max_bytes=1_000_000)
    assert excinfo.value.reason_code == "surrogate_document_hash_mismatch"


def test_inspect_refuses_an_unsupported_schema(tmp_path, owned_root) -> None:
    path = _write(owned_root / "other.json", {"schema": "comsol_mcp.something_else"})
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(path, max_bytes=1_000_000)
    assert excinfo.value.reason_code == "surrogate_document_kind_unsupported"


def test_inspect_refuses_missing_schema_empty_and_non_json(tmp_path, owned_root) -> None:
    no_schema = _write(owned_root / "noschema.json", {"a": 1})
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(no_schema, max_bytes=1_000_000)
    assert excinfo.value.reason_code == "surrogate_document_schema_missing"

    empty = owned_root / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(empty, max_bytes=1_000_000)
    assert excinfo.value.reason_code == "surrogate_document_empty"

    bad = owned_root / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(bad, max_bytes=1_000_000)
    assert excinfo.value.reason_code == "surrogate_document_unparsable"

    listed = _write(owned_root / "list.json", [1, 2, 3])
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(listed, max_bytes=1_000_000)
    assert excinfo.value.reason_code == "surrogate_document_not_an_object"


def test_inspect_refuses_an_oversized_document(tmp_path, owned_root) -> None:
    path = _write(owned_root / "big.json", _model_card())
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(path, max_bytes=16)
    assert excinfo.value.reason_code == "surrogate_document_too_large"


def test_inspect_refuses_a_missing_file(owned_root) -> None:
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        inspect_surrogate_document(owned_root / "absent.json", max_bytes=1000)
    assert excinfo.value.reason_code == "surrogate_document_unavailable"


# ---------------------------------------------------------------------------
# Evidence separation inside document validation
# ---------------------------------------------------------------------------


def test_model_card_claiming_fem_verification_is_refused(owned_root) -> None:
    card = _model_card()
    card["scientific_disposition"] = "fem_verified"
    from comsol_mcp.durable.canonical import canonical_sha256_v1

    body = {k: v for k, v in card.items() if k != "entry_sha256"}
    card["entry_sha256"] = canonical_sha256_v1(body)
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_model_card_document(card)
    assert excinfo.value.reason_code == "surrogate_document_claims_fem_evidence"


def test_model_card_must_attest_it_never_upgrades_evidence(owned_root) -> None:
    card = _model_card()
    card["never_upgrades_fem_evidence"] = False
    from comsol_mcp.durable.canonical import canonical_sha256_v1

    body = {k: v for k, v in card.items() if k != "entry_sha256"}
    card["entry_sha256"] = canonical_sha256_v1(body)
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_model_card_document(card)
    assert excinfo.value.reason_code == "surrogate_document_claims_fem_evidence"


def test_export_manifest_claiming_evidence_upgrade_is_refused() -> None:
    manifest = _export_manifest()
    manifest["upgrades_fem_evidence"] = True
    from comsol_mcp.durable.canonical import canonical_sha256_v1

    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    manifest["manifest_sha256"] = canonical_sha256_v1(body)
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_export_manifest_document(manifest)
    assert excinfo.value.reason_code == "surrogate_document_claims_fem_evidence"


def test_registry_entry_with_a_bad_artifact_hash_is_refused() -> None:
    entry = _registry_entry("c" * 64)
    entry["artifacts"] = {"onnx": "not-a-hash"}
    from comsol_mcp.durable.canonical import canonical_sha256_v1

    body = {k: v for k, v in entry.items() if k != "entry_sha256"}
    entry["entry_sha256"] = canonical_sha256_v1(body)
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_registry_entry_document(entry)
    assert excinfo.value.reason_code == "surrogate_document_field_invalid"


# ---------------------------------------------------------------------------
# Verification: only declared expectations, never vacuously satisfied
# ---------------------------------------------------------------------------


def test_verify_matches_declared_expectations(owned_root) -> None:
    path = _write(owned_root / "card.json", _model_card())
    result = verify_surrogate_document(
        path,
        max_bytes=1_000_000,
        expected_trained_chksum="1" * 64,
        expected_architecture_sha256="c" * 64,
        expected_dataset_manifest_sha256="a" * 64,
        expected_split_manifest_sha256="b" * 64,
    )
    assert result["verified"] is True
    assert result["mismatched_fields"] == []
    assert result["unavailable_fields"] == []
    assert result["upgrades_fem_evidence"] is False


def test_verify_reports_a_mismatch(owned_root) -> None:
    path = _write(owned_root / "card2.json", _model_card())
    result = verify_surrogate_document(
        path,
        max_bytes=1_000_000,
        expected_architecture_sha256="f" * 64,
    )
    assert result["verified"] is False
    assert result["mismatched_fields"] == ["architecture_sha256"]


def test_verify_without_expectations_is_not_verified(owned_root) -> None:
    path = _write(owned_root / "card.json", _model_card())
    result = verify_surrogate_document(path, max_bytes=1_000_000)
    # Nothing was declared, so nothing was proven; reporting success here would
    # be a vacuous pass.
    assert result["verified"] is False
    assert result["checked_count"] == 0
    assert all(
        check["state"] == "not_checked" for check in result["checks"]
    )


def test_verify_marks_an_unavailable_expectation_instead_of_passing(owned_root) -> None:
    card_path = _write(owned_root / "card.json", _model_card())
    result = verify_surrogate_document(
        card_path, max_bytes=1_000_000, expected_artifact_sha256="d" * 64
    )
    # A card carries no artifact list, so the expectation cannot be met.
    assert result["verified"] is False
    assert result["unavailable_fields"] == ["artifact_sha256"]


def test_verify_reads_artifact_hashes_from_either_layout(owned_root) -> None:
    entry = _registry_entry("c" * 64)
    entry_path = _write(owned_root / "entry.json", entry)
    result = verify_surrogate_document(
        entry_path, max_bytes=1_000_000, expected_artifact_sha256="d" * 64
    )
    assert result["verified"] is True

    manifest_path = _write(owned_root / "manifest.json", _export_manifest())
    result = verify_surrogate_document(
        manifest_path,
        max_bytes=1_000_000,
        expected_artifact_sha256="e" * 64,
        require_consistent_export=True,
    )
    assert result["verified"] is True


def test_require_consistent_export_demands_an_export_manifest(owned_root) -> None:
    card_path = _write(owned_root / "card.json", _model_card())
    result = verify_surrogate_document(
        card_path, max_bytes=1_000_000, require_consistent_export=True
    )
    assert result["verified"] is False
    assert result["unavailable_fields"] == ["consistent_export"]


def test_verify_reports_undeclared_expectations_as_not_checked(owned_root) -> None:
    path = _write(owned_root / "card.json", _model_card())
    result = verify_surrogate_document(
        path, max_bytes=1_000_000, expected_trained_chksum="1" * 64
    )
    states = {check["name"]: check["state"] for check in result["checks"]}
    assert states["trained_chksum"] == "matched"
    assert states["architecture_sha256"] == "not_checked"
    assert result["undeclared_expectations_reported_as_satisfied"] is False


# ---------------------------------------------------------------------------
# Dataset validation
# ---------------------------------------------------------------------------


def test_dataset_validate_accepts_a_headered_file(owned_root) -> None:
    path = owned_root / "data.csv"
    path.write_bytes(b"a1, a2, qoi\r\n1.0, 2.0, 3.0\r\n1.5, 2.5, 4.0\r\n")
    result = validate_dataset_document(
        path,
        max_bytes=100_000,
        max_rows=100,
        max_columns=10,
        expected_row_count=2,
        expected_feature_names=["a1", "a2"],
        expected_target_names=["qoi"],
    )
    assert result["valid"] is True
    assert result["row_count"] == 2
    assert result["column_count"] == 3
    assert result["header"] == ["a1", "a2", "qoi"]
    assert result["clipped"] is False


def test_dataset_validate_reports_a_headerless_file_honestly(owned_root) -> None:
    path = owned_root / "headerless.csv"
    path.write_bytes(b"1.0, 2.0, 3.0\r\n1.5, 2.5, 4.0\r\n")
    result = validate_dataset_document(
        path, max_bytes=100_000, max_rows=100, max_columns=10
    )
    assert result["header_present"] is False
    assert result["header"] is None
    assert result["row_count"] == 2

    # A declared column expectation cannot be met without a header, and that is
    # reported as unavailable rather than silently passing.
    result = validate_dataset_document(
        path,
        max_bytes=100_000,
        max_rows=100,
        max_columns=10,
        expected_feature_names=["a1", "a2"],
    )
    assert result["valid"] is False
    assert result["unavailable_fields"] == ["column_names"]


def test_dataset_validate_detects_a_row_count_mismatch(owned_root) -> None:
    path = owned_root / "data.csv"
    path.write_bytes(b"a,b\r\n1,2\r\n")
    result = validate_dataset_document(
        path, max_bytes=100_000, max_rows=100, max_columns=10, expected_row_count=5
    )
    assert result["valid"] is False
    assert result["mismatched_fields"] == ["row_count"]


def test_dataset_validate_detects_a_column_name_mismatch(owned_root) -> None:
    path = owned_root / "data.csv"
    path.write_bytes(b"x,y\r\n1,2\r\n")
    result = validate_dataset_document(
        path,
        max_bytes=100_000,
        max_rows=100,
        max_columns=10,
        expected_feature_names=["a1", "a2"],
    )
    assert result["valid"] is False
    assert result["mismatched_fields"] == ["column_names"]


def test_dataset_validate_refuses_ragged_rows(owned_root) -> None:
    path = owned_root / "ragged.csv"
    path.write_bytes(b"a,b,c\r\n1,2,3\r\n4,5\r\n")
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_dataset_document(path, max_bytes=100_000, max_rows=100, max_columns=10)
    assert excinfo.value.reason_code == "surrogate_dataset_ragged"


def test_dataset_validate_refuses_bounds_instead_of_clipping(owned_root) -> None:
    path = owned_root / "wide.csv"
    path.write_bytes(b"a,b,c,d\r\n1,2,3,4\r\n")
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_dataset_document(path, max_bytes=100_000, max_rows=100, max_columns=2)
    assert excinfo.value.reason_code == "surrogate_dataset_too_wide"

    long_path = owned_root / "long.csv"
    long_path.write_bytes(b"a,b\r\n" + b"1,2\r\n" * 10)
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_dataset_document(long_path, max_bytes=100_000, max_rows=3, max_columns=10)
    assert excinfo.value.reason_code == "surrogate_dataset_too_long"


def test_dataset_validate_refuses_empty_and_unterminated_quote(owned_root) -> None:
    empty = owned_root / "empty.csv"
    empty.write_bytes(b"")
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_dataset_document(empty, max_bytes=1000, max_rows=10, max_columns=10)
    assert excinfo.value.reason_code == "surrogate_document_empty"

    quoted = owned_root / "quoted.csv"
    quoted.write_bytes(b'a,b\r\n"unterminated,2\r\n')
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_dataset_document(quoted, max_bytes=1000, max_rows=10, max_columns=10)
    assert excinfo.value.reason_code == "surrogate_document_unparsable"


def test_dataset_validate_handles_quoted_fields_and_crlf(owned_root) -> None:
    path = owned_root / "quoted_ok.csv"
    path.write_bytes(b'a,b\r\n"x,y",2\r\n"he said ""hi""",3\r\n')
    result = validate_dataset_document(
        path, max_bytes=100_000, max_rows=10, max_columns=10
    )
    assert result["row_count"] == 2
    assert result["column_count"] == 2


def test_dataset_identity_is_a_content_hash(owned_root) -> None:
    payload = b"a,b\r\n1,2\r\n"
    path = owned_root / "data.csv"
    path.write_bytes(payload)
    result = validate_dataset_document(
        path, max_bytes=100_000, max_rows=10, max_columns=10
    )
    assert result["dataset_identity"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["dataset_identity"]["byte_count"] == len(payload)


# ---------------------------------------------------------------------------
# Prediction validation: never evidence
# ---------------------------------------------------------------------------


def test_prediction_validate_keeps_predictions_as_predictions(owned_root) -> None:
    path = owned_root / "pred.json"
    path.write_text(
        json.dumps(
            {
                "state": "predicted",
                "upgrades_fem_evidence": False,
                "predictions": [[1.0, 2.0], [3.0, 4.0]],
            }
        ),
        encoding="utf-8",
    )
    result = validate_prediction_document(
        path, max_bytes=100_000, max_rows=100, max_columns=10, ood_state="in_domain"
    )
    assert result["is_prediction"] is True
    assert result["is_fem_evidence"] is False
    assert result["upgrades_fem_evidence"] is False
    assert result["requires_fresh_fem"] is True
    assert result["row_count"] == 2


def test_prediction_validate_refuses_a_fem_evidence_claim(owned_root) -> None:
    for field, value in (
        ("is_fem_evidence", True),
        ("fem_evidence", {"state": "verified"}),
        ("measured_objective", 1.0),
        ("fem_artifact_sha256", "a" * 64),
    ):
        path = owned_root / f"claim_{field}.json"
        path.write_text(
            json.dumps({"state": "predicted", field: value, "predictions": [[1.0]]}),
            encoding="utf-8",
        )
        with pytest.raises(SurrogateEvidenceError) as excinfo:
            validate_prediction_document(
                path, max_bytes=100_000, max_rows=100, max_columns=10
            )
        assert excinfo.value.reason_code == "surrogate_prediction_claims_fem_evidence"


def test_prediction_validate_refuses_a_verified_state(owned_root) -> None:
    path = owned_root / "verified.json"
    path.write_text(
        json.dumps({"state": "verified", "predictions": [[1.0]]}), encoding="utf-8"
    )
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_prediction_document(
            path, max_bytes=100_000, max_rows=100, max_columns=10
        )
    assert excinfo.value.reason_code == "surrogate_prediction_state_invalid"


def test_prediction_validate_requires_fresh_fem_outside_in_domain(owned_root) -> None:
    path = owned_root / "pred.json"
    path.write_text(
        json.dumps({"state": "predicted", "predictions": [[1.0]]}), encoding="utf-8"
    )
    for ood_state, expected in (
        ("in_domain", False),
        ("edge", True),
        ("out_of_domain", True),
        ("uncalibrated", True),
    ):
        result = validate_prediction_document(
            path, max_bytes=100_000, max_rows=100, max_columns=10, ood_state=ood_state
        )
        assert result["escalation_required"] is expected, ood_state
        assert result["requires_fresh_fem"] is True
    # No declared state means escalation cannot be ruled out.
    result = validate_prediction_document(
        path, max_bytes=100_000, max_rows=100, max_columns=10
    )
    assert result["escalation_required"] is True


def test_prediction_validate_accepts_rows_key_and_alternate_layout(owned_root) -> None:
    path = owned_root / "rows.json"
    path.write_text(
        json.dumps({"rows": [[1.0, 2.0]]}), encoding="utf-8"
    )
    result = validate_prediction_document(
        path, max_bytes=100_000, max_rows=100, max_columns=10
    )
    assert result["row_count"] == 1


def test_prediction_validate_refuses_bad_rows(owned_root) -> None:
    missing = owned_root / "missing.json"
    missing.write_text(json.dumps({"state": "predicted"}), encoding="utf-8")
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_prediction_document(
            missing, max_bytes=100_000, max_rows=100, max_columns=10
        )
    assert excinfo.value.reason_code == "surrogate_document_field_missing"

    nonnumeric = owned_root / "nonnumeric.json"
    nonnumeric.write_text(
        json.dumps({"predictions": [["x"]]}), encoding="utf-8"
    )
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_prediction_document(
            nonnumeric, max_bytes=100_000, max_rows=100, max_columns=10
        )
    assert excinfo.value.reason_code == "surrogate_document_field_invalid"

    nonfinite = owned_root / "nonfinite.json"
    nonfinite.write_text(
        json.dumps({"predictions": [[1.0, 2.0], [1.0]]}), encoding="utf-8"
    )
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        validate_prediction_document(
            nonfinite, max_bytes=100_000, max_rows=100, max_columns=10
        )
    assert excinfo.value.reason_code == "surrogate_dataset_ragged"


# ---------------------------------------------------------------------------
# Training preview
# ---------------------------------------------------------------------------


def _configuration(**overrides) -> dict:
    base = {
        "configuration_id": "p1",
        "input_features": ["a1", "a2"],
        "output_features": ["qoi"],
        "hidden_layers": [16, 8],
        "activation": "tanh",
        "optimizer": "adam",
        "loss": "mse",
        "learning_rate": 1e-2,
        "batch_size": 8,
        "maximum_epochs": 60,
        "seed": 17,
        "validation_mode": "fraction",
        "test_mode": "random",
    }
    base.update(overrides)
    return base


def test_training_preview_never_trains_or_starts_a_solver(owned_root) -> None:
    preview = preview_training_configuration(
        _configuration(), max_bytes=100_000, max_rows=100, max_columns=10
    )
    assert preview["training_performed"] is False
    assert preview["solver_started"] is False
    assert preview["filesystem_modified"] is False
    assert preview["upgrades_fem_evidence"] is False
    assert preview["configuration_sha256"]
    assert preview["dataset_bound"] is False


def test_training_preview_reports_the_deferred_args_write(owned_root) -> None:
    preview = preview_training_configuration(
        _configuration(), max_bytes=100_000, max_rows=100, max_columns=10
    )
    deferred = preview["write_plan"]["deferred_writes"]
    assert [item["property"] for item in deferred] == ["args"]
    assert deferred[0]["reason"] == "data_source_not_bound"
    # The write plan is inspectable before anything is bound.
    assert "activation" in preview["write_plan"]["properties"]
    assert "layertype" in preview["write_plan"]["properties"]


def test_training_preview_binds_a_dataset_when_supplied(owned_root) -> None:
    path = owned_root / "data.csv"
    path.write_bytes(b"a1, a2, qoi\r\n1.0, 2.0, 3.0\r\n")
    preview = preview_training_configuration(
        _configuration(),
        dataset_path=str(path),
        max_bytes=100_000,
        max_rows=100,
        max_columns=10,
        declared_fem_row_count=1,
    )
    assert preview["dataset_bound"] is True
    assert preview["dataset_column_match"] is True
    assert preview["row_count_evidence"]["matches"] is True
    assert preview["row_count_evidence"]["declared_fem_row_count"] == 1


def test_training_preview_reports_a_column_mismatch(owned_root) -> None:
    path = owned_root / "mismatch.csv"
    path.write_bytes(b"x, y, z\r\n1.0, 2.0, 3.0\r\n")
    preview = preview_training_configuration(
        _configuration(),
        dataset_path=str(path),
        max_bytes=100_000,
        max_rows=100,
        max_columns=10,
    )
    assert preview["dataset_column_match"] is False


def test_training_preview_refuses_an_invalid_configuration(owned_root) -> None:
    with pytest.raises(SurrogateEvidenceError) as excinfo:
        preview_training_configuration(
            _configuration(hidden_layers=[0]),
            max_bytes=100_000,
            max_rows=100,
            max_columns=10,
        )
    assert excinfo.value.reason_code == "surrogate_configuration_invalid"

    with pytest.raises(SurrogateEvidenceError) as excinfo:
        preview_training_configuration(
            {"configuration_id": "p1"}, max_bytes=100_000, max_rows=100, max_columns=10
        )
    assert excinfo.value.reason_code == "surrogate_configuration_invalid"


# ---------------------------------------------------------------------------
# Tool surface: catalog and profile membership
# ---------------------------------------------------------------------------


def test_surrogate_tools_are_catalogued_as_solver_free_read_only() -> None:
    from comsol_mcp.tools.catalog import TOOL_SPECS, validate_tool_specs

    validate_tool_specs()
    for name in SURROGATE_TOOLS:
        spec = TOOL_SPECS[name]
        assert spec.side_effect_class == "read_only", name
        assert spec.concurrency_class == "solver_free", name
        assert spec.starts_solver is False, name
        assert spec.requires_model_revision is False, name
        assert spec.feature_gate is None, name
        assert spec.group == "surrogate_evidence", name


def test_surrogate_tools_are_present_in_every_profile() -> None:
    for profile in ALL_PROFILES:
        server = create_server(f"surrogate-surface-{profile}", profile=profile)
        listed = {tool.name for tool in asyncio.run(server.list_tools())}
        assert SURROGATE_TOOLS <= listed, profile


def test_surrogate_tools_do_not_enter_the_frozen_comsolless_profile() -> None:
    server = create_server("surrogate-comsolless", profile="comsolless_read_only")
    listed = {tool.name for tool in asyncio.run(server.list_tools())}
    assert listed == {
        "mph_inspect",
        "mph_diff",
        "model_identity",
        "runtime_compatibility_status",
        "offline_export_validate",
    }
    assert not (SURROGATE_TOOLS & listed)


# ---------------------------------------------------------------------------
# Real dispatch
# ---------------------------------------------------------------------------


def _server(owned_root) -> object:
    return create_server("surrogate-dispatch", profile="core")


def test_dispatch_validates_real_documents(owned_root) -> None:
    server = _server(owned_root)
    card_path = _write(owned_root / "card.json", _model_card())
    manifest_path = _write(owned_root / "manifest.json", _export_manifest())
    data_path = owned_root / "data.csv"
    data_path.write_bytes(b"a1, a2, qoi\r\n1.0, 2.0, 3.0\r\n")
    pred_path = owned_root / "pred.json"
    pred_path.write_text(
        json.dumps({"state": "predicted", "predictions": [[1.0, 2.0]]}),
        encoding="utf-8",
    )

    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_dataset_validate",
                {
                    "dataset_path": str(data_path),
                    "expected_row_count": 1,
                    "expected_feature_names": ["a1", "a2"],
                    "expected_target_names": ["qoi"],
                },
            )
        )
    )
    assert result["success"] is True
    assert result["valid"] is True
    assert result["solver_started"] is False
    assert result["path_policy"]["accepted"] is True

    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_model_inspect", {"document_path": str(card_path)}
            )
        )
    )
    assert result["success"] is True
    assert result["summary"]["document_kind"] == "model_card"

    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_model_inspect",
                {"document_path": str(card_path), "document_kind": "export_manifest"},
            )
        )
    )
    assert result["success"] is False
    assert result["reason_code"] == "surrogate_document_kind_mismatch"

    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_model_verify",
                {
                    "document_path": str(manifest_path),
                    "expected_artifact_sha256": "e" * 64,
                    "require_consistent_export": True,
                },
            )
        )
    )
    assert result["success"] is True
    assert result["verified"] is True

    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_prediction_validate",
                {"prediction_path": str(pred_path), "ood_state": "in_domain"},
            )
        )
    )
    assert result["success"] is True
    assert result["is_fem_evidence"] is False
    assert result["requires_fresh_fem"] is True

    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_training_preview",
                {"configuration": _configuration(), "dataset_path": str(data_path)},
            )
        )
    )
    assert result["success"] is True
    assert result["training_performed"] is False
    assert result["solver_started"] is False


def test_dispatch_refuses_a_path_outside_the_owned_root(owned_root) -> None:
    outside = Path("D:/mcp_tests/a75s10outside")
    outside.mkdir(parents=True, exist_ok=True)
    target = outside / "data.csv"
    target.write_bytes(b"a,b\r\n1,2\r\n")
    server = _server(owned_root)
    for tool_name, argument in (
        ("surrogate_dataset_validate", "dataset_path"),
        ("surrogate_model_inspect", "document_path"),
        ("surrogate_model_verify", "document_path"),
        ("surrogate_prediction_validate", "prediction_path"),
    ):
        result = decode_tool_result(
            asyncio.run(server.call_tool(tool_name, {argument: str(target)}))
        )
        assert result["success"] is False, tool_name
        assert result["path_policy"]["accepted"] is False, tool_name
        assert result["path_policy"]["enforced"] is True, tool_name


def test_dispatch_refuses_a_missing_file(owned_root) -> None:
    server = _server(owned_root)
    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_model_inspect",
                {"document_path": str(owned_root / "absent.json")},
            )
        )
    )
    assert result["success"] is False
    assert result["path_policy"]["accepted"] is False


def test_dispatch_refuses_an_invalid_input_contract(owned_root) -> None:
    server = _server(owned_root)
    result = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "surrogate_dataset_validate",
                {
                    "dataset_path": str(owned_root / "data.csv"),
                    "expected_feature_names": ["a", "a"],
                },
            )
        )
    )
    assert result["success"] is False


def test_dispatch_stays_solver_free_and_reports_no_filesystem_write(owned_root) -> None:
    server = _server(owned_root)
    card_path = _write(owned_root / "card.json", _model_card())
    before = sorted(p.name for p in owned_root.iterdir())
    result = decode_tool_result(
        asyncio.run(
            server.call_tool("surrogate_model_inspect", {"document_path": str(card_path)})
        )
    )
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert sorted(p.name for p in owned_root.iterdir()) == before


# ---------------------------------------------------------------------------
# Cold discovery: no heavy optional import
# ---------------------------------------------------------------------------


def test_cold_discovery_loads_no_heavy_optional_module(owned_root) -> None:
    code = """
import asyncio, json, sys
from pathlib import Path
from src.server import create_server
server = create_server('surrogate-cold', profile='core')
names = sorted(t.name for t in asyncio.run(server.list_tools()))
banned = ('mph', 'jpype', 'torch', 'tensorflow', 'onnx', 'numpy', 'chromadb',
          'sentence_transformers')
print(json.dumps({'surrogate': [n for n in names if n.startswith('surrogate_')],
                  'loaded': [n for n in banned if n in sys.modules]}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload["surrogate"]) == SURROGATE_TOOLS
    assert payload["loaded"] == []


# ---------------------------------------------------------------------------
# Solver-free guard on the new modules
# ---------------------------------------------------------------------------

BANNED_SOURCE = (
    "import mph",
    "from mph",
    "import jpype",
    "from jpype",
    "import comsol",
    "from comsol.",
    "import torch",
    "import tensorflow",
    "import numpy",
    "import onnx",
)


def _import_statements(module_path: str) -> list[str]:
    """Return only real import statements, ignoring prose in docstrings.

    A naive substring search over the whole file also matches the sentence in a
    module docstring that says the module never imports MPh, which would make the
    guard fail on its own documentation.  Parsing the AST is exact.
    """
    import ast

    tree = ast.parse(open(module_path, encoding="utf-8").read())
    statements: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                statements.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                statements.append(f"from {node.module}")
    return statements


@pytest.mark.parametrize(
    "module_name",
    [
        "comsol_mcp.tools.surrogate",
        "comsol_mcp.evidence.surrogate_evidence",
        "comsol_mcp.contracts.surrogate",
    ],
)
def test_new_modules_are_solver_free(module_name: str) -> None:
    from importlib import import_module

    module = import_module(module_name)
    statements = _import_statements(module.__file__)
    for statement in statements:
        lowered = statement.lower()
        for banned in BANNED_SOURCE:
            assert not lowered.startswith(banned), (
                f"{module_name} imports {banned!r}"
            )


def test_the_solver_free_guard_detects_a_real_import(tmp_path) -> None:
    """The guard must actually fail on a banned import, not merely pass."""
    source = tmp_path / "sneaky.py"
    source.write_text("import mph\n", encoding="utf-8")
    statements = _import_statements(str(source))
    assert statements == ["import mph"]
    assert any(
        statement.lower().startswith("import mph") for statement in statements
    )


def test_tool_module_never_imports_mphe_or_starts_a_solver() -> None:
    import comsol_mcp.tools.surrogate as module

    source = open(module.__file__, encoding="utf-8").read()
    assert "session_manager" not in source
    assert "study_solve" not in source
    assert "SolverOwnership" not in source
