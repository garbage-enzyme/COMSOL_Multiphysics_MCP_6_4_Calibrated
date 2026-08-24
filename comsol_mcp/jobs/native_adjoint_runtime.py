"""Licensed fixed-topology native adjoint optimization runtime."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.durable import atomic_write_json
from comsol_mcp.research.adjoint_adapter import (
    ClientapiAdjointStudyBackend,
    configure_native_adjoint,
)
from comsol_mcp.tools.derived_geometry import _set_vector


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _numeric_series(value: Any) -> list[float]:
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    flattened: list[float] = []

    def collect(item: Any) -> None:
        if isinstance(item, (list, tuple)):
            for child in item:
                collect(child)
            return
        scalar = complex(item)
        if not math.isfinite(scalar.real) or not math.isfinite(scalar.imag):
            raise ValueError("native objective contains a nonfinite value")
        if abs(scalar.imag) > 1e-12 * max(1.0, abs(scalar.real)):
            raise ValueError("native objective unexpectedly contains a complex component")
        flattened.append(float(scalar.real))

    collect(value)
    if not flattened:
        raise ValueError("native objective evaluation is empty")
    return flattened


def _dataset_for_solution(model: Any, solution_tag: str) -> Any:
    matches = []
    for dataset in model / "datasets":
        properties = dataset.properties()
        linked = None
        if "solution" in properties:
            linked = dataset.property("solution")
        elif "data" in properties:
            linked = dataset.property("data")
        if str(linked) == solution_tag:
            matches.append(dataset)
    if len(matches) != 1:
        raise ValueError("native optimizer dataset identity is ambiguous")
    return matches[0]


def _forward_sweep_dataset(model: Any) -> Any:
    matches = []
    for solution_tag in [str(item) for item in list(model.java.sol().tags())]:
        solution = model.java.sol(solution_tag)
        feature_types = {
            str(solution.feature(tag).getType())
            for tag in [str(item) for item in list(solution.feature().tags())]
        }
        if "StoreSolution" in feature_types:
            matches.append(_dataset_for_solution(model, solution_tag))
    if len(matches) != 1:
        raise ValueError("direct forward sweep dataset identity is ambiguous")
    return matches[0]


def _freeze_wavelength(model: Any, support: Mapping[str, Any]) -> None:
    study = model.java.study("std1")
    sweep = study.feature("sweep1")
    wavelength = study.feature("step1")
    sensitivity = study.feature("sens_a71")
    expression = f"{support['objective']['wavelength_um']:.17g}e-6[m]"
    variables = list(support["variables"])
    model.java.param().set("wl", expression)
    _set_vector(sweep, "pname", ["wl"])
    _set_vector(sweep, "plistarr", [expression])
    _set_vector(sweep, "punit", ["m"])
    wavelength.set("plist", expression)
    _set_vector(sensitivity, "pname", [item["variable_id"] for item in variables])
    _set_vector(sensitivity, "punit", [item["unit"] for item in variables])
    _set_vector(
        sensitivity,
        "initval",
        [f"{item['baseline']:.17g}[{item['unit']}]" for item in variables],
    )
    _set_vector(
        sensitivity,
        "scale",
        [f"{item['scale']:.17g}[{item['unit']}]" for item in variables],
    )
    _set_vector(sensitivity, "valuetype", ["real"] * len(variables))
    _set_vector(sensitivity, "optobj", [support["objective"]["expression"]])


def _configure_solver(model: Any, study: Any, optimizer: Mapping[str, Any]) -> dict[str, Any]:
    study.createAutoSequences("sol")
    matches = []
    solutions = model.java.sol()
    for solution_tag in [str(item) for item in list(solutions.tags())]:
        solution = model.java.sol(solution_tag)
        features = solution.feature()
        for feature_tag in [str(item) for item in list(features.tags())]:
            feature = solution.feature(feature_tag)
            if str(feature.getType()) == "Optimization":
                matches.append((solution_tag, feature_tag, feature))
    if len(matches) != 1:
        raise ValueError("native optimization solver identity is ambiguous")
    solution_tag, feature_tag, feature = matches[0]
    feature.set("movelimitactive", "on")
    feature.set("movelimit", f"{optimizer['move_limit']:.17g}")
    feature.set("mmamaxiteractive", "on")
    feature.set("mmamaxiter", str(optimizer["budget"]["max_iterations"]))
    return {
        "solution_tag": solution_tag,
        "feature_tag": feature_tag,
        "movelimitactive": str(feature.getString("movelimitactive")),
        "movelimit": str(feature.getString("movelimit")),
        "mmamaxiteractive": str(feature.getString("mmamaxiteractive")),
        "mmamaxiter": str(feature.getString("mmamaxiter")),
    }


def _mesh_statistics(model: Any) -> dict[str, Any]:
    statistics = model.java.component("comp1").mesh("mesh1").stat()
    return {
        "element_count": int(statistics.getNumElem()),
        "minimum_quality": float(statistics.getMinQuality()),
        "mean_quality": float(statistics.getMeanQuality()),
        "quality_measure": str(statistics.getQualityMeasure()),
    }


def execute_native_adjoint_optimization(
    spec: Mapping[str, Any],
    workdir: Path,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute the one accepted adapter and persist a path-redacted receipt."""
    source = Path(spec["source_model_path"])
    source_before = _sha(source)
    if source_before != spec["source_model_sha256"]:
        raise ValueError("native optimization source identity changed")
    support = spec["support"]
    optimizer = spec["optimizer"]
    if optimizer["method"] != "gcmma":
        raise ValueError("licensed product runtime accepts only validated gcmma")
    if spec["cores"] != optimizer["budget"]["cores"]:
        raise ValueError("submission cores differ from optimizer budget")
    workdir.mkdir(parents=True, exist_ok=True)
    configured = workdir / "configured.mph"
    receipt_path = workdir / "native-optimizer-receipt.json"
    configured.unlink(missing_ok=True)
    receipt: dict[str, Any] = {
        "schema_name": "comsol_mcp.native_optimizer_runtime_receipt",
        "schema_version": "1.0.0",
        "success": False,
        "source_sha256": source_before,
        "support_fingerprint": support["support_fingerprint"],
        "optimizer_fingerprint": optimizer["optimizer_fingerprint"],
        "budget": optimizer["budget"],
    }
    started = time.monotonic()
    client = None
    try:
        if client_factory is None:
            import mph

            client_factory = mph.Client
        client = client_factory(cores=spec["cores"], version=spec["version"])
        model = client.load(str(source))
        configure_native_adjoint(ClientapiAdjointStudyBackend(model), support, optimizer)
        _freeze_wavelength(model, support)
        model.java.save(str(configured), True)
        client.remove(model)

        baseline_model = client.load(str(configured))
        baseline_study = baseline_model.java.study("std1")
        baseline_study.feature().remove("sens_a71")
        baseline_study.run()
        baseline_dataset = _forward_sweep_dataset(baseline_model)
        baseline = _numeric_series(
            baseline_model.evaluate(
                support["objective"]["expression"], dataset=baseline_dataset, outer=1
            )
        )[-1]
        client.remove(baseline_model)

        model = client.load(str(configured))
        study = model.java.study("std2")
        study.feature("opt_a71").set("nsolvemax", str(optimizer["budget"]["max_solves"]))
        solver = _configure_solver(model, study, optimizer)
        study.run()
        dataset = _dataset_for_solution(model, solver["solution_tag"])
        variables = [item["variable_id"] for item in support["variables"]]
        evaluated = model.evaluate(
            [support["objective"]["expression"], *variables], dataset=dataset
        )
        final = _numeric_series(evaluated[0])[-1]
        final_variables = {
            name: _numeric_series(value)[-1]
            for name, value in zip(variables, evaluated[1:], strict=True)
        }
        client.remove(model)

        finalist = client.load(str(configured))
        for name, value in final_variables.items():
            finalist.java.param().set(name, f"{value:.17g}[m]")
        finalist_study = finalist.java.study("std1")
        finalist_study.feature().remove("sens_a71")
        mesh = finalist.java.component("comp1").mesh("mesh1")
        mesh.run()
        mesh_evidence = _mesh_statistics(finalist)
        if mesh_evidence["minimum_quality"] <= 0.1:
            raise ValueError("remeshed finalist minimum quality is not acceptable")
        finalist_study.run()
        finalist_dataset = _forward_sweep_dataset(finalist)
        expressions = [
            support["objective"]["expression"],
            "ewfd.Rtotal",
            "ewfd.Ttotal",
            "ewfd.Atotal",
            "wl",
            "c_const/freq",
        ]
        values = finalist.evaluate(expressions, dataset=finalist_dataset, outer=1)
        physical = {
            name: _numeric_series(value)[-1]
            for name, value in zip(expressions, values, strict=True)
        }
        fresh = physical[support["objective"]["expression"]]
        closure = physical["ewfd.Rtotal"] + physical["ewfd.Ttotal"] + physical["ewfd.Atotal"]
        if not math.isclose(fresh, final, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("fresh forward objective differs from native optimizer result")
        if not math.isclose(closure, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("remeshed finalist R/T/A closure is outside tolerance")
        client.remove(finalist)
        elapsed = time.monotonic() - started
        if elapsed > optimizer["budget"]["max_wall_time_seconds"]:
            raise TimeoutError("native optimization exceeded its caller wall budget")
        receipt.update(
            {
                "success": True,
                "baseline_objective": baseline,
                "final_objective": final,
                "fresh_forward_objective": fresh,
                "objective_delta": fresh - baseline,
                "final_variables_si": final_variables,
                "solver": solver,
                "mesh": mesh_evidence,
                "physical_evidence": {
                    "reflectance": physical["ewfd.Rtotal"],
                    "transmittance": physical["ewfd.Ttotal"],
                    "absorption": physical["ewfd.Atotal"],
                    "closure": closure,
                    "evaluated_wavelength_m": physical["wl"],
                    "solved_wavelength_m": physical["c_const/freq"],
                },
                "elapsed_seconds": elapsed,
            }
        )
    finally:
        # Cleanup itself is fallible; neither a re-hash failure nor client
        # teardown failure may mask the original outcome or lose the durable
        # receipt, so every step is guarded and the receipt is always written.
        cleanup = {"client_clear": False, "source_unchanged": False}
        cleanup_errors: list[str] = []
        try:
            cleanup["source_unchanged"] = _sha(source) == source_before
        except Exception as exc:
            cleanup_errors.append(f"source_hash:{type(exc).__name__}:{exc}")
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                cleanup_errors.append(f"client_clear:{type(exc).__name__}:{exc}")
        cleanup_record: dict[str, Any] = dict(cleanup)
        if cleanup_errors:
            cleanup_record["cleanup_errors"] = cleanup_errors
        receipt["cleanup"] = cleanup_record
        receipt["success"] = (
            receipt.get("success") is True and not cleanup_errors and all(cleanup.values())
        )
        atomic_write_json(receipt_path, receipt)
    return receipt


__all__ = ["execute_native_adjoint_optimization"]
