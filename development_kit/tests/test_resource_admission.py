"""resource admission solver-free resource policy and free-space admission free-space admission gates."""

from __future__ import annotations

from pathlib import Path
import shutil
import uuid

import pytest

from src.jobs.manager import validate_staged_sweep_spec
from src.jobs.resource_admission import (
    ResourceStageAdapter,
    build_resource_admission_entries,
    build_resource_calibration_report,
    build_resource_warning_continuation_entry,
    collect_resource_telemetry,
    evaluate_resource_admission,
    normalize_resource_policy,
    normalize_telemetry_sample,
    replay_resource_journal,
)
from src.jobs.store import JobStore
from src.tools.workflow import _sweep_point_id, run_staged_parametric_sweep
from development_kit.tests.test_workflow import FakeModel, read_csv


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


@pytest.fixture
def ascii_jobs_root():
    root = Path("D:/comsol_runtime_test/resource_admission_journal") / uuid.uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


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


def test_collector_output_feeds_admission_without_dropping_sample_integrity(tmp_path):
    collected = collect_resource_telemetry(
        stage="pre_solve",
        runtime_path=tmp_path,
        elapsed_wall_seconds=1.0,
    )

    decision = evaluate_resource_admission(
        {"runtime_free_space_refuse_bytes": 1},
        collected,
    )

    assert decision["decision"] == "allow"
    assert decision["telemetry"]["sample_sha256"] == collected["sample_sha256"]


def test_collector_envelope_still_rejects_unknown_metadata(tmp_path):
    collected = collect_resource_telemetry(
        stage="pre_solve",
        runtime_path=tmp_path,
    )
    collected["unbounded_extra"] = "refuse"

    with pytest.raises(ValueError, match="invalid fields"):
        evaluate_resource_admission(None, collected)


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


def test_offline_calibration_compares_only_to_declared_known_safe_baseline():
    baseline = sample(mesh_elements=100, dof=200, elapsed_wall_seconds=10.0)
    candidate = sample(
        mesh_elements=150,
        dof=300,
        elapsed_wall_seconds=20.0,
        available_memory_bytes=20,
    )
    report = build_resource_calibration_report(
        baseline_id="safe-medium",
        baseline_status="known_safe",
        baseline_sample=baseline,
        candidates=[{"sample_id": "candidate-focused", "telemetry": candidate}],
    )

    comparison = report["candidates"][0]["comparison"]
    assert comparison["mesh_elements"]["ratio_to_baseline"] == 1.5
    assert comparison["dof"]["ratio_to_baseline"] == 1.5
    assert comparison["elapsed_wall_seconds"]["ratio_to_baseline"] == 2.0
    assert comparison["available_memory_fraction"]["delta"] == pytest.approx(-0.1)
    assert report["automatic_policy"] is None
    assert report["policy_scope"] == "project_local_only"
    assert report["assessment"] == "calibration_only"
    assert report["solver_started"] is False


def test_calibration_is_deterministic_and_marks_missing_metrics_unavailable():
    arguments = {
        "baseline_id": "safe",
        "baseline_status": "known_safe",
        "baseline_sample": {"stage": "post_solve", "mesh_elements": 100},
        "candidates": [
            {"sample_id": "coarse", "telemetry": {"stage": "post_solve", "mesh_elements": 80}}
        ],
    }
    first = build_resource_calibration_report(**arguments)
    second = build_resource_calibration_report(**arguments)

    assert first == second
    assert "dof" in first["candidates"][0]["unavailable_comparisons"]
    assert len(first["report_sha256"]) == 64


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"baseline_status": "assumed_safe"}, "exactly known_safe"),
        ({"candidates": []}, "non-empty list"),
        ({"candidates": [{"sample_id": "safe", "telemetry": {"stage": "post_solve"}}]}, "unique"),
        ({"candidates": [{"sample_id": "x", "telemetry": {"stage": "post_solve"}}, {"sample_id": "x", "telemetry": {"stage": "post_solve"}}]}, "unique"),
    ],
)
def test_invalid_calibration_contract_fails_closed(changes, match):
    arguments = {
        "baseline_id": "safe",
        "baseline_status": "known_safe",
        "baseline_sample": {"stage": "post_solve", "mesh_elements": 100},
        "candidates": [{"sample_id": "candidate", "telemetry": {"stage": "post_solve"}}],
        **changes,
    }
    with pytest.raises(ValueError, match=match):
        build_resource_calibration_report(**arguments)


