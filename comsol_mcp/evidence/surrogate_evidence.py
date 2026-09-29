"""Bounded, solver-free validation of surrogate artifacts.

This module implements the read-only logic behind the public surrogate tools.  It
never starts COMSOL, Java, MPh, or JPype, never acquires a solver lease, and never
promotes a prediction to FEM evidence.

Design rules that the tests enforce:

* Every document is read through a caller-supplied already-contained path and is
  size-bounded before parsing; an oversized document is refused, never clipped.
* Every document is validated against its own declared schema by re-deriving the
  canonical hash, so a tampered document cannot pass inspection.
* A document is inspected, never rewritten.  Nothing here writes to disk.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from comsol_mcp.contracts.surrogate import parse_dbmodel_uri, validate_source_reference
from comsol_mcp.durable.canonical import canonical_sha256_v1
from comsol_mcp.durable.io import read_file_bytes_bounded

SURROGATE_EVIDENCE_SCHEMA_NAME = "comsol_mcp.surrogate_evidence_verdict"
SURROGATE_EVIDENCE_SCHEMA_VERSION = "1.0.0"

# Document schemas this layer recognizes.  An unrecognized schema is refused
# rather than partially interpreted.
EXPORT_MANIFEST_SCHEMA = "comsol_mcp.surrogate_export_manifest"
MODEL_CARD_SCHEMA = "comsol_mcp.surrogate_model_card"
REGISTRY_ENTRY_SCHEMA = "comsol_mcp.surrogate_registry_entry"

DOCUMENT_KINDS = {
    EXPORT_MANIFEST_SCHEMA: "export_manifest",
    MODEL_CARD_SCHEMA: "model_card",
    REGISTRY_ENTRY_SCHEMA: "registry_entry",
}

# A prediction document must not carry any of these fields: their presence would
# mean a prediction is being presented as a measurement.
FEM_EVIDENCE_FIELDS = (
    "fem_evidence",
    "fem_result",
    "fem_artifact_sha256",
    "measured_objective",
    "is_fem_evidence",
    "upgrades_fem_evidence",
)

PREDICTION_STATES = ("predicted", "unverified")


class SurrogateEvidenceError(ValueError):
    """Raised when a surrogate artifact cannot be validated."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def read_bounded_document(
    path: str | Path, *, max_bytes: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read and parse one bounded JSON document, returning it with its identity.

    The document must be a JSON object.  A non-object, non-JSON, oversized, or
    empty document is refused with a specific reason code.
    """
    resolved = Path(path)
    if not resolved.is_file():
        raise SurrogateEvidenceError(
            "surrogate_document_unavailable", "The surrogate document is not readable."
        )
    try:
        payload = read_file_bytes_bounded(resolved, max_bytes=max_bytes)
    except ValueError as exc:
        raise SurrogateEvidenceError(
            "surrogate_document_too_large",
            "The surrogate document exceeds the declared reading limit.",
        ) from exc
    if not payload:
        raise SurrogateEvidenceError("surrogate_document_empty", "The surrogate document is empty.")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SurrogateEvidenceError(
            "surrogate_document_unparsable", "The surrogate document is not valid JSON object data."
        ) from exc
    if not isinstance(document, dict):
        raise SurrogateEvidenceError(
            "surrogate_document_not_an_object",
            "The surrogate document must be a JSON object.",
        )
    identity = {
        "byte_count": len(payload),
        "sha256": _sha256_bytes(payload),
        "path_name": resolved.name,
    }
    return document, identity


def identify_document_kind(document: Mapping[str, Any]) -> str:
    """Return the declared document kind, refusing an unrecognized schema."""
    schema = document.get("schema") or document.get("schema_name")
    if not isinstance(schema, str) or not schema:
        raise SurrogateEvidenceError(
            "surrogate_document_schema_missing",
            "The surrogate document does not declare a schema.",
        )
    kind = DOCUMENT_KINDS.get(schema)
    if kind is None:
        raise SurrogateEvidenceError(
            "surrogate_document_kind_unsupported",
            "The surrogate document schema is not supported by this tool.",
        )
    return kind


def _require_hex64(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise SurrogateEvidenceError(
            "surrogate_document_field_invalid", f"{name} must be a 64-character hex digest."
        )
    try:
        int(value, 16)
    except ValueError as exc:
        raise SurrogateEvidenceError(
            "surrogate_document_field_invalid", f"{name} must be a 64-character hex digest."
        ) from exc
    return value.lower()


def validate_export_manifest_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an export manifest by re-deriving its canonical hash."""
    declared = document.get("manifest_sha256")
    body = {key: value for key, value in document.items() if key != "manifest_sha256"}
    if not isinstance(declared, str) or declared != canonical_sha256_v1(body):
        raise SurrogateEvidenceError(
            "surrogate_document_hash_mismatch",
            "The export manifest hash does not match its content.",
        )
    if document.get("upgrades_fem_evidence") is not False:
        raise SurrogateEvidenceError(
            "surrogate_document_claims_fem_evidence",
            "An export manifest must never claim to upgrade FEM evidence.",
        )
    artifacts = document.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SurrogateEvidenceError(
            "surrogate_document_field_invalid", "An export manifest must list its artifacts."
        )
    checked: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            raise SurrogateEvidenceError(
                "surrogate_document_field_invalid", f"artifacts[{index}] must be an object."
            )
        checked.append(
            {
                "export_format": str(artifact.get("export_format")),
                "artifact_name": str(artifact.get("artifact_name")),
                "size_bytes": int(artifact.get("size_bytes", 0)),
                "sha256": _require_hex64(f"artifacts[{index}].sha256", artifact.get("sha256")),
            }
        )
    return {
        "document_kind": "export_manifest",
        "manifest_sha256": declared,
        "trained_chksum": document.get("trained_chksum"),
        "architecture_sha256": document.get("architecture_sha256"),
        "artifacts": checked,
        "artifact_count": len(checked),
        "onnx_available": bool(document.get("onnx_available")),
        "hash_verified": True,
        "upgrades_fem_evidence": False,
    }


