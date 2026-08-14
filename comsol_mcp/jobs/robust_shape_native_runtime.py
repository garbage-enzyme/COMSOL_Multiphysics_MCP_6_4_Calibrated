"""ClientAPI backend for one-owner Lin2025 robust condition execution."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from comsol_mcp.durable import atomic_write_json
from comsol_mcp.research.lin2025_pedot_backend import (
    ClientapiLin2025PedotControlBackend,
    prepare_lin2025_pedot_shape_controls,
)

from .robust_condition_runtime import RobustConditionBackend, execute_robust_conditions


def _tags(container: Any) -> list[str]:
    return [str(value) for value in list(container.tags())]


def _get(container: Any, tag: str) -> Any:
    try:
        return container.get(tag)
    except Exception:
        return container(tag)


def _flatten_real(value: Any) -> list[float]:
    values: list[float] = []

    def visit(item: Any) -> None:
        if isinstance(item, (list, tuple)):
            for child in item:
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


class ClientapiLin2025ConditionBackend(RobustConditionBackend):
    """Apply all caller-declared condition controls to one derived model."""

    def __init__(self, model: Any, spec: Mapping[str, Any]):
        self.model = model
        configuration = spec["adapter_configuration"]["configuration"]
        self.controls = configuration["condition_controls"]
        self.fixture = configuration["fixture"]
        self.tree = configuration["tree_readback"]
        self.support = spec["support"]
        self.shape_support = configuration["shape_support"]
        self.material = ClientapiLin2025PedotControlBackend(model)
        component = _get(model.java.component(), self.controls["component_tag"])
        physics = _get(component.physics(), self.controls["physics_tag"])
        self.periodic = _get(physics, self.controls["periodic_structure_tag"])
        self.ports = [_get(physics, tag) for tag in self.controls["periodic_port_tags"]]
        self.study = _get(model.java.study(), self.controls["study_tag"])
        numerical = model.java.result().numerical()
        self.numerical = numerical
        self._counter = 0

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
        geometry = _get(self.model.java.component(), self.controls["component_tag"]).geom("geom1")
        geometry.run()
        mesh = _get(self.model.java.component(), self.controls["component_tag"]).mesh(
            self.controls["mesh_tag"]
        )
        mesh.run()
        return controls

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
            node.set("alpha2_inc", self.controls["azimuth_parameter"])
        self.periodic.set(self.controls["polarization_property"], "LinearPol")
        basis = condition["polarization_basis_id"]
        try:
            polarization = self.controls["polarization_values"][basis]
        except KeyError as exc:
            raise ValueError(f"no polarization mapping for {basis}") from exc
        self.periodic.set(self.controls["linear_polarization_property"], polarization)
        if str(self.periodic.getString(self.controls["angle_property"])) != self.controls[
            "elevation_parameter"
        ]:
            raise ValueError("periodic parent elevation readback differs")
        if str(self.periodic.getString("alpha2_inc")) != self.controls["azimuth_parameter"]:
            raise ValueError("periodic parent azimuth readback differs")
        if str(self.periodic.getString(self.controls["linear_polarization_property"])) != str(
            polarization
        ):
            raise ValueError("periodic polarization readback differs")

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
        from jpype import JArray, JString

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


def execute_lin2025_conditions(
    spec: Mapping[str, Any],
    directory: Path,
    *,
    attempt: int,
    client_factory: Callable[..., Any] | None = None,
    cancel_requested: Callable[[], bool],
) -> dict[str, Any]:
    """Load one derived copy, configure controls, and execute durable conditions."""
    source = Path(spec["source_model_path"])
    configured = directory / "robust-working.mph"
    cleanup_path = directory / "native-cleanup.json"
    client = None
    model = None
    source_model = None
    cleanup: dict[str, Any] = {
        "source_model_removed": False,
        "working_model_removed": False,
        "client_clear": False,
        "client_disconnect": "not_applicable",
        "errors": [],
    }
    try:
        if client_factory is None:
            import mph

            client_factory = mph.Client
        client = client_factory(cores=spec["cores"], version=spec["version"])
        source_model = client.load(str(source))
        source_model.java.save(str(configured), True)
        client.remove(source_model)
        source_model = None
        cleanup["source_model_removed"] = True
        model = client.load(str(configured))
        backend = ClientapiLin2025ConditionBackend(model, spec)
        controls = backend.prepare(spec["initial_values"])
        observations = execute_robust_conditions(
            spec,
            directory,
            attempt=attempt,
            backend=backend,
            cancel_requested=cancel_requested,
        )
        atomic_write_json(directory / "robust-controls.json", controls)
        return {
            "observations": observations,
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
        atomic_write_json(cleanup_path, cleanup)


__all__ = ["ClientapiLin2025ConditionBackend", "execute_lin2025_conditions"]
