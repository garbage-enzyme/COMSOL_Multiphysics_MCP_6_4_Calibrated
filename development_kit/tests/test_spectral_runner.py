"""Durable adaptive spectral point-loop and fault-recovery tests."""

from __future__ import annotations

from collections import Counter

import pytest

from development_kit.tests.spectral_job_fixtures import (
    spectral_job_spec,
    write_fake_point_audit,
)
from src.jobs.spectral_rows import read_spectral_rows
from src.jobs.spectral_runner import run_spectral_characterization
from src.jobs.spectral_stages import read_spectral_stage_plans


def _absorption(wavelength: float) -> float:
    return 0.1 + 0.8 / (1.0 + ((wavelength - 5e-6) / 0.18e-6) ** 2)


def _executor(spec, calls):
    def execute(point, artifact_dir):
        calls[point["point_fingerprint"]] += 1
        return write_fake_point_audit(
            artifact_dir,
            spec,
            point,
            absorption=_absorption(point["wavelength"]["value"]),
        )

    return execute


def test_runner_completes_full_bundle_one_durable_point_at_a_time(tmp_path):
    spec = spectral_job_spec(tmp_path)
    job = tmp_path / "job"
    calls = Counter()
    result = run_spectral_characterization(
        spec,
        job,
        attempt=1,
        point_executor=_executor(spec, calls),
    )

    assert result["completed"] is True
    assert result["progress"]["scientific_disposition"] == "accepted"
    rows = read_spectral_rows(job / "spectral_rows.jsonl", spec, artifact_root=job)
    assert len(rows) > 5
    assert len(calls) == len(rows)
    assert set(calls.values()) == {1}
    assert result["summary"]["row_count"] == len(rows)
    assert result["summary"]["artifacts"]["spectral_bundle"]["sha256"]


@pytest.mark.parametrize("phase", ["before_solve", "after_raw_row", "during_refinement_planning", "during_summary_write"])
def test_faults_resume_without_duplicate_complete_points(tmp_path, phase):
    spec = spectral_job_spec(tmp_path)
    job = tmp_path / f"job-{phase}"
    calls = Counter()
    fired = False

    def fault(observed_phase, _payload):
        nonlocal fired
        if observed_phase == phase and not fired:
            fired = True
            raise RuntimeError(f"injected {phase}")

    with pytest.raises(RuntimeError, match="injected"):
        run_spectral_characterization(
            spec,
            job,
            attempt=1,
            point_executor=_executor(spec, calls),
            fault_hook=fault,
        )
    rows_before = read_spectral_rows(job / "spectral_rows.jsonl", spec, artifact_root=job)
    result = run_spectral_characterization(
        spec,
        job,
        attempt=2,
        point_executor=_executor(spec, calls),
    )
    rows_after = read_spectral_rows(job / "spectral_rows.jsonl", spec, artifact_root=job)

    assert fired is True
    assert result["completed"] is True
    assert len({row["point_fingerprint"] for row in rows_after}) == len(rows_after)
    if phase == "after_raw_row":
        assert len(rows_before) == 1
        assert calls[rows_before[0]["point_fingerprint"]] == 1
    if phase == "during_refinement_planning":
        assert len(read_spectral_stage_plans(job, spec)) == 2
    if phase == "during_summary_write":
        assert result["solved_this_attempt"] == 0


def test_during_solve_failure_leaves_no_false_complete_row_and_retries_on_resume(tmp_path):
    spec = spectral_job_spec(tmp_path)
    job = tmp_path / "job-solve"
    calls = Counter()
    failed_point = None

    def failing(point, artifact_dir):
        nonlocal failed_point
        calls[point["point_fingerprint"]] += 1
        if failed_point is None:
            failed_point = point["point_fingerprint"]
            raise RuntimeError("injected during solve")
        return write_fake_point_audit(
            artifact_dir,
            spec,
            point,
            absorption=_absorption(point["wavelength"]["value"]),
        )

    with pytest.raises(RuntimeError, match="during solve"):
        run_spectral_characterization(spec, job, attempt=1, point_executor=failing)
    assert read_spectral_rows(job / "spectral_rows.jsonl", spec, artifact_root=job) == []
    result = run_spectral_characterization(
        spec, job, attempt=2, point_executor=_executor(spec, calls)
    )
    assert result["completed"] is True
    assert calls[failed_point] == 2


def test_control_stop_occurs_only_at_a_safe_point_boundary(tmp_path):
    spec = spectral_job_spec(tmp_path)
    job = tmp_path / "job-control"
    calls = Counter()
    result = run_spectral_characterization(
        spec,
        job,
        attempt=1,
        point_executor=_executor(spec, calls),
        after_durable_row_hook=lambda _row: {"action": "stop"},
    )
    assert result["completed"] is False
    assert result["stop_reason"] == "after_durable_row_stop"
    assert len(read_spectral_rows(job / "spectral_rows.jsonl", spec, artifact_root=job)) == 1
    resumed = run_spectral_characterization(
        spec, job, attempt=2, point_executor=_executor(spec, calls)
    )
    assert resumed["completed"] is True
    assert max(calls.values()) == 1