def _identity_value(document: Mapping[str, Any], name: str) -> Any:
    """Resolve one identity from the top level or from a nested ``identities``.

    A model card carries its identities nested under ``identities`` while an
    export manifest carries several at the top level, so both layouts are
    resolved here.  Returning ``None`` for an absent identity keeps the caller
    honest: an absent expectation resolves to ``unavailable``, never to a match.
    """
    if name in document:
        return document.get(name)
    identities = document.get("identities")
    if isinstance(identities, Mapping):
        return identities.get(name)
    return None


def validate_model_card_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a model card by re-deriving its declared identity hash."""
    declared = document.get("entry_sha256")
    if not isinstance(declared, str):
        raise SurrogateEvidenceError(
            "surrogate_document_hash_missing", "The model card does not declare its hash."
        )
    body = {key: value for key, value in document.items() if key != "entry_sha256"}
    if declared != canonical_sha256_v1(body):
        raise SurrogateEvidenceError(
            "surrogate_document_hash_mismatch",
            "The model card hash does not match its content.",
        )
    # A surrogate model card may never assert FEM verification, and it must
    # explicitly attest that it does not upgrade FEM evidence.
    if document.get("scientific_disposition") == "fem_verified":
        raise SurrogateEvidenceError(
            "surrogate_document_claims_fem_evidence",
            "A model card must not claim FEM verification.",
        )
    if document.get("never_upgrades_fem_evidence") is not True:
        raise SurrogateEvidenceError(
            "surrogate_document_claims_fem_evidence",
            "A model card must attest that it never upgrades FEM evidence.",
        )
    identities = document.get("identities")
    if not isinstance(identities, Mapping):
        raise SurrogateEvidenceError(
            "surrogate_document_field_invalid", "A model card must declare its identities."
        )
    return {
        "document_kind": "model_card",
        "entry_sha256": declared,
        "trained_chksum": document.get("trained_chksum"),
        "architecture_sha256": _identity_value(document, "architecture_sha256"),
        "dataset_manifest_sha256": _identity_value(document, "dataset_manifest_sha256"),
        "split_manifest_sha256": _identity_value(document, "split_manifest_sha256"),
        "identity_names": sorted(str(name) for name in identities),
        "scientific_disposition": document.get("scientific_disposition"),
        "lifecycle_state": document.get("state"),
        "hash_verified": True,
        "upgrades_fem_evidence": False,
    }


def validate_registry_entry_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a registry entry by re-deriving its declared identity hash."""
    declared = document.get("entry_sha256")
    if not isinstance(declared, str):
        raise SurrogateEvidenceError(
            "surrogate_document_hash_missing", "The registry entry does not declare its hash."
        )
    body = {key: value for key, value in document.items() if key != "entry_sha256"}
    if declared != canonical_sha256_v1(body):
        raise SurrogateEvidenceError(
            "surrogate_document_hash_mismatch",
            "The registry entry hash does not match its content.",
        )
    history = document.get("history")
    if history is not None and not isinstance(history, list):
        raise SurrogateEvidenceError(
            "surrogate_document_field_invalid", "The registry history must be a list."
        )
    artifacts = document.get("artifacts")
    if artifacts is not None and not isinstance(artifacts, Mapping):
        raise SurrogateEvidenceError(
            "surrogate_document_field_invalid",
            "The registry artifacts must be a name-to-hash mapping.",
        )
    artifact_hashes: list[str] = []
    for name, value in (artifacts or {}).items():
        artifact_hashes.append(_require_hex64(f"artifacts.{name}", value))
    return {
        "document_kind": "registry_entry",
        "entry_sha256": declared,
        "trained_chksum": document.get("trained_chksum"),
        "architecture_sha256": _identity_value(document, "architecture_sha256"),
        "model_card_sha256": document.get("model_card_sha256"),
        "lifecycle_state": document.get("state"),
        "history_length": len(history) if isinstance(history, list) else 0,
        "artifact_hashes": artifact_hashes,
        "hash_verified": True,
        "upgrades_fem_evidence": False,
    }


