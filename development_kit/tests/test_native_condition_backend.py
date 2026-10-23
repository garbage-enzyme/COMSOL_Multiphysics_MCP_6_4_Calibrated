"""Retained native backend regressions; no campaign worker dependency."""

import json
from pathlib import Path

import pytest

from comsol_mcp.adapter import condition_backend
from comsol_mcp.jobs.store import read_json


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
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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

    assert condition_backend._get(FeatureCollection(), "ps1") is feature


def test_native_result_flatten_accepts_array_like_iterables():
    class ArrayLike:
        def __iter__(self):
            return iter((1.0, (2.0, 3.0)))

    assert condition_backend._flatten_real(ArrayLike()) == [1.0, 2.0, 3.0]


def test_native_solver_memory_policy_is_explicit_and_read_back():
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
    backend.controls = {"out_of_core_property": "ooc", "out_of_core_value": "on"}
    backend.linear_solver = _Feature()
    assert backend._set_solver_memory_policy() == {
        "property": "ooc",
        "requested": "on",
        "observed": "on",
    }


def test_native_mesh_reference_policy_is_explicit_and_read_back():
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
    backend.controls = {}
    assert backend._set_mesh_reference_policy() == {"mode": "model_existing"}


def test_native_mesh_reference_policy_rejects_readback_drift():
    class DriftModel(_ModelWithParameters):
        def parameter(self, name, value=None, *, evaluate=False):
            if value is not None:
                self.java.parameters.set(name, value)
            return "820[nm]"

    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
    backend.model = DriftModel()
    backend.controls = {
        "mesh_reference_parameter": "mesh_ref_wl",
        "mesh_reference_value": "1600[nm]",
    }
    with pytest.raises(ValueError, match="mesh reference parameter readback"):
        backend._set_mesh_reference_policy()


def test_native_solver_memory_policy_rejects_readback_drift():
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
    backend.controls = {"out_of_core_property": "ooc", "out_of_core_value": "on"}
    backend.linear_solver = _Feature(drift={"ooc": "auto"})
    with pytest.raises(ValueError, match="out-of-core policy readback"):
        backend._set_solver_memory_policy()


def test_native_solver_memory_policy_applies_active_coarse_solver_path():
    coarse = _Feature()
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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

    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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

    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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

    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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
    admissions = []
    backend._admit_mesh = lambda boundary: admissions.append(boundary)

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
    assert admissions == ["before-condition-solve"]
    assert backend.model.java.saved == [
        (
            str(backend.working_model_path),
            (),
            {"wl": "7.9999999999999996e-07[m]"},
        )
    ]


def test_native_sensitivity_preparation_follows_exact_condition_staging():
    events = []
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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
        ("deformation", True),
        ("sensitivity_prepare", ["rx", "ry"], 8e-7),
    ]


def _sensitivity_controls():
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
        "observable_expression": "comp1.ewfd.Torder_0_0",
        "wavelength_parameter": "wl",
        "forward_shape_application_mode": "deformation_stage",
        "forward_deformation_step_tag": "dg_step",
        "forward_deformation_step_type": "Stationary",
        "forward_deformation_physics_tag": "dg_pedot72",
        "forward_solved_shape_expressions": ["comp1.material.u", "comp1.material.v"],
        "forward_solved_shape_relative_tolerance": 5e-3,
        "sensitivity_parametric_sweep_tag": "sweep_pedot72",
        "sensitivity_feature_tag": "sens_pedot72",
        "sensitivity_solver_tag": "sn1",
        "sensitivity_segregated_solver_tag": "se1",
        "sensitivity_direct_solver_tags": ["dDef", "d1"],
        "sensitivity_solution_tags": ["sol1", "sol2", "sol3"],
        "sensitivity_dataset_tags": ["dset1", "dset2"],
        "derivative_solution_tag": "sol2",
        "derivative_dataset_tag": "dset2",
        "sensitivity_gradient_method": "adjoint",
        "sensitivity_solver_regeneration": "replace_existing_auto_sequence",
        "sensitivity_stationary_nonlinearity": "auto",
        "sensitivity_segregated_step_tags": ["ss1", "ss2"],
        "sensitivity_merged_step_tag": "ss1",
        "sensitivity_removed_step_tag": "ss2",
        "sensitivity_merged_linear_solver_tag": "d1",
        "sensitivity_constraint_group_policy": "merge_material_coordinates_into_wave_optics",
    }


