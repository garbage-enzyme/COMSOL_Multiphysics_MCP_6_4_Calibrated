"""Settings-aware formal evidence verification tests."""

from __future__ import annotations

import _winapi
import json
from copy import deepcopy
from pathlib import Path

import pytest
import src.evidence.integrity_verifier as integrity_verifier_module
from mcp.server.mcpserver import MCPServer
from src.evidence.integrity_controls import (
    DISABLED_CHECK_WARNING,
    DISABLED_CHECK_WARNING_CODE,
    EVIDENCE_CHECKS,
    EVIDENCE_INTEGRITY_VERSION,
    EVIDENCE_SETTINGS_ENV,
    EVIDENCE_SETTINGS_SCHEMA,
    load_evidence_integrity_status,
)
from src.evidence.integrity_verifier import verify_evidence_integrity
from src.path_policy import ARTIFACT_WRITE_ROOT_ENV
from src.tools.evidence_integrity import register_evidence_integrity_tools

import comsol_mcp.evidence.integrity_controls as integrity_controls_module
from development_kit.tests.test_portfolio_verifier import _fixture, _rehash_request


def _settings(tmp_path, checks: dict[str, bool]) -> dict:
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "schema_name": EVIDENCE_SETTINGS_SCHEMA,
                "schema_version": EVIDENCE_INTEGRITY_VERSION,
                "checks": checks,
            }
        ),
        encoding="utf-8",
    )
    return load_evidence_integrity_status({EVIDENCE_SETTINGS_ENV: str(path)})


def _compatibility(driver: str = "d" * 64) -> dict:
    identity = {
        "producer": "comsol-mcp",
        "producer_version": "3.0.0",
        "driver_sha256": driver,
        "schema_version": "1.0.0",
    }
    return {"expected": identity, "observed": deepcopy(identity)}


@pytest.fixture
def ascii_artifact_root(ascii_tmp_path: Path):
    root = ascii_tmp_path / "evidence_integrity"
    root.mkdir()
    return root


def test_all_default_checks_produce_one_strictly_verified_receipt(tmp_path):
    request, _raw, _fit = _fixture(tmp_path)

    result = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(tmp_path)},
        settings_status=load_evidence_integrity_status({}),
    )

    assert result["success"] is True
    assert result["verification_state"] == "verified"
    assert result["strictly_verified"] is True
    assert result["reason_code"] == "all_enabled_checks_passed"
    assert result["check_results"]["producer_driver_compatibility"]["state"] == "not_applicable"
    file_checks = set(EVIDENCE_CHECKS) - {"producer_driver_compatibility"}
    assert all(result["check_results"][name]["state"] == "passed" for name in file_checks)
    assert result["paths_included"] is False
    assert len(result["verification_sha256"]) == 64


def test_default_checks_enforce_cross_contract_outcome_chain_binding(tmp_path):
    request, _raw, _fit = _fixture(tmp_path)
    outcome = request["cases"][0]["outcome"]
    outcome.pop("outcome_sha256")
    outcome["evidence"]["raw_artifact_ids"] = ["different-raw"]
    from src.evidence.outcome_contract import build_outcome_contract

    request["cases"][0]["outcome"] = build_outcome_contract(outcome)
    request = _rehash_request(request)

    result = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(tmp_path.resolve())},
        settings_status=load_evidence_integrity_status({}),
    )

    assert result["success"] is False
    assert result["strictly_verified"] is False
    assert result["verification_state"] == "failed"


@pytest.mark.parametrize("disabled_check", EVIDENCE_CHECKS)
def test_each_disabled_check_is_the_only_skipped_check_and_forces_unverified(
    tmp_path, disabled_check
):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    request, _raw, _fit = _fixture(artifact_root)
    status = _settings(tmp_path, {disabled_check: False})

    result = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(artifact_root)},
        settings_status=status,
    )

    assert result["success"] is True
    assert result["verification_state"] == "unverified"
    assert result["strictly_verified"] is False
    assert result["check_results"][disabled_check] == {
        "state": "skipped",
        "reason_code": "disabled_by_settings",
    }
    for name in set(EVIDENCE_CHECKS) - {"producer_driver_compatibility", disabled_check}:
        assert result["check_results"][name]["state"] == "passed"
    if disabled_check != "producer_driver_compatibility":
        assert result["check_results"]["producer_driver_compatibility"]["state"] == "not_applicable"
    assert result["disabled_evidence_checks"] == [disabled_check]
    assert result["evidence_integrity_warning_codes"] == [DISABLED_CHECK_WARNING_CODE]
    assert result["evidence_integrity_warnings"] == [DISABLED_CHECK_WARNING]