def _collect_artifact_hashes(document: Mapping[str, Any]) -> list[str]:
    """Collect every artifact content hash a surrogate document carries.

    An export manifest lists artifacts while a registry entry maps artifact
    names to hashes, so both layouts are read and empty or malformed entries are
    skipped rather than reported as a hash.
    """
    observed: list[str] = []
    artifacts = document.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, Mapping):
                value = item.get("sha256")
                if isinstance(value, str) and len(value) == 64:
                    observed.append(value)
    elif isinstance(artifacts, Mapping):
        for value in artifacts.values():
            if isinstance(value, str) and len(value) == 64:
                observed.append(value)
    return observed


_DOCUMENT_VALIDATORS = {
    "export_manifest": validate_export_manifest_document,
    "model_card": validate_model_card_document,
    "registry_entry": validate_registry_entry_document,
}


def inspect_surrogate_document(path: str | Path, *, max_bytes: int) -> dict[str, Any]:
    """Inspect one surrogate document without modifying it."""
    document, identity = read_bounded_document(path, max_bytes=max_bytes)
    kind = identify_document_kind(document)
    summary = _DOCUMENT_VALIDATORS[kind](document)
    return {
        "schema_name": SURROGATE_EVIDENCE_SCHEMA_NAME,
        "schema_version": SURROGATE_EVIDENCE_SCHEMA_VERSION,
        "success": True,
        "document_identity": identity,
        "summary": summary,
        "solver_started": False,
        "filesystem_modified": False,
    }


