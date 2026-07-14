"""Solver-free gates for portable controlled real-COMSOL fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.evidence.real_fixture import (
    DOMAINS_ENV,
    MODEL_ENV,
    RANGE_ENV,
    WAVELENGTH_ENV,
    controlled_fixture_environment_from_h1_spec,
    controlled_fixture_from_environment,
)


ROOT = Path(__file__).parents[1]


def _spec(tmp_path: Path) -> Path:
    source = tmp_path / "controlled.mph"
    source.write_bytes(b"fixture")
    path = tmp_path / "h1.json"
    path.write_text(
        json.dumps(
            {
                "source_model_path": str(source),
                "wavelength": {"value": 5.292, "unit": "um"},
                "reference_air": {
                    "top_air_domain_ids": [6],
                    "top_air_coordinate_range": {
                        "x": [-1e-7, 3.4e-6],
                        "y": [-1.5e-6, 1.5e-6],
                        "z": [2.25e-6, 2.55e-6],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_h1_spec_translates_to_explicit_subprocess_only_fixture_environment(tmp_path):
    environment = controlled_fixture_environment_from_h1_spec(
        _spec(tmp_path), base_environment={"PRESERVED": "yes"}
    )
    fixture = controlled_fixture_from_environment(environment)

    assert environment["PRESERVED"] == "yes"
    assert fixture["source"].name == "controlled.mph"
    assert fixture["wavelength_um"] == 5.292
    assert fixture["top_air_domain_ids"] == [6]
    assert fixture["top_air_coordinate_range"]["z"] == [2.25e-6, 2.55e-6]


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda env: env.pop(MODEL_ENV), "incomplete"),
        (lambda env: env.update({WAVELENGTH_ENV: "nan"}), "finite and positive"),
        (lambda env: env.update({DOMAINS_ENV: "[0]"}), "positive integers"),
        (lambda env: env.update({RANGE_ENV: '{"x":[0,1]}'}), "exactly x, y, and z"),
    ],
)
def test_fixture_environment_fails_closed_on_missing_or_ambiguous_metadata(tmp_path, mutation, match):
    environment = controlled_fixture_environment_from_h1_spec(_spec(tmp_path), base_environment={})
    mutation(environment)
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        controlled_fixture_from_environment(environment)


def test_real_probe_sources_contain_no_private_model_defaults():
    probes = (
        "tests/integration/h2d_real_cancel.py",
        "tests/integration/h3d_real_preflight.py",
        "tests/integration/h3e_real_point_audit.py",
        "tests/integration/h3f_live_acceptance.py",
        "tests/integration/m1_periodic_mesh_audit.py",
        "tests/integration/m3_incidence_config.py",
    )
    for relative in probes:
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "C:\\Users\\" not in text
        assert "Desktop\\iterations" not in text