def _sensitivity_support():
    return {
        "variables": [
            {
                "variable_id": "pedot_cylinder_radius_x",
                "unit": "nm",
                "baseline": 260.0,
                "scale": 1.0,
                "mapping": {"property_index": 0},
            },
            {
                "variable_id": "pedot_cylinder_radius_y",
                "unit": "nm",
                "baseline": 260.0,
                "scale": 1.0,
                "mapping": {"property_index": 1},
            },
        ]
    }


def test_native_sensitivity_initval_uses_prepared_initial_values():
    """Regression: the gradient is evaluated at the candidate point.

    The Sensitivity study solves and differentiates at its initval, so the
    initval must be the prepared initial values (the candidate point), not
    the declared baseline; otherwise the accepted-point gradient is silently
    computed at the baseline shape and the fail-closed observation
    comparison trips.
    """
    events: list[tuple] = []
    controls = _sensitivity_controls()

    class SensitivityNode:
        def __init__(self, tag):
            self.tag = tag
            self.values = {}
            self.active_state = True

        def active(self, value):
            self.active_state = bool(value)

        def isActive(self):
            return self.active_state

        def set(self, name, value):
            self.values[name] = list(value) if isinstance(value, list) else str(value)

        def getString(self, name):
            if name == "gradientMethod":
                return "adjoint"
            raise KeyError(name)

        def getType(self):
            return "Sensitivity"

        def getStringArray(self, name):
            return self.values[name]

    sensitivity = SensitivityNode("sens_pedot72")

    class SegregatedStep:
        def __init__(self, variables, components, solver):
            self.values = {"segvar": variables, "segcomp": components, "linsolver": solver}

        def getType(self):
            return "SegregatedStep"

        def getStringArray(self, name):
            return self.values[name]

        def getString(self, name):
            return self.values[name]

        def set(self, name, value):
            self.values[name] = list(value) if isinstance(value, list) else str(value)

    class SegregatedSteps:
        def __init__(self):
            self.items = {
                "ss1": SegregatedStep(["ewfd", "conpar1"], ["Ex", "rx"], "d1"),
                "ss2": SegregatedStep(["material", "conpar1"], ["u", "rx"], "dDef"),
            }

        def tags(self):
            return list(self.items)

        def remove(self, tag):
            del self.items[tag]

        def feature(self, tag=None):
            return self if tag is None else self.items[tag]

    class Segregated:
        def __init__(self):
            self.tag = "se1"
            self.steps = SegregatedSteps()

        def getType(self):
            return "Segregated"

        def feature(self, tag=None):
            return self.steps if tag is None else self.steps.items[tag]

    class DirectNode:
        def __init__(self, tag):
            self.tag = tag
            self.properties = {"ooc": "auto"}

        def getType(self):
            return "Direct"

        def set(self, name, value):
            self.properties[name] = str(value)

        def getString(self, name):
            return self.properties[name]

    class Stationary:
        tag = "s1"

        def __init__(self):
            self.items = {
                "sn1": SensitivityNode("sn1"),
                "se1": Segregated(),
                "dDef": DirectNode("dDef"),
                "d1": DirectNode("d1"),
            }
            self.nonlin = "auto"

        def feature(self, tag=None):
            if tag is None:
                return _FakeSolverFeatures(list(self.items.values()))
            return self.items[tag]

        def getString(self, name):
            if name == "nonlin":
                return self.nonlin
            raise KeyError(name)

        def getType(self):
            return "Stationary"

    stationary = Stationary()

    class Solution:
        def __init__(self):
            self.attributes = [
                _FakeSolverFeature("st1", "StudyStep"),
                stationary,
            ]

        def feature(self, tag=None):
            if tag is None:
                return _FakeSolverFeatures(list(self.attributes))
            for attribute in self.attributes:
                if attribute.tag == tag:
                    return attribute
            raise KeyError(tag)

    class Solutions:
        def __init__(self):
            self.items = ["sol1"]

        def remove(self, tag):
            events.append(("sol_remove", tag))
            self.items.remove(tag)

        def tags(self):
            return list(self.items)

        def sol(self, tag):
            assert tag == "sol1"
            return Solution()

    solutions = Solutions()

    class StudyStep:
        def setSolveFor(self, path, value):
            events.append(("step_solvefor", path, bool(value)))

    class Study:
        def __init__(self):
            self.items = {"dg_step": _StageStep("dg_step"), "wl_step": _StageStep("wl_step")}
            self.order = ["dg_step", "wl_step"]

        def feature(self):
            return self

        def tags(self):
            return list(self.order)

        def create(self, tag, feature_type):
            events.append(("create", tag, feature_type))
            # New study features land before the wave-optics study step, so
            # the regenerated order is [sweep, sensitivity, wl_step].
            self.order.insert(max(0, len(self.order) - 1), tag)
            if tag == sweep_tag:
                return SensitivityNode(tag)
            if tag == sens_tag:
                return sensitivity
            raise KeyError(tag)

        def remove(self, tag):
            events.append(("remove", tag))
            self.order.remove(tag)
            del self.items[tag]

        def createAutoSequences(self, scope):
            events.append(("auto_sequences", scope))
            solutions.items = ["sol1"]

    sweep_tag = controls["sensitivity_parametric_sweep_tag"]
    sens_tag = controls["sensitivity_feature_tag"]
    study = Study()

    class PhysicsPath:
        def resolveModelPath(self):
            return "/physics/dg_pedot72"

    class PhysicsCollection:
        def get(self, tag):
            assert tag == "dg_pedot72"
            return PhysicsPath()

        def __call__(self, tag):
            return self.get(tag)

    class Component:
        def physics(self, _tag=None):
            return PhysicsCollection()

    class Components:
        def get(self, _tag):
            return Component()

        def __call__(self, tag):
            return self.get(tag)

    class Java:
        def study(self, _tag):
            return study

        def component(self, _tag=None):
            return Components() if _tag is None else Component()

        def sol(self, *args):
            if args:
                return solutions.sol(args[0])
            return solutions

    class Model:
        java = Java()

    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
    backend.controls = controls
    backend.model = Model()
    backend.study = study
    backend.study_step = StudyStep()
    backend.support = _sensitivity_support()
    backend._initial_values = [271.998421905626, 271.997609904722]
    backend._forward_stage_readback = {"receipt_fingerprint": "x" * 64}

    readback = backend._prepare_native_sensitivity(
        ["pedot_cylinder_radius_x", "pedot_cylinder_radius_y"],
        wavelength_m=8e-7,
    )

    initval = [str(item) for item in sensitivity.getStringArray("initval")]
    assert initval == [
        "2.7199842190562602e-07[m]",
        "2.7199760990472204e-07[m]",
    ]
    assert readback["initial_values_m"] == pytest.approx([271.998421905626e-9, 271.997609904722e-9])
    assert readback["receipt_fingerprint"]
    assert sensitivity.values["pname"] == ["pedot_cylinder_radius_x", "pedot_cylinder_radius_y"]
    assert ("remove", "dg_step") in events
    assert ("auto_sequences", "all") in events