def verify_surrogate_document(
    path: str | Path,
    *,
    max_bytes: int,
    expected_trained_chksum: str | None = None,
    expected_artifact_sha256: str | None = None,
    expected_architecture_sha256: str | None = None,
    expected_dataset_manifest_sha256: str | None = None,
    expected_split_manifest_sha256: str | None = None,
    require_consistent_export: bool = False,
) -> dict[str, Any]:
    """Verify a surrogate document against only the caller's declared expectations.

    An undeclared expectation is reported as ``not_checked``; it is never
    reported as satisfied.
    """
    document, identity = read_bounded_document(path, max_bytes=max_bytes)
    kind = identify_document_kind(document)
    _DOCUMENT_VALIDATORS[kind](document)

    checks: list[dict[str, Any]] = []

    def _check(name: str, expected: Any, actual: Any) -> None:
        if expected is None:
            checks.append({"name": name, "state": "not_checked", "matched": None})
            return
        matched = actual is not None and str(actual) == str(expected)
        checks.append(
            {
                "name": name,
                "state": "matched" if matched else "mismatched",
                "matched": matched,
                "expected": str(expected),
                "observed": None if actual is None else str(actual),
            }
        )

    _check("trained_chksum", expected_trained_chksum, document.get("trained_chksum"))
    _check(
        "architecture_sha256",
        expected_architecture_sha256,
        _identity_value(document, "architecture_sha256"),
    )
    _check(
        "dataset_manifest_sha256",
        expected_dataset_manifest_sha256,
        _identity_value(document, "dataset_manifest_sha256"),
    )
    _check(
        "split_manifest_sha256",
        expected_split_manifest_sha256,
        _identity_value(document, "split_manifest_sha256"),
    )

    # A content hash expectation is checked against whichever artifact hashes the
    # document actually carries, so the check cannot pass vacuously.
    if expected_artifact_sha256 is None:
        checks.append({"name": "artifact_sha256", "state": "not_checked", "matched": None})
    else:
        observed = _collect_artifact_hashes(document)
        if not observed:
            checks.append(
                {
                    "name": "artifact_sha256",
                    "state": "unavailable",
                    "matched": False,
                    "expected": str(expected_artifact_sha256),
                    "observed": None,
                }
            )
        else:
            matched = str(expected_artifact_sha256).lower() in {value.lower() for value in observed}
            checks.append(
                {
                    "name": "artifact_sha256",
                    "state": "matched" if matched else "mismatched",
                    "matched": matched,
                    "expected": str(expected_artifact_sha256),
                    "observed": observed,
                }
            )

    if require_consistent_export:
        if kind != "export_manifest":
            checks.append(
                {
                    "name": "consistent_export",
                    "state": "unavailable",
                    "matched": False,
                    "reason": "document_is_not_an_export_manifest",
                }
            )
        else:
            consistent = bool(document.get("artifacts")) and document.get("onnx_available") is True
            checks.append(
                {
                    "name": "consistent_export",
                    "state": "matched" if consistent else "mismatched",
                    "matched": consistent,
                }
            )
    else:
        checks.append({"name": "consistent_export", "state": "not_checked", "matched": None})

    mismatched = [check["name"] for check in checks if check["state"] == "mismatched"]
    unavailable = [check["name"] for check in checks if check["state"] == "unavailable"]
    checked = [check for check in checks if check["state"] != "not_checked"]
    verified = not mismatched and not unavailable and bool(checked)
    return {
        "schema_name": SURROGATE_EVIDENCE_SCHEMA_NAME,
        "schema_version": SURROGATE_EVIDENCE_SCHEMA_VERSION,
        "success": True,
        "document_identity": identity,
        "document_kind": kind,
        "verified": verified,
        "checks": checks,
        "checked_count": len(checked),
        "mismatched_fields": mismatched,
        "unavailable_fields": unavailable,
        "undeclared_expectations_reported_as_satisfied": False,
        "upgrades_fem_evidence": False,
        "solver_started": False,
        "filesystem_modified": False,
    }


def _parse_csv_rows(payload: str) -> list[list[str]]:
    """Parse bounded CSV text without a third-party dependency.

    A minimal RFC4180-subset reader: comma separated, optional double quotes, and
    CRLF or LF line endings.  It refuses an unterminated quote rather than
    guessing at the remaining data.
    """
    rows: list[list[str]] = []
    field: list[str] = []
    row: list[str] = []
    in_quotes = False
    index = 0
    length = len(payload)
    while index < length:
        character = payload[index]
        if in_quotes:
            if character == '"':
                if index + 1 < length and payload[index + 1] == '"':
                    field.append('"')
                    index += 2
                    continue
                in_quotes = False
                index += 1
                continue
            field.append(character)
            index += 1
            continue
        if character == '"':
            in_quotes = True
            index += 1
            continue
        if character == ",":
            row.append("".join(field).strip())
            field = []
            index += 1
            continue
        if character in "\r\n":
            row.append("".join(field).strip())
            rows.append(row)
            field = []
            row = []
            if character == "\r" and index + 1 < length and payload[index + 1] == "\n":
                index += 2
            else:
                index += 1
            continue
        field.append(character)
        index += 1
    if in_quotes:
        raise SurrogateEvidenceError(
            "surrogate_document_unparsable", "The dataset contains an unterminated quote."
        )
    if field or row:
        row.append("".join(field).strip())
        rows.append(row)
    return [entry for entry in rows if entry and any(cell != "" for cell in entry)]


