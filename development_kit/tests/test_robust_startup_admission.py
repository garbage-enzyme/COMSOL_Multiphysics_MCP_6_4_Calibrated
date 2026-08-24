"""Startup-only absolute RAM and disk admission tests."""

from __future__ import annotations

import pytest

from comsol_mcp.research.robust_startup_admission import (
    evaluate_robust_startup_admission,
    normalize_robust_startup_policy,
)

GIB = 1024**3


def _policy() -> dict:
    return {
        "schema_name": "comsol_mcp.robust_startup_admission_policy",
        "schema_version": "1.0.0",
        "minimum_available_memory_bytes": 1 * GIB,
        "minimum_runtime_free_bytes": 100 * GIB,
        "check_frequency": "startup_only",
    }


def _sample(*, memory: int = 2 * GIB, disk: int = 200 * GIB, stage: str = "pre_mesh"):
    return {
        "stage": stage,
        "available_memory_bytes": memory,
        "total_memory_bytes": 16 * GIB,
        "runtime_free_bytes": disk,
    }


def test_fixture_thresholds_are_caller_values_and_checked_once_at_startup():
    normalized = normalize_robust_startup_policy(_policy())
    assert normalized["minimum_available_memory_bytes"] == GIB
    assert normalized["minimum_runtime_free_bytes"] == 100 * GIB
    receipt = evaluate_robust_startup_admission(normalized, _sample())
    assert receipt["decision"] == "allow"
    assert receipt["recheck_required_before_condition_batches"] is False
    assert receipt["recheck_required_before_remesh_or_solve"] is False


@pytest.mark.parametrize(
    ("memory", "disk", "failed_check"),
    [
        (GIB - 1, 200 * GIB, "available_memory_meets_minimum"),
        (2 * GIB, 100 * GIB - 1, "runtime_free_space_meets_minimum"),
    ],
)
def test_one_byte_below_either_absolute_threshold_refuses(memory, disk, failed_check):
    receipt = evaluate_robust_startup_admission(_policy(), _sample(memory=memory, disk=disk))
    assert receipt["decision"] == "refuse"
    assert receipt["checks"][failed_check] is False


@pytest.mark.parametrize(
    ("field", "present_check"),
    [
        ("available_memory_bytes", "available_memory_present"),
        ("runtime_free_bytes", "runtime_free_space_present"),
    ],
)
def test_boolean_telemetry_is_not_a_valid_byte_count(field, present_check):
    sample = _sample()
    sample[field] = True
    receipt = evaluate_robust_startup_admission(_policy(), sample)
    assert receipt["decision"] == "refuse"
    assert receipt["checks"][present_check] is False
    negative = _sample()
    negative[field] = -1
    receipt = evaluate_robust_startup_admission(_policy(), negative)
    assert receipt["checks"][present_check] is False


def test_policy_rejects_per_batch_frequency_or_non_startup_sample():
    policy = _policy()
    policy["check_frequency"] = "per_batch"
    with pytest.raises(ValueError, match="startup_only"):
        normalize_robust_startup_policy(policy)
    with pytest.raises(ValueError, match="pre_mesh"):
        evaluate_robust_startup_admission(_policy(), _sample(stage="pre_solve"))
