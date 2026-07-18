"""Tests for owned cleanup versus non-owning attached detach semantics."""

from __future__ import annotations

import pytest

from src.shared_session.cleanup import evaluate_attached_detach, evaluate_owned_cleanup
from src.shared_session.identity import normalize_attached_server_identity


def _server(*, pid=4200, observed=2000.0):
    return normalize_attached_server_identity(
        {
            "endpoint": {"host": "127.0.0.1", "port": 2036},
            "server_pid": pid,
            "server_process_create_time": 1000.0,
            "server_command_signature": "a" * 64,
            "listener_observed_at_epoch": observed,
        }
    )


def _detach(**overrides):
    values = {
        "server_before": _server(observed=2000.0),
        "server_after": _server(observed=3000.0),
        "model_inventory_before_sha256": "b" * 64,
        "model_inventory_after_sha256": "b" * 64,
        "client_disconnected": True,
        "lease_released": True,
        "listener_active_after": True,
        "model_clear_attempted": False,
        "external_server_shutdown_attempted": False,
        "external_server_termination_attempted": False,
    }
    values.update(overrides)
    return evaluate_attached_detach(**values)


def test_attached_detach_requires_server_listener_and_models_to_remain():
    outcome = _detach()

    assert outcome.to_dict() == {
        "schema_name": "comsol_mcp.cleanup_outcome",
        "schema_version": "1.0.0",
        "resource_mode": "attached_server",
        "success": True,
        "client_disconnected": True,
        "lease_released": True,
        "external_resources_preserved": True,
        "owned_resources_absent": None,
        "violations": [],
    }


@pytest.mark.parametrize(
    ("overrides", "violation"),
    [
        ({"server_after": None}, "external_server_identity_unavailable_after_detach"),
        ({"server_after": _server(pid=4201)}, "external_server_identity_changed"),
        ({"listener_active_after": False}, "external_listener_not_preserved"),
        ({"model_inventory_after_sha256": None}, "model_inventory_unavailable_after_detach"),
        ({"model_inventory_after_sha256": "c" * 64}, "server_model_inventory_changed"),
        ({"model_clear_attempted": True}, "model_clear_attempted"),
        ({"external_server_shutdown_attempted": True}, "external_server_shutdown_attempted"),
        ({"external_server_termination_attempted": True}, "external_server_termination_attempted"),
        ({"client_disconnected": False}, "mcp_client_still_connected"),
        ({"lease_released": False}, "attached_lease_not_released"),
    ],
)
def test_attached_detach_fails_closed_on_preservation_gaps(overrides, violation):
    outcome = _detach(**overrides)

    assert outcome.success is False
    assert violation in outcome.violations


def test_owned_cleanup_requires_owned_resources_to_be_absent():
    outcome = evaluate_owned_cleanup(
        client_disconnected=True,
        lease_released=True,
        owned_server_process_active_after=False,
        owned_listener_active_after=False,
        owned_models_present_after=False,
    )

    assert outcome.success is True
    assert outcome.resource_mode == "owned_standalone"
    assert outcome.owned_resources_absent is True
    assert outcome.external_resources_preserved is None


def test_owned_cleanup_does_not_misclassify_lingering_resources_as_success():
    outcome = evaluate_owned_cleanup(
        client_disconnected=True,
        lease_released=False,
        owned_server_process_active_after=True,
        owned_listener_active_after=True,
        owned_models_present_after=True,
    )

    assert outcome.success is False
    assert set(outcome.violations) == {
        "owned_lease_not_released",
        "owned_server_process_still_active",
        "owned_listener_still_active",
        "owned_models_still_present",
    }


def test_cleanup_contract_rejects_non_boolean_evidence():
    with pytest.raises(ValueError, match="must be boolean"):
        _detach(listener_active_after=1)
