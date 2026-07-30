"""Default-on evidence-integrity settings and disclosure tests."""

from __future__ import annotations

import json

import pytest

from src.evidence.integrity_controls import (
    DISABLED_CHECK_WARNING,
    DISABLED_CHECK_WARNING_CODE,
    EVIDENCE_CHECKS,
    EVIDENCE_INTEGRITY_VERSION,
    EVIDENCE_SETTINGS_ENV,
    EVIDENCE_SETTINGS_SCHEMA,
    INVALID_SETTINGS_WARNING_CODE,
    evidence_integrity_capability,
    load_evidence_integrity_status,
    warning_fields,
)
from src.tools.capabilities import get_capabilities
from src.tools.profiles import ProfileSelection
from src.operation_arbiter import guard_tool_call


def _selection() -> ProfileSelection:
    return ProfileSelection(
        name="core",
        source="evidence-integrity-test",
        environment_variable="COMSOL_MCP_PROFILE",
        default_used=False,
    )


def _write_settings(path, checks: dict[str, bool]) -> None:
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


def test_absent_settings_enable_every_check_by_default():
    status = load_evidence_integrity_status({})

    assert status["success"] is True
    assert status["settings_source"] == "default"
    assert status["strict_verification_active"] is True
    assert status["disabled_checks"] == []
    assert status["warning_codes"] == []
    assert set(status["checks"]) == set(EVIDENCE_CHECKS)
    assert all(item == {"enabled": True, "source": "default"} for item in status["checks"].values())
    assert len(status["settings_fingerprint_sha256"]) == 64
    assert status["settings_path_included"] is False


@pytest.mark.parametrize("disabled_check", EVIDENCE_CHECKS)
def test_each_check_can_be_explicitly_disabled_without_changing_other_checks(
    tmp_path, disabled_check
):
    path = tmp_path / "evidence-settings.json"
    _write_settings(path, {disabled_check: False})

    status = load_evidence_integrity_status({EVIDENCE_SETTINGS_ENV: str(path)})

    assert status["success"] is True
    assert status["strict_verification_active"] is False
    assert status["disabled_checks"] == [disabled_check]
    assert status["checks"][disabled_check] == {
        "enabled": False,
        "source": "explicit_settings",
    }
    for name in set(EVIDENCE_CHECKS) - {disabled_check}:
        assert status["checks"][name] == {"enabled": True, "source": "default"}
    assert status["warning_codes"] == [DISABLED_CHECK_WARNING_CODE]
    assert status["warning_messages"] == [DISABLED_CHECK_WARNING]
    assert warning_fields(status)["strictly_verified"] is False


@pytest.mark.parametrize(
    ("payload", "reason_code"),
    [
        ('{"schema_name":"comsol_mcp.evidence_integrity_settings",', "settings_json_invalid"),
        (
            '{"schema_name":"comsol_mcp.evidence_integrity_settings",'
            '"schema_version":"1.0.0","checks":{"artifact_chain_verification":true,'
            '"artifact_chain_verification":false}}',
            "settings_json_invalid",
        ),
        (
            '{"schema_name":"comsol_mcp.evidence_integrity_settings",'
            '"schema_version":"1.0.0","checks":{"artifact_chain_verification":"false"}}',
            "settings_rejected",
        ),
        (
            '{"schema_name":"comsol_mcp.evidence_integrity_settings",'
            '"schema_version":"1.0.0","checks":{},"unknown":true}',
            "settings_rejected",
        ),
    ],
)
def test_malformed_ambiguous_or_unknown_settings_fail_closed(tmp_path, payload, reason_code):
    path = tmp_path / "bad-settings.json"
    path.write_text(payload, encoding="utf-8")

    status = load_evidence_integrity_status({EVIDENCE_SETTINGS_ENV: str(path)})

    assert status["success"] is False
    assert status["configuration_state"] == "invalid"
    assert status["strict_verification_active"] is False
    assert status["reason_code"] == reason_code
    assert status["warning_codes"] == [INVALID_SETTINGS_WARNING_CODE]
    assert status["settings_path_included"] is False


def test_degraded_project_settings_disable_strict_verification_and_emit_warning(monkeypatch):
    import src.evidence.integrity_controls as integrity_controls_module

    project = integrity_controls_module.load_settings({})
    monkeypatch.delenv(EVIDENCE_SETTINGS_ENV, raising=False)
    monkeypatch.setattr(integrity_controls_module, "load_settings", lambda: project)
    monkeypatch.setattr(
        integrity_controls_module,
        "settings_status",
        lambda: {
            "success": True,
            "configuration_state": "degraded",
            "reason_code": "settings_invalid",
            "settings_errors": [{"reason_code": "settings_invalid"}],
        },
    )

    status = load_evidence_integrity_status()

    assert status["success"] is False
    assert status["configuration_state"] == "degraded"
    assert status["strict_verification_active"] is False
    assert status["warning_codes"] == [INVALID_SETTINGS_WARNING_CODE]
    assert warning_fields(status)["strictly_verified"] is False


def test_capabilities_report_effective_checks_without_exposing_settings_path(tmp_path, monkeypatch):
    path = tmp_path / "private-name-settings.json"
    _write_settings(path, {"summary_claim_verification": False})
    monkeypatch.setenv(EVIDENCE_SETTINGS_ENV, str(path))

    capability = evidence_integrity_capability()
    root_capabilities = get_capabilities(_selection())["evidence_integrity"]

    assert capability == root_capabilities
    assert capability["strict_verification_active"] is False
    assert capability["disabled_checks"] == ["summary_claim_verification"]
    assert capability["settings_path_included"] is False
    assert str(path) not in json.dumps(capability)
    assert capability["hashes_prove_physical_correctness"] is False


def test_disabled_check_warning_propagates_to_affected_tool_responses(
    tmp_path, monkeypatch
):
    path = tmp_path / "evidence-settings.json"
    _write_settings(path, {"artifact_chain_verification": False})
    monkeypatch.setenv(EVIDENCE_SETTINGS_ENV, str(path))

    def exploratory_summary() -> dict:
        return {"success": True, "summary": {"peak": 1.0}}

    guarded = guard_tool_call(
        exploratory_summary,
        tool_name="spectral_characterize",
        side_effect_class="read_only",
        concurrency_class="solver_free",
        profile_name="core",
    )
    result = guarded()

    assert result["success"] is True
    assert result["strictly_verified"] is False
    assert result["disabled_evidence_checks"] == ["artifact_chain_verification"]
    assert result["evidence_integrity_warning_codes"] == [
        DISABLED_CHECK_WARNING_CODE
    ]
    assert result["evidence_integrity_warnings"] == [DISABLED_CHECK_WARNING]
    assert result["path_policy"]["accepted"] is True


def test_capabilities_and_status_keep_their_structured_discovery_contract(
    tmp_path, monkeypatch
):
    path = tmp_path / "evidence-settings.json"
    _write_settings(path, {"artifact_chain_verification": False})
    monkeypatch.setenv(EVIDENCE_SETTINGS_ENV, str(path))

    guarded = guard_tool_call(
        lambda: get_capabilities(_selection()),
        tool_name="capabilities",
        side_effect_class="read_only",
        concurrency_class="control_plane",
        profile_name="core",
    )
    result = guarded()

    assert result["evidence_integrity"]["disabled_checks"] == [
        "artifact_chain_verification"
    ]
    assert "disabled_evidence_checks" not in result