def test_normalized_policy_and_telemetry_are_idempotent_and_integrity_checked():
    policy = normalize_resource_policy(POLICY)
    telemetry = normalize_telemetry_sample(sample())

    assert normalize_resource_policy(policy) == policy
    assert normalize_telemetry_sample(telemetry) == telemetry
    with pytest.raises(ValueError, match="integrity"):
        normalize_resource_policy({**policy, "policy_sha256": "0" * 64})
    with pytest.raises(ValueError, match="integrity"):
        normalize_telemetry_sample({**telemetry, "sample_sha256": "0" * 64})


def test_green_resource_transition_authorizes_only_after_telemetry_and_decision():
    entries = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=normalize_resource_policy(POLICY),
        sample=normalize_telemetry_sample(sample()),
    )
    telemetry_only = replay_resource_journal(entries[:1], attempt=1)
    replay = replay_resource_journal(entries, attempt=1)

    assert telemetry_only["points"]["wl:4.25"]["action"] == "admission_required"
    assert telemetry_only["points"]["wl:4.25"]["start_authorized"] is False
    assert replay["points"]["wl:4.25"]["action"] == "start_point"
    assert replay["next_attempt_sequence"] == 2


def test_warning_requires_separate_exact_caller_confirmation_transition():
    entries = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(available_memory_bytes=20),
    )
    blocked = replay_resource_journal(entries, attempt=1)
    continuation = build_resource_warning_continuation_entry(
        warning_admission=entries[1],
        attempt_sequence=2,
        confirmation_id="operator-confirm-001",
    )
    continued = replay_resource_journal(entries + [continuation], attempt=1)

    assert blocked["points"]["wl:4.25"]["action"] == "await_confirmation"
    assert continued["points"]["wl:4.25"]["decision"] == "allow_with_warning"
    assert continued["points"]["wl:4.25"]["action"] == "start_point"


def test_refuse_checkpoints_then_recovery_rechecks_same_point_without_losing_history():
    refused = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(available_memory_bytes=12),
    )
    blocked = replay_resource_journal(refused, attempt=1)
    recovered = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=2,
        policy=POLICY,
        sample=sample(stage="recovery", observed_at_epoch=1100.0),
    )
    resumed = replay_resource_journal(refused + recovered, attempt=1)

    assert blocked["points"]["wl:4.25"]["action"] == "checkpoint_no_start"
    assert resumed["entry_count"] == 4
    assert resumed["points"]["wl:4.25"]["stage"] == "recovery"
    assert resumed["points"]["wl:4.25"]["action"] == "start_point"


def test_completed_point_is_never_authorized_for_a_duplicate_valid_row():
    entries = build_resource_admission_entries(
        attempt=2,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(),
    )
    replay = replay_resource_journal(
        entries,
        attempt=2,
        completed_point_ids=["wl:4.25"],
    )

    assert replay["points"]["wl:4.25"]["action"] == "skip_completed"
    assert replay["points"]["wl:4.25"]["start_authorized"] is False
    assert replay["duplicate_valid_rows_authorized"] is False


def test_stale_attempt_and_stale_warning_confirmation_fail_closed():
    warning = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(available_memory_bytes=20),
    )
    recovered = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=2,
        policy=POLICY,
        sample=sample(stage="recovery"),
    )
    stale_confirmation = build_resource_warning_continuation_entry(
        warning_admission=warning[1],
        attempt_sequence=4,
        confirmation_id="late-confirmation",
    )

    with pytest.raises(ValueError, match="latest journal attempt"):
        replay_resource_journal(warning, attempt=2)
    with pytest.raises(ValueError, match="stale or mismatched"):
        replay_resource_journal(warning + recovered + [stale_confirmation], attempt=1)


