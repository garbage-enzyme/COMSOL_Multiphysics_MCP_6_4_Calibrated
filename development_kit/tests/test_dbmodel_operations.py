"""``dbmodel://`` Model Manager operation contract tests.

These tests are solver-free by construction: the module under test is a pure
admission and receipt layer, so every rule the plan freezes can be proven without
a licensed host. The licensed smoke that exercises a real Model Manager is a
separate, serial, caller-authorized gate.

The refusals carry most of the value here. A silent overwrite, a write admitted
while the gate is off, or an unknown revision treated as a match would each be a
real data-loss defect that no happy-path test would reveal.
"""

from __future__ import annotations

import pytest

from comsol_mcp.shared_session.dbmodel_operations import (
    DBMODEL_OPERATIONS,
    READ_ONLY_OPERATIONS,
    UNKNOWN,
    WRITE_OPERATIONS,
    DbmodelOperationError,
    IdentityRecord,
    build_request,
    capability_document,
    evaluate_destination_collision,
    evaluate_revision_collision,
    normalize_identity,
    normalize_local_destination,
    operation_receipt,
    parse_operation,
)
from development_kit.tests.platform_fixtures import platform_test_root

VALID_URI = "dbmodel://library/models/cell"
HASHED_URI = f"dbmodel://library/models/cell?sha256={'a' * 64}"
DIGEST = "b" * 64
DOWNLOAD_PATH = str(platform_test_root("downloads") / "cell.mph")


def _failure(operation: str, **kwargs: object) -> DbmodelOperationError:
    with pytest.raises(DbmodelOperationError) as excinfo:
        build_request(operation=operation, uri=VALID_URI, **kwargs)
    return excinfo.value


# ---------------------------------------------------------------------------
# Operation vocabulary
# ---------------------------------------------------------------------------


def test_exactly_four_operations_are_admitted() -> None:
    assert DBMODEL_OPERATIONS == ("open", "run", "download", "upload")
    assert set(READ_ONLY_OPERATIONS) | set(WRITE_OPERATIONS) == set(DBMODEL_OPERATIONS)
    assert not set(READ_ONLY_OPERATIONS) & set(WRITE_OPERATIONS)
    assert WRITE_OPERATIONS == ("upload",)


@pytest.mark.parametrize(
    "value", ["delete", "list", "shell", "", None, 5, "OPEN", "open ", "__import__"]
)
def test_an_unsupported_operation_is_refused(value: object) -> None:
    """No generic escape hatch: the vocabulary is closed."""
    with pytest.raises(DbmodelOperationError) as excinfo:
        parse_operation(value)
    assert excinfo.value.reason_code == "unsupported_operation"


# ---------------------------------------------------------------------------
# URI reuse: the frozen 0.7.5 contract, not a widening
# ---------------------------------------------------------------------------


def test_the_uri_is_reused_from_the_frozen_contract() -> None:
    request = build_request(operation="open", uri=HASHED_URI)
    assert request.authority == "library"
    assert request.resource == "models/cell"
    assert request.declared_sha256 == "a" * 64


@pytest.mark.parametrize(
    "uri",
    [
        "file:///C:/model.mph",
        "http://example.com/model.mph",
        "dbmodel://library/../secret",
        "dbmodel://library/models/../../etc/passwd",
        "dbmodel:///models/cell",
        "dbmodel://library/models/cell?sha256=short",
        "C:\\models\\cell.mph",
        "",
    ],
)
def test_only_the_frozen_uri_form_is_admitted(uri: str) -> None:
    with pytest.raises(DbmodelOperationError) as excinfo:
        build_request(operation="open", uri=uri)
    assert excinfo.value.reason_code == "invalid_uri"


# ---------------------------------------------------------------------------
# The write gate: separate, default-off, independent
# ---------------------------------------------------------------------------


def test_upload_is_refused_while_the_gate_is_off() -> None:
    failure = _failure(
        "upload",
        derived_source_identity={"revision": "r1", "sha256": DIGEST},
    )
    assert failure.reason_code == "write_gate_disabled"


def test_upload_is_admitted_only_with_the_gate_on_and_an_identity() -> None:
    request = build_request(
        operation="upload",
        uri=VALID_URI,
        derived_source_identity={"revision": "r1", "sha256": DIGEST},
        write_enabled=True,
    )
    assert request.operation == "upload"
    assert request.write_enabled is True
    assert request.derived_source_identity is not None
    assert request.derived_source_identity.sha256 == DIGEST


