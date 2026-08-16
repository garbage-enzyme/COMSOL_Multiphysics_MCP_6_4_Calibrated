"""ClientAPI backend for one-owner Lin2025 robust condition execution."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from comsol_mcp.durable import atomic_write_json, domain_sha256_v2
from comsol_mcp.research.lin2025_pedot_backend import (
    ClientapiLin2025PedotControlBackend,
    prepare_lin2025_pedot_shape_controls,
)
from comsol_mcp.tools.derived_geometry import _set_vector

from .robust_condition_runtime import RobustConditionBackend, execute_robust_conditions


def _tags(container: Any) -> list[str]:
    return [str(value) for value in list(container.tags())]


def _ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _get(container: Any, tag: str) -> Any:
    errors: list[Exception] = []
    for method_name in ("get", "feature"):
        method = getattr(container, method_name, None)
        if callable(method):
            try:
                return method(tag)
            except Exception as exc:
                errors.append(exc)
    if callable(container):
        try:
            return container(tag)
        except Exception as exc:
            errors.append(exc)
    detail = type(errors[-1]).__name__ if errors else type(container).__name__
    raise TypeError(f"cannot resolve COMSOL tag {tag!r} from {type(container).__name__}: {detail}")


def _flatten_real(value: Any) -> list[float]:
    values: list[float] = []

    def visit(item: Any) -> None:
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
            return
        if not isinstance(item, (str, bytes)):
            try:
                iterator = iter(item)
            except TypeError:
                iterator = None
            if iterator is not None:
                for child in iterator:
                    visit(child)
                return
        scalar = complex(item)
        if not math.isfinite(scalar.real) or not math.isfinite(scalar.imag):
            raise ValueError("COMSOL result contains a nonfinite value")
        if abs(scalar.imag) > 1e-10 * max(1.0, abs(scalar.real)):
            raise ValueError("COMSOL result unexpectedly contains an imaginary component")
        values.append(float(scalar.real))

    visit(value)
    if not values:
        raise ValueError("COMSOL result is empty")
    return values


def _complex_scalar(value: Any) -> complex:
    values: list[complex] = []

    def visit(item: Any) -> None:
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
            return
        if not isinstance(item, (str, bytes)):
            try:
                iterator = iter(item)
            except TypeError:
                iterator = None
            if iterator is not None:
                for child in iterator:
                    visit(child)
                return
        scalar = complex(item)
        if not math.isfinite(scalar.real) or not math.isfinite(scalar.imag):
            raise ValueError("COMSOL sensitivity result is nonfinite")
        values.append(scalar)

    visit(value)
    if len(values) != 1:
        raise ValueError("COMSOL sensitivity result is not scalar")
    return values[0]


def _dataset_by_tag(model: Any, tag: str) -> Any:
    matches = [dataset for dataset in model / "datasets" if str(dataset.tag()) == tag]
    if len(matches) != 1:
        raise ValueError(f"COMSOL sensitivity dataset identity is ambiguous: {tag}")
    return matches[0]


def _generated_sensitivity_identity(model: Any) -> dict[str, Any]:
    solutions = []
    for solution_tag in _tags(model.java.sol()):
        solution = model.java.sol(solution_tag)
        solutions.append(
            {
                "tag": solution_tag,
                "study": str(solution.study()),
                "empty": bool(solution.isEmpty()),
                "features": [
                    {
                        "tag": feature_tag,
                        "type": str(solution.feature(feature_tag).getType()),
                        "active": bool(solution.feature(feature_tag).isActive()),
                    }
                    for feature_tag in _tags(solution.feature())
                ],
            }
        )
    datasets = []
    for dataset_tag in _tags(model.java.result().dataset()):
        dataset = model.java.result().dataset(dataset_tag)
        linked_solution = ""
        try:
            linked_solution = str(dataset.getString("solution"))
        except Exception:
            linked_solution = ""
        datasets.append(
            {
                "tag": dataset_tag,
                "type": str(dataset.getType()),
                "solution": linked_solution,
            }
        )
    return {"solutions": solutions, "datasets": datasets}


class ClientapiLin2025ConditionBackend(RobustConditionBackend):
    """Apply all caller-declared condition controls to one derived model."""

    def __init__(self, model: Any, spec: Mapping[str, Any], *, working_model_path: Path):
        self.model = model
        self.working_model_path = working_model_path.resolve()
        configuration = spec["adapter_configuration"]["configuration"]
        self.controls = configuration["condition_controls"]
        self.fixture = configuration["fixture"]
        self.tree = configuration["tree_readback"]
        self.support = spec["support"]
        self.shape_support = configuration["shape_support"]
        self.material = ClientapiLin2025PedotControlBackend(
            model,
            component_tag=self.controls["component_tag"],
            geometry_tag=self.controls["geometry_tag"],
        )
        component = _get(model.java.component(), self.controls["component_tag"])
        physics = _get(component.physics(), self.controls["physics_tag"])
        self.periodic = _get(physics, self.controls["periodic_structure_tag"])
        self.ports = [_get(self.periodic, tag) for tag in self.controls["periodic_port_tags"]]
        self.study = _get(model.java.study(), self.controls["study_tag"])
        self.study_step = self.study.feature(self.controls["study_step_tag"])
        solution = model.java.sol(self.controls["solution_tag"])
        self.stationary_solver = solution.feature(self.controls["stationary_solver_tag"])
        self.linear_solver = self.stationary_solver.feature(self.controls["linear_solver_tag"])
        numerical = model.java.result().numerical()
        self.numerical = numerical
        self._counter = 0
        self._native_sensitivity_prepared = False
        self._sensitivity_sweep = None
        self._sensitivity_readback: dict[str, Any] | None = None

    def set_shape_deformation_active(self, requested: bool) -> dict[str, Any]:
        """Set and read back the derived shape deformation phase explicitly.

        The forward baseline must not solve the mesh-deformation physics: even
        at zero displacement it turns the otherwise linear Wave Optics solve
        into a stationary Newton system. Native gradient/shape phases enable it
        explicitly after the forward baseline has been durably recorded.
        """
        component = _get(self.model.java.component(), self.controls["component_tag"])
        deformation = _get(component.physics(), "dg_pedot72")
        deformation.active(bool(requested))
        observed = bool(deformation.isActive())
        if observed is not bool(requested):
            raise ValueError("Lin2025 shape deformation active-state readback differs")
        return {
            "physics_tag": "dg_pedot72",
            "requested_active": bool(requested),
            "observed_active": observed,
        }

    def prepare(self, initial_values: list[float]) -> dict[str, Any]:
        if len(initial_values) != 2:
            raise ValueError("Lin2025 native runtime requires two initial radius values")
        controls = prepare_lin2025_pedot_shape_controls(
            self.material, self.fixture, self.tree, self.support
        )
        variables = self.support["variables"]
        for variable, value in zip(variables, initial_values, strict=True):
            self.model.java.param().set(
                variable["variable_id"], f"{float(value):.17g}[{variable['unit']}]"
            )
        geometry = _get(self.model.java.component(), self.controls["component_tag"]).geom(
            self.controls["geometry_tag"]
        )
        geometry.run()
        mesh = _get(self.model.java.component(), self.controls["component_tag"]).mesh(
            self.controls["mesh_tag"]
        )
        mesh_reference = self._set_mesh_reference_policy()
        mesh.run()
        body: dict[str, Any] = {
            "shape_controls": controls,
            "forward_shape_deformation": self.set_shape_deformation_active(False),
            "mesh_reference": mesh_reference,
            "solver_memory": self._set_solver_memory_policy(),
            "solver_selection": self._set_solver_selection(),
        }
        body["receipt_fingerprint"] = domain_sha256_v2("comsol_mcp.robust_native_controls", body)
        return body

    def _set_mesh_reference_policy(self) -> dict[str, Any]:
        parameter = self.controls.get("mesh_reference_parameter")
        requested = self.controls.get("mesh_reference_value")
        if parameter is None and requested is None:
            return {"mode": "model_existing"}
        if not isinstance(parameter, str) or not isinstance(requested, str):
            raise ValueError("mesh reference control is incomplete")
        self.model.parameter(parameter, requested)
        observed = str(self.model.parameter(parameter, evaluate=False))
        if observed != requested:
            raise ValueError("mesh reference parameter readback differs")
        return {
            "mode": "explicit",
            "parameter": parameter,
            "requested": requested,
            "observed": observed,
        }

    def _set_incidence(self, condition: Mapping[str, Any]) -> None:
        params = self.model.java.param()
        params.set(
            self.controls["elevation_parameter"],
            f"{float(condition['incidence_elevation_deg']):.17g}[deg]",
        )
        params.set(
            self.controls["azimuth_parameter"],
            f"{float(condition['incidence_azimuth_deg']):.17g}[deg]",
        )
        for node in [self.periodic, *self.ports]:
            node.set(self.controls["angle_property"], self.controls["elevation_parameter"])
            node.set(self.controls["azimuth_property"], self.controls["azimuth_parameter"])
        self.periodic.set(self.controls["polarization_property"], "LinearPol")
        basis = condition["polarization_basis_id"]
        try:
            polarization = self.controls["polarization_values"][basis]
        except KeyError as exc:
            raise ValueError(f"no polarization mapping for {basis}") from exc
        self.periodic.set(self.controls["linear_polarization_property"], polarization)
        for label, node in [
            ("periodic parent", self.periodic),
            *[(f"periodic port {index}", port) for index, port in enumerate(self.ports, start=1)],
        ]:
            if (
                str(node.getString(self.controls["angle_property"]))
                != self.controls["elevation_parameter"]
            ):
                raise ValueError(f"{label} elevation readback differs")
            if (
                str(node.getString(self.controls["azimuth_property"]))
                != self.controls["azimuth_parameter"]
            ):
                raise ValueError(f"{label} azimuth readback differs")
        if str(self.periodic.getString(self.controls["polarization_property"])) != "LinearPol":
            raise ValueError("periodic polarization mode readback differs")
        if str(self.periodic.getString(self.controls["linear_polarization_property"])) != str(
            polarization
        ):
            raise ValueError("periodic polarization readback differs")

    def _set_solver_memory_policy(self) -> dict[str, Any]:
        property_name = self.controls["out_of_core_property"]
        requested = self.controls["out_of_core_value"]
        self.linear_solver.set(property_name, requested)
        observed = str(self.linear_solver.getString(property_name))
        if observed != requested:
            raise ValueError("linear solver out-of-core policy readback differs")
        body: dict[str, Any] = {
            "property": property_name,
            "requested": requested,
            "observed": observed,
        }
        feature_path = self.controls.get("coarse_solver_feature_path")
        if feature_path is None:
            return body
        if not isinstance(feature_path, list):
            raise ValueError("coarse solver feature path is invalid")
        coarse_solver = self.stationary_solver
        for tag in feature_path:
            coarse_solver = coarse_solver.feature(tag)
        coarse_property = self.controls["coarse_solver_out_of_core_property"]
        coarse_requested = self.controls["coarse_solver_out_of_core_value"]
        coarse_solver.set(coarse_property, coarse_requested)
        coarse_observed = str(coarse_solver.getString(coarse_property))
        if coarse_observed != coarse_requested:
            raise ValueError("coarse solver out-of-core policy readback differs")
        body["coarse_solver"] = {
            "feature_path": list(feature_path),
            "property": coarse_property,
            "requested": coarse_requested,
            "observed": coarse_observed,
        }
        return body

    def _set_solver_selection(self) -> dict[str, Any]:
        selected_tag = self.controls.get("selected_linear_solver_tag")
        inactive_tags = self.controls.get("inactive_linear_solver_tags")
        if selected_tag is None and inactive_tags is None:
            return {"mode": "model_existing"}
        if not isinstance(selected_tag, str) or not isinstance(inactive_tags, list):
            raise ValueError("linear solver selection contract is incomplete")
        selected = self.stationary_solver.feature(selected_tag)
        inactive = [self.stationary_solver.feature(tag) for tag in inactive_tags]
        selected.active(True)
        for feature in inactive:
            feature.active(False)
        observed = {
            selected_tag: bool(selected.isActive()),
            **{
                tag: bool(feature.isActive())
                for tag, feature in zip(inactive_tags, inactive, strict=True)
            },
        }
        if observed[selected_tag] is not True or any(observed[tag] for tag in inactive_tags):
            raise ValueError("linear solver selection readback differs")
        return {
            "mode": "explicit",
            "selected_linear_solver_tag": selected_tag,
            "inactive_linear_solver_tags": list(inactive_tags),
            "observed_active": observed,
        }

    def evaluate_condition(
        self, condition: Mapping[str, Any], tensor_expressions: list[str]
    ) -> Mapping[str, Any]:
        self._counter += 1
        self.material.apply_material_state(condition["material_state_id"], tensor_expressions)
        params = self.model.java.param()
        params.set(
            self.controls["wavelength_parameter"],
            f"{float(condition['wavelength_m']):.17g}[m]",
        )
        self._set_incidence(condition)
        self._set_solver_memory_policy()
        self._set_solver_selection()
        wavelength_expression = self.controls["wavelength_parameter"]
        self.study_step.set(self.controls["study_step_property"], wavelength_expression)
        from jpype import JArray, JString

        array_property = self.controls["study_step_array_property"]
        if array_property is not None:
            self.study_step.set(array_property, JArray(JString)([wavelength_expression]))
        if str(self.study_step.getString(self.controls["study_step_property"])) != (
            wavelength_expression
        ):
            raise ValueError("study wavelength property readback differs")
        self.model.java.save(str(self.working_model_path))
        self.study.run()
        tag = f"robust_eval_{self._counter:04d}"
        evaluator = self.numerical.create(tag, "EvalGlobal")
        evaluator.set("data", self.controls["dataset_tag"])
        expressions = [
            self.controls["observable_expression"],
            self.controls["reflectance_expression"],
            self.controls["transmittance_expression"],
            self.controls["absorption_expression"],
            self.controls["evaluated_wavelength_expression"],
            self.controls["solved_wavelength_expression"],
        ]
        evaluator.set("expr", JArray(JString)(expressions))
        values = _flatten_real(evaluator.computeResult())
        if len(values) < len(expressions):
            raise ValueError("COMSOL condition evaluator returned too few values")
        mesh = _get(self.model.java.component(), self.controls["component_tag"]).mesh(
            self.controls["mesh_tag"]
        )
        statistics = mesh.stat()
        self.numerical.remove(tag)
        return {
            "condition_id": condition["condition_id"],
            "observable_id": condition["observable_id"],
            "observable_value": values[0],
            "requested_wavelength_m": condition["wavelength_m"],
            "evaluated_wavelength_m": values[4],
            "solved_wavelength_m": values[5],
            "reflectance": values[1],
            "transmittance": values[2],
            "absorption": values[3],
            "mesh_elements": int(statistics.getNumElem()),
            "minimum_mesh_quality": float(statistics.getMinQuality()),
            "dataset_id": self.controls["dataset_tag"],
            "solution_id": self.controls["solution_tag"],
        }

    def _prepare_native_sensitivity(
        self, variable_ids: list[str], *, wavelength_m: float
    ) -> dict[str, Any]:
        controls = self.controls
        required = {
            "sensitivity_parametric_sweep_tag",
            "sensitivity_feature_tag",
            "sensitivity_solver_tag",
            "sensitivity_segregated_solver_tag",
            "sensitivity_direct_solver_tags",
            "sensitivity_solution_tags",
            "sensitivity_dataset_tags",
            "derivative_solution_tag",
            "derivative_dataset_tag",
            "sensitivity_gradient_method",
            "sensitivity_solver_regeneration",
            "sensitivity_stationary_nonlinearity",
            "sensitivity_segregated_step_tags",
            "sensitivity_merged_step_tag",
            "sensitivity_removed_step_tag",
            "sensitivity_merged_linear_solver_tag",
            "sensitivity_constraint_group_policy",
        }
        if not required <= set(controls):
            raise ValueError("native sensitivity controls are incomplete")
        if variable_ids != [item["variable_id"] for item in self.support["variables"]]:
            raise ValueError("native sensitivity variable order differs from support")
        features = self.study.feature()
        sweep_tag = controls["sensitivity_parametric_sweep_tag"]
        sensitivity_tag = controls["sensitivity_feature_tag"]
        if sweep_tag in _tags(features) or sensitivity_tag in _tags(features):
            raise ValueError("native sensitivity study features already exist before preparation")
        sweep = features.create(sweep_tag, "Parametric")
        sensitivity = features.create(sensitivity_tag, "Sensitivity")
        sensitivity.active(True)
        sensitivity.set("gradientMethod", controls["sensitivity_gradient_method"])
        unit_scale = {"m": 1.0, "um": 1e-6, "nm": 1e-9}
        try:
            baselines_m = [
                float(item["baseline"]) * unit_scale[item["unit"]]
                for item in self.support["variables"]
            ]
            scales_m = [
                float(item["scale"]) * unit_scale[item["unit"]]
                for item in self.support["variables"]
            ]
        except KeyError as exc:
            raise ValueError("native sensitivity variable unit is unsupported") from exc
        _set_vector(sensitivity, "pname", variable_ids)
        _set_vector(sensitivity, "punit", ["m"] * len(variable_ids))
        _set_vector(sensitivity, "initval", [f"{item:.17g}[m]" for item in baselines_m])
        _set_vector(sensitivity, "scale", [f"{item:.17g}[m]" for item in scales_m])
        _set_vector(sensitivity, "valuetype", ["real"] * len(variable_ids))
        _set_vector(sensitivity, "optobj", [controls["observable_expression"]])
        _set_vector(sweep, "pname", [controls["wavelength_parameter"]])
        _set_vector(sweep, "plistarr", [f"{float(wavelength_m):.17g}[m]"])
        _set_vector(sweep, "punit", ["m"])
        if controls["sensitivity_solver_regeneration"] != "replace_existing_auto_sequence":
            raise ValueError("native sensitivity solver regeneration policy changed")
        self.model.java.sol().remove(controls["solution_tag"])
        self.study.createAutoSequences("all")
        observed_solutions = _tags(self.model.java.sol())
        if observed_solutions != controls["sensitivity_solution_tags"][:1]:
            raise ValueError("pre-solve native sensitivity solution identity changed")
        solution = self.model.java.sol(controls["solution_tag"])
        stationary = solution.feature(controls["stationary_solver_tag"])
        children = {
            tag: str(stationary.feature(tag).getType()) for tag in _tags(stationary.feature())
        }
        if children.get(controls["sensitivity_solver_tag"]) != "Sensitivity":
            raise ValueError("native sensitivity solver feature identity changed")
        if children.get(controls["sensitivity_segregated_solver_tag"]) != "Segregated":
            raise ValueError("native sensitivity segregated solver identity changed")
        if str(stationary.getString("nonlin")) != controls["sensitivity_stationary_nonlinearity"]:
            raise ValueError("native sensitivity stationary nonlinearity changed")
        direct_ooc = {}
        for tag in controls["sensitivity_direct_solver_tags"]:
            if children.get(tag) != "Direct":
                raise ValueError("native sensitivity direct solver identity changed")
            direct = stationary.feature(tag)
            direct.set(controls["out_of_core_property"], controls["out_of_core_value"])
            direct_ooc[tag] = str(direct.getString(controls["out_of_core_property"]))
        if any(value != controls["out_of_core_value"] for value in direct_ooc.values()):
            raise ValueError("native sensitivity direct solver OOC readback differs")
        segregated_groups = self._merge_sensitivity_constraint_groups(stationary)
        self.stationary_solver = stationary
        self.linear_solver = stationary.feature(controls["linear_solver_tag"])
        readback = {
            "study_feature_order": _tags(features),
            "gradient_method": str(sensitivity.getString("gradientMethod")),
            "variable_ids": [str(item) for item in list(sensitivity.getStringArray("pname"))],
            "variable_units": [str(item) for item in list(sensitivity.getStringArray("punit"))],
            "objective": [str(item) for item in list(sensitivity.getStringArray("optobj"))],
            "solver_children": children,
            "direct_ooc": direct_ooc,
            "segregated_groups": segregated_groups,
            "stationary_nonlinearity": str(stationary.getString("nonlin")),
        }
        expected_order = [sweep_tag, sensitivity_tag, controls["study_step_tag"]]
        if (
            readback["study_feature_order"] != expected_order
            or readback["gradient_method"] != "adjoint"
            or readback["variable_ids"] != variable_ids
            or readback["variable_units"] != ["m"] * len(variable_ids)
            or readback["objective"] != [controls["observable_expression"]]
        ):
            raise ValueError("native sensitivity study readback differs")
        readback["receipt_fingerprint"] = domain_sha256_v2(
            "comsol_mcp.robust_native_sensitivity_controls", readback
        )
        self._sensitivity_sweep = sweep
        self._sensitivity_readback = readback
        self._native_sensitivity_prepared = True
        return readback

    def _merge_sensitivity_constraint_groups(self, stationary: Any) -> dict[str, Any]:
        controls = self.controls
        if (
            controls["sensitivity_constraint_group_policy"]
            != "merge_material_coordinates_into_wave_optics"
        ):
            raise ValueError("native sensitivity constraint group policy changed")
        segregated = stationary.feature(controls["sensitivity_segregated_solver_tag"])
        step_tags = controls["sensitivity_segregated_step_tags"]
        observed_types = {
            tag: str(segregated.feature(tag).getType()) for tag in _tags(segregated.feature())
        }
        if observed_types != {tag: "SegregatedStep" for tag in step_tags}:
            raise ValueError("native sensitivity segregated step identity changed")
        merged_tag = controls["sensitivity_merged_step_tag"]
        removed_tag = controls["sensitivity_removed_step_tag"]
        merged = segregated.feature(merged_tag)
        removed = segregated.feature(removed_tag)
        merged_variables = _ordered_unique(
            [str(item) for item in list(merged.getStringArray("segvar"))]
            + [str(item) for item in list(removed.getStringArray("segvar"))]
        )
        merged_components = _ordered_unique(
            [str(item) for item in list(merged.getStringArray("segcomp"))]
            + [str(item) for item in list(removed.getStringArray("segcomp"))]
        )
        _set_vector(merged, "segvar", merged_variables)
        _set_vector(merged, "segcomp", merged_components)
        segregated.feature().remove(removed_tag)
        merged.set("linsolver", controls["sensitivity_merged_linear_solver_tag"])
        observed_steps = _tags(segregated.feature())
        observed_variables = [str(item) for item in list(merged.getStringArray("segvar"))]
        observed_components = [str(item) for item in list(merged.getStringArray("segcomp"))]
        observed_solver = str(merged.getString("linsolver"))
        if (
            observed_steps != [merged_tag]
            or observed_variables != merged_variables
            or observed_components != merged_components
            or observed_solver != controls["sensitivity_merged_linear_solver_tag"]
        ):
            raise ValueError("native sensitivity merged constraint group readback differs")
        return {
            "policy": controls["sensitivity_constraint_group_policy"],
            "source_step_tags": list(step_tags),
            "observed_step_tags": observed_steps,
            "merged_step_tag": merged_tag,
            "removed_step_tag": removed_tag,
            "linear_solver_tag": observed_solver,
            "variable_ids": observed_variables,
            "component_ids": observed_components,
        }

    def evaluate_condition_gradient(
        self,
        condition: Mapping[str, Any],
        tensor_expressions: list[str],
        variable_ids: list[str],
    ) -> Mapping[str, Any]:
        controls = self.controls
        self.material.apply_material_state(condition["material_state_id"], tensor_expressions)
        wavelength = f"{float(condition['wavelength_m']):.17g}[m]"
        self.model.java.param().set(controls["wavelength_parameter"], wavelength)
        self._set_incidence(condition)
        self.study_step.set(controls["study_step_property"], controls["wavelength_parameter"])
        self._set_solver_memory_policy()
        self._set_solver_selection()
        if not self._native_sensitivity_prepared:
            self.set_shape_deformation_active(True)
            self._prepare_native_sensitivity(
                variable_ids, wavelength_m=float(condition["wavelength_m"])
            )
        if self._sensitivity_sweep is None or self._sensitivity_readback is None:
            raise RuntimeError("native sensitivity preparation state is incomplete")
        _set_vector(self._sensitivity_sweep, "plistarr", [wavelength])
        self.set_shape_deformation_active(True)
        for tag in controls["sensitivity_direct_solver_tags"]:
            direct = self.stationary_solver.feature(tag)
            direct.set(controls["out_of_core_property"], controls["out_of_core_value"])
            if (
                str(direct.getString(controls["out_of_core_property"]))
                != controls["out_of_core_value"]
            ):
                raise ValueError("native sensitivity direct solver OOC readback differs")
        if (
            str(self.stationary_solver.getString("nonlin"))
            != controls["sensitivity_stationary_nonlinearity"]
        ):
            raise ValueError("native sensitivity stationary nonlinearity changed")
        self.model.java.save(str(self.working_model_path))
        self.study.run()
        identity = _generated_sensitivity_identity(self.model)
        if [item["tag"] for item in identity["solutions"]] != controls["sensitivity_solution_tags"]:
            raise ValueError("native sensitivity generated solution identity changed")
        if [item["tag"] for item in identity["datasets"]] != controls["sensitivity_dataset_tags"]:
            raise ValueError("native sensitivity generated dataset identity changed")
        derivative_dataset_id = controls["derivative_dataset_tag"]
        derivative_solution_id = controls["derivative_solution_tag"]
        derivative_dataset = next(
            item for item in identity["datasets"] if item["tag"] == derivative_dataset_id
        )
        if derivative_dataset["solution"] != derivative_solution_id:
            raise ValueError("native sensitivity derivative dataset binding changed")
        expressions = [controls["observable_expression"]]
        for variable_id in variable_ids:
            expressions.extend(
                [
                    f"fsens({variable_id})",
                    f"real(fsens({variable_id}))",
                    f"imag(fsens({variable_id}))",
                ]
            )
        expressions.extend(
            [
                controls["reflectance_expression"],
                controls["transmittance_expression"],
                controls["absorption_expression"],
                controls["evaluated_wavelength_expression"],
                controls["solved_wavelength_expression"],
            ]
        )
        values = self.model.evaluate(
            expressions,
            dataset=_dataset_by_tag(self.model, derivative_dataset_id),
            outer=1,
        )
        scalars = {
            expression: _complex_scalar(value)
            for expression, value in zip(expressions, values, strict=True)
        }
        objective = scalars[controls["observable_expression"]]
        if abs(objective.imag) > 1e-12:
            raise ValueError("native sensitivity objective unexpectedly contains an imaginary part")
        raw_gradients = []
        accepted_gradients = []
        for variable_id in variable_ids:
            raw = scalars[f"fsens({variable_id})"]
            accepted = scalars[f"real(fsens({variable_id}))"]
            imaginary = scalars[f"imag(fsens({variable_id}))"]
            if accepted.imag != 0.0 or accepted.real != raw.real or imaginary.real != raw.imag:
                raise ValueError("native sensitivity raw and accepted gradients differ")
            raw_gradients.append({"real": raw.real, "imaginary": raw.imag})
            accepted_gradients.append(accepted.real)
        physical_expressions = expressions[-5:]
        physical = [scalars[item] for item in physical_expressions]
        if any(abs(item.imag) > 1e-10 for item in physical):
            raise ValueError("native sensitivity physical evidence is unexpectedly complex")
        mesh = _get(self.model.java.component(), controls["component_tag"]).mesh(
            controls["mesh_tag"]
        )
        statistics = mesh.stat()
        identities = {
            "primal": domain_sha256_v2(
                "comsol_mcp.native_primal_identity", identity["solutions"][0]
            ),
            "adjoint": domain_sha256_v2(
                "comsol_mcp.native_adjoint_identity",
                next(
                    item for item in identity["solutions"] if item["tag"] == derivative_solution_id
                ),
            ),
            "study": domain_sha256_v2(
                "comsol_mcp.native_study_identity", self._sensitivity_readback
            ),
            "solution": domain_sha256_v2(
                "comsol_mcp.native_solution_identity", identity["solutions"]
            ),
            "dataset": domain_sha256_v2("comsol_mcp.native_dataset_identity", identity["datasets"]),
        }
        return {
            "condition_id": condition["condition_id"],
            "observable_id": condition["observable_id"],
            "observable_value": objective.real,
            "requested_wavelength_m": condition["wavelength_m"],
            "evaluated_wavelength_m": physical[3].real,
            "solved_wavelength_m": physical[4].real,
            "reflectance": physical[0].real,
            "transmittance": physical[1].real,
            "absorption": physical[2].real,
            "mesh_elements": int(statistics.getNumElem()),
            "minimum_mesh_quality": float(statistics.getMinQuality()),
            "dataset_id": controls["dataset_tag"],
            "solution_id": controls["solution_tag"],
            "derivative_dataset_id": derivative_dataset_id,
            "derivative_solution_id": derivative_solution_id,
            "variable_ids": list(variable_ids),
            "raw_gradients": raw_gradients,
            "accepted_real_gradients": accepted_gradients,
            "gradient_unit": self.support["result_identity"]["derivative_units"],
            "identity_fingerprints": identities,
        }


def execute_lin2025_conditions(
    spec: Mapping[str, Any],
    directory: Path,
    *,
    attempt: int,
    client_factory: Callable[..., Any] | None = None,
    java_environment_reader: Callable[[str], str | None] | None = None,
    cancel_requested: Callable[[], bool],
    include_gradients: bool = True,
) -> dict[str, Any]:
    """Load one derived copy, configure controls, and execute durable conditions."""
    source = Path(spec["source_model_path"])
    configured = directory / "robust-working.mph"
    cleanup_path = directory / "native-cleanup.json"
    client = None
    model = None
    source_model = None
    cleanup: dict[str, Any] = {
        "client_acquired": False,
        "source_model_loaded": False,
        "working_model_loaded": False,
        "source_model_removed": False,
        "working_model_removed": False,
        "client_clear": True,
        "client_disconnect": "not_applicable",
        "errors": [],
    }
    temporary_directory = spec["comsol_temporary_directory"]
    previous_temporary_directory = os.environ.get("COMSOL_TMPDIR")
    environment_path = directory / "comsol-temporary-directory.json"
    os.environ["COMSOL_TMPDIR"] = temporary_directory
    atomic_write_json(
        environment_path,
        {
            "control": "COMSOL_TMPDIR",
            "requested_path": temporary_directory,
            "process_environment_path": os.environ.get("COMSOL_TMPDIR"),
            "java_environment_path": None,
            "matches": False,
        },
    )
    try:
        if client_factory is None:
            import jpype
            import mph

            client_factory = mph.Client

            def read_java_environment(name: str) -> str:
                return str(jpype.JClass("java.lang.System").getenv(name))

            java_environment_reader = read_java_environment
        client = client_factory(cores=spec["cores"], version=spec["version"])
        cleanup["client_acquired"] = True
        cleanup["client_clear"] = False
        if java_environment_reader is None:
            java_environment_reader = os.environ.get
        java_temporary_directory = java_environment_reader("COMSOL_TMPDIR")
        environment_receipt = {
            "control": "COMSOL_TMPDIR",
            "requested_path": temporary_directory,
            "process_environment_path": os.environ.get("COMSOL_TMPDIR"),
            "java_environment_path": java_temporary_directory,
            "matches": java_temporary_directory == temporary_directory,
        }
        atomic_write_json(environment_path, environment_receipt)
        if not environment_receipt["matches"]:
            raise RuntimeError("COMSOL temporary directory readback mismatch")
        source_model = client.load(str(source))
        cleanup["source_model_loaded"] = True
        cleanup["source_model_removed"] = False
        source_model.java.save(str(configured), True)
        client.remove(source_model)
        source_model = None
        cleanup["source_model_removed"] = True
        model = client.load(str(configured))
        cleanup["working_model_loaded"] = True
        cleanup["working_model_removed"] = False
        backend = ClientapiLin2025ConditionBackend(model, spec, working_model_path=configured)
        controls = backend.prepare(spec["initial_values"])
        atomic_write_json(directory / "robust-controls.json", controls)
        model.java.save(str(configured))
        observations = execute_robust_conditions(
            spec,
            directory,
            attempt=attempt,
            backend=backend,
            cancel_requested=cancel_requested,
        )
        aggregate_gradient = None
        sensitivity_controls = None
        if include_gradients and backend.controls.get("schema_version") == "1.4.0":
            from .robust_gradient_runtime import execute_native_condition_gradients

            aggregate_gradient = execute_native_condition_gradients(
                spec,
                directory,
                backend=backend,
                observations=observations,
                cancel_requested=cancel_requested,
            )
            sensitivity_controls = backend._sensitivity_readback
        return {
            "observations": observations,
            "aggregate_gradient": aggregate_gradient,
            "sensitivity_controls": sensitivity_controls,
            "controls_fingerprint": controls.get("receipt_fingerprint"),
            "solver_started": True,
            "configured_model": str(configured),
            "cleanup": cleanup,
        }
    finally:
        if source_model is not None and client is not None:
            try:
                client.remove(source_model)
                cleanup["source_model_removed"] = True
            except Exception as exc:
                cleanup["errors"].append(f"source_model_remove:{type(exc).__name__}")
        if model is not None and client is not None:
            try:
                client.remove(model)
                cleanup["working_model_removed"] = True
            except Exception as exc:
                cleanup["errors"].append(f"working_model_remove:{type(exc).__name__}")
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                cleanup["errors"].append(f"client_clear:{type(exc).__name__}")
            if getattr(client, "port", None):
                try:
                    client.disconnect()
                    cleanup["client_disconnect"] = True
                except Exception as exc:
                    cleanup["client_disconnect"] = False
                    cleanup["errors"].append(f"client_disconnect:{type(exc).__name__}")
        if previous_temporary_directory is None:
            os.environ.pop("COMSOL_TMPDIR", None)
        else:
            os.environ["COMSOL_TMPDIR"] = previous_temporary_directory
        atomic_write_json(cleanup_path, cleanup)


__all__ = ["ClientapiLin2025ConditionBackend", "execute_lin2025_conditions"]