def test_journal_cannot_return_to_an_older_attempt_after_resume():
    first = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(),
    )
    resumed = build_resource_admission_entries(
        attempt=2,
        point_id="wl:4.30",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(stage="recovery"),
    )
    stale = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.35",
        attempt_sequence=2,
        policy=POLICY,
        sample=sample(),
    )

    with pytest.raises(ValueError, match="attempts are not monotonic"):
        replay_resource_journal(first + resumed + stale, attempt=2)


def test_job_store_persists_validated_resource_journal_with_fsync_contract(ascii_jobs_root):
    store = JobStore(ascii_jobs_root / "jobs")
    job_id = store.create({}, {"attempt": 1, "status": "submitted"}, job_id="job-resource")
    warning = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(available_memory_bytes=20),
    )
    first = store.append_resource_journal(job_id, warning)
    continuation = build_resource_warning_continuation_entry(
        warning_admission=warning[1],
        attempt_sequence=2,
        confirmation_id="operator-confirm-001",
    )
    final = store.append_resource_journal(job_id, [continuation])

    assert first["points"]["wl:4.25"]["action"] == "await_confirmation"
    assert final["points"]["wl:4.25"]["action"] == "start_point"
    assert store.read_resource_journal(job_id) == warning + [continuation]
    assert (store.job_dir(job_id) / "resource.jsonl").read_bytes().endswith(b"\n")


def test_job_store_rejects_wrong_attempt_before_appending(ascii_jobs_root):
    store = JobStore(ascii_jobs_root / "jobs")
    job_id = store.create({}, {"attempt": 2, "status": "starting"}, job_id="job-resource")
    stale = build_resource_admission_entries(
        attempt=1,
        point_id="wl:4.25",
        attempt_sequence=0,
        policy=POLICY,
        sample=sample(),
    )

    with pytest.raises(ValueError, match="latest journal attempt"):
        store.append_resource_journal(job_id, stale)
    assert store.read_resource_journal(job_id) == []


def test_stage_adapter_drives_fake_runner_through_all_bounded_actions(ascii_jobs_root):
    store = JobStore(ascii_jobs_root / "jobs")
    job_id = store.create({}, {"attempt": 1, "status": "running"}, job_id="job-adapter")
    completed = set()
    available = {"wl:4.25": 30, "wl:4.30": 20, "wl:4.35": 12}
    provider_calls = []

    def telemetry_provider(stage, point_id):
        provider_calls.append((stage, point_id))
        return sample(stage=stage, available_memory_bytes=available[point_id])

    adapter = ResourceStageAdapter(
        store=store,
        job_id=job_id,
        attempt=1,
        policy=POLICY,
        telemetry_provider=telemetry_provider,
        completed_point_ids_provider=lambda: completed,
    )

    first = adapter.evaluate(stage="pre_solve", point_id="wl:4.25")
    assert first["action"] == "start_point"
    completed.add("wl:4.25")
    post = adapter.evaluate(stage="post_solve", point_id="wl:4.25")
    duplicate = adapter.evaluate(stage="pre_solve", point_id="wl:4.25")

    warning = adapter.evaluate(stage="pre_solve", point_id="wl:4.30")
    confirmed = adapter.confirm_warning(
        point_id="wl:4.30",
        confirmation_id="operator-confirm-030",
    )
    completed.add("wl:4.30")

    refused = adapter.evaluate(stage="pre_solve", point_id="wl:4.35")
    available["wl:4.35"] = 30
    recovered = adapter.evaluate(stage="recovery", point_id="wl:4.35")

    assert post["action"] == "skip_completed"
    assert duplicate == {
        "stage": "pre_solve",
        "point_id": "wl:4.25",
        "action": "skip_completed",
        "start_authorized": False,
        "journal_entries_appended": 0,
    }
    assert warning["action"] == "await_confirmation"
    assert confirmed["action"] == "start_point"
    assert refused["action"] == "checkpoint_no_start"
    assert recovered["action"] == "start_point"
    assert ("pre_solve", "wl:4.25") in provider_calls
    assert provider_calls.count(("pre_solve", "wl:4.25")) == 1
    assert len(store.read_resource_journal(job_id)) == 11