def validate_dataset_document(
    path: str | Path,
    *,
    max_bytes: int,
    max_rows: int,
    max_columns: int,
    expected_row_count: int | None = None,
    expected_feature_names: Sequence[str] | None = None,
    expected_target_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate one surrogate dataset file's shape and content identity.

    A dataset with a text header is inspected using that header; a headerless
    dataset is reported as headerless rather than having one invented.  The file
    is never rewritten and never clipped to satisfy a declared bound.
    """
    resolved = Path(path)
    if not resolved.is_file():
        raise SurrogateEvidenceError(
            "surrogate_document_unavailable", "The dataset is not readable."
        )
    try:
        payload = read_file_bytes_bounded(resolved, max_bytes=max_bytes)
    except ValueError as exc:
        raise SurrogateEvidenceError(
            "surrogate_document_too_large",
            "The dataset exceeds the declared reading limit.",
        ) from exc
    if not payload:
        raise SurrogateEvidenceError("surrogate_document_empty", "The dataset is empty.")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SurrogateEvidenceError(
            "surrogate_document_unparsable", "The dataset is not valid UTF-8 text."
        ) from exc
    rows = _parse_csv_rows(text)
    if not rows:
        raise SurrogateEvidenceError("surrogate_dataset_empty", "The dataset contains no rows.")
    width = len(rows[0])
    if width == 0:
        raise SurrogateEvidenceError("surrogate_dataset_empty", "The dataset has no columns.")
    if width > max_columns:
        raise SurrogateEvidenceError(
            "surrogate_dataset_too_wide",
            "The dataset exceeds the declared column limit.",
        )
    if len(rows) > max_rows:
        raise SurrogateEvidenceError(
            "surrogate_dataset_too_long",
            "The dataset exceeds the declared row limit.",
        )
    ragged = [index for index, row in enumerate(rows) if len(row) != width]
    if ragged:
        raise SurrogateEvidenceError(
            "surrogate_dataset_ragged",
            "The dataset rows do not all have the same column count.",
        )

    header_is_text = not all(_is_number(cell) for cell in rows[0])
    header = rows[0] if header_is_text else None
    data_rows = rows[1:] if header_is_text else rows

    feature_names = list(expected_feature_names or ())
    target_names = list(expected_target_names or ())
    declared_names = feature_names + target_names

    checks: list[dict[str, Any]] = []
    if expected_row_count is not None:
        checks.append(
            {
                "name": "row_count",
                "state": "matched" if len(data_rows) == expected_row_count else "mismatched",
                "expected": expected_row_count,
                "observed": len(data_rows),
            }
        )
    else:
        checks.append({"name": "row_count", "state": "not_checked"})

    if declared_names:
        if header is None:
            checks.append(
                {
                    "name": "column_names",
                    "state": "unavailable",
                    "reason": "dataset_is_headerless",
                    "observed": None,
                }
            )
        else:
            matched = list(header) == declared_names
            checks.append(
                {
                    "name": "column_names",
                    "state": "matched" if matched else "mismatched",
                    "expected": declared_names,
                    "observed": list(header),
                }
            )
    else:
        checks.append({"name": "column_names", "state": "not_checked"})

    mismatched = [check["name"] for check in checks if check["state"] == "mismatched"]
    unavailable = [check["name"] for check in checks if check["state"] == "unavailable"]
    checked = [check for check in checks if check["state"] != "not_checked"]
    content_sha256 = _sha256_bytes(payload)
    return {
        "schema_name": SURROGATE_EVIDENCE_SCHEMA_NAME,
        "schema_version": SURROGATE_EVIDENCE_SCHEMA_VERSION,
        "success": True,
        "dataset_identity": {
            "path_name": resolved.name,
            "byte_count": len(payload),
            "sha256": content_sha256,
        },
        "row_count": len(data_rows),
        "column_count": width,
        "header_present": header is not None,
        "header": list(header) if header is not None else None,
        "checks": checks,
        "checked_count": len(checked),
        "mismatched_fields": mismatched,
        "unavailable_fields": unavailable,
        "valid": not mismatched and not unavailable,
        "clipped": False,
        "upgrades_fem_evidence": False,
        "solver_started": False,
        "filesystem_modified": False,
    }


def _is_number(value: str) -> bool:
    try:
        number = float(value)
    except TypeError, ValueError:
        return False
    return math.isfinite(number)


def validate_prediction_document(
    path: str | Path,
    *,
    max_bytes: int,
    max_rows: int,
    max_columns: int,
    absolute_tolerance: float = 0.0,
    relative_tolerance: float = 0.0,
    ood_state: str | None = None,
) -> dict[str, Any]:
    """Validate one prediction row set while refusing to call it evidence.

    A prediction document that carries an FEM evidence field is refused, because
    that is exactly the confusion this tool exists to prevent.
    """
    document, identity = read_bounded_document(path, max_bytes=max_bytes)
    present_evidence_fields = [field for field in FEM_EVIDENCE_FIELDS if field in document]
    # `upgrades_fem_evidence: false` is the only acceptable FEM-related field.
    if "upgrades_fem_evidence" in present_evidence_fields:
        if document.get("upgrades_fem_evidence") is False:
            present_evidence_fields.remove("upgrades_fem_evidence")
    if present_evidence_fields:
        raise SurrogateEvidenceError(
            "surrogate_prediction_claims_fem_evidence",
            "A prediction row set must not carry FEM evidence fields: "
            + ", ".join(sorted(present_evidence_fields)),
        )

    rows = document.get("predictions", document.get("rows"))
    if rows is None:
        raise SurrogateEvidenceError(
            "surrogate_document_field_missing",
            "A prediction document must declare its rows under 'predictions' or 'rows'.",
        )
    if not isinstance(rows, list) or not rows:
        raise SurrogateEvidenceError(
            "surrogate_document_field_invalid", "Prediction rows must be a non-empty list."
        )
    if len(rows) > max_rows:
        raise SurrogateEvidenceError(
            "surrogate_dataset_too_long", "The prediction row count exceeds the declared limit."
        )

    numeric_rows: list[list[float]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise SurrogateEvidenceError(
                "surrogate_document_field_invalid", f"predictions[{index}] must be a sequence."
            )
        values: list[float] = []
        for cell in row:
            if isinstance(cell, bool) or not isinstance(cell, (int, float)):
                raise SurrogateEvidenceError(
                    "surrogate_document_field_invalid",
                    f"predictions[{index}] must contain numbers.",
                )
            number = float(cell)
            if not math.isfinite(number):
                raise SurrogateEvidenceError(
                    "surrogate_document_field_invalid",
                    f"predictions[{index}] must contain finite numbers.",
                )
            values.append(number)
        if not values:
            raise SurrogateEvidenceError(
                "surrogate_document_field_invalid", f"predictions[{index}] must be non-empty."
            )
        numeric_rows.append(values)
    width = len(numeric_rows[0])
    if width > max_columns:
        raise SurrogateEvidenceError(
            "surrogate_dataset_too_wide", "The prediction width exceeds the declared limit."
        )
    if any(len(row) != width for row in numeric_rows):
        raise SurrogateEvidenceError(
            "surrogate_dataset_ragged", "Prediction rows must all share one column count."
        )

    # A declared prediction state must be a prediction state, never a verified one.
    declared_state = document.get("state")
    if declared_state is not None and declared_state not in PREDICTION_STATES:
        raise SurrogateEvidenceError(
            "surrogate_prediction_state_invalid",
            "A prediction state must be one of: " + ", ".join(PREDICTION_STATES),
        )

    if ood_state is not None:
        if ood_state not in ("in_domain", "edge", "out_of_domain", "uncalibrated"):
            raise SurrogateEvidenceError(
                "surrogate_document_field_invalid", "Unknown out-of-domain state."
            )
        escalation_required = ood_state != "in_domain"
    else:
        escalation_required = True

    return {
        "schema_name": SURROGATE_EVIDENCE_SCHEMA_NAME,
        "schema_version": SURROGATE_EVIDENCE_SCHEMA_VERSION,
        "success": True,
        "prediction_identity": identity,
        "row_count": len(numeric_rows),
        "column_count": width,
        "declared_state": declared_state or "predicted",
        "ood_state": ood_state,
        "absolute_tolerance": absolute_tolerance,
        "relative_tolerance": relative_tolerance,
        "is_prediction": True,
        "is_fem_evidence": False,
        "upgrades_fem_evidence": False,
        "requires_fresh_fem": True,
        "escalation_required": escalation_required,
        "solver_started": False,
        "filesystem_modified": False,
    }


def preview_training_configuration(
    configuration: Mapping[str, Any],
    *,
    dataset_path: str | None = None,
    max_bytes: int,
    max_rows: int,
    max_columns: int,
    declared_fem_row_count: int | None = None,
) -> dict[str, Any]:
    """Validate and preview one surrogate training configuration without training.

    The configuration is validated and its exact typed write plan is derived, so
    a caller can inspect precisely which COMSOL properties would be written.  No
    COMSOL node is created, no solver is started, and nothing is trained.
    """
    from comsol_mcp.surrogate.dnn_adapter import build_dnn_configuration, build_write_plan

    try:
        validated = build_dnn_configuration(**dict(configuration))
    except (TypeError, ValueError) as exc:
        raise SurrogateEvidenceError(
            "surrogate_configuration_invalid",
            "The surrogate training configuration was not accepted.",
        ) from exc

    # `configuration_id` is a required argument, so its presence is guaranteed by
    # successful validation above.
    write_plan = build_write_plan(validated, data_source_bound=False)

    dataset_evidence: dict[str, Any] | None = None
    dataset_bound = False
    if dataset_path is not None:
        dataset_evidence = validate_dataset_document(
            dataset_path,
            max_bytes=max_bytes,
            max_rows=max_rows,
            max_columns=max_columns,
        )
        dataset_bound = True

    # The validated configuration nests its architecture, so the declared column
    # order is read from there rather than from a top-level key that does not
    # exist on the sealed form.
    architecture = validated.get("architecture") or {}
    input_features = list(architecture.get("input_features") or [])
    output_features = list(architecture.get("output_features") or [])
    declared_columns = input_features + output_features
    if dataset_evidence is not None:
        header = dataset_evidence.get("header")
        if header is None:
            column_match: bool | None = None
        else:
            column_match = list(header) == declared_columns
    else:
        column_match = None

    # A caller may declare how many rows were verified by FEM; the preview
    # reports the comparison but never asserts sample sufficiency.
    row_count_evidence: dict[str, Any] | None = None
    if declared_fem_row_count is not None:
        observed = dataset_evidence["row_count"] if dataset_evidence else None
        row_count_evidence = {
            "declared_fem_row_count": declared_fem_row_count,
            "dataset_row_count": observed,
            "matches": observed == declared_fem_row_count if observed is not None else None,
        }

    return {
        "schema_name": "comsol_mcp.surrogate_training_preview",
        "schema_version": "1.0.0",
        "success": True,
        "configuration_id": validated.get("configuration_id"),
        "configuration_sha256": validated.get("configuration_sha256"),
        "architecture": {
            "input_features": input_features,
            "output_features": output_features,
            "hidden_layers": architecture.get("hidden_layers"),
            "layer_configuration": architecture.get("layer_configuration"),
        },
        "write_plan": {
            "scalar_write_count": len(write_plan.get("scalar_writes", [])),
            "entry_write_count": len(write_plan.get("entry_writes", [])),
            "string_map_write_count": len(write_plan.get("string_map_writes", [])),
            "step_scalar_write_count": len(write_plan.get("step_scalar_writes", [])),
            "deferred_writes": write_plan.get("deferred_writes", []),
            "properties": sorted(
                {
                    str(item["property"])
                    for item in (
                        list(write_plan.get("scalar_writes", []))
                        + list(write_plan.get("entry_writes", []))
                        + list(write_plan.get("string_map_writes", []))
                        + list(write_plan.get("step_scalar_writes", []))
                    )
                }
            ),
        },
        "dataset": dataset_evidence,
        "dataset_bound": dataset_bound,
        "declared_columns": declared_columns,
        "dataset_column_match": column_match,
        "row_count_evidence": row_count_evidence,
        "training_performed": False,
        "solver_started": False,
        "filesystem_modified": False,
        "upgrades_fem_evidence": False,
    }


def build_evidence_verdict(*, checks: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """Assemble one bounded public verdict with a canonical identity."""
    body = {
        "schema_name": SURROGATE_EVIDENCE_SCHEMA_NAME,
        "schema_version": SURROGATE_EVIDENCE_SCHEMA_VERSION,
        "checks": checks,
        **extra,
    }
    return {**body, "verdict_sha256": canonical_sha256_v1(body)}


def resolve_dbmodel_source(
    *,
    source_uri: str,
    source_path: str | None,
) -> dict[str, Any]:
    """Validate one frozen ``dbmodel://`` source without touching anything live.

    This is the solver-free half of the Model Manager contract.  It resolves the
    URI as pure syntax and reports an explicit ``unavailable`` evidence state,
    because 0.7.5 deliberately performs no Model Manager operation.  Nothing is
    read, connected, authenticated, or inferred: the report says what is known
    (the parsed components) and what is not (any live identity).
    """
    components = parse_dbmodel_uri(source_uri)
    reference = validate_source_reference(
        source_kind="dbmodel",
        source_path=source_path,
        source_uri=source_uri,
    )
    return {
        "schema_name": SURROGATE_EVIDENCE_SCHEMA_NAME,
        "schema_version": SURROGATE_EVIDENCE_SCHEMA_VERSION,
        "success": True,
        "checks": [
            {
                "check": "dbmodel_uri_syntax",
                "state": "verified",
                "detail": "the URI matches dbmodel://authority/resource with an optional sha256",
            },
            {
                "check": "local_path_absent",
                "state": "verified",
                "detail": "a dbmodel source declares no local path, so it cannot alias a file",
            },
            {
                "check": "live_model_manager_identity",
                "state": "unavailable",
                "detail": "0.7.5 performs no Model Manager operation; identity is not resolved",
            },
            {
                "check": "content_hash",
                "state": ("declared_by_uri" if components["sha256"] else "not_declared"),
                "detail": (
                    "the URI declares a sha256 that was parsed as syntax only"
                    if components["sha256"]
                    else "the URI declares no sha256, so no content identity is claimed"
                ),
            },
        ],
        "document_kind": "dbmodel_source",
        "source_kind": "dbmodel",
        "read": False,
        "filesystem_access": False,
        "live_model_manager_access": False,
        "components": components,
        "source_reference": reference,
        "resolution_state": "unavailable",
        "upgrades_fem_evidence": False,
        "solver_started": False,
        "filesystem_modified": False,
    }


__all__ = [
    "DOCUMENT_KINDS",
    "EXPORT_MANIFEST_SCHEMA",
    "FEM_EVIDENCE_FIELDS",
    "MODEL_CARD_SCHEMA",
    "PREDICTION_STATES",
    "REGISTRY_ENTRY_SCHEMA",
    "SURROGATE_EVIDENCE_SCHEMA_NAME",
    "SURROGATE_EVIDENCE_SCHEMA_VERSION",
    "SurrogateEvidenceError",
    "build_evidence_verdict",
    "identify_document_kind",
    "inspect_surrogate_document",
    "preview_training_configuration",
    "read_bounded_document",
    "resolve_dbmodel_source",
    "validate_dataset_document",
    "validate_export_manifest_document",
    "validate_model_card_document",
    "validate_prediction_document",
    "validate_registry_entry_document",
    "verify_surrogate_document",
]