def test_disabled_summary_check_allows_exploration_but_never_verified(tmp_path):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    request, _raw, _fit = _fixture(artifact_root)
    request["cases"][0]["summary_claims"][0]["value"] = "invented-value"
    request = _rehash_request(request)

    result = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(artifact_root)},
        settings_status=_settings(tmp_path, {"summary_claim_verification": False}),
    )

    assert result["success"] is True
    assert result["strictly_verified"] is False
    assert result["check_results"]["summary_claim_verification"]["state"] == "skipped"


def test_enabled_summary_check_rejects_a_claim_absent_from_raw_evidence(tmp_path):
    request, _raw, _fit = _fixture(tmp_path)
    request["cases"][0]["summary_claims"][0]["value"] = "invented-value"
    request = _rehash_request(request)

    result = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(tmp_path)},
        settings_status=load_evidence_integrity_status({}),
    )

    assert result["success"] is False
    assert result["verification_state"] == "failed"
    assert result["strictly_verified"] is False
    assert result["check_results"]["summary_claim_verification"]["state"] == "failed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("producer", "other-producer"),
        ("producer_version", "9.9.9"),
        ("driver_sha256", "e" * 64),
        ("schema_version", "2.0.0"),
    ],
)
def test_resume_requires_exact_producer_and_driver_identity(tmp_path, field, value):
    request, _raw, _fit = _fixture(tmp_path)
    matched = _compatibility()
    accepted = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(tmp_path)},
        resumed=True,
        producer_compatibility=matched,
        settings_status=load_evidence_integrity_status({}),
    )
    assert accepted["strictly_verified"] is True
    assert accepted["check_results"]["producer_driver_compatibility"]["state"] == "passed"

    mismatched = _compatibility()
    mismatched["observed"][field] = value
    rejected = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(tmp_path)},
        resumed=True,
        producer_compatibility=mismatched,
        settings_status=load_evidence_integrity_status({}),
    )
    assert rejected["success"] is False
    assert rejected["strictly_verified"] is False
    assert rejected["check_results"]["producer_driver_compatibility"]["state"] == "failed"


def test_invalid_settings_block_formal_verification_before_artifact_reads(tmp_path, monkeypatch):
    path = tmp_path / "bad.json"
    path.write_text("{", encoding="utf-8")
    status = load_evidence_integrity_status({EVIDENCE_SETTINGS_ENV: str(path)})
    artifact = tmp_path / "must-not-read.json"
    artifact.write_text('{"evidence":true}', encoding="utf-8")
    monkeypatch.setattr(
        integrity_verifier_module,
        "verify_portfolio_evidence_checks",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid settings must block before artifact verification"
        ),
    )

    result = verify_evidence_integrity(
        portfolio_request={"nonempty": True},
        artifact_roots={"case-one": str(tmp_path)},
        settings_status=status,
    )

    assert result["success"] is False
    assert result["verification_state"] == "blocked"
    assert result["strictly_verified"] is False
    assert result["reason_code"] == "evidence_integrity_settings_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda status: status.pop("checks"),
        lambda status: status["checks"].pop(EVIDENCE_CHECKS[0]),
        lambda status: status["checks"][EVIDENCE_CHECKS[0]].__setitem__("enabled", 1),
        lambda status: status.__setitem__("strict_verification_active", "yes"),
        lambda status: status.__setitem__("strict_verification_active", False),
    ],
)
def test_caller_supplied_settings_status_requires_complete_typed_checks(tmp_path, mutation):
    status = load_evidence_integrity_status({})
    mutation(status)

    with pytest.raises(ValueError, match="settings_status"):
        verify_evidence_integrity(
            portfolio_request={},
            artifact_roots={},
            settings_status=status,
        )


def test_degraded_project_settings_status_remains_a_blocked_receipt():
    status = load_evidence_integrity_status({})
    status["configuration_state"] = "degraded"
    status["reason_code"] = "settings_invalid"
    status["settings_errors"] = [{"reason_code": "settings_invalid"}]

    result = verify_evidence_integrity(
        portfolio_request={},
        artifact_roots={},
        settings_status=status,
    )

    assert result["success"] is False
    assert result["verification_state"] == "blocked"