def test_stage_adapter_starts_new_attempt_at_zero_and_rejects_stale_adapter(ascii_jobs_root):
    store = JobStore(ascii_jobs_root / "jobs")
    job_id = store.create({}, {"attempt": 1, "status": "running"}, job_id="job-adapter")
    values = {"available": 12}

    def provider(stage, _point_id):
        return sample(stage=stage, available_memory_bytes=values["available"])

    first = ResourceStageAdapter(
        store=store,
        job_id=job_id,
        attempt=1,
        policy=POLICY,
        telemetry_provider=provider,
        completed_point_ids_provider=lambda: (),
    )
    assert first.evaluate(stage="pre_solve", point_id="wl:4.25")["action"] == "checkpoint_no_start"
    store.update_state(job_id, patch={"attempt": 2})
    values["available"] = 30
    resumed = ResourceStageAdapter(
        store=store,
        job_id=job_id,
        attempt=2,
        policy=POLICY,
        telemetry_provider=provider,
        completed_point_ids_provider=lambda: (),
    )
    result = resumed.evaluate(stage="recovery", point_id="wl:4.25")

    assert result["action"] == "start_point"
    assert result["next_attempt_sequence"] == 2
    with pytest.raises(ValueError, match="attempt is stale"):
        first.evaluate(stage="recovery", point_id="wl:4.25")


def test_stage_adapter_rejects_provider_stage_mismatch_without_appending(ascii_jobs_root):
    store = JobStore(ascii_jobs_root / "jobs")
    job_id = store.create({}, {"attempt": 1, "status": "running"}, job_id="job-adapter")
    adapter = ResourceStageAdapter(
        store=store,
        job_id=job_id,
        attempt=1,
        policy=POLICY,
        telemetry_provider=lambda _stage, _point_id: sample(stage="post_solve"),
        completed_point_ids_provider=lambda: (),
    )

    with pytest.raises(ValueError, match="mismatched stage"):
        adapter.evaluate(stage="pre_solve", point_id="wl:4.25")
    assert store.read_resource_journal(job_id) == []


def test_stage_adapter_gates_in_process_fake_comsol_sweep(ascii_jobs_root):
    store = JobStore(ascii_jobs_root / "jobs")
    job_id = store.create({}, {"attempt": 1, "status": "running"}, job_id="job-sweep-hook")
    csv_path = ascii_jobs_root / "hooked-sweep.csv"
    completed = set()
    available = {
        _sweep_point_id("wl", "1[m]"): 30,
        _sweep_point_id("wl", "2[m]"): 20,
    }

    def telemetry_provider(stage, point_id):
        return sample(stage=stage, available_memory_bytes=available[point_id])

    adapter = ResourceStageAdapter(
        store=store,
        job_id=job_id,
        attempt=1,
        policy=POLICY,
        telemetry_provider=telemetry_provider,
        completed_point_ids_provider=lambda: completed,
    )

    def gate(context):
        return adapter.evaluate(stage=context["stage"], point_id=context["point_id"])

    def record_completed(row):
        if row["status"] == "ok":
            completed.add(_sweep_point_id("wl", row["parameter_value"]))

    model = FakeModel()
    result = run_staged_parametric_sweep(
        model,
        "wl",
        [1, 2],
        ["A"],
        parameter_unit="m",
        csv_path=str(csv_path),
        before_point_hook=gate,
        on_durable_row=record_completed,
        after_durable_row_hook=gate,
    )

    assert result["success"] is True
    assert result["stopped_early"] is True
    assert result["stop_reason"] == "before_point_await_confirmation"
    assert result["n_processed"] == 1
    assert model.java.study_node.run_count == 1
    assert [row["parameter_value"] for row in read_csv(csv_path)] == ["1[m]"]
    assert result["hook_action_counts"]["before_point"]["start_point"] == 1
    assert result["hook_action_counts"]["after_durable_row"]["skip_completed"] == 1
    assert result["hook_action_counts"]["before_point"]["await_confirmation"] == 1
    assert len(store.read_resource_journal(job_id)) == 6
