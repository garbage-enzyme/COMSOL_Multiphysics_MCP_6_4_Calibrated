"""Solver-free gates for portable controlled real-COMSOL fixtures."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from src.evidence.real_fixture import (
    DOMAINS_ENV,
    MODEL_ENV,
    RANGE_ENV,
    SOURCE_SHA256_ENV,
    WAVELENGTH_ENV,
    controlled_fixture_environment_from_reference_power_spec,
    controlled_fixture_from_environment,
)

ROOT = Path(__file__).parents[2]
_PRIVATE_HOME_PATH = re.compile(
    r"(?:(?<![a-z0-9_])[a-z]:)?/(?:users|documents and settings|home)/[^/\s\"']+",
    re.IGNORECASE,
)


def _contains_private_home_path(text: str) -> bool:
    return _PRIVATE_HOME_PATH.search(text.replace("\\", "/")) is not None


def _spec(tmp_path: Path) -> Path:
    source = tmp_path / "controlled.mph"
    source.write_bytes(b"fixture")
    path = tmp_path / "reference_power.json"
    path.write_text(
        json.dumps(
            {
                "source_model_path": str(source),
                "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
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


def test_reference_power_spec_translates_to_explicit_subprocess_only_fixture_environment(tmp_path):
    environment = controlled_fixture_environment_from_reference_power_spec(
        _spec(tmp_path), base_environment={"PRESERVED": "yes"}
    )
    fixture = controlled_fixture_from_environment(environment)

    assert environment["PRESERVED"] == "yes"
    assert fixture["source"].name == "controlled.mph"
    assert fixture["expected_source_sha256"] == hashlib.sha256(b"fixture").hexdigest()
    assert fixture["wavelength_um"] == 5.292
    assert fixture["top_air_domain_ids"] == [6]
    assert fixture["top_air_coordinate_range"]["z"] == [2.25e-6, 2.55e-6]


def test_reference_power_spec_overrides_inherited_reserved_fixture_environment(tmp_path):
    reserved = {
        MODEL_ENV: "C:/untrusted/inherited.mph",
        SOURCE_SHA256_ENV: "0" * 64,
        WAVELENGTH_ENV: "999",
        DOMAINS_ENV: "[999]",
        RANGE_ENV: '{"x":[9,9],"y":[9,9],"z":[9,9]}',
    }

    environment = controlled_fixture_environment_from_reference_power_spec(
        _spec(tmp_path), base_environment={**reserved, "PRESERVED": "yes"}
    )

    assert environment["PRESERVED"] == "yes"
    assert all(environment[name] != inherited for name, inherited in reserved.items())
    assert controlled_fixture_from_environment(environment)["source"].name == "controlled.mph"


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda env: env.pop(MODEL_ENV), "incomplete"),
        (lambda env: env.pop(SOURCE_SHA256_ENV), "incomplete"),
        (lambda env: env.update({WAVELENGTH_ENV: "nan"}), "finite and positive"),
        (lambda env: env.update({DOMAINS_ENV: "[0]"}), "positive integers"),
        (lambda env: env.update({RANGE_ENV: '{"x":[0,1]}'}), "exactly x, y, and z"),
    ],
)
def test_fixture_environment_fails_closed_on_missing_or_ambiguous_metadata(
    tmp_path, mutation, match
):
    environment = controlled_fixture_environment_from_reference_power_spec(
        _spec(tmp_path), base_environment={}
    )
    mutation(environment)
    with pytest.raises((ValueError, FileNotFoundError), match=match):
        controlled_fixture_from_environment(environment)


@pytest.mark.parametrize("value", [True, "5.292"])
def test_reference_power_spec_rejects_coercive_wavelength_values(tmp_path, value):
    path = _spec(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["wavelength"]["value"] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="must be numeric"):
        controlled_fixture_environment_from_reference_power_spec(path, base_environment={})


def test_reference_power_spec_rejects_malformed_json(tmp_path):
    path = _spec(tmp_path)
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        controlled_fixture_environment_from_reference_power_spec(path, base_environment={})


def test_fixture_rejects_source_bytes_that_differ_from_caller_bound_hash(tmp_path):
    environment = controlled_fixture_environment_from_reference_power_spec(
        _spec(tmp_path), base_environment={}
    )
    Path(environment[MODEL_ENV]).write_bytes(b"changed")

    with pytest.raises(ValueError, match="source SHA-256 mismatch"):
        controlled_fixture_from_environment(environment)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value, _tmp: value["wavelength"].update({"unit": "m"}), "declared in um"),
        (lambda value, _tmp: value["wavelength"].update({"value": 0}), "finite and positive"),
        (
            lambda value, _tmp: value["reference_air"].update({"top_air_domain_ids": []}),
            "non-empty",
        ),
        (
            lambda value, _tmp: value["reference_air"].update({"top_air_domain_ids": [1, 1]}),
            "duplicates",
        ),
        (
            lambda value, _tmp: value["reference_air"]["top_air_coordinate_range"].update(
                {"x": [2, 1]}
            ),
            "is invalid",
        ),
    ],
)
def test_reference_power_spec_rejects_invalid_physical_and_path_boundaries(
    tmp_path, mutation, match
):
    path = _spec(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutation(payload, tmp_path)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises((ValueError, FileNotFoundError), match=match):
        controlled_fixture_environment_from_reference_power_spec(path, base_environment={})


def test_reference_power_spec_rejects_a_directory_as_the_model_source(tmp_path):
    path = _spec(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["source_model_path"] = str(tmp_path)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FileNotFoundError) as caught:
        controlled_fixture_environment_from_reference_power_spec(path, base_environment={})

    assert caught.value.args == (tmp_path.resolve(),)


def test_missing_fixture_error_identifies_the_declared_model_not_the_spec(tmp_path):
    path = _spec(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = Path(payload["source_model_path"])
    source.unlink()
    assert path.is_file()

    with pytest.raises(FileNotFoundError) as caught:
        controlled_fixture_environment_from_reference_power_spec(path, base_environment={})

    assert caught.value.args == (source.resolve(),)


@pytest.mark.parametrize("value", [True, "0", None, [], {}])
def test_fixture_coordinate_bounds_require_strict_json_numbers(tmp_path, value):
    environment = controlled_fixture_environment_from_reference_power_spec(
        _spec(tmp_path), base_environment={}
    )
    coordinate_range = json.loads(environment[RANGE_ENV])
    coordinate_range["x"][0] = value
    environment[RANGE_ENV] = json.dumps(coordinate_range)

    with pytest.raises(
        ValueError,
        match=r"COMSOL_REAL_TEST_TOP_AIR_COORDINATE_RANGE.x\[0\] must be numeric",
    ):
        controlled_fixture_from_environment(environment)


def test_real_probe_sources_contain_no_private_model_defaults():
    probes = sorted((ROOT / "development_kit" / "tests" / "integration").glob("*_acceptance.py"))

    assert probes
    for path in probes:
        text = path.read_text(encoding="utf-8")
        assert not _contains_private_home_path(text)


@pytest.mark.parametrize(
    "private_path",
    [
        "C:\\Users\\Alice\\project\\model.mph",
        "e:/USERS/Bob/other/model.mph",
        "C:\\Documents and Settings\\Carol\\model.mph",
        "/home/dave/project/model.mph",
        "/Users/Erin/project/model.mph",
    ],
)
def test_private_path_scan_normalizes_drive_case_and_separators(private_path):
    assert _contains_private_home_path(private_path)
