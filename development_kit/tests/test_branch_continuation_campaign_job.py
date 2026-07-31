"""Solver-free durable branch-continuation campaign specification tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from src.jobs.branch_continuation_campaign import (
    current_branch_continuation_campaign_driver_identity,
    normalize_branch_continuation_campaign_spec,
    validate_branch_continuation_campaign_driver_identity,
)

from development_kit.tests.spectral_job_fixtures import spectral_job_spec

_SPECTRAL_INPUT_FIELDS = {
    "job_type",
    "source_model_path",
    "source_model_relative_identity",
    "configuration_sha256",
    "parameter_state",
    "wavelength_parameter",
    "initial_grid",
    "refinement_policy",
    "expansion_policy",
    "maximum_points",
    "collector",
    "analysis_policy",
    "measurement_configuration",
    "resource_policy",
    "cores",
    "version",
    "max_retries",
    "continue_on_error",
}


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _raw_spectral(tmp_path, index: int) -> dict:
    root = tmp_path / f"state-{index}"
    root.mkdir(parents=True, exist_ok=True)
    normalized = spectral_job_spec(root, maximum_points=10)
    (root / "source.mph").write_bytes(f"model-state-{index}".encode("ascii"))
    value = {
        key: deepcopy(item) for key, item in normalized.items() if key in _SPECTRAL_INPUT_FIELDS
    }
    value["source_model_relative_identity"] = f"fixtures/state-{index}.mph"
    value["configuration_sha256"] = f"{index + 1:x}" * 64
    return value


def _readback(alpha1_deg: float, spectral: dict) -> dict:
    angles = {"alpha1_deg": alpha1_deg, "alpha2_deg": 0.0}
    source_hash = hashlib.sha256(Path(spectral["source_model_path"]).read_bytes()).hexdigest()
    body = {
        "measurement_state": "measured",
        "source_model_sha256": source_hash,
        "configuration_sha256": spectral["configuration_sha256"],
        "requested": angles,
        "parent": dict(angles),
        "ports": [
            {"port_tag": "pport1", **angles},
            {"port_tag": "pport2", **angles},
        ],
    }
    return {**body, "evidence_sha256": _hash(body)}


def _coordinate_identity(*, state: dict, readback: dict, spectral: dict) -> str:
    coordinate = state["coordinate"]
    return _hash(
        {
            "coordinate_name": coordinate["name"],
            "coordinate_value": coordinate["value"],
            "coordinate_unit": coordinate["unit"],
            "polarization": state["polarization"],
            "material_identity_sha256": state["material_identity_sha256"],
            "source_model_sha256": readback["source_model_sha256"],
            "configuration_sha256": spectral["configuration_sha256"],
            "incidence_readback_sha256": readback["evidence_sha256"],
        }
    )


def _raw_campaign(tmp_path) -> dict:
    states = []
    for index, angle in enumerate((0.0, 5.0, 10.0)):
        spectral = _raw_spectral(tmp_path, index)
        readback = _readback(angle, spectral)
        state = {
            "state_id": f"angle-{index}",
            "ordinal": index,
            "declared_predecessor_state_id": None if index == 0 else f"angle-{index - 1}",
            "model_preparation": {"mode": "exact_model"},
            "coordinate": {
                "name": "incidence_elevation",
                "value": angle,
                "unit": "deg",
            },
            "polarization": "P",
            "material_identity_sha256": "d" * 64,
            "incidence_readback": readback,
            "spectral_job": spectral,
        }
        state["coordinate"]["identity_sha256"] = _coordinate_identity(
            state=state, readback=readback, spectral=spectral
        )
        states.append(state)
    return {
        "job_type": "branch_continuation_campaign",
        "campaign_id": "three-angle-campaign",
        "states": states,
        "continuation_policy": {
            "policy_id": "bounded-angle-following",
            "guard_window_m": 0.5e-6,
            "absolute_bounds_m": {"lower_m": 3e-6, "upper_m": 7e-6},
            "max_expansions": 3,
            "max_total_window_m": 4e-6,
            "request_grid": {"point_count": 7, "spacing_rule": "uniform_inclusive"},
            "stop_policy": "continue_all_declared",
        },
        "maximum_total_points": 30,
        "wall_time_budget_seconds": 300,
    }


def _rebind_state(state: dict) -> None:
    spectral = state["spectral_job"]
    readback = state["incidence_readback"]
    readback["source_model_sha256"] = hashlib.sha256(
        Path(spectral["source_model_path"]).read_bytes()
    ).hexdigest()
    readback["configuration_sha256"] = spectral["configuration_sha256"]
    readback_body = dict(readback)
    readback_body.pop("evidence_sha256")
    readback["evidence_sha256"] = _hash(readback_body)
    state["coordinate"]["identity_sha256"] = _coordinate_identity(
        state=state, readback=readback, spectral=spectral
    )


def _change_polarization(value: dict) -> None:
    value["states"][1]["polarization"] = "S"
    _rebind_state(value["states"][1])


def test_exact_model_sequence_is_canonical_bounded_and_hash_bound(tmp_path):
    raw = _raw_campaign(tmp_path)
    first = normalize_branch_continuation_campaign_spec(raw)
    second = normalize_branch_continuation_campaign_spec(deepcopy(raw))

    assert first == second
    assert first["declared_state_count"] == 3
    assert first["declared_point_count"] == 30
    assert first["driver_identity"] == current_branch_continuation_campaign_driver_identity()
    assert validate_branch_continuation_campaign_driver_identity(first) == first["driver_identity"]
    assert [item["coordinate"]["value"] for item in first["states"]] == [0.0, 5.0, 10.0]


def test_coordinate_identity_rejects_a_stale_bound_configuration(tmp_path):
    raw = _raw_campaign(tmp_path)
    state = raw["states"][1]
    state["spectral_job"]["configuration_sha256"] = "f" * 64
    state["incidence_readback"]["configuration_sha256"] = "f" * 64
    readback_body = dict(state["incidence_readback"])
    readback_body.pop("evidence_sha256")
    state["incidence_readback"]["evidence_sha256"] = _hash(readback_body)

    with pytest.raises(ValueError, match="coordinate.identity_sha256"):
        normalize_branch_continuation_campaign_spec(raw)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value.__setitem__("automatic_state", True), "requires exactly"),
        (lambda value: value["states"][1].__setitem__("ordinal", 2), "ordinal"),
        (
            lambda value: value["states"][2].__setitem__(
                "declared_predecessor_state_id", "angle-0"
            ),
            "adjacency",
        ),
        (
            lambda value: value["states"][1]["model_preparation"].__setitem__("mode", "mutation"),
            "exact_model",
        ),
        (
            _change_polarization,
            "polarization must be constant",
        ),
        (
            lambda value: value["states"][1]["incidence_readback"]["parent"].__setitem__(
                "alpha1_deg", 4.0
            ),
            "exactly match",
        ),
        (
            lambda value: value["states"][1]["incidence_readback"].__setitem__(
                "evidence_sha256", "0" * 64
            ),
            "does not match",
        ),
        (
            lambda value: value["states"][1]["spectral_job"]["expansion_policy"].__setitem__(
                "absolute_lower_m", 2e-6
            ),
            "exceed the continuation policy",
        ),
        (lambda value: value.__setitem__("maximum_total_points", 29), "30 to"),
        (lambda value: value.__setitem__("wall_time_budget_seconds", 299), "smaller"),
    ],
)
def test_invalid_sequence_hidden_work_and_unverified_readback_fail_closed(
    tmp_path, mutation, match
):
    raw = _raw_campaign(tmp_path)
    mutation(raw)
    with pytest.raises(ValueError, match=match):
        normalize_branch_continuation_campaign_spec(raw)


def test_duplicate_model_and_configuration_identities_fail_closed(tmp_path):
    raw = _raw_campaign(tmp_path)
    raw["states"][1]["spectral_job"]["source_model_path"] = raw["states"][0]["spectral_job"][
        "source_model_path"
    ]
    _rebind_state(raw["states"][1])
    with pytest.raises(ValueError, match="distinct source model bytes"):
        normalize_branch_continuation_campaign_spec(raw)

    raw = _raw_campaign(tmp_path)
    raw["states"][1]["spectral_job"]["configuration_sha256"] = raw["states"][0]["spectral_job"][
        "configuration_sha256"
    ]
    _rebind_state(raw["states"][1])
    with pytest.raises(ValueError, match="configuration identities"):
        normalize_branch_continuation_campaign_spec(raw)


def test_changed_driver_identity_cannot_resume_campaign(tmp_path):
    spec = normalize_branch_continuation_campaign_spec(_raw_campaign(tmp_path))
    spec["driver_identity"]["package_content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="driver identity"):
        validate_branch_continuation_campaign_driver_identity(spec)
