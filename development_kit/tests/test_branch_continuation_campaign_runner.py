"""Continuation campaign composition, stopping, and resume tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import src.jobs.branch_continuation_campaign_runner as runner_module
from src.jobs.branch_continuation_campaign import normalize_branch_continuation_campaign_spec
from src.jobs.branch_continuation_campaign_rows import (
    read_branch_continuation_campaign_states,
)
from src.jobs.branch_continuation_campaign_runner import (
    branch_continuation_state_directory,
    run_branch_continuation_campaign,
)
from src.jobs.spectral_runner import run_spectral_characterization

from development_kit.tests.spectral_job_fixtures import write_fake_point_audit
from development_kit.tests.test_branch_continuation_campaign_job import _raw_campaign


def _spec(
    tmp_path,
    *,
    stop_policy="continue_all_declared",
    guard=0.25e-6,
    single_boundary_expansion=False,
):
    raw = _raw_campaign(tmp_path / "sources")
    raw["continuation_policy"]["stop_policy"] = stop_policy
    raw["continuation_policy"]["guard_window_m"] = guard
    if single_boundary_expansion:
        raw["continuation_policy"]["max_expansions"] = 1
        for index, state in enumerate(raw["states"]):
            state["spectral_job"]["expansion_policy"]["maximum_expansions"] = 1 if index == 1 else 0
    for state in raw["states"]:
        state["spectral_job"]["measurement_configuration"]["peak_method"] = (
            "quadratic_interpolation"
        )
        state["spectral_job"]["refinement_policy"]["peak_shift_abs_tolerance_m"] = 1e-6
    return normalize_branch_continuation_campaign_spec(raw)


def _executor(
    centers: list[float],
    *,
    boundary_ordinal: int | None = None,
    competing_ordinal: int | None = None,
):
    def execute_state(state, directory):
        child = state["spectral_job"]
        ordinal = state["ordinal"]

        def execute_point(point, artifact_dir):
            wavelength = point["wavelength"]["value"]
            if ordinal == boundary_ordinal:
                absorption = 0.1 + 0.1 * wavelength / 1e-6
            elif ordinal == competing_ordinal:
                left = ((wavelength - 4.5e-6) / 0.12e-6) ** 2
                right = ((wavelength - 5.5e-6) / 0.12e-6) ** 2
                absorption = 0.1 + 0.38 / (1.0 + left) + 0.38 / (1.0 + right)
            else:
                coordinate = (wavelength - centers[ordinal]) / 0.4e-6
                absorption = 0.1 + 0.8 / (1.0 + coordinate * coordinate)
            return write_fake_point_audit(artifact_dir, child, point, absorption=absorption)

        return run_spectral_characterization(
            child, directory, attempt=1, point_executor=execute_point
        )

    return execute_state


@pytest.mark.parametrize(
    "centers",
    [
        [5.0e-6, 5.08e-6, 5.16e-6],
        [5.2e-6, 5.1e-6, 5.0e-6],
        [5.0e-6, 5.12e-6, 4.98e-6],
    ],
)
def test_red_blue_and_reversing_branches_complete_from_each_own_peak(tmp_path, centers):
    spec = _spec(tmp_path)
    result = run_branch_continuation_campaign(
        spec, tmp_path / "campaign", attempt=1, state_executor=_executor(centers)
    )

    assert result["completed"] is True
    assert result["summary"]["scientific_disposition"] == "accepted"
    assert result["summary"]["completed_state_count"] == 3
    assert result["summary"]["branch_disappearance_claimed"] is False
    assert result["progress"]["continuation_plan"]["branch_followed_transition_count"] == 2


def test_guard_crossing_stops_before_later_declared_state_when_policy_requires(tmp_path):
    spec = _spec(tmp_path, stop_policy="stop_at_first_unresolved", guard=0.05e-6)
    root = tmp_path / "campaign"
    result = run_branch_continuation_campaign(
        spec,
        root,
        attempt=1,
        state_executor=_executor([5.0e-6, 5.3e-6, 5.4e-6]),
    )

    assert result["summary"]["scientific_disposition"] == "residual"
    assert result["summary"]["completed_state_count"] == 2
    assert not branch_continuation_state_directory(root, 2).exists()


def test_boundary_state_completes_as_unresolved_at_declared_cap(tmp_path):
    spec = _spec(tmp_path, single_boundary_expansion=True)
    result = run_branch_continuation_campaign(
        spec,
        tmp_path / "campaign",
        attempt=1,
        state_executor=_executor([5.0e-6, 5.0e-6, 5.0e-6], boundary_ordinal=1),
    )

    assert result["completed"] is True
    assert result["summary"]["scientific_disposition"] == "unresolved_at_declared_cap"
    assert result["summary"]["completed_state_count"] == 2
    transition = result["progress"]["continuation_plan"]["coordinate_transitions"][0]
    assert transition["expansion_required"] is True
    assert result["summary"]["observed_expansion_count"] == 1
    assert result["summary"]["remaining_expansion_count"] == 0
    assert result["summary"]["reason_code"] == ("expansion_count_exceeded_at_declared_cap")
    assert result["summary"]["branch_disappearance_claimed"] is False


def test_competing_candidates_are_not_assigned_a_branch_identity(tmp_path):
    spec = _spec(tmp_path, stop_policy="stop_at_first_unresolved")
    result = run_branch_continuation_campaign(
        spec,
        tmp_path / "campaign",
        attempt=1,
        state_executor=_executor([5.0e-6, 5.0e-6, 5.0e-6], competing_ordinal=1),
    )

    assert result["completed"] is True
    assert result["summary"]["scientific_disposition"] != "accepted"
    transition = result["progress"]["continuation_plan"]["coordinate_transitions"][0]
    assert transition["ambiguous_candidates"] is True
    assert transition["branch_followed"] is False


def test_fault_after_state_row_resumes_without_duplicate_spectrum(tmp_path):
    spec = _spec(tmp_path)
    root = tmp_path / "campaign"
    calls = []

    def execute(state, directory):
        calls.append(state["state_id"])
        return _executor([5.0e-6, 5.08e-6, 5.16e-6])(state, directory)

    def fault(phase, payload):
        if phase == "after_state_row":
            raise RuntimeError("injected continuation interruption")

    with pytest.raises(RuntimeError, match="injected"):
        run_branch_continuation_campaign(
            spec, root, attempt=1, state_executor=execute, fault_hook=fault
        )
    assert calls == ["angle-0"]

    result = run_branch_continuation_campaign(spec, root, attempt=2, state_executor=execute)
    assert result["completed"] is True
    assert calls == ["angle-0", "angle-1", "angle-2"]
    rows = read_branch_continuation_campaign_states(
        root / "continuation_states.jsonl", spec, artifact_root=root
    )
    assert [row["state_id"] for row in rows] == ["angle-0", "angle-1", "angle-2"]


def test_complete_state_artifacts_close_executor_to_row_gap_without_reexecution(
    tmp_path, monkeypatch
):
    spec = _spec(tmp_path)
    root = tmp_path / "campaign-row-gap"
    calls = []
    real_append = runner_module.append_branch_continuation_campaign_state

    def execute(state, directory):
        calls.append(state["state_id"])
        return _executor([5.0e-6, 5.08e-6, 5.16e-6])(state, directory)

    monkeypatch.setattr(
        runner_module,
        "append_branch_continuation_campaign_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("row gap")),
    )
    with pytest.raises(RuntimeError, match="row gap"):
        run_branch_continuation_campaign(spec, root, attempt=1, state_executor=execute)

    monkeypatch.setattr(runner_module, "append_branch_continuation_campaign_state", real_append)
    result = run_branch_continuation_campaign(spec, root, attempt=2, state_executor=execute)

    assert result["completed"] is True
    assert calls.count("angle-0") == 1


def test_state_directory_stays_inside_the_windows_legacy_path_budget():
    root = Path("D:/comsol_runtime/jobs") / ("job-" + "a" * 32)
    directory = branch_continuation_state_directory(root, 7)
    suffix = (
        "point_artifacts/"
        + "b" * 64
        + "/"
        + "b" * 64
        + "/1784320847805-afba0aeb/manifest.json.tmp-33728"
    )
    assert directory.name == "s7"
    assert len(str(directory / suffix)) <= 259
