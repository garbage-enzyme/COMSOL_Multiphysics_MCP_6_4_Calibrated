"""M4 solver-free resource policy and P12 free-space admission gates."""

from __future__ import annotations

import pytest

from src.jobs.manager import validate_staged_sweep_spec
from src.jobs.resource_admission import (
    collect_resource_telemetry,
    evaluate_resource_admission,
    normalize_resource_policy,
    normalize_telemetry_sample,
)


POLICY = {
    "available_memory_warn_fraction": 0.25,
    "available_memory_refuse_fraction": 0.125,
    "remaining_commit_warn_fraction": 0.20,
    "remaining_commit_refuse_fraction": 0.10,
    "runtime_free_space_warn_bytes": 20_000,
    "runtime_free_space_refuse_bytes": 10_000,
    "max_mesh_elements": 350_000,
    "max_dof": 2_000_000,
    "wall_time_budget_seconds": 3600.0,
    "minimum_next_point_seconds": 600.0,
}


def sample(**overrides):
    values = {
        "stage": "pre_solve",
        "observed_at_epoch": 1000.0,
        "available_memory_bytes": 30,
        "total_memory_bytes": 100,
        "remaining_commit_bytes": 30,
        "commit_limit_bytes": 100,
        "runtime_free_bytes": 30_000,
        "mesh_elements": 200_000,
        "dof": 1_000_000,
        "elapsed_wall_seconds": 1000.0,
    }
    values.update(overrides)
    return values


def test_explicit_policy_and_sample_are_deterministic_without_host_defaults():
    first = normalize_resource_policy(POLICY)
    second = normalize_resource_policy(dict(reversed(list(POLICY.items()))))
    telemetry = normalize_telemetry_sample(sample())

    assert first == second
    assert first["host_defaults_applied"] is False
    assert first["temporary_scavenging"] == "disabled"
    assert len(first["policy_sha256"]) == 64
    assert len(telemetry["sample_sha256"]) == 64
    assert "worker_private_bytes" in telemetry["unavailable"]


@pytest.mark.parametrize(
    "policy,match",
    [
        ({}, "at least one"),
        ({"unknown": 1}, "unknown"),
        ({"available_memory_warn_fraction": 1.1}, "between 0 and 1"),
        ({"available_memory_warn_fraction": 0.1, "available_memory_refuse_fraction": 0.2}, "must not exceed"),
        ({"runtime_free_space_warn_bytes": 100, "runtime_free_space_refuse_bytes": 200}, "must not exceed"),
        ({"max_mesh_elements": 1.5}, "positive integer"),
        ({"wall_time_budget_seconds": 10}, "declared together"),
        ({"wall_time_budget_seconds": 10, "minimum_next_point_seconds": 20}, "must not exceed"),
    ],
)
def test_invalid_policy_fails_closed(policy, match):
    with pytest.raises(ValueError, match=match):
        normalize_resource_policy(policy)


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"stage": "during_magic"}, "stage must be"),
        ({"new_metric": 1}, "unknown telemetry"),
        ({"available_memory_bytes": -1}, "nonnegative integer"),
        ({"available_memory_bytes": 101}, "must not exceed"),
        ({"total_memory_bytes": 0}, "must be positive"),
        ({"observed_at_epoch": float("nan")}, "finite"),
    ],
)
def test_invalid_telemetry_fails_closed(changes, match):
    with pytest.raises(ValueError, match=match):
        normalize_telemetry_sample(sample(**changes))


def test_green_policy_allows_without_cleanup_side_effects():
    result = evaluate_resource_admission(POLICY, sample())

    assert result["state"] == "green"
    assert result["decision"] == "allow"
    assert {item["outcome"] for item in result["evidence"]} == {"ok"}
    assert result["temporary_scavenging"] == "disabled"
    assert result["cleanup_action"] == "none"


def test_warning_requires_explicit_continuation_and_never_becomes_green():
    warning_sample = sample(available_memory_bytes=20)
    blocked = evaluate_resource_admission(POLICY, warning_sample)
    continued = evaluate_resource_admission(POLICY, warning_sample, continue_on_warning=True)

    assert blocked["state"] == "warning"
    assert blocked["decision"] == "require_confirmation"
    assert continued["state"] == "warning"
    assert continued["decision"] == "allow_with_warning"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"available_memory_bytes": 12}, "available_memory_refuse"),
        ({"remaining_commit_bytes": 9}, "remaining_commit_refuse"),
        ({"runtime_free_bytes": 9_999}, "runtime_free_space_refuse"),
        ({"mesh_elements": 350_001}, "mesh_elements_refuse"),
        ({"dof": 2_000_001}, "dof_refuse"),
        ({"elapsed_wall_seconds": 3001.0}, "wall_time_refuse"),
    ],
)
def test_each_no_start_threshold_refuses(changes, code):
    result = evaluate_resource_admission(POLICY, sample(**changes))

    assert result["state"] == "red"
    assert result["decision"] == "refuse"
    assert code in {item["code"] for item in result["evidence"]}


def test_threshold_boundaries_are_inclusive_and_do_not_refuse():
    result = evaluate_resource_admission(
        POLICY,
        sample(
            available_memory_bytes=125,
            total_memory_bytes=1000,
            remaining_commit_bytes=10,
            runtime_free_bytes=10_000,
            mesh_elements=350_000,
            dof=2_000_000,
            elapsed_wall_seconds=3000.0,
        ),
    )

    assert result["state"] == "warning"
    assert result["decision"] == "require_confirmation"
    assert not any(item["outcome"] == "refuse" for item in result["evidence"])


