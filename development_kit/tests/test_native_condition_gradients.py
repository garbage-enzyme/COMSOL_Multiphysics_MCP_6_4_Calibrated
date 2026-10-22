"""Raw native gradient integrity regressions without a campaign runtime."""

from __future__ import annotations

import json
import math

import pytest

from comsol_mcp.contracts.native_condition import native_condition_execution_spec
from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.jobs.native_condition_gradients import collect_native_condition_gradients
from development_kit.tests.test_native_condition_matrix import _raw


def _spec(path):
    return native_condition_execution_spec(_raw(path)["points"][0]["collectors"][0]["inputs"])


def _observations(spec):
    return [
        {
            "condition_id": row["condition_id"],
            "observable_id": row["observable_id"],
            "value": 0.7,
            "evidence_sha256": "a" * 64,
            "disposition": "measured",
        }
        for row in spec["condition_table"]["conditions"]
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
            "gradient_unit": "1/m",
            "identity_fingerprints": {
                "primal": "1" * 64,
                "adjoint": "2" * 64,
                "study": "3" * 64,
                "solution": "4" * 64,
                "dataset": "5" * 64,
            },
        }


def test_native_gradient_refuses_declared_units_on_unconverted_si_values(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)

    class MislabeledBackend(_Backend):
        def evaluate_condition_gradient(self, condition, tensor_expressions, variable_ids):
            result = super().evaluate_condition_gradient(
                condition, tensor_expressions, variable_ids
            )
            result["gradient_unit"] = "1/nm"
            return result

    with pytest.raises(ValueError, match="gradient unit"):
        collect_native_condition_gradients(
            spec,
            ascii_tmp_path,
            conditions=spec["condition_table"]["conditions"],
            backend=MislabeledBackend(observations),
            observations=observations,
            cancel_requested=lambda: False,
        )
    assert not list(ascii_tmp_path.glob("condition-gradient-*.json"))


def test_native_gradient_replay_refuses_rehashed_non_si_receipt(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    collect_native_condition_gradients(
        spec,
        ascii_tmp_path,
        conditions=spec["condition_table"]["conditions"],
        backend=_Backend(observations),
        observations=observations,
        cancel_requested=lambda: False,
    )
    path = ascii_tmp_path / "condition-gradient-0000.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["gradient_unit"] = "1/nm"
    receipt["receipt_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.robust_condition_gradient_receipt",
        {key: value for key, value in receipt.items() if key != "receipt_fingerprint"},
    )
    path.write_text(json.dumps(receipt), encoding="utf-8")
    replay = _Backend(observations)
    with pytest.raises(ValueError, match="gradient unit"):
        collect_native_condition_gradients(
            spec,
            ascii_tmp_path,
            conditions=spec["condition_table"]["conditions"],
            backend=replay,
            observations=observations,
            cancel_requested=lambda: False,
        )
    assert replay.calls == []


@pytest.mark.parametrize("field", ["reflectance", "transmittance", "absorption"])
def test_native_condition_gradient_rejects_out_of_range_power_channels(ascii_tmp_path, field):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    backend = _Backend(observations)
    original_evaluate = backend.evaluate_condition_gradient

    def evaluate_with_bad_channel(condition, tensor_expressions, variable_ids):
        result = original_evaluate(condition, tensor_expressions, variable_ids)
        result[field] = 1.5
        return result

    backend.evaluate_condition_gradient = evaluate_with_bad_channel
    target = ascii_tmp_path / f"overshoot-{field}"
    target.mkdir()
    with pytest.raises(ValueError, match=f"{field} must be within"):
        collect_native_condition_gradients(
            spec,
            target,
            conditions=spec["condition_table"]["conditions"],
            backend=backend,
            observations=observations,
            cancel_requested=lambda: False,
        )


def test_native_condition_gradient_rejects_tampered_replay(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    collect_native_condition_gradients(
        spec,
        ascii_tmp_path,
        conditions=spec["condition_table"]["conditions"],
        backend=_Backend(observations),
        observations=observations,
        cancel_requested=lambda: False,
    )
    path = ascii_tmp_path / "condition-gradient-0000.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    receipt["accepted_real_gradients"][0] += 1.0
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="persisted native condition gradient"):
        collect_native_condition_gradients(
            spec,
            ascii_tmp_path,
            conditions=spec["condition_table"]["conditions"],
            backend=_Backend(observations),
            observations=observations,
            cancel_requested=lambda: False,
        )


def test_native_condition_gradient_rejects_rehashed_semantic_drift(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    collect_native_condition_gradients(
        spec,
        ascii_tmp_path,
        conditions=spec["condition_table"]["conditions"],
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
        collect_native_condition_gradients(
            spec,
            ascii_tmp_path,
            conditions=spec["condition_table"]["conditions"],
            backend=_Backend(observations),
            observations=observations,
            cancel_requested=lambda: False,
        )


def test_native_condition_gradient_rejects_baseline_objective_drift(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    observations[0]["value"] += 0.1
    with pytest.raises(ValueError, match="objective differs from baseline"):
        collect_native_condition_gradients(
            spec,
            ascii_tmp_path,
            conditions=spec["condition_table"]["conditions"],
            backend=_Backend(_observations(spec)),
            observations=observations,
            cancel_requested=lambda: False,
        )


class _UlpBackend(_Backend):
    """Backend whose accepted real part is one ulp above the raw value."""

    def evaluate_condition_gradient(self, condition, tensor_expressions, variable_ids):
        result = super().evaluate_condition_gradient(condition, tensor_expressions, variable_ids)
        result["accepted_real_gradients"] = [
            math.nextafter(item, math.inf) for item in result["accepted_real_gradients"]
        ]
        return result


def test_native_condition_gradient_rejects_duplicate_baseline_observations(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    duplicated = [*observations, dict(observations[0])]
    with pytest.raises(ValueError, match="unique baseline observations"):
        collect_native_condition_gradients(
            spec,
            ascii_tmp_path,
            conditions=spec["condition_table"]["conditions"],
            backend=_Backend(_observations(spec)),
            observations=duplicated,
            cancel_requested=lambda: False,
        )


def test_raw_collection_replays_without_solves_and_observes_cancel(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)

    def collect(backend, cancelled=False):
        return collect_native_condition_gradients(
            spec,
            ascii_tmp_path,
            backend=backend,
            conditions=spec["condition_table"]["conditions"],
            observations=observations,
            cancel_requested=lambda: cancelled,
        )

    rows = collect(_Backend(observations))
    replay = _Backend(observations)
    assert collect(replay) == rows
    assert replay.calls == []
    assert not (ascii_tmp_path / "native-aggregate-gradient.json").exists()
    with pytest.raises(InterruptedError):
        collect(replay, True)
    assert replay.calls == []


def test_raw_accepted_real_gradient_tolerates_last_ulp_rounding(ascii_tmp_path):
    spec = _spec(ascii_tmp_path)
    observations = _observations(spec)
    rows = collect_native_condition_gradients(
        spec,
        ascii_tmp_path,
        backend=_UlpBackend(observations),
        conditions=spec["condition_table"]["conditions"],
        observations=observations,
        cancel_requested=lambda: False,
    )
    assert len(rows) == 1
