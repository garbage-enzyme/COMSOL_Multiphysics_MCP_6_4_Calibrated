"""JobManager and synthetic robust shape worker lifecycle tests."""

from __future__ import annotations

import os

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