def test_an_upload_without_an_identity_is_refused_even_with_the_gate_on() -> None:
    failure = _failure("upload", write_enabled=True)
    assert failure.reason_code == "invalid_identity"


def test_an_upload_with_a_wholly_unknown_identity_is_refused() -> None:
    """Declaring the identity as unknown is not the same as declaring it."""
    failure = _failure(
        "upload",
        derived_source_identity={"revision": UNKNOWN, "sha256": UNKNOWN},
        write_enabled=True,
    )
    assert failure.reason_code == "invalid_identity"


def test_overwrite_is_refused_before_the_gate_is_even_consulted() -> None:
    """An explicit overwrite can never look permitted by enabling writes."""
    failure = _failure(
        "upload",
        derived_source_identity={"revision": "r1", "sha256": DIGEST},
        write_enabled=True,
        overwrite_existing=True,
    )
    assert failure.reason_code == "overwrite_refused"


@pytest.mark.parametrize("operation", READ_ONLY_OPERATIONS)
def test_read_operations_need_no_write_gate(operation: str) -> None:
    """Read/open/run/download must stay usable with the write gate off."""
    kwargs: dict[str, object] = {}
    if operation == "download":
        kwargs["destination"] = DOWNLOAD_PATH
    request = build_request(operation=operation, uri=VALID_URI, **kwargs)
    assert request.write_enabled is False
    assert request.operation in READ_ONLY_OPERATIONS


@pytest.mark.parametrize("operation", READ_ONLY_OPERATIONS)
def test_a_read_operation_cannot_declare_a_derived_identity(operation: str) -> None:
    kwargs: dict[str, object] = {"derived_source_identity": {"revision": "r1"}}
    if operation == "download":
        kwargs["destination"] = DOWNLOAD_PATH
    with pytest.raises(DbmodelOperationError) as excinfo:
        build_request(operation=operation, uri=VALID_URI, **kwargs)
    assert excinfo.value.reason_code == "unexpected_identity"


def test_enabling_write_does_not_imply_any_other_permission() -> None:
    document = capability_document(upload_enabled=True)
    assert document["overwrite_permitted"] is False
    assert document["generic_cloud_api"] is False
    assert document["upload_gate"]["enabled"] is True


def test_read_access_does_not_turn_the_write_gate_on() -> None:
    """The advertised gate state is reported, never inferred from usage."""
    assert capability_document(upload_enabled=False)["upload_gate"]["enabled"] is False
    assert capability_document(upload_enabled=False)["upload_gate"]["default"] is False
    assert (
        capability_document(upload_enabled=False)["upload_gate"]["independent_of_shared_server"]
        is True
    )


def test_the_capability_document_distinguishes_read_from_write() -> None:
    document = capability_document(upload_enabled=False)
    assert document["operations"]["open"]["mutates_model_manager"] is False
    assert document["operations"]["run"]["mutates_model_manager"] is False
    assert document["operations"]["download"]["mutates_model_manager"] is False
    assert document["operations"]["upload"]["mutates_model_manager"] is True
    assert document["operations"]["upload"]["requires_write_gate"] is True


# ---------------------------------------------------------------------------
# Local destination rules
# ---------------------------------------------------------------------------


def test_a_valid_download_destination_is_admitted() -> None:
    request = build_request(
        operation="download", uri=VALID_URI, destination=DOWNLOAD_PATH
    )
    assert request.destination is not None
    assert request.destination.endswith(".mph")


@pytest.mark.parametrize(
    "destination",
    [
        None,
        "",
        "cell.mph",
        "relative/cell.mph",
        "D:/downloads/cell.txt",
        "D:/downloads/cell",
        "D:/downloads/../escape.mph",
        "\\\\?\\D:\\downloads\\cell.mph",
        "//./D:/downloads/cell.mph",
        "D:/下载/cell.mph",
    ],
)
def test_an_invalid_download_destination_is_refused(destination: object) -> None:
    with pytest.raises(DbmodelOperationError) as excinfo:
        build_request(operation="download", uri=VALID_URI, destination=destination)
    assert excinfo.value.reason_code == "invalid_destination"


