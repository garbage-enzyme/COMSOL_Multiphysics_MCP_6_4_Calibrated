"""Solver-free durable native robust-gradient runtime tests."""

from __future__ import annotations

import json

import pytest

from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.jobs.robust_gradient_runtime import execute_native_condition_gradients
from comsol_mcp.jobs.robust_shape_optimization import expand_robust_shape_manifest
from development_kit.tests.test_robust_shape_optimization import _write_manifest


def _observations(spec):
    first_state = spec["objective"]["state_ids"][0]
    return [
        {
            "condition_id": condition["condition_id"],
            "observable_id": condition["observable_id"],
            "value": 0.7 if condition["material_state_id"] == first_state else 0.5,
            "evidence_sha256": f"{condition['order'] + 1:064x}",
            "disposition": "measured",
        }
        for condition in spec["condition_table"]["conditions"]
        if condition["active"] and condition["objective_role"] == "objective"
    ]


class _Backend:
    def __init__(self, observations):
        self.values = {item["condition_id"]: item["value"] for item in observations}
        self.calls = []

    def evaluate_condition_gradient(self, condition, tensor_expressions, variable_ids):
        self.calls.append((condition["condition_id"], tensor_expressions, variable_ids))
        value = self.values[condition["condition_id"]]
        return {
            "condition_id": condition["condition_id"],
            "observable_id": condition["observable_id"],
            "observable_value": value,
            "requested_wavelength_m": condition["wavelength_m"],
            "evaluated_wavelength_m": condition["wavelength_m"],
            "solved_wavelength_m": condition["wavelength_m"],
            "reflectance": 0.1,
            "transmittance": 0.7,
            "absorption": 0.2,
            "mesh_elements": 10_000,
            "minimum_mesh_quality": 0.2,
            "dataset_id": "dset1",
            "solution_id": "sol1",
            "derivative_dataset_id": "dset2",
            "derivative_solution_id": "sol2",
            "variable_ids": list(variable_ids),
            "raw_gradients": [
                {"real": value, "imaginary": 0.01},
                {"real": 2.0 * value, "imaginary": -0.02},
            ],
            "accepted_real_gradients": [value, 2.0 * value],
            "gradient_unit": "1/nm",
            "identity_fingerprints": {
                "primal": "1" * 64,
                "adjoint": "2" * 64,
                "study": "3" * 64,
                "solution": "4" * 64,
                "dataset": "5" * 64,
            },
        }


def _spec(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    spec = expand_robust_shape_manifest(envelope)
    wavelengths = sorted(
        {condition["wavelength_m"] for condition in spec["condition_table"]["conditions"]}
    )
    spec["adapter_configuration"]["configuration"]["material_tensor_rows"] = {
        "states": [
            {
                "state_id": state_id,
                "rows": [
                    {
                        "wavelength_m": wavelength,
                        "xx_real": 2.0,
                        "xx_imag": -0.1,
                        "yy_real": 2.0,
                        "yy_imag": -0.1,
                        "zz_real": 3.0,
                        "zz_imag": -0.2,
                    }
                    for wavelength in wavelengths
                ],
            }
            for state_id in spec["objective"]["state_ids"]
        ]
    }
    spec["adapter_configuration"]["configuration"]["condition_controls"] = {
        "dataset_tag": "dset1",
        "solution_tag": "sol1",
    }
    return spec


def test_complete_native_condition_gradients_are_durable_and_aggregate(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    backend = _Backend(observations)
    receipt = execute_native_condition_gradients(
        spec,
        ascii_tmp_path,
        backend=backend,
        observations=observations,
        cancel_requested=lambda: False,
    )
    assert receipt["complete"] is True
    assert receipt["variable_ids"] == ["patch_length_x", "patch_length_y"]
    assert len(backend.calls) == 24
    assert len(list(ascii_tmp_path.glob("condition-gradient-*.json"))) == 24
    assert (ascii_tmp_path / "native-aggregate-gradient.json").is_file()

    replay = _Backend(observations)
    second = execute_native_condition_gradients(
        spec,
        ascii_tmp_path,
        backend=replay,
        observations=observations,
        cancel_requested=lambda: False,
    )
    assert second == receipt
    assert replay.calls == []


def test_native_condition_gradient_rejects_tampered_replay(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    execute_native_condition_gradients(
        spec,
        ascii_tmp_path,
        backend=_Backend(observations),
        observations=observations,
        cancel_requested=lambda: False,
    )
    path = ascii_tmp_path / "condition-gradient-0000.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["accepted_real_gradients"][0] += 1.0
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="persisted native condition gradient"):
        execute_native_condition_gradients(
            spec,
            ascii_tmp_path,
            backend=_Backend(observations),
            observations=observations,
            cancel_requested=lambda: False,
        )


def test_native_condition_gradient_rejects_rehashed_semantic_drift(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    execute_native_condition_gradients(
        spec,
        ascii_tmp_path,
        backend=_Backend(observations),
        observations=observations,
        cancel_requested=lambda: False,
    )
    path = ascii_tmp_path / "condition-gradient-0000.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["accepted_real_gradients"][0] += 1.0
    receipt["receipt_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.robust_condition_gradient_receipt",
        {key: value for key, value in receipt.items() if key != "receipt_fingerprint"},
    )
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="accepted native gradient component"):
        execute_native_condition_gradients(
            spec,
            ascii_tmp_path,
            backend=_Backend(observations),
            observations=observations,
            cancel_requested=lambda: False,
        )


def test_native_condition_gradient_rejects_baseline_objective_drift(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    observations[0]["value"] += 0.1
    with pytest.raises(ValueError, match="objective differs from baseline"):
        execute_native_condition_gradients(
            spec,
            ascii_tmp_path,
            backend=_Backend(_observations(spec)),
            observations=observations,
            cancel_requested=lambda: False,
        )
