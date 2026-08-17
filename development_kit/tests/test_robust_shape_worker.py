"""JobManager and synthetic robust shape worker lifecycle tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from comsol_mcp.jobs import (
    robust_condition_runtime,
    robust_shape_native_runtime,
    robust_shape_worker,
)
from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.robust_condition_runtime import execute_robust_conditions
from comsol_mcp.jobs.robust_shape_rows import append_robust_shape_row, read_robust_shape_rows
from comsol_mcp.jobs.robust_shape_worker import run as run_robust_worker
from comsol_mcp.jobs.store import atomic_write_json, process_identity, read_json
from development_kit.tests.test_gradient_contracts import _outer_optimizer
from development_kit.tests.test_robust_shape_optimization import _write_manifest


def _condition_runtime_spec() -> dict:
    conditions = []
    for index, state_id in enumerate(("OX", "MR")):
        conditions.append(
            {
                "condition_id": f"condition-{index}",
                "order": index,
                "wavelength_m": 8e-7,
                "incidence_elevation_deg": 0.0,
                "incidence_azimuth_deg": 0.0,
                "polarization_basis_id": "x_linear",
                "material_state_id": state_id,
                "objective_role": "objective",
                "observable_id": "transmission_order_0_0",
                "active": True,
            }
        )
    return {
        "spec_fingerprint": "f" * 64,
        "condition_table": {"conditions": conditions},
        "adapter_configuration": {
            "configuration": {
                "condition_controls": {
                    "geometry_tag": "geom1",
                    "dataset_tag": "dset1",
                    "solution_tag": "sol1",
                },
                "material_tensor_rows": {
                    "states": [
                        {
                            "state_id": state_id,
                            "rows": [
                                {
                                    "wavelength_m": 8e-7,
                                    "xx_real": 2.0,
                                    "xx_imag": -0.1,
                                    "yy_real": 2.0,
                                    "yy_imag": -0.1,
                                    "zz_real": 3.0,
                                    "zz_imag": -0.2,
                                }
                            ],
                        }
                        for state_id in ("OX", "MR")
                    ]
                },
            }
        },
        "finalist_validation_policy": {
            "mesh_convergence": {
                "max_elements_per_model": 1000,
                "minimum_element_quality": 0.2,
            }
        },
    }


class _ConditionBackend:
    def __init__(
        self,
        *,
        dataset_id="dset1",
        solution_id="sol1",
        mesh_elements=1000,
        minimum_mesh_quality=0.2,
    ):
        self.calls = []
        self.dataset_id = dataset_id
        self.solution_id = solution_id
        self.mesh_elements = mesh_elements
        self.minimum_mesh_quality = minimum_mesh_quality

    def evaluate_condition(self, condition, tensor_expressions):
        self.calls.append((condition["condition_id"], tensor_expressions))
        return {
            "condition_id": condition["condition_id"],
            "observable_id": condition["observable_id"],
            "observable_value": 0.6 + condition["order"] * 0.1,
            "requested_wavelength_m": condition["wavelength_m"],
            "evaluated_wavelength_m": condition["wavelength_m"],
            "solved_wavelength_m": condition["wavelength_m"],
            "reflectance": 0.2,
            "transmittance": 0.6,
            "absorption": 0.2,
            "mesh_elements": self.mesh_elements,
            "minimum_mesh_quality": self.minimum_mesh_quality,
            "dataset_id": self.dataset_id,
            "solution_id": self.solution_id,
        }


def test_outer_optimizer_worker_state_binds_units_budget_and_candidate_identity():
    optimizer = _outer_optimizer()
    optimizer["budget"]["max_solves"] = 105
    spec = {
        "spec_fingerprint": "a" * 64,
        "native_optimizer": optimizer,
        "initial_values": [260.0, 260.0],
        "support": {
            "variables": [
                {"variable_id": "radius_x", "lower": 200.0, "upper": 320.0, "unit": "nm"},
                {"variable_id": "radius_y", "lower": 200.0, "upper": 320.0, "unit": "nm"},
            ]
        },
    }
    state = robust_shape_worker._optimizer_state_from_spec(spec)
    assert state["max_condition_solves"] == 105
    assert robust_shape_worker._gradient_in_declared_units(spec, [2e5, 3e5]) == pytest.approx(
        [2e-4, 3e-4]
    )
    candidate = robust_shape_worker._candidate_spec(spec, [270.0, 250.0])
    assert candidate["initial_values"] == [270.0, 250.0]
    assert candidate["spec_fingerprint"] != spec["spec_fingerprint"]


def test_condition_runtime_persists_receipts_rows_and_exact_replay(ascii_tmp_path):
    spec = _condition_runtime_spec()
    backend = _ConditionBackend()
    observations = execute_robust_conditions(
        spec,
        ascii_tmp_path,
        attempt=1,
        backend=backend,
        cancel_requested=lambda: False,
    )
    assert [item["value"] for item in observations] == [0.6, 0.7]
    assert len(backend.calls) == 2
    replay_backend = _ConditionBackend()
    replay = execute_robust_conditions(
        spec,
        ascii_tmp_path,
        attempt=1,
        backend=replay_backend,
        cancel_requested=lambda: False,
    )
    assert replay == observations
    assert replay_backend.calls == []


def test_condition_runtime_honors_explicit_execution_limit(ascii_tmp_path):
    spec = _condition_runtime_spec()
    spec["condition_execution_limit"] = 1
    backend = _ConditionBackend()
    observations = execute_robust_conditions(
        spec,
        ascii_tmp_path,
        attempt=1,
        backend=backend,
        cancel_requested=lambda: False,
    )
    assert len(observations) == 1
    assert [call[0] for call in backend.calls] == ["condition-0"]


def test_condition_runtime_recovers_receipt_written_before_row(ascii_tmp_path, monkeypatch):
    spec = _condition_runtime_spec()
    spec["condition_table"]["conditions"] = spec["condition_table"]["conditions"][:1]
    backend = _ConditionBackend()
    original = robust_condition_runtime.append_robust_shape_row
    monkeypatch.setattr(
        robust_condition_runtime,
        "append_robust_shape_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected row failure")),
    )
    with pytest.raises(OSError, match="injected row failure"):
        execute_robust_conditions(
            spec,
            ascii_tmp_path,
            attempt=1,
            backend=backend,
            cancel_requested=lambda: False,
        )
    assert len(backend.calls) == 1
    monkeypatch.setattr(robust_condition_runtime, "append_robust_shape_row", original)
    recovery_backend = _ConditionBackend()
    execute_robust_conditions(
        spec,
        ascii_tmp_path,
        attempt=1,
        backend=recovery_backend,
        cancel_requested=lambda: False,
    )
    assert recovery_backend.calls == []


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        (_ConditionBackend(dataset_id="dset-other"), "dataset identity"),
        (_ConditionBackend(solution_id="sol-other"), "solution identity"),
    ],
)
def test_condition_runtime_rejects_dataset_or_solution_drift(ascii_tmp_path, backend, message):
    spec = _condition_runtime_spec()
    spec["condition_table"]["conditions"] = spec["condition_table"]["conditions"][:1]
    with pytest.raises(ValueError, match=message):
        execute_robust_conditions(
            spec,
            ascii_tmp_path,
            attempt=1,
            backend=backend,
            cancel_requested=lambda: False,
        )
    assert not (ascii_tmp_path / "condition-0000.json").exists()


@pytest.mark.parametrize(
    ("backend", "message"),
    [
        (_ConditionBackend(mesh_elements=1001), "element cap"),
        (_ConditionBackend(minimum_mesh_quality=0.199), "quality"),
    ],
)
def test_condition_runtime_enforces_caller_mesh_admission(ascii_tmp_path, backend, message):
    spec = _condition_runtime_spec()
    spec["condition_table"]["conditions"] = spec["condition_table"]["conditions"][:1]
    with pytest.raises(ValueError, match=message):
        execute_robust_conditions(
            spec,
            ascii_tmp_path,
            attempt=1,
            backend=backend,
            cancel_requested=lambda: False,
        )
    assert not (ascii_tmp_path / "condition-0000.json").exists()


class _LoadFailureClient:
    port = None

    def __init__(self, *, clear_fails=False):
        self.clear_fails = clear_fails
        self.clear_calls = 0

    def load(self, _path):
        raise RuntimeError("injected load failure")

    def clear(self):
        self.clear_calls += 1
        if self.clear_fails:
            raise RuntimeError("injected clear failure")


@pytest.mark.parametrize("clear_fails", [False, True])
def test_native_runtime_records_observed_client_cleanup_on_startup_failure(
    ascii_tmp_path, clear_fails
):
    source = ascii_tmp_path / "source.mph"
    source.write_bytes(b"fixture")
    client = _LoadFailureClient(clear_fails=clear_fails)
    prior_temporary_directory = os.environ.get("COMSOL_TMPDIR")
    with pytest.raises(RuntimeError, match="injected load failure"):
        robust_shape_native_runtime.execute_lin2025_conditions(
            {
                "source_model_path": str(source),
                "cores": 2,
                "version": "6.4",
                "comsol_temporary_directory": str(ascii_tmp_path),
            },
            ascii_tmp_path,
            attempt=1,
            client_factory=lambda **_kwargs: client,
            java_environment_reader=os.environ.get,
            cancel_requested=lambda: False,
        )
    cleanup = read_json(ascii_tmp_path / "native-cleanup.json")
    environment = read_json(ascii_tmp_path / "comsol-temporary-directory.json")
    assert client.clear_calls == 1
    assert cleanup["client_clear"] is (not clear_fails)
    assert cleanup["source_model_removed"] is False
    assert cleanup["working_model_removed"] is False
    assert cleanup["client_disconnect"] == "not_applicable"
    assert cleanup["errors"] == ([] if not clear_fails else ["client_clear:RuntimeError"])
    assert environment == {
        "control": "COMSOL_TMPDIR",
        "java_environment_path": str(ascii_tmp_path),
        "matches": True,
        "process_environment_path": str(ascii_tmp_path),
        "requested_path": str(ascii_tmp_path),
    }
    assert os.environ.get("COMSOL_TMPDIR") == prior_temporary_directory


class _CleanupOwnership:
    def __init__(self, *, inventory_complete=True, external=None, release=True):
        self.inventory_complete = inventory_complete
        self.external = external or []
        self.release_result = release
        self.status_calls = []

    def release(self):
        return {"success": self.release_result, "released": self.release_result}

    def status(self, *, require_fresh_inventory=False):
        self.status_calls.append(require_fresh_inventory)
        return {
            "process_inventory": {"complete": self.inventory_complete},
            "lease": {"state": "absent" if self.release_result else "active"},
            "external_solver_processes": self.external,
        }


def test_licensed_cleanup_uses_native_receipt_and_fresh_ownership_status(ascii_tmp_path):
    atomic_write_json(
        ascii_tmp_path / "native-cleanup.json",
        {
            "source_model_removed": True,
            "working_model_removed": True,
            "client_clear": True,
            "client_disconnect": "not_applicable",
            "errors": [],
        },
    )
    ownership = _CleanupOwnership()
    payload = robust_shape_worker._finalize_licensed_cleanup(
        ascii_tmp_path,
        ownership=ownership,
        lease_acquired=True,
        native_runtime_entered=True,
        source_unchanged=True,
    )
    assert ownership.status_calls == [True]
    assert all(payload.values())
    receipt = read_json(ascii_tmp_path / "licensed-cleanup.json")
    assert receipt["errors"] == []
    assert receipt["inventory_fingerprint"]
    assert receipt["native_cleanup_fingerprint"]


def test_licensed_cleanup_fails_closed_on_false_clear_or_incomplete_inventory(
    ascii_tmp_path,
):
    atomic_write_json(
        ascii_tmp_path / "native-cleanup.json",
        {"client_clear": False, "errors": ["client_clear:RuntimeError"]},
    )
    payload = robust_shape_worker._finalize_licensed_cleanup(
        ascii_tmp_path,
        ownership=_CleanupOwnership(inventory_complete=False),
        lease_acquired=True,
        native_runtime_entered=True,
        source_unchanged=True,
    )
    assert payload["client_clear"] is False
    assert payload["owned_processes_absent"] is False
    assert payload["lease_released"] is True
    assert read_json(ascii_tmp_path / "licensed-cleanup.json")["errors"] == [
        "native_cleanup:reported_errors",
        "owned_processes_absent:unproved",
    ]


class _Feature:
    def __init__(self, *, drift=None, active=None, active_events=None, tag=None, children=None):
        self.values = {}
        self.drift = drift or {}
        self.active_state = active
        self.active_events = active_events
        self.tag = tag
        self.children = children or {}

    def set(self, name, value):
        self.values[name] = value

    def getString(self, name):
        return self.drift.get(name, self.values[name])

    def active(self, value):
        if self.active_events is not None:
            self.active_events.append((self.tag, bool(value)))
        self.active_state = bool(value)

    def isActive(self):
        return self.active_state

    def feature(self, tag):
        return self.children[tag]


class _Parameters:
    def __init__(self):
        self.values = {}

    def set(self, name, value):
        self.values[name] = value


class _JavaWithParameters:
    def __init__(self):
        self.parameters = _Parameters()

    def param(self):
        return self.parameters


class _ModelWithParameters:
    def __init__(self):
        self.java = _JavaWithParameters()

    def parameter(self, name, value=None, *, evaluate=False):
        if value is not None:
            self.java.parameters.set(name, value)
        return self.java.parameters.values[name]


class _JavaWithSave(_JavaWithParameters):
    def __init__(self):
        super().__init__()
        self.saved = []

    def save(self, path, *args):
        self.saved.append((path, args, dict(self.parameters.values)))


class _ModelWithSave:
    def __init__(self):
        self.java = _JavaWithSave()


def _incidence_backend(*, periodic_drift=None, port_drift=None):
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.model = _ModelWithParameters()
    backend.controls = {
        "elevation_parameter": "theta",
        "azimuth_parameter": "phi",
        "geometry_tag": "geom1",
        "angle_property": "alpha1_inc",
        "azimuth_property": "alpha2_inc",
        "polarization_property": "Polarization",
        "linear_polarization_property": "LinearPol",
        "polarization_values": {"x_linear": "S", "y_linear": "P"},
    }
    backend.periodic = _Feature(drift=periodic_drift)
    backend.ports = [_Feature(drift=port_drift), _Feature()]
    return backend


def test_native_incidence_reads_back_parent_ports_and_polarization():
    backend = _incidence_backend()
    backend._set_incidence(
        {
            "incidence_elevation_deg": 30.0,
            "incidence_azimuth_deg": 0.0,
            "polarization_basis_id": "y_linear",
        }
    )
    assert backend.model.java.parameters.values == {
        "theta": "30[deg]",
        "phi": "0[deg]",
    }
    assert backend.periodic.values["Polarization"] == "LinearPol"
    assert backend.periodic.values["LinearPol"] == "P"


def test_native_tag_resolution_supports_physics_feature_collections():
    feature = object()

    class FeatureCollection:
        def feature(self, tag):
            assert tag == "ps1"
            return feature

    assert robust_shape_native_runtime._get(FeatureCollection(), "ps1") is feature


def test_native_result_flatten_accepts_array_like_iterables():
    class ArrayLike:
        def __iter__(self):
            return iter((1.0, (2.0, 3.0)))

    assert robust_shape_native_runtime._flatten_real(ArrayLike()) == [1.0, 2.0, 3.0]


def test_native_solver_memory_policy_is_explicit_and_read_back():
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {"out_of_core_property": "ooc", "out_of_core_value": "on"}
    backend.linear_solver = _Feature()
    assert backend._set_solver_memory_policy() == {
        "property": "ooc",
        "requested": "on",
        "observed": "on",
    }


def test_native_mesh_reference_policy_is_explicit_and_read_back():
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.model = _ModelWithParameters()
    backend.controls = {
        "mesh_reference_parameter": "mesh_ref_wl",
        "mesh_reference_value": "1600[nm]",
    }

    assert backend._set_mesh_reference_policy() == {
        "mode": "explicit",
        "parameter": "mesh_ref_wl",
        "requested": "1600[nm]",
        "observed": "1600[nm]",
    }
    assert backend.model.java.parameters.values == {"mesh_ref_wl": "1600[nm]"}


def test_native_mesh_reference_policy_preserves_legacy_model_state():
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {}
    assert backend._set_mesh_reference_policy() == {"mode": "model_existing"}


def test_native_mesh_reference_policy_rejects_readback_drift():
    class DriftModel(_ModelWithParameters):
        def parameter(self, name, value=None, *, evaluate=False):
            if value is not None:
                self.java.parameters.set(name, value)
            return "820[nm]"

    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.model = DriftModel()
    backend.controls = {
        "mesh_reference_parameter": "mesh_ref_wl",
        "mesh_reference_value": "1600[nm]",
    }
    with pytest.raises(ValueError, match="mesh reference parameter readback"):
        backend._set_mesh_reference_policy()


def test_native_solver_memory_policy_rejects_readback_drift():
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {"out_of_core_property": "ooc", "out_of_core_value": "on"}
    backend.linear_solver = _Feature(drift={"ooc": "auto"})
    with pytest.raises(ValueError, match="out-of-core policy readback"):
        backend._set_solver_memory_policy()


def test_native_solver_memory_policy_applies_active_coarse_solver_path():
    coarse = _Feature()
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {
        "out_of_core_property": "ooc",
        "out_of_core_value": "on",
        "coarse_solver_feature_path": ["i1", "mg1", "cs", "dDef"],
        "coarse_solver_out_of_core_property": "ooc",
        "coarse_solver_out_of_core_value": "on",
    }
    backend.linear_solver = _Feature()
    backend.stationary_solver = _Feature(
        children={
            "i1": _Feature(
                children={"mg1": _Feature(children={"cs": _Feature(children={"dDef": coarse})})}
            )
        }
    )
    assert backend._set_solver_memory_policy() == {
        "property": "ooc",
        "requested": "on",
        "observed": "on",
        "coarse_solver": {
            "feature_path": ["i1", "mg1", "cs", "dDef"],
            "property": "ooc",
            "requested": "on",
            "observed": "on",
        },
    }


def test_native_solver_memory_policy_rejects_coarse_readback_drift():
    coarse = _Feature(drift={"ooc": "auto"})
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {
        "out_of_core_property": "ooc",
        "out_of_core_value": "on",
        "coarse_solver_feature_path": ["i1", "dDef"],
        "coarse_solver_out_of_core_property": "ooc",
        "coarse_solver_out_of_core_value": "on",
    }
    backend.linear_solver = _Feature()
    backend.stationary_solver = _Feature(children={"i1": _Feature(children={"dDef": coarse})})
    with pytest.raises(ValueError, match="coarse solver out-of-core"):
        backend._set_solver_memory_policy()


def test_native_solver_selection_is_explicit_ordered_and_read_back():
    events = []
    features = {
        "d1": _Feature(active=True, active_events=events, tag="d1"),
        "i1": _Feature(active=False, active_events=events, tag="i1"),
    }

    class Stationary:
        def feature(self, tag):
            return features[tag]

    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {
        "selected_linear_solver_tag": "i1",
        "inactive_linear_solver_tags": ["d1"],
    }
    backend.stationary_solver = Stationary()
    assert backend._set_solver_selection() == {
        "mode": "explicit",
        "selected_linear_solver_tag": "i1",
        "inactive_linear_solver_tags": ["d1"],
        "observed_active": {"i1": True, "d1": False},
    }
    assert events == [("i1", True), ("d1", False)]


def test_native_solver_selection_preserves_legacy_model_state():
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {}
    assert backend._set_solver_selection() == {"mode": "model_existing"}


def test_native_solver_selection_rejects_active_readback_drift():
    class InactiveFeature(_Feature):
        def active(self, value):
            pass

    features = {
        "d1": _Feature(active=True),
        "i1": InactiveFeature(active=False),
    }

    class Stationary:
        def feature(self, tag):
            return features[tag]

    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {
        "selected_linear_solver_tag": "i1",
        "inactive_linear_solver_tags": ["d1"],
    }
    backend.stationary_solver = Stationary()
    with pytest.raises(ValueError, match="selection readback differs"):
        backend._set_solver_selection()


def _deformation_backend(feature):
    class Physics:
        def get(self, tag):
            assert tag == "dg_pedot72"
            return feature

    class Component:
        def physics(self):
            return Physics()

    class Components:
        def get(self, tag):
            assert tag == "comp1"
            return Component()

    class Java:
        def component(self):
            return Components()

    class Model:
        java = Java()

    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.model = Model()
    backend.controls = {"component_tag": "comp1"}
    return backend


def test_forward_phase_explicitly_deactivates_shape_deformation():
    feature = _Feature(active=True)
    receipt = _deformation_backend(feature).set_shape_deformation_active(False)
    assert receipt == {
        "physics_tag": "dg_pedot72",
        "requested_active": False,
        "observed_active": False,
    }


def test_shape_deformation_active_readback_drift_fails_closed():
    class Drift(_Feature):
        def active(self, value):
            pass

    with pytest.raises(ValueError, match="active-state readback differs"):
        _deformation_backend(Drift(active=True)).set_shape_deformation_active(False)


def test_native_condition_saves_exact_configured_model_before_solve(ascii_tmp_path):
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.model = _ModelWithSave()
    backend.working_model_path = (ascii_tmp_path / "robust-working.mph").resolve()
    backend._counter = 0
    backend.controls = {
        "wavelength_parameter": "wl",
        "study_step_property": "plist",
        "study_step_array_property": None,
    }
    backend.material = type(
        "Material",
        (),
        {"apply_material_state": lambda _self, _state, _tensor: None},
    )()
    backend._set_incidence = lambda _condition: None
    backend._set_solver_memory_policy = lambda: {
        "property": "ooc",
        "requested": "on",
        "observed": "on",
    }
    backend.study_step = _Feature()

    class Study:
        def run(self):
            assert backend.model.java.saved
            raise RuntimeError("injected solve failure")

    backend.study = Study()
    with pytest.raises(RuntimeError, match="injected solve failure"):
        backend.evaluate_condition(
            {
                "condition_id": "condition-0",
                "material_state_id": "OX",
                "wavelength_m": 8e-7,
            },
            ["1"] * 9,
        )
    assert backend.model.java.saved == [
        (
            str(backend.working_model_path),
            (),
            {"wl": "7.9999999999999996e-07[m]"},
        )
    ]


def test_native_sensitivity_preparation_follows_exact_condition_staging():
    events = []
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend._native_sensitivity_prepared = False
    backend.controls = {
        "wavelength_parameter": "wl",
        "study_step_property": "plist",
    }

    class Material:
        def apply_material_state(self, state, tensor):
            events.append(("material", state, tensor))

    class Parameters:
        def set(self, name, value):
            events.append(("parameter", name, value))

    class Java:
        def param(self):
            return Parameters()

    class Model:
        java = Java()

    class StudyStep:
        def set(self, name, value):
            events.append(("study_step", name, value))

    backend.material = Material()
    backend.model = Model()
    backend.study_step = StudyStep()
    backend._set_incidence = lambda _condition: events.append(("incidence",))
    backend._set_solver_memory_policy = lambda: events.append(("solver_memory",))
    backend._set_solver_selection = lambda: events.append(("solver_selection",))
    backend.set_shape_deformation_active = lambda active: events.append(("deformation", active))

    def prepare(variable_ids, *, wavelength_m):
        events.append(("sensitivity_prepare", variable_ids, wavelength_m))
        raise RuntimeError("stop after ordered staging")

    backend._prepare_native_sensitivity = prepare
    condition = {
        "material_state_id": "OX",
        "wavelength_m": 8e-7,
    }

    with pytest.raises(RuntimeError, match="ordered staging"):
        backend.evaluate_condition_gradient(condition, ["1"] * 9, ["rx", "ry"])

    assert events == [
        ("material", "OX", ["1"] * 9),
        ("parameter", "wl", "7.9999999999999996e-07[m]"),
        ("incidence",),
        ("study_step", "plist", "wl"),
        ("solver_memory",),
        ("solver_selection",),
        ("deformation", True),
        ("sensitivity_prepare", ["rx", "ry"], 8e-7),
    ]


def _forward_stage_controls():
    return {
        "schema_version": "1.5.0",
        "component_tag": "comp1",
        "geometry_tag": "geom1",
        "physics_tag": "ewfd",
        "study_tag": "std1",
        "study_step_tag": "wl_step",
        "solution_tag": "sol1",
        "stationary_solver_tag": "s1",
        "linear_solver_tag": "d1",
        "selected_linear_solver_tag": "d1",
        "inactive_linear_solver_tags": ["i1"],
        "out_of_core_property": "ooc",
        "out_of_core_value": "on",
        "dataset_tag": "dset1",
        "mesh_tag": "mesh1",
        "forward_shape_application_mode": "deformation_stage",
        "forward_deformation_step_tag": "dg_step",
        "forward_deformation_step_type": "Stationary",
        "forward_deformation_physics_tag": "dg_pedot72",
        "forward_solved_shape_expressions": ["comp1.material.u", "comp1.material.v"],
        "forward_solved_shape_relative_tolerance": 5e-3,
    }


class _PhysicsPath:
    def __init__(self, path):
        self.path = path

    def resolveModelPath(self):
        return self.path


class _StageStep:
    def __init__(self, tag, feature_type="Stationary", solve_for_drift=None):
        self.tag = tag
        self.feature_type = feature_type
        self.solve_for = {}
        self.solve_for_drift = solve_for_drift or {}
        self.created = False

    def setSolveFor(self, path, requested):
        self.solve_for[path] = bool(requested)

    def solveFor(self, path):
        return self.solve_for_drift.get(path, self.solve_for.get(path, None))

    def getType(self):
        return self.feature_type


class _StageFeatures:
    def __init__(self, items, events):
        self.items = dict(items)
        self.order = list(items)
        self.events = events

    def tags(self):
        return list(self.order)

    def create(self, tag, feature_type):
        self.events.append(("create", tag, feature_type))
        step = _StageStep(tag, feature_type)
        step.created = True
        self.items[tag] = step
        self.order.append(tag)
        return step

    def move(self, tag, index):
        self.events.append(("move", tag, index))
        self.order.remove(tag)
        self.order.insert(index, tag)

    def remove(self, tag):
        self.events.append(("remove", tag))
        self.order.remove(tag)
        self.items.pop(tag)

    def get(self, tag):
        return self.items[tag]


class _FakeSolverFeature:
    def __init__(self, tag, feature_type, children=None, properties=None):
        self.tag = tag
        self.feature_type = feature_type
        self.children = list(children or [])
        self.properties = dict(properties or {})
        self.active_state = True

    def getType(self):
        return self.feature_type

    def getString(self, name):
        if name not in self.properties:
            raise ValueError(f"unknown property {name}")
        return self.properties[name]

    def set(self, name, value):
        self.properties[name] = str(value)

    def active(self, value):
        self.active_state = bool(value)

    def isActive(self):
        return self.active_state

    def feature(self, tag=None):
        if tag is None:
            return _FakeSolverFeatures(self.children)
        for child in self.children:
            if child.tag == tag:
                return child
        raise KeyError(tag)


class _FakeSolverFeatures:
    def __init__(self, items):
        self.items = list(items)

    def tags(self):
        return [item.tag for item in self.items]

    def feature(self, tag=None):
        if tag is None:
            return self
        for item in self.items:
            if item.tag == tag:
                return item
        raise KeyError(tag)


def _regenerated_solver_features(study_order):
    """Regenerated sol1 tree: per study step a StudyStep plus a Stationary.

    The wave-optics stationary solver owns the declared Direct solver tag and
    the inactive iterative tag; the deformation solver only owns dDef, so the
    re-resolution must pick the wave-optics solver structurally.
    """
    nodes = []
    for index, step in enumerate(study_order, start=1):
        nodes.append(_FakeSolverFeature(f"st{index}", "StudyStep", properties={"studystep": step}))
        if step == "wl_step":
            children = [
                _FakeSolverFeature("dDef", "Direct", properties={"ooc": "auto"}),
                _FakeSolverFeature("aDef", "Advanced"),
                _FakeSolverFeature("p1", "Parametric"),
                _FakeSolverFeature("fc1", "FullyCoupled"),
                _FakeSolverFeature("d1", "Direct", properties={"ooc": "auto"}),
                _FakeSolverFeature("i1", "Iterative"),
            ]
        else:
            children = [
                _FakeSolverFeature("dDef", "Direct"),
                _FakeSolverFeature("fc1", "FullyCoupled"),
            ]
        nodes.append(_FakeSolverFeature(f"s{index}", "Stationary", children=children))
    return nodes


class _FakeSolution:
    def __init__(self, tag, features):
        self.tag = tag
        self._features = _FakeSolverFeatures(features)

    def feature(self, tag=None):
        if tag is None:
            return self._features
        return self._features.feature(tag)


def _stage_backend(events, *, with_shape_support=True, solve_for_drift=None):
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    features = _StageFeatures(
        {"wl_step": _StageStep("wl_step", solve_for_drift=solve_for_drift)},
        events,
    )
    dg = _PhysicsPath("/physics/dg_pedot72")
    ewfd = _PhysicsPath("/physics/ewfd")

    class Physics:
        def __init__(self, node):
            self.node = node

        def resolveModelPath(self):
            return self.node.path

    class PhysicsCollection:
        def __init__(self):
            self.nodes = {"dg_pedot72": Physics(dg), "ewfd": Physics(ewfd)}

        def get(self, tag):
            events.append(("physics", tag))
            return self.nodes[tag]

        def __call__(self, tag):
            return self.get(tag)

    class Component:
        def physics(self, _tag=None):
            return PhysicsCollection()

    class Components:
        def get(self, tag):
            events.append(("component", tag))
            return Component()

        def __call__(self, tag):
            return self.get(tag)

    class Solutions:
        def __init__(self):
            self.tree = {
                "sol1": _FakeSolution("sol1", _regenerated_solver_features(["wl_step"])),
            }
            self.items = ["sol1"]

        def remove(self, tag):
            events.append(("sol_remove", tag))
            self.items.remove(tag)
            self.tree.pop(tag, None)

        def tags(self):
            return list(self.items)

        def sol(self, tag):
            return self.tree[tag]

    solutions = Solutions()

    class Study:
        def feature(self):
            return features

        def createAutoSequences(self, scope):
            events.append(("auto_sequences", scope))
            features.events.append(("auto_sequences", scope))
            study_order = list(features.order)
            solutions.tree["sol1"] = _FakeSolution(
                "sol1", _regenerated_solver_features(study_order)
            )
            solutions.items = ["sol1", "sol2"] if len(study_order) > 1 else ["sol1"]
            solutions.tree.setdefault("sol2", _FakeSolution("sol2", []))

    class Java:
        def study(self, _tag):
            return Study()

        def component(self, _tag=None):
            return Components() if _tag is None else Component()

        def sol(self, *args):
            if args:
                return solutions.sol(args[0])
            return solutions

    class Model:
        java = Java()

    backend.model = Model()
    backend.controls = _forward_stage_controls()
    backend.study_step = features.items["wl_step"]
    backend.study = Study()
    backend.shape_support = (
        {
            "center_um": [0.85, 0.0],
            "baseline_radius_um": 0.26,
        }
        if with_shape_support
        else {}
    )
    backend.support = {
        "variables": [
            {
                "variable_id": "pedot_cylinder_radius_x",
                "unit": "nm",
                "baseline": 260.0,
                "mapping": {
                    "feature_tag": "pedot_pedot72",
                    "feature_type": "PrescribedMeshDisplacement",
                    "property_index": 0,
                    "property_name": "dx",
                },
            },
            {
                "variable_id": "pedot_cylinder_radius_y",
                "unit": "nm",
                "baseline": 260.0,
                "mapping": {
                    "feature_tag": "pedot_pedot72",
                    "feature_type": "PrescribedMeshDisplacement",
                    "property_index": 1,
                    "property_name": "dx",
                },
            },
        ]
    }
    backend._initial_values = [272.0, 272.0]
    backend.working_model_path = Path("C:/mcp_tests/robust-working.mph")
    return backend


def test_forward_shape_stage_creates_ordered_scoped_step_and_regenerates():
    events = []
    backend = _stage_backend(events)
    receipt = backend._prepare_forward_shape_stage()

    assert receipt["mode"] == "deformation_stage"
    assert receipt["study_step_order"] == ["dg_step", "wl_step"]
    assert receipt["solve_for"] == {
        "deformation_step_forward": False,
        "deformation_step_deformation": True,
        "forward_step_deformation": False,
        "forward_step_forward": True,
    }
    assert receipt["solution_tags"] == ["sol1", "sol2"]
    assert receipt["forward_solver"] == {
        "stationary_solver_tag": "s2",
        "linear_solver_tag": "d1",
        "observed_solution_tags": ["sol1", "sol2"],
    }
    assert backend.stationary_solver.tag == "s2"
    assert backend.linear_solver.tag == "d1"
    assert ("create", "dg_step", "Stationary") in events
    assert ("move", "dg_step", 0) in events
    assert ("sol_remove", "sol1") in events
    assert ("auto_sequences", "all") in events
    assert receipt["receipt_fingerprint"]
    assert receipt["receipt_fingerprint"] == backend._forward_stage_readback["receipt_fingerprint"]


def test_forward_shape_stage_re_resolves_solver_for_memory_and_selection_policy():
    """Regression: regenerating the sequence invalidates cached solver nodes.

    The declared stationary tag now belongs to the deformation solver; the
    memory and selection policies must act on the re-resolved wave-optics
    solver (s2/d1), not on a dangling or wrong-solver node.
    """
    events = []
    backend = _stage_backend(events)
    backend._prepare_forward_shape_stage()

    memory = backend._set_solver_memory_policy()
    assert memory["requested"] == "on"
    assert memory["observed"] == "on"
    assert backend.linear_solver.properties["ooc"] == "on"
    assert backend.stationary_solver.tag == "s2"

    selection = backend._set_solver_selection()
    assert selection["observed_active"] == {"d1": True, "i1": False}
    d1 = backend.stationary_solver.feature("d1")
    i1 = backend.stationary_solver.feature("i1")
    assert d1.isActive() is True
    assert i1.isActive() is False


def test_forward_shape_stage_rejects_existing_step_or_drift():
    events = []
    backend = _stage_backend(events)
    backend.controls = dict(backend.controls)
    backend.controls["forward_deformation_step_tag"] = "wl_step"
    with pytest.raises(ValueError, match="already exists"):
        backend._prepare_forward_shape_stage()

    events = []
    backend = _stage_backend(
        events,
        solve_for_drift={"/physics/dg_pedot72": True},
    )
    with pytest.raises(ValueError, match="solve-for readback differs"):
        backend._prepare_forward_shape_stage()


def test_forward_shape_controls_require_1_5_0_and_deformation_stage():
    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {"schema_version": "1.4.0"}
    with pytest.raises(ValueError, match="1.5.0"):
        backend._forward_shape_controls()
    backend.controls = {"schema_version": "1.5.0", "forward_shape_application_mode": "remesh"}
    with pytest.raises(ValueError, match="mode is unsupported"):
        backend._forward_shape_controls()


def _application_model(events, *, moved=(12.0e-9, 12.0e-9), baseline=(260.0e-9, 260.0e-9)):
    """Fake model for run_shape_application with radius-extent evaluations.

    Vertex 0 sits at the deformed x-extreme (x = center_x + baseline_x +
    moved_x), vertex 1 at the deformed y-extreme. The declared component
    expressions evaluate to the moved components at those vertices.
    """

    class MeshStats:
        def getNumElem(self):
            return 18985

        def getMinQuality(self):
            return 0.2314

    class Mesh:
        def stat(self):
            return MeshStats()

    class PhysicsPath:
        def __init__(self):
            self.active_state = True

        def active(self, value):
            self.active_state = bool(value)

        def isActive(self):
            return self.active_state

        def resolveModelPath(self):
            return "/physics/dg_pedot72"

    class Physics:
        def __init__(self):
            self.active_state = True

        def active(self, value):
            self.active_state = bool(value)

        def isActive(self):
            return self.active_state

        def resolveModelPath(self):
            return "/physics/ewfd"

    class PhysicsCollection:
        def __init__(self):
            self.dg = PhysicsPath()

        def get(self, tag):
            return self.dg if tag == "dg_pedot72" else Physics()

        def __call__(self, tag):
            return self.get(tag)

    class Component:
        def physics(self, _tag=None):
            return PhysicsCollection()

        def mesh(self, _tag):
            return Mesh()

    class Components:
        def get(self, tag):
            return Component()

        def __call__(self, tag):
            return self.get(tag)

    class MeshNode:
        def __init__(self, tag):
            self.name = tag

        def tag(self):
            return self.name

    class DatasetCollection:
        def __iter__(self):
            return iter([MeshNode("dset1")])

    class Java:
        def component(self, _tag=None):
            return Components() if _tag is None else Component()

        def save(self, _path):
            events.append(("save",))

    class Model:
        java = Java()

        def __truediv__(self, group):
            assert group == "datasets"
            return DatasetCollection()

        def evaluate(self, expressions, dataset=None, outer=1):
            assert dataset is not None and outer == 1
            events.append(("evaluate", list(expressions)))
            xs = [0.85e-6 + baseline[0] + moved[0], 0.85e-6]
            ys = [0.0, baseline[1] + moved[1]]
            results = [xs, ys]
            for expression in expressions[2:]:
                if "material.u" in expression:
                    results.append([moved[0], 0.0])
                elif "material.v" in expression:
                    results.append([0.0, moved[1]])
                elif "disp" in expression:
                    results.append([moved[0], moved[1]])
                else:
                    results.append([0.0, 0.0])
            return results

    return Model()


def test_shape_application_solves_stage_and_reads_back_solved_shape():
    events = []
    backend = _stage_backend(events)
    backend._prepare_forward_shape_stage()
    backend.model = _application_model(events)

    class Study:
        def run(self):
            events.append(("study_run",))

    backend.study = Study()
    receipt = backend.run_shape_application()

    assert ("study_run",) in events
    assert ("save",) in events
    assert receipt["mode"] == "deformation_stage"
    assert receipt["initial_values"] == [272.0, 272.0]
    assert receipt["mesh_elements"] == 18985
    assert receipt["minimum_mesh_quality"] == 0.2314
    solved = receipt["solved_shape"]
    assert [item["variable_id"] for item in solved] == [
        "pedot_cylinder_radius_x",
        "pedot_cylinder_radius_y",
    ]
    assert [item["axis"] for item in solved] == [0, 1]
    assert all(item["expected_displacement_m"] == pytest.approx(12.0e-9) for item in solved)
    assert all(item["max_component_m"] == pytest.approx(12.0e-9) for item in solved)
    assert all(item["observed_radius_m"] == pytest.approx(272.0e-9) for item in solved)
    assert all(item["matches"] for item in solved)
    assert receipt["receipt_fingerprint"]
    assert receipt["stage_fingerprint"] == backend._forward_stage_readback["receipt_fingerprint"]


def test_shape_application_readback_mismatch_fails_closed():
    events = []
    backend = _stage_backend(events)
    backend._prepare_forward_shape_stage()
    backend.model = _application_model(events, moved=(1.0e-9, 1.0e-9))

    class Study:
        def run(self):
            events.append(("study_run",))

    backend.study = Study()
    with pytest.raises(ValueError, match="solved-shape readback differs"):
        backend.run_shape_application()


def test_shape_application_zero_movement_readback_accepts_null_field():
    events = []
    backend = _stage_backend(events)
    backend._prepare_forward_shape_stage()
    backend.model = _application_model(events, moved=(0.0, 0.0))
    backend._initial_values = [260.0, 260.0]

    class Study:
        def run(self):
            events.append(("study_run",))

    backend.study = Study()
    receipt = backend.run_shape_application()

    assert all(item["matches"] for item in receipt["solved_shape"])
    assert all(item["max_component_m"] == 0.0 for item in receipt["solved_shape"])
    assert all(
        item["observed_radius_m"] == pytest.approx(260.0e-9) for item in receipt["solved_shape"]
    )


def test_shape_application_requires_prepared_initial_values():
    events = []
    backend = _stage_backend(events)
    del backend._initial_values
    with pytest.raises(RuntimeError, match="requires prepared initial values"):
        backend.run_shape_application()


def test_worker_shape_application_fingerprint_helper():
    assert robust_shape_worker._shape_application_fingerprint({}) is None
    assert (
        robust_shape_worker._shape_application_fingerprint(
            {"shape_application": {"receipt_fingerprint": "a" * 64}}
        )
        == "a" * 64
    )
    assert robust_shape_worker._shape_application_fingerprint({"shape_application": None}) is None
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        robust_shape_worker._shape_application_fingerprint(
            {"shape_application": {"receipt_fingerprint": "short"}}
        )


def test_native_sensitivity_constraint_groups_are_merged_and_read_back():
    class Step:
        def __init__(self, variables, components, solver):
            self.values = {"segvar": variables, "segcomp": components, "linsolver": solver}

        def getType(self):
            return "SegregatedStep"

        def getStringArray(self, name):
            return self.values[name]

        def getString(self, name):
            return self.values[name]

        def set(self, name, value):
            self.values[name] = value

    class Steps:
        def __init__(self):
            self.items = {
                "ss1": Step(["ewfd", "conpar1"], ["Ex", "rx"], "d1"),
                "ss2": Step(["material", "conpar1"], ["u", "rx"], "dDef"),
            }

        def tags(self):
            return list(self.items)

        def remove(self, tag):
            del self.items[tag]

    class Segregated:
        def __init__(self):
            self.steps = Steps()

        def feature(self, tag=None):
            return self.steps if tag is None else self.steps.items[tag]

    class Stationary:
        def __init__(self):
            self.segregated = Segregated()

        def feature(self, tag):
            assert tag == "se1"
            return self.segregated

    backend = object.__new__(robust_shape_native_runtime.ClientapiLin2025ConditionBackend)
    backend.controls = {
        "sensitivity_constraint_group_policy": "merge_material_coordinates_into_wave_optics",
        "sensitivity_segregated_solver_tag": "se1",
        "sensitivity_segregated_step_tags": ["ss1", "ss2"],
        "sensitivity_merged_step_tag": "ss1",
        "sensitivity_removed_step_tag": "ss2",
        "sensitivity_merged_linear_solver_tag": "d1",
    }

    receipt = backend._merge_sensitivity_constraint_groups(Stationary())

    assert receipt["observed_step_tags"] == ["ss1"]
    assert receipt["variable_ids"] == ["ewfd", "conpar1", "material"]
    assert receipt["component_ids"] == ["Ex", "rx", "u"]
    assert receipt["linear_solver_tag"] == "d1"


def test_native_runtime_persists_controls_before_condition_failure(ascii_tmp_path, monkeypatch):
    source = ascii_tmp_path / "source.mph"
    source.write_bytes(b"fixture")
    saved = []

    class Java:
        def __init__(self, label):
            self.label = label

        def save(self, path, *args):
            saved.append((self.label, path, args))

    class Model:
        def __init__(self, label):
            self.java = Java(label)

    class Client:
        port = None

        def __init__(self):
            self.models = [Model("source"), Model("working")]

        def load(self, _path):
            return self.models.pop(0)

        def remove(self, _model):
            return None

        def clear(self):
            return None

    class Backend:
        def __init__(self, model, _spec, *, working_model_path):
            self.model = model
            self.working_model_path = working_model_path
            self.controls = {}

        def prepare(self, _initial_values):
            return {"receipt_fingerprint": "a" * 64, "configured": True}

    monkeypatch.setattr(robust_shape_native_runtime, "ClientapiLin2025ConditionBackend", Backend)
    monkeypatch.setattr(
        robust_shape_native_runtime,
        "execute_robust_conditions",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected condition failure")),
    )
    with pytest.raises(RuntimeError, match="injected condition failure"):
        robust_shape_native_runtime.execute_lin2025_conditions(
            {
                "source_model_path": str(source),
                "cores": 2,
                "version": "6.4",
                "comsol_temporary_directory": str(ascii_tmp_path),
                "initial_values": [260.0, 260.0],
            },
            ascii_tmp_path,
            attempt=1,
            client_factory=lambda **_kwargs: Client(),
            java_environment_reader=os.environ.get,
            cancel_requested=lambda: False,
        )
    assert read_json(ascii_tmp_path / "robust-controls.json") == {
        "configured": True,
        "receipt_fingerprint": "a" * 64,
    }
    assert saved[-1] == (
        "working",
        str((ascii_tmp_path / "robust-working.mph").resolve()),
        (),
    )


@pytest.mark.parametrize(
    ("periodic_drift", "port_drift", "message"),
    [
        ({"Polarization": "UserDefined"}, None, "polarization mode"),
        (None, {"alpha1_inc": "wrong_theta"}, "periodic port 1 elevation"),
        (None, {"alpha2_inc": "wrong_phi"}, "periodic port 1 azimuth"),
    ],
)
def test_native_incidence_rejects_parent_or_port_readback_drift(
    periodic_drift, port_drift, message
):
    backend = _incidence_backend(periodic_drift=periodic_drift, port_drift=port_drift)
    with pytest.raises(ValueError, match=message):
        backend._set_incidence(
            {
                "incidence_elevation_deg": 30.0,
                "incidence_azimuth_deg": 0.0,
                "polarization_basis_id": "x_linear",
            }
        )


def _manager(root, monkeypatch):
    manager = JobManager(root, preflight=lambda **_kwargs: {"success": True, "ready": True})
    monkeypatch.setattr(
        manager,
        "_launch_worker",
        lambda _job_id, module: (
            process_identity(os.getpid())
            if module == "comsol_mcp.jobs.robust_shape_worker"
            else (_ for _ in ()).throw(AssertionError(module))
        ),
    )
    monkeypatch.setattr(
        robust_shape_worker,
        "collect_resource_telemetry",
        lambda **_kwargs: {
            "stage": "pre_mesh",
            "available_memory_bytes": 2 * 1024**3,
            "total_memory_bytes": 16 * 1024**3,
            "runtime_free_bytes": 200 * 1024**3,
        },
    )
    return manager


def test_licensed_optimizer_budget_exhaustion_is_not_reported_as_finalist_pending():
    error, event = robust_shape_worker._licensed_optimizer_terminal(
        {"status": "budget_exhausted"}, 0
    )

    assert error == {
        "type": "RobustOptimizerBudgetExhausted",
        "message": (
            "Bounded robust GCMMA exhausted its condition-solve budget "
            "before an accepted optimizer step"
        ),
    }
    assert event == "robust_gcmma_budget_exhausted"


def test_licensed_optimizer_accepted_step_advances_to_finalist_boundary():
    error, event = robust_shape_worker._licensed_optimizer_terminal(
        {"status": "budget_exhausted"}, 1
    )

    assert error["type"] == "RobustFinalistValidationPending"
    assert event == "robust_gcmma_phase_completed"


def test_manager_dispatches_synthetic_24_condition_job_without_solver(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    spec = manager.store.read_spec(submitted["job_id"])
    state = manager.store.read_state(submitted["job_id"])
    assert state["progress"] == {"completed": 0, "total": 24}
    assert run_robust_worker(str(manager.store.root), submitted["job_id"]) == 0
    terminal = manager.status(submitted["job_id"])
    rows = read_robust_shape_rows(
        manager.store.job_dir(submitted["job_id"]) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    assert terminal["status"] == "completed"
    assert terminal["solver_started"] is False
    assert terminal["robust_shape_progress"] == {
        "declared_conditions": 24,
        "completed_conditions": 24,
        "pending_conditions": 0,
        "row_count": 29,
        "last_row_sha256": rows[-1]["row_sha256"],
        "cleanup_recorded": True,
    }
    assert [row["kind"] for row in rows[-5:]] == [
        "gradient",
        "iteration",
        "finalist_validation",
        "checkpoint",
        "cleanup",
    ]
    finalist = read_json(manager.store.job_dir(submitted["job_id"]) / "finalist-validation.json")
    assert finalist["accepted"] is True
    assert terminal["finalist_validation_fingerprint"] == finalist["receipt_fingerprint"]


def test_worker_replays_complete_condition_without_duplicate_row(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    spec = manager.store.read_spec(job_id)
    first_condition = spec["condition_table"]["conditions"][0]
    first = append_robust_shape_row(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
        attempt=1,
        kind="condition",
        payload={
            "iteration_id": "it-0",
            "condition_id": first_condition["condition_id"],
            "condition_order": first_condition["order"],
            "status": "completed",
            "observation_fingerprint": "a" * 64,
            "objective_contribution": 0.7,
            "reason_code": "preexisting_complete",
        },
    )
    assert run_robust_worker(str(manager.store.root), job_id) == 0
    rows = read_robust_shape_rows(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    matches = [
        row
        for row in rows
        if row["kind"] == "condition"
        and row["payload"]["condition_id"] == first_condition["condition_id"]
    ]
    assert len(matches) == 1
    assert matches[0]["row_sha256"] == first["row_sha256"]


def test_exact_duplicate_submission_reuses_existing_job(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    first = manager.submit(envelope)
    second = manager.submit(envelope)
    assert second["duplicate"] is True
    assert second["job_id"] == first["job_id"]


def test_attempt_bound_cancel_records_cleanup_before_cooperative_observation(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    manager.store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
    assert run_robust_worker(str(manager.store.root), job_id) == 0
    spec = manager.store.read_spec(job_id)
    rows = read_robust_shape_rows(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    assert [row["kind"] for row in rows] == ["cleanup"]
    assert all(rows[0]["payload"].values())
    state = manager.store.read_state(job_id)
    assert state["cancel"]["cooperative_observation"]["target_attempt"] == 1


def test_startup_resource_refusal_is_durable_and_solver_free(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    monkeypatch.setattr(
        robust_shape_worker,
        "collect_resource_telemetry",
        lambda **_kwargs: {
            "stage": "pre_mesh",
            "available_memory_bytes": 1024**3 - 1,
            "total_memory_bytes": 16 * 1024**3,
            "runtime_free_bytes": 200 * 1024**3,
        },
    )
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    assert run_robust_worker(str(manager.store.root), job_id) == 1
    state = manager.store.read_state(job_id)
    receipt = read_json(manager.store.job_dir(job_id) / "startup-admission.json")
    assert state["status"] == "failed"
    assert state["solver_started"] is False
    assert receipt["decision"] == "refuse"
    assert receipt["checks"]["available_memory_meets_minimum"] is False


def test_rejected_finalist_is_durable_and_cleans_before_terminal_failure(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    original = robust_shape_worker._synthetic_finalist_evidence

    def rejected_evidence(spec, candidate, objective_value):
        evidence = original(spec, candidate, objective_value)
        evidence["manufacturability"]["minimum_gap_m"] = 0.0
        return evidence

    monkeypatch.setattr(robust_shape_worker, "_synthetic_finalist_evidence", rejected_evidence)
    submitted = manager.submit(envelope)
    job_id = submitted["job_id"]
    assert run_robust_worker(str(manager.store.root), job_id) == 1
    spec = manager.store.read_spec(job_id)
    rows = read_robust_shape_rows(
        manager.store.job_dir(job_id) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
    )
    assert [row["kind"] for row in rows[-2:]] == ["finalist_validation", "cleanup"]
    assert not any(row["kind"] == "checkpoint" for row in rows)
    assert rows[-2]["payload"]["status"] == "rejected"
    assert rows[-2]["payload"]["reason_codes"] == ["manufacturability_failed"]
    state = manager.store.read_state(job_id)
    assert state["status"] == "failed"
    assert state["solver_started"] is False