def test_native_sensitivity_requires_prepared_initial_values():
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
    backend.controls = _sensitivity_controls()
    backend.support = _sensitivity_support()
    with pytest.raises(RuntimeError, match="requires prepared initial values"):
        backend._prepare_native_sensitivity(
            ["pedot_cylinder_radius_x", "pedot_cylinder_radius_y"],
            wavelength_m=8e-7,
        )


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
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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
            "domain_count": 6,
            "boundary_count": 32,
            "free_domains": [1, 2, 3, 4, 5, 6],
            "fixed_boundaries": [1, 2],
            "pedot_boundaries": [18, 19, 20, 23, 25, 27],
            "support_fingerprint": "a" * 64,
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
    backend._shape_controls_receipt = {
        "shape_support_fingerprint": "a" * 64,
        "controls": {
            "deformed_geometry": {
                "free_domains": [1, 2, 3, 4, 5, 6],
                "fixed_boundaries": [1, 2],
                "pedot_boundaries": [18, 19, 20, 23, 25, 27],
                "height_preserved": True,
                "center_preserved": True,
            }
        },
    }
    backend.mesh_policy = {
        "max_elements_per_model": 20000,
        "minimum_element_quality": 0.1,
        "quality_measure": "volcircum",
    }
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
    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
    backend.controls = {"schema_version": "1.4.0"}
    with pytest.raises(ValueError, match="1.5.0"):
        backend._forward_shape_controls()
    backend.controls = {"schema_version": "1.5.0", "forward_shape_application_mode": "remesh"}
    with pytest.raises(ValueError, match="mode is unsupported"):
        backend._forward_shape_controls()