@pytest.mark.parametrize("operation", ["open", "run", "upload"])
def test_only_a_download_may_declare_a_local_destination(operation: str) -> None:
    kwargs: dict[str, object] = {"destination": DOWNLOAD_PATH}
    if operation == "upload":
        kwargs["write_enabled"] = True
        kwargs["derived_source_identity"] = {"revision": "r1"}
    with pytest.raises(DbmodelOperationError) as excinfo:
        build_request(operation=operation, uri=VALID_URI, **kwargs)
    assert excinfo.value.reason_code == "unexpected_destination"


def test_the_destination_normalizer_is_directly_testable() -> None:
    assert normalize_local_destination(DOWNLOAD_PATH) == DOWNLOAD_PATH
    with pytest.raises(DbmodelOperationError):
        normalize_local_destination("b.mph")


# ---------------------------------------------------------------------------
# Collisions: no overwrite, and unknown is never a match
# ---------------------------------------------------------------------------


def test_an_absent_download_destination_may_be_written() -> None:
    decision = evaluate_destination_collision(
        destination_exists=False, existing_sha256=None, incoming_sha256=DIGEST
    )
    assert decision["write_allowed"] is True
    assert decision["reason"] == "destination_absent"


def test_an_existing_download_destination_is_never_overwritten() -> None:
    decision = evaluate_destination_collision(
        destination_exists=True, existing_sha256=DIGEST, incoming_sha256="c" * 64
    )
    assert decision["write_allowed"] is False
    assert decision["reason"] == "destination_exists"


def test_identical_existing_content_is_still_reported_rather_than_replaced() -> None:
    """A silent replacement path is refused even when the bytes match."""
    decision = evaluate_destination_collision(
        destination_exists=True, existing_sha256=DIGEST, incoming_sha256=DIGEST
    )
    assert decision["write_allowed"] is False
    assert decision["reason"] == "identical_content_already_present"


def test_a_matching_revision_permits_the_upload() -> None:
    decision = evaluate_revision_collision(expected_revision="r7", observed_revision="r7")
    assert decision["write_allowed"] is True


@pytest.mark.parametrize(
    ("expected", "observed", "reason"),
    [
        ("r7", "r8", "revision_collision"),
        ("r7", None, "observed_revision_unknown"),
        ("r7", UNKNOWN, "observed_revision_unknown"),
        (None, "r7", "expected_revision_unknown"),
        (UNKNOWN, "r7", "expected_revision_unknown"),
    ],
)
def test_a_revision_collision_or_unknown_refuses_the_upload(
    expected: str | None, observed: str | None, reason: str
) -> None:
    decision = evaluate_revision_collision(expected_revision=expected, observed_revision=observed)
    assert decision["write_allowed"] is False
    assert decision["reason"] == reason


# ---------------------------------------------------------------------------
# Identity: unknown stays unknown
# ---------------------------------------------------------------------------


def test_an_absent_identity_is_recorded_as_unknown_not_inferred() -> None:
    identity = normalize_identity(None, source="manager")
    assert identity.revision == UNKNOWN
    assert identity.sha256 == UNKNOWN
    assert identity.source == "manager"


@pytest.mark.parametrize("value", [{"revision": ""}, {"sha256": "   "}, {"revision": None}])
def test_a_blank_identity_field_becomes_unknown(value: dict[str, object]) -> None:
    identity = normalize_identity(value, source="manager")
    assert identity.revision == UNKNOWN or identity.sha256 == UNKNOWN
    assert UNKNOWN in (identity.revision, identity.sha256)


@pytest.mark.parametrize("digest", ["abc", "g" * 64, "A" * 63, 5])
def test_a_malformed_hash_is_refused_rather_than_recorded(digest: object) -> None:
    with pytest.raises(DbmodelOperationError) as excinfo:
        normalize_identity({"sha256": digest}, source="manager")
    assert excinfo.value.reason_code == "invalid_identity"


def test_an_uppercase_hash_is_normalized_not_rejected() -> None:
    identity = normalize_identity({"sha256": "A" * 64}, source="manager")
    assert identity.sha256 == "a" * 64


def test_the_unknown_identity_names_its_provenance() -> None:
    identity = IdentityRecord.unknown("unresolved")
    assert identity.to_document() == {
        "revision": UNKNOWN,
        "sha256": UNKNOWN,
        "source": "unresolved",
    }