def test_malformed_portfolio_receipt_becomes_a_failed_check(tmp_path, monkeypatch):
    request, _raw, _fit = _fixture(tmp_path)
    monkeypatch.setattr(
        integrity_verifier_module,
        "verify_portfolio_evidence_checks",
        lambda *_args, **_kwargs: {},
    )

    result = verify_evidence_integrity(
        portfolio_request=request,
        artifact_roots={"case-one": str(tmp_path.resolve())},
        settings_status=load_evidence_integrity_status({}),
    )

    assert result["success"] is False
    assert result["verification_state"] == "failed"
    assert {
        item["error_type"] for item in result["check_results"].values() if item["state"] == "failed"
    } == {"KeyError"}


@pytest.mark.parametrize("artifact_root", ["", "relative/artifacts"])
def test_direct_verifier_rejects_non_absolute_artifact_roots(artifact_root):
    with pytest.raises(ValueError, match="absolute directory strings"):
        verify_evidence_integrity(
            portfolio_request={},
            artifact_roots={"case-one": artifact_root},
            settings_status=load_evidence_integrity_status({}),
        )


def test_mcp_verify_tool_enforces_owned_artifact_root_and_returns_no_path(
    ascii_artifact_root, monkeypatch
):
    artifact_root = ascii_artifact_root / "case"
    artifact_root.mkdir()
    request, _raw, _fit = _fixture(artifact_root)
    monkeypatch.delenv(EVIDENCE_SETTINGS_ENV, raising=False)
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(ascii_artifact_root))
    server = MCPServer("evidence-integrity-tool-test")
    register_evidence_integrity_tools(server)

    result = server._tool_manager._tools["evidence_integrity_verify"].fn(
        request,
        {"case-one": str(artifact_root)},
    )

    assert result["success"] is True
    assert result["strictly_verified"] is True
    assert result["artifact_root_validation"]["validated_root_count"] == 1
    assert result["artifact_root_validation"]["paths_included"] is False
    assert str(ascii_artifact_root) not in json.dumps(result)


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (ValueError("bad portfolio"), "integrity_verification_rejected"),
        (RuntimeError("verifier failure"), "integrity_verification_failed"),
    ],
)
def test_mcp_verify_tool_distinguishes_verifier_failures_from_root_rejection(
    ascii_artifact_root, monkeypatch, error, reason_code
):
    artifact_root = ascii_artifact_root / "case"
    artifact_root.mkdir()
    monkeypatch.delenv(EVIDENCE_SETTINGS_ENV, raising=False)
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(ascii_artifact_root))
    monkeypatch.setattr(
        integrity_verifier_module,
        "verify_evidence_integrity",
        lambda **_kwargs: (_ for _ in ()).throw(error),
    )
    server = MCPServer("evidence-integrity-verifier-failure-test")
    register_evidence_integrity_tools(server)

    result = server._tool_manager._tools["evidence_integrity_verify"].fn(
        {}, {"case-one": str(artifact_root)}
    )

    assert result["success"] is False
    assert result["reason_code"] == reason_code
    assert result["artifact_root_validation"]["accepted"] is True


def test_mcp_verify_tool_contains_invalid_request_when_settings_are_degraded(monkeypatch):
    degraded = load_evidence_integrity_status({})
    degraded["configuration_state"] = "degraded"
    monkeypatch.setattr(
        integrity_controls_module,
        "load_evidence_integrity_status",
        lambda: degraded,
    )
    server = MCPServer("evidence-integrity-degraded-request-test")
    register_evidence_integrity_tools(server)

    result = server._tool_manager._tools["evidence_integrity_verify"].fn([], {}, resumed="yes")

    assert result["success"] is False
    assert result["reason_code"] == "integrity_verification_rejected"
    assert result["verification_state"] == "blocked"
    assert result["artifact_root_validation"]["accepted"] is False


def test_mcp_verify_tool_rejects_external_and_junction_artifact_roots(
    ascii_artifact_root, tmp_path, monkeypatch
):
    owned = ascii_artifact_root / "owned"
    owned.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = owned / "linked"
    _winapi.CreateJunction(str(outside), str(junction))
    monkeypatch.delenv(EVIDENCE_SETTINGS_ENV, raising=False)
    monkeypatch.setenv(ARTIFACT_WRITE_ROOT_ENV, str(owned))
    server = MCPServer("evidence-integrity-path-negative-test")
    register_evidence_integrity_tools(server)
    tool = server._tool_manager._tools["evidence_integrity_verify"].fn

    external = tool({}, {"case-one": str(outside)})
    linked = tool({}, {"case-one": str(junction)})

    for result in (external, linked):
        assert result["success"] is False
        assert result["verification_state"] == "blocked"
        assert result["artifact_root_validation"]["accepted"] is False
        assert str(outside) not in json.dumps(result)
    junction.rmdir()