def _application_model(
    events,
    *,
    moved=(12.0e-9, 12.0e-9),
    baseline=(260.0e-9, 260.0e-9),
    flipped_extreme=False,
):
    """Fake model for run_shape_application with radius-extent evaluations.

    Vertex 0 sits at the deformed x-extreme (x = center_x + baseline_x +
    moved_x), vertex 1 at the deformed y-extreme. The declared component
    expressions evaluate to the moved components at those vertices.  With
    flipped_extreme the first vertex is the opposite-side extreme carrying
    the same displacement magnitude with the wrong sign (the readback must
    still measure the radius magnitude, not the signed offset).
    """

    class MeshStats:
        def getNumElem(self):
            return 18985

        def getMinQuality(self):
            return 0.2314

        def getQualityMeasure(self):
            return "volcircum"

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
            if flipped_extreme:
                # First vertex: opposite-side extreme (same magnitude, wrong
                # sign); second vertex: movement-side extreme.
                xs = [0.85e-6 - baseline[0] - moved[0], 0.85e-6 + baseline[0] + moved[0]]
                ys = [0.0, baseline[1] + moved[1]]
                results = [xs, ys]
                for expression in expressions[2:]:
                    if "material.u" in expression:
                        results.append([-moved[0] - 8.0e-11, moved[0]])
                    elif "material.v" in expression:
                        results.append([0.0, moved[1]])
                    else:
                        results.append([0.0, 0.0])
                return results
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


def test_shape_application_solves_stage_and_reads_back_solved_shape(ascii_tmp_path):
    events = []
    backend = _stage_backend(events)
    backend.working_model_path = ascii_tmp_path / "working.mph"
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


def test_shape_application_refuses_over_cap_mesh_before_study_run(ascii_tmp_path):
    events = []
    backend = _stage_backend(events)
    backend._prepare_forward_shape_stage()
    backend.model = _application_model(events)
    backend.mesh_policy = {
        "max_elements_per_model": 1,
        "minimum_element_quality": 0.1,
        "quality_measure": "volcircum",
    }
    backend.working_model_path = ascii_tmp_path / "working.mph"
    backend.study = type("Study", (), {"run": lambda self: events.append(("study_run",))})()
    with pytest.raises(ValueError, match="mesh admission refused"):
        backend.run_shape_application()
    assert ("study_run",) not in events
    receipt = json.loads((ascii_tmp_path / "mesh-admission-before-shape-solve.json").read_text())
    assert receipt["admitted"] is False
    assert receipt["element_count"] == 18985


@pytest.mark.parametrize(
    "cap,quality,measure,admitted",
    [
        (18985, 0.2314, "volcircum", True),
        (18984, 0.2314, "volcircum", False),
        (18985, 0.2315, "volcircum", False),
        (18985, 0.1, "other", False),
    ],
)
def test_mesh_admission_checks_equality_quality_and_measure(
    ascii_tmp_path, cap, quality, measure, admitted
):
    backend = _stage_backend([])
    backend.model = _application_model([])
    backend.working_model_path = ascii_tmp_path / "working.mph"
    backend.mesh_policy = {
        "max_elements_per_model": cap,
        "minimum_element_quality": quality,
        "quality_measure": measure,
    }
    if admitted:
        assert backend._admit_mesh("after-build")["admitted"] is True
    else:
        with pytest.raises(ValueError, match="mesh admission refused"):
            backend._admit_mesh("after-build")
    receipt = read_json(ascii_tmp_path / "mesh-admission-after-build.json")
    assert receipt["admitted"] is admitted
    assert receipt["quality_measure"] == "volcircum"


def test_shape_application_readback_mismatch_fails_closed(ascii_tmp_path):
    events = []
    backend = _stage_backend(events)
    backend.working_model_path = ascii_tmp_path / "working.mph"
    backend._prepare_forward_shape_stage()
    backend.model = _application_model(events, moved=(1.0e-9, 1.0e-9))

    class Study:
        def run(self):
            events.append(("study_run",))

    backend.study = Study()
    with pytest.raises(ValueError, match="solved-shape readback differs"):
        backend.run_shape_application()


def test_shape_application_does_not_widen_strict_relative_tolerance_to_one_nanometre(
    ascii_tmp_path,
):
    events = []
    backend = _stage_backend(events)
    backend.working_model_path = ascii_tmp_path / "working.mph"
    backend.controls["forward_solved_shape_relative_tolerance"] = 1e-6
    backend._prepare_forward_shape_stage()
    backend.model = _application_model(events, moved=(12.58e-9, 12.58e-9))

    class Study:
        def run(self):
            events.append(("study_run",))

    backend.study = Study()
    with pytest.raises(ValueError, match="solved-shape readback differs"):
        backend.run_shape_application()


def test_shape_application_zero_movement_readback_accepts_null_field(ascii_tmp_path):
    events = []
    backend = _stage_backend(events)
    backend.working_model_path = ascii_tmp_path / "working.mph"
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


