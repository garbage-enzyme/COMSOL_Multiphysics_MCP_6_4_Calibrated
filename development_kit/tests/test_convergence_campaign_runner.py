"""Solver-free convergence campaign composition, stopping, and resume tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from development_kit.tests.spectral_job_fixtures import write_fake_point_audit
from development_kit.tests.test_convergence_campaign_job import _raw_campaign
from src.jobs.convergence_campaign import normalize_convergence_campaign_spec
from src.jobs.convergence_campaign_rows import read_convergence_campaign_levels
from src.jobs.convergence_campaign_runner import (
    convergence_level_directory,
    run_convergence_campaign,
)
from src.jobs.spectral_runner import run_spectral_characterization


def _spec(tmp_path, *, early: bool, cap: bool = False, tolerance: float = 10e-9):
    raw = _raw_campaign(tmp_path / "sources")
    raw["convergence_policy"]["minimum_level_count"] = 2
    raw["convergence_policy"]["metrics"] = [
        {
            "metric": "peak_wavelength_m",
            "unit": "m",
            "absolute_tolerance": tolerance,
            "relative_tolerance": None,
        }
    ]
    raw["convergence_policy"]["declared_cap_reached"] = cap
    raw["stop_policy"] = {
        "allow_early_acceptance": early,
        "minimum_completed_levels": 2,
    }
    for level in raw["levels"]:
        level["spectral_job"]["measurement_configuration"]["peak_method"] = "quadratic_interpolation"
    return normalize_convergence_campaign_spec(raw)


def _executor(centers: list[float], *, boundary_ordinal: int | None = None):
    def execute_level(level, directory):
        child = level["spectral_job"]
        ordinal = level["ordinal"]

        def execute_point(point, artifact_dir):
            wavelength = point["wavelength"]["value"]
            if ordinal == boundary_ordinal:
                absorption = 0.1 + 0.1 * wavelength / 1e-6
            else:
                coordinate = (wavelength - centers[ordinal]) / 0.4e-6
                absorption = 0.1 + 0.8 / (1.0 + coordinate * coordinate)
            return write_fake_point_audit(
                artifact_dir, child, point, absorption=absorption
            )

        return run_spectral_characterization(
            child, directory, attempt=1, point_executor=execute_point
        )

    return execute_level


def test_declared_early_acceptance_stops_without_starting_later_level(tmp_path):
    spec = _spec(tmp_path, early=True, tolerance=20e-9)
    root = tmp_path / "campaign"
    result = run_convergence_campaign(
        spec,
        root,
        attempt=1,
        level_executor=_executor([5.0e-6, 5.001e-6, 5.002e-6]),
    )

    assert result["completed"] is True
    assert result["progress"]["scientific_disposition"] == "accepted"
    assert result["progress"]["reason_code"] == "early_acceptance_allowed"
    assert result["summary"]["completed_level_count"] == 2
    assert result["summary"]["declared_level_count"] == 3
    assert not convergence_level_directory(root, 2).exists()


def test_without_early_stop_every_declared_level_runs(tmp_path):
    spec = _spec(tmp_path, early=False, tolerance=20e-9)
    result = run_convergence_campaign(
        spec,
        tmp_path / "campaign",
        attempt=1,
        level_executor=_executor([5.0e-6, 5.001e-6, 5.002e-6]),
    )

    assert result["summary"]["completed_level_count"] == 3
    assert result["summary"]["scientific_disposition"] == "accepted"
    assert result["progress"]["evaluation"]["undeclared_configuration_started"] is False


def test_excessive_own_peak_shift_completes_with_residual(tmp_path):
    spec = _spec(tmp_path, early=False, tolerance=1e-9)
    result = run_convergence_campaign(
        spec,
        tmp_path / "campaign",
        attempt=1,
        level_executor=_executor([5.0e-6, 5.05e-6, 5.10e-6]),
    )

    assert result["completed"] is True
    assert result["summary"]["execution_state"] == "completed"
    assert result["summary"]["scientific_disposition"] == "residual"
    comparison = result["progress"]["evaluation"]["pair_comparisons"][-1]["comparisons"][0]
    assert comparison["absolute_change"] > 1e-9
    assert comparison["passed"] is False


def test_fault_after_level_row_resumes_without_duplicate_spectrum(tmp_path):
    spec = _spec(tmp_path, early=False, tolerance=20e-9)
    root = tmp_path / "campaign"
    calls = []

    def execute(level, directory):
        calls.append(level["level_id"])
        return _executor([5.0e-6, 5.001e-6, 5.002e-6])(level, directory)

    def fault(phase, payload):
        if phase == "after_level_row":
            raise RuntimeError("injected campaign interruption")

    with pytest.raises(RuntimeError, match="injected"):
        run_convergence_campaign(
            spec, root, attempt=1, level_executor=execute, fault_hook=fault
        )
    assert calls == ["mesh-0"]

    result = run_convergence_campaign(
        spec, root, attempt=2, level_executor=execute
    )
    assert result["completed"] is True
    assert calls == ["mesh-0", "mesh-1", "mesh-2"]
    rows = read_convergence_campaign_levels(
        root / "convergence_levels.jsonl", spec, artifact_root=root
    )
    assert [row["level_id"] for row in rows] == ["mesh-0", "mesh-1", "mesh-2"]


def test_unresolved_level_is_scientific_completion_not_execution_failure(tmp_path):
    spec = _spec(tmp_path, early=False, cap=False, tolerance=1e-9)
    root = tmp_path / "campaign"
    result = run_convergence_campaign(
        spec,
        root,
        attempt=1,
        level_executor=_executor([5.0e-6, 5.0e-6, 5.0e-6], boundary_ordinal=0),
    )

    assert result["completed"] is True
    assert result["summary"]["completed_level_count"] == 1
    assert result["summary"]["scientific_disposition"] == "unresolved_at_declared_cap"
    assert result["summary"]["reason_code"].startswith("level_spectrum_")
    assert not convergence_level_directory(root, 1).exists()


def test_changed_stop_policy_has_a_distinct_campaign_identity(tmp_path):
    first = _spec(tmp_path / "first", early=False)
    raw = _raw_campaign(tmp_path / "second" / "sources")
    raw["convergence_policy"]["declared_cap_reached"] = False
    raw["stop_policy"]["allow_early_acceptance"] = True
    changed = normalize_convergence_campaign_spec(raw)
    assert changed["spec_fingerprint"] != first["spec_fingerprint"]


def test_level_directory_stays_inside_the_windows_legacy_path_budget():
    root = Path("D:/comsol_runtime/jobs") / ("job-" + "a" * 32)
    directory = convergence_level_directory(root, 7)
    suffix = (
        "point_artifacts/" + "b" * 64 + "/" + "b" * 64
        + "/1784320847805-afba0aeb/manifest.json.tmp-33728"
    )
    assert directory.name == "l7"
    assert len(str(directory / suffix)) <= 259