# ---------------------------------------------------------------------------
# Receipts: transport, success, and cleanup stay separate
# ---------------------------------------------------------------------------


def test_a_receipt_never_infers_success_from_a_transport_reply() -> None:
    request = build_request(operation="open", uri=VALID_URI)
    receipt = operation_receipt(request, outcome="unknown", transport_reply_received=True)
    assert receipt["transport_reply_received"] is True
    assert receipt["success_inferred_from_transport"] is False
    assert receipt["outcome"] == "unknown"


def test_a_receipt_records_the_original_uri_and_the_derived_parts() -> None:
    request = build_request(operation="open", uri=HASHED_URI)
    receipt = operation_receipt(request, outcome="succeeded")
    assert receipt["uri"] == HASHED_URI
    assert receipt["authority"] == "library"
    assert receipt["resource"] == "models/cell"
    assert receipt["declared_sha256"] == "a" * 64


def test_a_receipt_records_an_unresolved_identity_as_unknown() -> None:
    request = build_request(operation="open", uri=VALID_URI)
    receipt = operation_receipt(request, outcome="succeeded")
    assert receipt["resolved_identity"]["revision"] == UNKNOWN
    assert receipt["resolved_identity"]["sha256"] == UNKNOWN


def test_a_receipt_records_the_resolved_identity_when_exposed() -> None:
    request = build_request(operation="open", uri=VALID_URI)
    receipt = operation_receipt(
        request,
        outcome="succeeded",
        resolved_identity=IdentityRecord(revision="rev-42", sha256=DIGEST, source="manager"),
    )
    assert receipt["resolved_identity"]["revision"] == "rev-42"
    assert receipt["resolved_identity"]["sha256"] == DIGEST


def test_a_receipt_never_records_credentials() -> None:
    request = build_request(operation="open", uri=VALID_URI)
    receipt = operation_receipt(request, outcome="succeeded")
    assert receipt["authentication"] == {
        "delegated_to": "configured_comsol_mph_environment",
        "credentials_recorded": False,
    }


def test_a_receipt_declares_source_immutability() -> None:
    request = build_request(operation="open", uri=VALID_URI)
    assert operation_receipt(request, outcome="succeeded")["source_immutable"] is True


def test_a_receipt_records_cleanup_uncertainty_rather_than_assuming_success() -> None:
    request = build_request(operation="run", uri=VALID_URI)
    receipt = operation_receipt(request, outcome="succeeded")
    assert receipt["cleanup"] == {"state": UNKNOWN}

    explicit = operation_receipt(
        request,
        outcome="succeeded",
        cleanup={"state": "verified", "leased_processes_absent": True},
    )
    assert explicit["cleanup"]["state"] == "verified"


def test_a_receipt_is_deterministic_and_path_free() -> None:
    request = build_request(operation="open", uri=VALID_URI)
    first = operation_receipt(request, outcome="succeeded")
    second = operation_receipt(request, outcome="succeeded")
    assert first == second
    assert first["receipt_sha256"] == second["receipt_sha256"]
    assert "C:\\" not in repr(first)


def test_an_upload_receipt_records_the_gate_and_the_derived_identity() -> None:
    request = build_request(
        operation="upload",
        uri=VALID_URI,
        derived_source_identity={"revision": "r1", "sha256": DIGEST},
        write_enabled=True,
    )
    receipt = operation_receipt(request, outcome="refused", detail={"reason": "revision_collision"})
    assert receipt["is_write_operation"] is True
    assert receipt["write_gate_enabled"] is True
    assert receipt["derived_source_identity"]["sha256"] == DIGEST
    assert receipt["detail"]["reason"] == "revision_collision"


def test_the_receipt_never_serializes_a_local_destination_as_a_confirmed_write() -> None:
    """A download receipt records the destination; it does not claim a write."""
    request = build_request(
        operation="download", uri=VALID_URI, destination=DOWNLOAD_PATH
    )
    receipt = operation_receipt(request, outcome="refused", detail={"reason": "destination_exists"})
    assert receipt["local_destination"] == DOWNLOAD_PATH
    assert receipt["outcome"] == "refused"
    assert receipt["is_write_operation"] is False