def test_shape_application_opposite_extreme_flip_reads_radius_magnitude(ascii_tmp_path):
    """Regression: the max |component| vertex may sit on the opposite side.

    The opposite extreme carries the same displacement magnitude with the
    wrong sign; the readback must measure the radius magnitude and stay on
    the movement-side extreme, otherwise the signed offset becomes negative
    and fails closed for legitimate GCMMA proposals.
    """
    events = []
    backend = _stage_backend(events)
    backend.working_model_path = ascii_tmp_path / "working.mph"
    backend._prepare_forward_shape_stage()
    backend.model = _application_model(events, flipped_extreme=True)

    class Study:
        def run(self):
            events.append(("study_run",))

    backend.study = Study()
    receipt = backend.run_shape_application()

    solved = receipt["solved_shape"]
    assert solved[0]["max_component_m"] == pytest.approx(12.0e-9)
    assert solved[0]["observed_radius_m"] == pytest.approx(272.0e-9)
    assert all(item["matches"] for item in solved)


def test_shape_application_requires_prepared_initial_values():
    events = []
    backend = _stage_backend(events)
    del backend._initial_values
    with pytest.raises(RuntimeError, match="requires prepared initial values"):
        backend.run_shape_application()


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

    backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
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


def test_condition_evaluator_requires_exact_arity(monkeypatch):
    """Excess EvalGlobal values mean a widened scope; they must not pass."""
    import jpype

    monkeypatch.setattr(jpype, "JArray", lambda *_args: lambda values: values)

    def build(value_count):
        backend = object.__new__(condition_backend.ClientapiLin2025ConditionBackend)
        backend._counter = 0
        backend.controls = {
            "wavelength_parameter": "wl",
            "study_step_property": "plist",
            "study_step_array_property": None,
            "component_tag": "comp1",
            "mesh_tag": "mesh1",
            "dataset_tag": "dset1",
            "solution_tag": "sol1",
            "observable_expression": "obs",
            "reflectance_expression": "R",
            "transmittance_expression": "T",
            "absorption_expression": "A",
            "evaluated_wavelength_expression": "wl_eval",
            "solved_wavelength_expression": "wl_sol",
        }
        backend.material = type("Material", (), {"apply_material_state": lambda *_: None})()
        backend._set_incidence = lambda _condition: None
        backend._set_solver_memory_policy = lambda: {
            "property": "ooc",
            "requested": "on",
            "observed": "on",
        }
        backend._set_solver_selection = lambda: None
        backend._admit_mesh = lambda _boundary: None

        class Parameters:
            def set(self, _name, _value):
                return None

        class Java(Parameters):
            removed = []

            def __init__(self):
                self.param_handle = Parameters()

            def param(self):
                return self.param_handle

            def save(self, _path):
                return None

            def component(self):
                java = self

                class Component:
                    def get(self, _tag):
                        return self

                    def mesh(self, _tag):
                        class Statistics:
                            def getNumElem(self):
                                return 12

                            def getMinQuality(self):
                                return 0.9

                        class Mesh:
                            def stat(self):
                                return Statistics()

                        return Mesh()

                return Component()

        class StudyStep:
            value = None

            def set(self, _name, value):
                self.value = value

            def getString(self, _name):
                return self.value

        class Evaluator:
            def set(self, _name, _value):
                return None

            def computeResult(self):
                return [float(index) for index in range(value_count)]

        class Numerical:
            def __init__(self):
                self.created = []

            def create(self, _tag, _kind):
                return Evaluator()

            def remove(self, _tag):
                return None

        class Study:
            def run(self):
                return None

        class Model:
            def __init__(self):
                self.java = Java()

        backend.study_step = StudyStep()
        backend.numerical = Numerical()
        backend.study = Study()
        backend.model = Model()
        backend.working_model_path = "unused.mph"
        return backend

    excess = build(7)
    with pytest.raises(ValueError, match="exactly one value per expression"):
        excess.evaluate_condition(
            {
                "condition_id": "condition-0",
                "observable_id": "obs-0",
                "material_state_id": "OX",
                "wavelength_m": 8e-7,
            },
            ["1"] * 9,
        )

    exact = build(6)
    receipt = exact.evaluate_condition(
        {
            "condition_id": "condition-0",
            "observable_id": "obs-0",
            "material_state_id": "OX",
            "wavelength_m": 8e-7,
        },
        ["1"] * 9,
    )
    assert receipt["observable_value"] == 0.0
    assert receipt["mesh_elements"] == 12
