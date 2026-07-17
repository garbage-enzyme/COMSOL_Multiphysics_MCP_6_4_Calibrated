"""Solver-free immutable durable convergence campaign specification tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from development_kit.tests.spectral_job_fixtures import spectral_job_spec
from src.jobs.convergence_campaign import (
    current_convergence_campaign_driver_identity,
    normalize_convergence_campaign_spec,
    validate_convergence_campaign_driver_identity,
)


_SPECTRAL_INPUT_FIELDS = {
    "job_type", "source_model_path", "source_model_relative_identity",
    "configuration_sha256", "parameter_state", "wavelength_parameter",
    "initial_grid", "refinement_policy", "expansion_policy", "maximum_points",
    "collector", "analysis_policy", "measurement_configuration", "resource_policy",
    "cores", "version", "max_retries", "continue_on_error",
}


def _raw_spectral(tmp_path, index: int) -> dict:
    root = tmp_path / f"level-{index}"
    root.mkdir(parents=True, exist_ok=True)
    normalized = spectral_job_spec(root, maximum_points=10)
    (root / "source.mph").write_bytes(f"model-level-{index}".encode("ascii"))
    value = {
        key: deepcopy(item)
        for key, item in normalized.items()
        if key in _SPECTRAL_INPUT_FIELDS
    }
    value["source_model_relative_identity"] = f"fixtures/level-{index}.mph"
    value["configuration_sha256"] = f"{index + 1:x}" * 64
    return value


def _raw_campaign(tmp_path) -> dict:
    levels = [
        {
            "level_id": f"mesh-{index}",
            "ordinal": index,
            "declared_predecessor_level_id": None if index == 0 else f"mesh-{index - 1}",
            "model_preparation": {"mode": "exact_model"},
            "material_identity_sha256": "d" * 64,
            "incidence_identity_sha256": "e" * 64,
            "spectral_job": _raw_spectral(tmp_path, index),
        }
        for index in range(3)
    ]
    return {
        "job_type": "convergence_campaign",
        "campaign_id": "three-mesh-campaign",
        "levels": levels,
        "convergence_policy": {
            "policy_id": "declared-own-peak-policy",
            "metrics": [
                {
                    "metric": "peak_wavelength_m", "unit": "m",
                    "absolute_tolerance": 1e-9, "relative_tolerance": None,
                },
                {
                    "metric": "peak_response_value", "unit": "1",
                    "absolute_tolerance": 1e-3, "relative_tolerance": None,
                },
            ],
            "minimum_level_count": 3,
            "governing_pairs": "final_pair",
            "relative_denominator": "previous_abs",
            "declared_cap_reached": False,
        },
        "stop_policy": {"allow_early_acceptance": False, "minimum_completed_levels": 3},
        "maximum_total_points": 30,
        "wall_time_budget_seconds": 300,
    }


def test_exact_model_ladder_is_canonical_bounded_and_hash_bound(tmp_path):
    raw = _raw_campaign(tmp_path)
    first = normalize_convergence_campaign_spec(raw)
    second = normalize_convergence_campaign_spec(deepcopy(raw))

    assert first == second
    assert first["declared_level_count"] == 3
    assert first["declared_point_count"] == 30
    assert first["driver_identity"] == current_convergence_campaign_driver_identity()
    assert validate_convergence_campaign_driver_identity(first) == first["driver_identity"]
    assert [item["level_id"] for item in first["levels"]] == ["mesh-0", "mesh-1", "mesh-2"]
    assert len({item["spectral_job"]["source_model_sha256"] for item in first["levels"]}) == 3


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value.__setitem__("automatic_mesh", True), "requires exactly"),
        (lambda value: value["levels"][1].__setitem__("ordinal", 2), "ordinal"),
        (
            lambda value: value["levels"][2].__setitem__(
                "declared_predecessor_level_id", "mesh-0"
            ),
            "adjacency",
        ),
        (
            lambda value: value["levels"][1].__setitem__(
                "material_identity_sha256", "f" * 64
            ),
            "material identity",
        ),
        (
            lambda value: value["levels"][1].__setitem__(
                "incidence_identity_sha256", "f" * 64
            ),
            "incidence identity",
        ),
        (
            lambda value: value["levels"][1]["model_preparation"].__setitem__(
                "mode", "implicit_mutation"
            ),
            "exact_model",
        ),
        (
            lambda value: value["convergence_policy"]["metrics"][0].__setitem__(
                "unit", "nm"
            ),
            "unit",
        ),
        (
            lambda value: value["convergence_policy"]["metrics"][0].__setitem__(
                "absolute_tolerance", None
            ),
            "tolerance",
        ),
        (
            lambda value: value["stop_policy"].__setitem__(
                "minimum_completed_levels", 2
            ),
            "cannot precede",
        ),
        (lambda value: value.__setitem__("maximum_total_points", 29), "30 to"),
        (lambda value: value.__setitem__("wall_time_budget_seconds", 299), "smaller"),
    ],
)
def test_invalid_ladders_hidden_policy_and_unbounded_work_fail_closed(tmp_path, mutation, match):
    raw = _raw_campaign(tmp_path)
    mutation(raw)
    with pytest.raises(ValueError, match=match):
        normalize_convergence_campaign_spec(raw)


def test_duplicate_exact_model_bytes_and_configuration_identities_fail_closed(tmp_path):
    raw = _raw_campaign(tmp_path)
    raw["levels"][1]["spectral_job"]["source_model_path"] = raw["levels"][0]["spectral_job"]["source_model_path"]
    with pytest.raises(ValueError, match="distinct source model bytes"):
        normalize_convergence_campaign_spec(raw)

    raw = _raw_campaign(tmp_path)
    raw["levels"][1]["spectral_job"]["configuration_sha256"] = raw["levels"][0]["spectral_job"]["configuration_sha256"]
    with pytest.raises(ValueError, match="configuration identities"):
        normalize_convergence_campaign_spec(raw)


def test_changed_driver_identity_cannot_resume_campaign(tmp_path):
    spec = normalize_convergence_campaign_spec(_raw_campaign(tmp_path))
    spec["driver_identity"]["package_content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="driver identity"):
        validate_convergence_campaign_driver_identity(spec)
