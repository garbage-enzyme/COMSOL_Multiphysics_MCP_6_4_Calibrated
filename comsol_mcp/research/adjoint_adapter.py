"""Trusted periodic-MIM native sensitivity/optimization adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import normalize_derivative_support
from .gradient_contracts import normalize_native_optimizer_configuration

ADJOINT_ADAPTER_ID = "periodic_mim_patch_shape_gradient_v1"
ADJOINT_ADAPTER_VERSION = "1.0.0"
_CANONICAL_UNITS = {"m", "s", "kg", "A", "K", "mol", "cd", "1"}
_UNIT_SCALE = {"m": 1.0, "um": 1e-6, "nm": 1e-9}


class AdjointStudyBackend(Protocol):
    """Exact operations used by the typed adapter; no generic property setter."""

    def study_tags(self) -> list[str]: ...

    def create_study(self, tag: str) -> Any: ...

    def get_study(self, tag: str) -> Any: ...

    def remove_study(self, tag: str) -> None: ...

    def snapshot(self) -> Mapping[str, Any]: ...

    def restore(self, snapshot: Mapping[str, Any]) -> None: ...

    def prepare_controls(self, support: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ClientapiAdjointStudyBackend:
    """Small ClientAPI bridge for one already loaded derived COMSOL model."""

    def __init__(self, model: Any):
        self.model = model

    @staticmethod
    def _tags(container: Any) -> list[str]:
        return [str(item) for item in list(container.tags())]

    def study_tags(self) -> list[str]:
        return self._tags(self.model.java.study())

    def create_study(self, tag: str) -> Any:
        return self.model.java.study().create(tag)

    def get_study(self, tag: str) -> Any:
        studies = self.model.java.study()
        try:
            return studies.get(tag)
        except Exception:
            return studies(tag)

    def remove_study(self, tag: str) -> None:
        self.model.java.study().remove(tag)

    def snapshot(self) -> Mapping[str, Any]:
        result: dict[str, Any] = {"studies": {}, "parameters": self.model.parameters()}
        studies = self.model.java.study()
        for tag in self._tags(studies):
            study = self.get_study(tag)
            result["studies"][tag] = {"features": self._tags(study.feature())}
        try:
            block = self.model.java.component("comp1").geom("geom1").feature("b_pat")
            result["patch_size"] = [str(item) for item in list(block.getStringArray("size"))]
        except Exception:
            result["patch_size"] = None
        return result

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        expected = snapshot.get("studies", {})
        for tag in list(self.study_tags()):
            if tag not in expected:
                self.remove_study(tag)
                continue
            study = self.get_study(tag)
            original_features = set(expected[tag].get("features", []))
            for feature_tag in self._tags(study.feature()):
                if feature_tag not in original_features:
                    study.feature().remove(feature_tag)
        current_parameters = dict(self.model.parameters())
        original_parameters = dict(snapshot.get("parameters", {}))
        parameters = self.model.java.param()
        for name in set(current_parameters) - set(original_parameters):
            parameters.remove(name)
        for name, expression in original_parameters.items():
            parameters.set(name, expression)
        if snapshot.get("patch_size") is not None:
            from comsol_mcp.tools.derived_geometry import _set_vector

            geometry = self.model.java.component("comp1").geom("geom1")
            block = geometry.feature("b_pat")
            _set_vector(block, "size", list(snapshot["patch_size"]))
            geometry.run()

    def prepare_controls(self, support: Mapping[str, Any]) -> Mapping[str, Any]:
        variables = support["variables"]
        if [item["variable_id"] for item in variables] != [
            "patch_length_x",
            "patch_length_y",
        ]:
            raise ValueError("trusted periodic MIM controls must be patch_length_x/y")
        geometry = self.model.java.component("comp1").geom("geom1")
        block = geometry.feature("b_pat")
        if str(block.getType()) != "Block":
            raise ValueError("trusted patch feature type changed")
        before = [str(item) for item in list(block.getStringArray("size"))]
        if len(before) != 3:
            raise ValueError("trusted patch size vector is invalid")
        parameters = self.model.java.param()
        expressions = {}
        for item in variables:
            mapping = item["mapping"]
            if (
                mapping["feature_tag"] != "b_pat"
                or mapping["feature_type"] != "Block"
                or mapping["property_name"] != "size"
                or mapping["property_index"] != item["order"]
            ):
                raise ValueError("trusted patch control mapping changed")
            expression = f"{item['baseline']:.17g}[{item['unit']}]"
            parameters.set(item["variable_id"], expression)
            expressions[item["variable_id"]] = expression
        requested_size = ["patch_length_x", "patch_length_y", before[2]]
        from comsol_mcp.tools.derived_geometry import _set_vector

        _set_vector(block, "size", requested_size)
        geometry.run()
        readback = [str(item) for item in list(block.getStringArray("size"))]
        if readback != requested_size:
            raise ValueError("trusted patch parameter binding readback mismatch")
        return {
            "parameters": expressions,
            "patch_size_before": before,
            "patch_size_readback": readback,
        }


def _canonical_unit(value: str) -> str:
    if value in _CANONICAL_UNITS:
        return value
    if value in _UNIT_SCALE:
        return "m"
    raise ValueError(f"unsupported derivative unit: {value}")


def _array(node: Any, name: str, values: list[str]) -> dict[str, Any]:
    from comsol_mcp.tools.derived_geometry import _set_vector

    _set_vector(node, name, values)
    observed = [str(item) for item in list(node.getStringArray(name))]
    if name == "punit":
        expected = [_canonical_unit(item) for item in values]
        if observed != expected:
            raise ValueError(
                f"{name} readback differs from canonical units: "
                f"expected={expected}, observed={observed}"
            )
    elif observed != values:
        raise ValueError(
            f"{name} readback differs from requested values: expected={values}, observed={observed}"
        )
    return {"requested": values, "readback": observed}


def _scalar(node: Any, name: str, value: str) -> dict[str, Any]:
    node.set(name, value)
    observed = str(node.getString(name))
    if observed != value:
        raise ValueError(f"{name} readback differs from requested value")
    return {"requested": value, "readback": observed}


def _variable_strings(support: Mapping[str, Any]) -> dict[str, list[str]]:
    variables = support["variables"]
    names = [item["variable_id"] for item in variables]
    units = [item["unit"] for item in variables]
    values = [f"{item['baseline']:.17g}[{item['unit']}]" for item in variables]
    scales = [f"{item['scale']:.17g}[{item['unit']}]" for item in variables]
    lower = [f"{item['lower']:.17g}[{item['unit']}]" for item in variables]
    upper = [f"{item['upper']:.17g}[{item['unit']}]" for item in variables]
    return {
        "names": names,
        "units": units,
        "values": values,
        "scales": scales,
        "lower": lower,
        "upper": upper,
    }


def configure_native_adjoint(
    backend: AdjointStudyBackend,
    support: object,
    optimizer: object,
    *,
    sensitivity_study_tag: str = "std1",
    sensitivity_feature_tag: str = "sens_a71",
    optimization_study_tag: str = "std2",
    optimization_feature_tag: str = "opt_a71",
) -> dict[str, Any]:
    """Atomically create and configure the only accepted native feature pair."""
    normalized_support = normalize_derivative_support(support)
    normalized_optimizer = normalize_native_optimizer_configuration(optimizer)
    if normalized_support["adapter_id"] != "periodic_mim_patch_v1":
        raise ValueError("native adjoint adapter refuses an untrusted structure family")
    if normalized_support["derivative_method"] != "adjoint":
        raise ValueError("accepted native lane requires adjoint")
    values = _variable_strings(normalized_support)
    objective_expression = normalized_support["objective"]["expression"]
    snapshot = dict(backend.snapshot())
    created: list[str] = []
    try:
        control_readback = dict(backend.prepare_controls(normalized_support))
        if sensitivity_study_tag not in backend.study_tags():
            backend.create_study(sensitivity_study_tag)
            created.append(sensitivity_study_tag)
        sensitivity_study = backend.get_study(sensitivity_study_tag)
        sensitivity = sensitivity_study.feature().create(sensitivity_feature_tag, "Sensitivity")
        created.append(f"{sensitivity_study_tag}/{sensitivity_feature_tag}")
        if str(sensitivity.getType()) != "Sensitivity":
            raise ValueError("Sensitivity feature type readback mismatch")
        sensitivity.set("gradientMethod", "adjoint")
        if str(sensitivity.getString("gradientMethod")) != "adjoint":
            raise ValueError("Sensitivity gradientMethod readback mismatch")
        sensitivity_readback = {
            "gradientMethod": "adjoint",
            "pname": _array(sensitivity, "pname", values["names"]),
            "punit": _array(sensitivity, "punit", values["units"]),
            "initval": _array(sensitivity, "initval", values["values"]),
            "scale": _array(sensitivity, "scale", values["scales"]),
            "optobj": _array(sensitivity, "optobj", [objective_expression]),
        }
        if optimization_study_tag not in backend.study_tags():
            backend.create_study(optimization_study_tag)
            created.append(optimization_study_tag)
        optimization_study = backend.get_study(optimization_study_tag)
        optimization = optimization_study.feature().create(optimization_feature_tag, "Optimization")
        created.append(f"{optimization_study_tag}/{optimization_feature_tag}")
        if str(optimization.getType()) != "Optimization":
            raise ValueError("Optimization feature type readback mismatch")
        optimization_readback = {
            "optmethod": _scalar(optimization, "optmethod", normalized_optimizer["method"]),
            "pname": _array(optimization, "pname", values["names"]),
            "punit": _array(optimization, "punit", values["units"]),
            "initval": _array(optimization, "initval", values["values"]),
            "scale": _array(optimization, "scale", values["scales"]),
            "lbound": _array(optimization, "lbound", values["lower"]),
            "ubound": _array(optimization, "ubound", values["upper"]),
            "optobj": _array(optimization, "optobj", [objective_expression]),
            "nsolvemax": _scalar(
                optimization,
                "nsolvemax",
                str(normalized_optimizer["budget"]["max_solves"]),
            ),
        }
        receipt = {
            "schema_name": "comsol_mcp.native_adjoint_adapter_receipt",
            "schema_version": "1.0.0",
            "adapter_id": ADJOINT_ADAPTER_ID,
            "adapter_version": ADJOINT_ADAPTER_VERSION,
            "support_fingerprint": normalized_support["support_fingerprint"],
            "optimizer_fingerprint": normalized_optimizer["optimizer_fingerprint"],
            "created_nodes": created,
            "controls": control_readback,
            "sensitivity": sensitivity_readback,
            "optimization": optimization_readback,
            "rollback": {"attempted": False, "verified": False},
        }
        receipt["receipt_fingerprint"] = domain_sha256_v2(
            "comsol_mcp.native_adjoint_adapter_receipt", receipt
        )
        return receipt
    except Exception as exc:
        rollback_error = None
        try:
            backend.restore(snapshot)
            if dict(backend.snapshot()) != snapshot:
                raise RuntimeError("adapter rollback readback differs from the original snapshot")
        except Exception as rollback_exc:
            rollback_error = f"{type(rollback_exc).__name__}: {rollback_exc}"
        if rollback_error is not None:
            raise RuntimeError(
                f"native adjoint configuration failed and rollback was uncertain: {rollback_error}"
            ) from exc
        raise


__all__ = [
    "ADJOINT_ADAPTER_ID",
    "ADJOINT_ADAPTER_VERSION",
    "ClientapiAdjointStudyBackend",
    "configure_native_adjoint",
]
