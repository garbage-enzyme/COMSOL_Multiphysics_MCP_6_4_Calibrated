"""Separated MCP SDK, protocol-revision, and extension wire identity tests.

These tests exist because a single "MCP version" claim hides three independent
facts. Upgrading the SDK must never be reported as implementing the Tasks
extension, and the two Tasks wire generations must stay distinguishable.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from comsol_mcp.protocol_identity import (
    SUPPORTED_LEGACY_REVISIONS,
    TARGET_PROTOCOL_REVISION,
    TASKS_EXTENSION_IDENTIFIER,
    TASKS_WIRE_GENERATIONS,
    get_protocol_identity,
    installed_sdk_version,
    sdk_extension_capability,
)

ROOT = Path(__file__).resolve().parents[2]


def _runtime_dependencies() -> list[str]:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return document["project"]["dependencies"]


def test_declared_sdk_lane_is_the_newest_stable_minor_line() -> None:
    """The server lane is the newest stable SDK minor, not a compatibility spread."""
    dependencies = _runtime_dependencies()
    assert "mcp>=2.2.0,<2.3" in dependencies
    # The superseded 2.0 lane must not linger as a second server lane.
    assert not any(item.startswith("mcp>=2.0") for item in dependencies)


def test_installed_sdk_satisfies_the_declared_lane() -> None:
    value = installed_sdk_version()
    assert value is not None, "the mcp distribution must be installed"
    major, minor, *_ = (int(part) for part in value.split("."))
    assert (major, minor) == (2, 2), value


def test_protocol_revision_is_not_the_sdk_version() -> None:
    """A date-based revision and a distribution version are different identities."""
    identity = get_protocol_identity()
    assert identity["protocol"]["target_revision"] == TARGET_PROTOCOL_REVISION
    assert identity["protocol"]["target_revision"] != identity["sdk"]["installed_version"]
    assert identity["protocol"]["revision_is_not_sdk_version"] is True


def test_legacy_revisions_are_declared_oldest_to_newest() -> None:
    assert tuple(sorted(SUPPORTED_LEGACY_REVISIONS)) == SUPPORTED_LEGACY_REVISIONS
    assert TARGET_PROTOCOL_REVISION not in SUPPORTED_LEGACY_REVISIONS
    assert SUPPORTED_LEGACY_REVISIONS[-1] == "2025-11-25"


def test_native_tasks_support_is_declared_without_overclaiming() -> None:
    """Native Tasks is implemented, but only for the stable dialect and one tool."""
    identity = get_protocol_identity()
    tasks = identity["extensions"]["tasks"]
    assert tasks["identifier"] == TASKS_EXTENSION_IDENTIFIER
    assert tasks["native_implemented"] is True
    assert tasks["native_scope"] == "tools/call only, gated per request"
    assert tasks["task_capable_tools"] == ["job_submit"]
    assert tasks["fallback"] == "ordinary_durable_job_tools"
    # An SDK bump alone still does not implement the extension.
    assert identity["claim_boundary"]["sdk_bump_implements_tasks"] is False


def test_the_declared_generation_status_matches_the_implementation() -> None:
    """The identity surface and the adapter must not disagree about support."""
    from comsol_mcp.jobs.tasks_extension import SUPPORTED_PROTOCOL_VERSIONS
    from comsol_mcp.protocol_identity import TARGET_PROTOCOL_REVISION

    stable = TASKS_WIRE_GENERATIONS[TARGET_PROTOCOL_REVISION]
    assert stable["status"] == "implemented"
    assert SUPPORTED_PROTOCOL_VERSIONS == frozenset({TARGET_PROTOCOL_REVISION})
    assert TASKS_WIRE_GENERATIONS["2025-11-25"]["status"] == "not_implemented"


def test_both_tasks_wire_generations_stay_distinguishable() -> None:
    """The experimental and stable dialects differ on four independent facts."""
    experimental = TASKS_WIRE_GENERATIONS["2025-11-25"]
    stable = TASKS_WIRE_GENERATIONS[TARGET_PROTOCOL_REVISION]
    assert experimental["status"] == "not_implemented"
    assert stable["status"] == "implemented"
    # Each of these alone is enough to make the two generations wire-incompatible.
    assert experimental["opt_in"] != stable["opt_in"]
    assert experimental["result_envelope"] != stable["result_envelope"]
    assert experimental["ttl_field"] != stable["ttl_field"]
    assert experimental["retrieval_method"] != stable["retrieval_method"]


def test_stable_dialect_records_the_extension_facts_verbatim() -> None:
    stable = TASKS_WIRE_GENERATIONS[TARGET_PROTOCOL_REVISION]
    assert stable["dialect"] == "stable_extension"
    assert stable["ttl_field"] == "ttlMs"
    assert stable["retrieval_method"] == "tasks/get"
    # The redesign deleted this method; it must not reappear anywhere here.
    assert "tasks/result" not in stable.values()


def test_no_task_shaped_result_is_claimed_without_request_capability() -> None:
    identity = get_protocol_identity()
    assert identity["claim_boundary"]["task_shaped_results_without_request_capability"] is False
    assert identity["claim_boundary"]["legacy_clients_use_ordinary_tools"] is True


def test_public_sdk_extension_hooks_are_importable() -> None:
    """A standards extension must be expressible without monkey-patching."""
    capability = sdk_extension_capability()
    assert capability["extension_base_class"] is True
    assert capability["method_binding"] is True
    assert capability["tool_call_interceptor"] is True
    assert capability["missing_client_capability_error_helper"] is True


def test_protocol_identity_is_deterministic_and_path_free() -> None:
    first = get_protocol_identity()
    second = get_protocol_identity()
    assert first == second
    assert first["protocol_identity_sha256"] == second["protocol_identity_sha256"]
    serialized = repr(first)
    assert "C:\\" not in serialized
    assert "D:\\" not in serialized


@pytest.mark.parametrize(
    "field",
    ["schema_name", "sdk", "protocol", "extensions", "sdk_extension_hooks", "claim_boundary"],
)
def test_protocol_identity_exposes_every_separated_fact(field: str) -> None:
    assert field in get_protocol_identity()