@pytest.mark.parametrize(
    "missing,code",
    [
        (("available_memory_bytes", "total_memory_bytes"), "available_memory_unavailable"),
        (("remaining_commit_bytes", "commit_limit_bytes"), "remaining_commit_unavailable"),
        (("runtime_free_bytes",), "runtime_free_space_unavailable"),
        (("mesh_elements",), "mesh_elements_unavailable"),
        (("dof",), "dof_unavailable"),
        (("elapsed_wall_seconds",), "wall_time_unavailable"),
    ],
)
def test_required_unavailable_telemetry_refuses(missing, code):
    values = sample()
    for name in missing:
        values.pop(name)
    result = evaluate_resource_admission(POLICY, values)

    assert result["decision"] == "refuse"
    assert code in {item["code"] for item in result["evidence"]}


def test_absent_policy_is_explicitly_disabled_not_silently_defaulted():
    result = evaluate_resource_admission(None, {"stage": "pre_solve"})

    assert result["state"] == "disabled"
    assert result["decision"] == "allow"
    assert result["policy"] is None
    assert result["cleanup_action"] == "none"


def staged_spec(source, resource_policy="absent"):
    result = {
        "job_type": "staged_sweep",
        "source_model_path": str(source),
        "parameter_name": "wl",
        "parameter_values": [4.25],
        "expressions": ["ewfd.Atotal"],
    }
    if resource_policy != "absent":
        result["resource_policy"] = resource_policy
    return result


def test_staged_sweep_spec_normalizes_policy_into_immutable_fingerprint(tmp_path):
    source = tmp_path / "baseline.mph"
    source.write_bytes(b"model")
    first = validate_staged_sweep_spec(staged_spec(source, dict(POLICY)))
    reordered = validate_staged_sweep_spec(
        staged_spec(source, dict(reversed(list(POLICY.items()))))
    )
    changed = validate_staged_sweep_spec(
        staged_spec(source, {**POLICY, "max_mesh_elements": 349_999})
    )

    assert first["resource_policy"]["policy_sha256"] == reordered["resource_policy"]["policy_sha256"]
    assert first["spec_fingerprint"] == reordered["spec_fingerprint"]
    assert changed["spec_fingerprint"] != first["spec_fingerprint"]
    assert first["resource_policy"]["host_defaults_applied"] is False
    assert first["resource_policy"]["temporary_scavenging"] == "disabled"


def test_absent_and_explicit_null_policy_have_one_canonical_spec(tmp_path):
    source = tmp_path / "baseline.mph"
    source.write_bytes(b"model")
    absent = validate_staged_sweep_spec(staged_spec(source))
    explicit_null = validate_staged_sweep_spec(staged_spec(source, None))

    assert "resource_policy" not in absent
    assert "resource_policy" not in explicit_null
    assert absent["spec_fingerprint"] == explicit_null["spec_fingerprint"]


def test_invalid_staged_sweep_policy_fails_before_submit_or_solver(tmp_path):
    source = tmp_path / "baseline.mph"
    source.write_bytes(b"model")

    with pytest.raises(ValueError, match="must not exceed"):
        validate_staged_sweep_spec(
            staged_spec(
                source,
                {
                    "available_memory_warn_fraction": 0.1,
                    "available_memory_refuse_fraction": 0.2,
                },
            )
        )


@pytest.mark.parametrize(
    "stage",
    ["pre_mesh", "post_mesh", "pre_solve", "post_solve", "recovery"],
)
def test_host_telemetry_collector_is_bounded_solver_free_and_stage_typed(tmp_path, stage):
    result = collect_resource_telemetry(
        stage=stage,
        runtime_path=tmp_path,
        mesh_elements=123,
        dof=456,
        elapsed_wall_seconds=7.5,
        durable_result_epoch=1000.0,
    )

    assert result["values"]["stage"] == stage
    assert result["values"]["mesh_elements"] == 123
    assert result["values"]["dof"] == 456
    assert result["values"]["runtime_free_bytes"] >= 0
    assert result["values"]["worker_working_set_bytes"] > 0
    assert 0 <= result["values"]["remaining_commit_bytes"] <= result["values"]["commit_limit_bytes"]
    assert result["solver_started"] is False
    assert result["runtime_volume"]["absolute_path_redacted"] is True
    assert len(result["collection_errors"]) <= 10


def test_commit_collection_failure_is_explicit_and_not_fabricated(tmp_path, monkeypatch):
    from src.jobs import resource_admission

    monkeypatch.setattr(
        resource_admission,
        "_windows_commit_bytes",
        lambda: (_ for _ in ()).throw(OSError("unavailable")),
    )
    result = collect_resource_telemetry(stage="pre_solve", runtime_path=tmp_path)

    assert "remaining_commit_bytes" in result["unavailable"]
    assert "commit_limit_bytes" in result["unavailable"]
    assert {item["code"] for item in result["collection_errors"]} >= {"commit_unavailable"}


def test_telemetry_collector_rejects_missing_runtime_and_invalid_pid(tmp_path):
    with pytest.raises(ValueError, match="existing directory"):
        collect_resource_telemetry(stage="pre_solve", runtime_path=tmp_path / "missing")
    with pytest.raises(ValueError, match="positive integer"):
        collect_resource_telemetry(stage="pre_solve", runtime_path=tmp_path, process_id=0)
