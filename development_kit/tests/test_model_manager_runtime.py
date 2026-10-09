"""Tests for the default-off Model Manager feature gate and runtime.

The runtime is exercised with an injected dispatch callable, so the feature gate,
the upload setting, the collision checks, the durable journal write, and the
receipt are all proven without a solver. A licensed run then only has to show the
injected callable works.

The refusals carry the value. A read blocked by the upload setting, a write
admitted with the setting off, or a refused attempt leaving no durable record
would each be a real defect that a happy-path test cannot see.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from comsol_mcp.shared_session.model_manager import (
    FEATURE_NAME,
    UPLOAD_SETTING,
    ModelManagerContext,
    ModelManagerError,
    ModelManagerRuntime,
    evaluate_gate,
)
from development_kit.tests.platform_fixtures import platform_test_root

VALID_URI = "dbmodel://library/models/cell"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
DOWNLOAD_PATH = str(platform_test_root("downloads") / "cell.mph")


class Dispatch:
    """Records every transport call so a test can prove when none happened."""

    def __init__(self, reply: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reply: Any = reply if reply is not None else {"outcome": "succeeded"}
        self.raise_error: Exception | None = None

    def __call__(self, operation: str, payload: Any) -> Any:
        self.calls.append((operation, dict(payload)))
        if self.raise_error is not None:
            raise self.raise_error
        # Returned verbatim so a test can inject a malformed reply and prove the
        # runtime does not treat "something came back" as success.
        return self.reply


def _runtime(
    tmp_path: Path,
    *,
    feature_enabled: bool = True,
    upload_enabled: bool = False,
    attached: bool = True,
    dispatch: Dispatch | None = None,
) -> tuple[ModelManagerRuntime, Dispatch]:
    transport = dispatch if dispatch is not None else Dispatch()
    runtime = ModelManagerRuntime(
        feature_enabled=feature_enabled,
        upload_enabled=upload_enabled,
        context=ModelManagerContext(
            attached=attached,
            session_identity="session-1" if attached else None,
            dispatch=transport if attached else None,
        ),
        runtime_dir=tmp_path / "runtime",
    )
    return runtime, transport


# ---------------------------------------------------------------------------
# Gate vocabulary
# ---------------------------------------------------------------------------


def test_the_feature_name_and_upload_setting_are_declared() -> None:
    assert FEATURE_NAME == "model_manager"
    assert UPLOAD_SETTING == "model_manager.upload_enabled"


def test_the_gate_reports_both_switches_and_defaults_to_off() -> None:
    gate = evaluate_gate(feature_enabled=False, upload_enabled=False)
    assert gate["feature"] == FEATURE_NAME
    assert gate["enabled"] is False
    assert gate["default"] is False
    assert gate["upload_enabled"] is False
    assert gate["upload_default"] is False
    assert gate["upload_independent_of_shared_server"] is True
    assert gate["restart_required"] is True


def test_the_gate_reports_the_enabled_combination() -> None:
    gate = evaluate_gate(feature_enabled=True, upload_enabled=True)
    assert gate["enabled"] is True and gate["upload_enabled"] is True


# ---------------------------------------------------------------------------
# The feature gate denies by default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.open(uri=VALID_URI),
        lambda r: r.run(uri=VALID_URI),
        lambda r: r.download(uri=VALID_URI, destination=DOWNLOAD_PATH),
        lambda r: r.upload(
            uri=VALID_URI, derived_source_identity={"revision": "r1", "sha256": DIGEST_A}
        ),
    ],
)
def test_a_disabled_feature_refuses_every_operation(tmp_path: Path, call: Any) -> None:
    """Default-off must mean off, including for reads."""
    runtime, transport = _runtime(tmp_path, feature_enabled=False)
    with pytest.raises(ModelManagerError) as excinfo:
        call(runtime)
    assert excinfo.value.reason_code == "feature_gate_closed"
    assert transport.calls == []


def test_a_disabled_feature_refuses_before_any_transport(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path, feature_enabled=False)
    with pytest.raises(ModelManagerError):
        runtime.open(uri=VALID_URI)
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Reads need a session but not the upload setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.open(uri=VALID_URI),
        lambda r: r.run(uri=VALID_URI, resource_policy={"cores": 1}),
        lambda r: r.download(uri=VALID_URI, destination=DOWNLOAD_PATH),
    ],
)
def test_reads_work_with_the_upload_setting_off(tmp_path: Path, call: Any) -> None:
    """The write setting exists to protect the manager, not to withhold reads."""
    runtime, transport = _runtime(tmp_path, upload_enabled=False)
    result = call(runtime)
    assert result["outcome"] == "succeeded"
    assert len(transport.calls) == 1


def test_the_upload_is_refused_with_the_setting_off(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path, upload_enabled=False)
    result = runtime.upload(
        uri=VALID_URI, derived_source_identity={"revision": "r1", "sha256": DIGEST_A}
    )
    assert result["outcome"] == "refused"
    assert result["reason"] == "write_gate_disabled"
    assert transport.calls == []


def test_the_upload_is_admitted_with_the_setting_on(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path, upload_enabled=True)
    result = runtime.upload(
        uri=VALID_URI,
        derived_source_identity={"revision": "r1", "sha256": DIGEST_A},
        expected_revision="r7",
        observed_revision="r7",
    )
    assert result["outcome"] == "succeeded"
    assert [name for name, _ in transport.calls] == ["upload"]


def test_enabling_the_upload_setting_alone_does_not_open_the_feature(tmp_path: Path) -> None:
    """The two switches are independent: writes cannot imply the feature is on."""
    runtime, transport = _runtime(tmp_path, feature_enabled=False, upload_enabled=True)
    with pytest.raises(ModelManagerError) as excinfo:
        runtime.upload(
            uri=VALID_URI, derived_source_identity={"revision": "r1", "sha256": DIGEST_A}
        )
    assert excinfo.value.reason_code == "feature_gate_closed"
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Upload ordering: revision before write, never an overwrite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "observed", "reason"),
    [
        ("r7", "r8", "revision_collision"),
        ("r7", None, "observed_revision_unknown"),
        (None, "r7", "expected_revision_unknown"),
    ],
)
def test_a_revision_problem_refuses_the_upload_before_any_write(
    tmp_path: Path, expected: str | None, observed: str | None, reason: str
) -> None:
    runtime, transport = _runtime(tmp_path, upload_enabled=True)
    result = runtime.upload(
        uri=VALID_URI,
        derived_source_identity={"revision": "r1", "sha256": DIGEST_A},
        expected_revision=expected,
        observed_revision=observed,
    )
    assert result["outcome"] == "refused"
    assert result["reason"] == reason
    assert transport.calls == []


def test_an_upload_without_a_derived_identity_is_refused(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path, upload_enabled=True)
    result = runtime.upload(uri=VALID_URI, derived_source_identity=None)
    assert result["outcome"] == "refused"
    assert result["reason"] == "invalid_identity"
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Download: destination collisions are decided before dispatch
# ---------------------------------------------------------------------------


def test_a_download_to_an_absent_destination_is_dispatched(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path)
    runtime.download(uri=VALID_URI, destination=DOWNLOAD_PATH, destination_exists=False)
    assert [name for name, _ in transport.calls] == ["download"]


def test_an_existing_destination_refuses_before_dispatch(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path)
    result = runtime.download(
        uri=VALID_URI,
        destination=DOWNLOAD_PATH,
        destination_exists=True,
        existing_sha256=DIGEST_B,
        incoming_sha256=DIGEST_A,
    )
    assert result["outcome"] == "refused"
    assert result["reason"] == "destination_exists"
    assert transport.calls == []


def test_identical_content_is_refused_rather_than_replaced(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path)
    result = runtime.download(
        uri=VALID_URI,
        destination=DOWNLOAD_PATH,
        destination_exists=True,
        existing_sha256=DIGEST_A,
        incoming_sha256=DIGEST_A,
    )
    assert result["outcome"] == "refused"
    assert result["reason"] == "identical_content_already_present"
    assert transport.calls == []


@pytest.mark.parametrize(
    "destination", [None, "", "cell.mph", "relative/cell.mph", "D:/x/cell.txt"]
)
def test_an_invalid_download_destination_is_refused(tmp_path: Path, destination: Any) -> None:
    runtime, transport = _runtime(tmp_path)
    result = runtime.download(uri=VALID_URI, destination=destination)
    assert result["outcome"] == "refused"
    assert transport.calls == []


# ---------------------------------------------------------------------------
# Session requirement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.open(uri=VALID_URI),
        lambda r: r.run(uri=VALID_URI),
        lambda r: r.download(uri=VALID_URI, destination=DOWNLOAD_PATH),
    ],
)
def test_an_unattached_session_refuses_every_operation(tmp_path: Path, call: Any) -> None:
    runtime, transport = _runtime(tmp_path, attached=False)
    with pytest.raises(ModelManagerError) as excinfo:
        call(runtime)
    assert excinfo.value.reason_code == "no_attached_session"
    assert transport.calls == []


def test_an_unattached_session_is_reported_in_capabilities(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path, attached=False)
    capabilities = runtime.capabilities()
    assert capabilities["attached"] is False
    assert capabilities["session_identity"] is None


# ---------------------------------------------------------------------------
# Transport outcome is never inferred
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["succeeded", "refused", "failed", "unknown"])
def test_the_reported_outcome_is_recorded_verbatim(tmp_path: Path, outcome: str) -> None:
    runtime, _ = _runtime(tmp_path, dispatch=Dispatch({"outcome": outcome}))
    assert runtime.open(uri=VALID_URI)["outcome"] == outcome


def test_an_unreported_outcome_is_recorded_as_unknown(tmp_path: Path) -> None:
    """A reply that merely arrived is not evidence of success."""
    runtime, _ = _runtime(tmp_path, dispatch=Dispatch({"something": "else"}))
    assert runtime.open(uri=VALID_URI)["outcome"] == "unknown"


def test_a_non_object_reply_is_recorded_as_unknown(tmp_path: Path) -> None:
    transport = Dispatch()
    transport.reply = ["not", "an", "object"]  # type: ignore[assignment]
    runtime, _ = _runtime(tmp_path, dispatch=transport)
    assert runtime.open(uri=VALID_URI)["outcome"] == "unknown"


def test_a_receipt_never_infers_success_from_a_reply(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path, dispatch=Dispatch({"outcome": "unknown"}))
    result = runtime.open(uri=VALID_URI)
    assert result["transport_reply_received"] is True
    assert result["success_inferred_from_transport"] is False


def test_a_transport_exception_is_journalled_before_it_propagates(tmp_path: Path) -> None:
    transport = Dispatch()
    transport.raise_error = RuntimeError("transport died")
    runtime, _ = _runtime(tmp_path, dispatch=transport)
    with pytest.raises(RuntimeError):
        runtime.open(uri=VALID_URI)
    state = runtime.replay()
    # The attempt is durable even though the call raised.
    assert state["row_count"] >= 1
    assert state["outcome_counts"]["failed"] == 1
    assert runtime.recovery_report()["row_count"] >= 1


def test_a_resolved_identity_is_recorded_when_the_reply_exposes_one(
    tmp_path: Path,
) -> None:
    runtime, _ = _runtime(
        tmp_path,
        dispatch=Dispatch(
            {"outcome": "succeeded", "identity": {"revision": "rev-9", "sha256": DIGEST_A}}
        ),
    )
    result = runtime.open(uri=VALID_URI)
    assert result["resolved_identity"]["revision"] == "rev-9"
    assert result["resolved_identity"]["sha256"] == DIGEST_A


def test_an_unresolved_identity_replays_as_unknown(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    result = runtime.open(uri=VALID_URI)
    assert result["resolved_identity"]["revision"] == "unknown"
    assert result["resolved_identity"]["sha256"] == "unknown"


# ---------------------------------------------------------------------------
# Durable journalling
# ---------------------------------------------------------------------------


def test_every_attempt_is_journalled_durably(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    runtime.open(uri=VALID_URI)
    journal = Path(tmp_path / "runtime" / "model_manager" / "operations.jsonl")
    assert journal.is_file()
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert [row["outcome"] for row in rows] == ["in_progress", "succeeded"]
    assert all(row["family"] == "dbmodel" for row in rows)
    assert all(row["subject"] == VALID_URI for row in rows)


def test_a_refusal_is_journalled_like_a_success(tmp_path: Path) -> None:
    """A refused attempt is evidence a caller needs."""
    runtime, _ = _runtime(tmp_path, upload_enabled=False)
    runtime.upload(uri=VALID_URI, derived_source_identity={"revision": "r1", "sha256": DIGEST_A})
    state = runtime.replay()
    assert state["outcome_counts"]["refused"] == 1
    assert state["row_count"] == 1


def test_a_refusal_before_admission_is_also_journalled(tmp_path: Path) -> None:
    """A request rejected on shape is still recorded."""
    runtime, _ = _runtime(tmp_path)
    runtime.download(uri=VALID_URI, destination="not-absolute.mph")
    assert runtime.replay()["outcome_counts"]["refused"] == 1


def test_the_journal_records_the_session_identity_as_owner(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    runtime.open(uri=VALID_URI)
    journal = Path(tmp_path / "runtime" / "model_manager" / "operations.jsonl")
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert {row["owner"] for row in rows} == {"session-1"}


def test_recovery_reports_an_interrupted_attempt(tmp_path: Path) -> None:
    transport = Dispatch()
    transport.raise_error = RuntimeError("died mid-call")
    runtime, _ = _runtime(tmp_path, dispatch=transport)
    with pytest.raises(RuntimeError):
        runtime.open(uri=VALID_URI)
    # The pre-dispatch row is left in a non-terminal state only if the failure
    # row could not be written; either way the report must not claim success.
    state = runtime.replay()
    assert state["outcome_counts"].get("succeeded", 0) == 0


def test_a_runtime_without_a_journal_reports_that_honestly() -> None:
    runtime = ModelManagerRuntime(
        feature_enabled=True,
        upload_enabled=False,
        context=ModelManagerContext(attached=True, session_identity="s", dispatch=Dispatch()),
        runtime_dir=None,
    )
    report = runtime.recovery_report()
    assert report["journal_state"] == "not_configured"
    assert report["row_count"] == 0
    assert runtime.capabilities()["journalled"] is False


def test_no_dispatch_available_is_refused_and_journalled(tmp_path: Path) -> None:
    """Without a transport the operation is refused, not silently skipped."""
    runtime = ModelManagerRuntime(
        feature_enabled=True,
        upload_enabled=False,
        context=ModelManagerContext(attached=True, session_identity="s", dispatch=None),
        runtime_dir=tmp_path / "runtime",
    )
    result = runtime.open(uri=VALID_URI)
    assert result["outcome"] == "refused"
    assert result["reason"] == "no_dispatch_available"
    assert runtime.replay()["outcome_counts"]["refused"] == 1


# ---------------------------------------------------------------------------
# Capability metadata
# ---------------------------------------------------------------------------


def test_capabilities_report_both_switches_and_the_session(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path, upload_enabled=False)
    capabilities = runtime.capabilities()
    assert capabilities["gate"]["enabled"] is True
    assert capabilities["gate"]["upload_enabled"] is False
    assert capabilities["attached"] is True
    assert capabilities["overwrite_permitted"] is False
    assert capabilities["generic_cloud_api"] is False


def test_capabilities_never_advertise_an_overwrite(tmp_path: Path) -> None:
    for upload in (False, True):
        runtime, _ = _runtime(tmp_path, upload_enabled=upload)
        assert runtime.capabilities()["overwrite_permitted"] is False


def test_the_run_operation_passes_the_caller_policy_through_unchanged(
    tmp_path: Path,
) -> None:
    """This layer must never supply, default, or relax a resource policy."""
    runtime, transport = _runtime(tmp_path)
    runtime.run(uri=VALID_URI, resource_policy={"cores": 1, "wall_time_budget_seconds": 60})
    _name, payload = transport.calls[0]
    assert payload["resource_policy"] == {"cores": 1, "wall_time_budget_seconds": 60}


def test_an_omitted_policy_is_sent_as_an_empty_object(tmp_path: Path) -> None:
    runtime, transport = _runtime(tmp_path)
    runtime.run(uri=VALID_URI)
    _name, payload = transport.calls[0]
    assert payload["resource_policy"] == {}
