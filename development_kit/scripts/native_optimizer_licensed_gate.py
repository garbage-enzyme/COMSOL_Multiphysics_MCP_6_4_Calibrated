"""Run one bounded native COMSOL optimization with fresh forward evidence."""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

from development_kit.scripts import native_adjoint_licensed_gate as structural
from development_kit.scripts import native_gradient_licensed_gate as gradient

REPOSITORY_ROOT = structural.REPOSITORY_ROOT
SCHEMA_NAME = "comsol_mcp.native_optimizer_licensed_gate"
SCHEMA_VERSION = "1.0.0"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--tree-audit", type=Path, required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--optimizer-method", choices=("gcmma", "mma", "ipopt"), required=True)
    parser.add_argument("--max-solves", type=int, required=True)
    parser.add_argument("--max-iterations", type=int, required=True)
    parser.add_argument("--optimizer-iterations", type=int, required=True)
    parser.add_argument("--move-limit", type=float, required=True)
    parser.add_argument("--max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--max-commit-fraction", type=float, required=True)
    parser.add_argument("--max-disk-bytes", type=int, required=True)
    parser.add_argument("--max-review-items", type=int, required=True)
    parser.add_argument("--max-elements-per-model", type=int, required=True)
    parser.add_argument("--minimum-element-quality", type=float, required=True)
    parser.add_argument("--deformation-jacobian-expression", required=True)
    parser.add_argument("--minimum-relative-jacobian", type=float, required=True)
    return parser


def _numeric_series(value) -> list[float]:
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()
    flattened = []

    def collect(item):
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


def _dataset_for_solution(model, solution_tag: str):
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


def _forward_sweep_dataset(model):
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


def _mesh_statistics(model) -> dict:
    statistics = model.java.component("comp1").mesh("mesh1").stat()
    return {
        "element_count": int(statistics.getNumElem()),
        "minimum_quality": float(statistics.getMinQuality()),
        "mean_quality": float(statistics.getMeanQuality()),
        "quality_measure": str(statistics.getQualityMeasure()),
    }


def _admit_mesh(statistics: dict, *, max_elements: int, minimum_quality: float) -> None:
    if isinstance(max_elements, bool) or not isinstance(max_elements, int) or max_elements < 1:
        raise ValueError("max_elements_per_model must be a caller-supplied positive integer")
    if not math.isfinite(minimum_quality) or not 0.0 < minimum_quality <= 1.0:
        raise ValueError("minimum_element_quality must be caller supplied in (0, 1]")
    if statistics["element_count"] > max_elements:
        raise ValueError("mesh element count exceeds the caller-supplied per-model ceiling")
    if statistics["minimum_quality"] < minimum_quality:
        raise ValueError("mesh minimum quality is below the caller-supplied threshold")


def _requested_optimizer_iterations(args: argparse.Namespace, budget: dict) -> int:
    requested = args.optimizer_iterations
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise ValueError("optimizer_iterations must be a caller-supplied positive integer")
    if requested > budget["max_iterations"]:
        raise ValueError("optimizer_iterations exceeds the caller iteration budget")
    return requested


def _requested_move_limit(args: argparse.Namespace) -> float:
    if isinstance(args.move_limit, bool):
        raise ValueError("move_limit must be a caller-supplied positive finite number")
    try:
        requested = float(args.move_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "move_limit must be a caller-supplied positive finite number"
        ) from exc
    if not math.isfinite(requested) or requested <= 0.0:
        raise ValueError("move_limit must be a caller-supplied positive finite number")
    return requested


def _deformation_feasibility_policy(args: argparse.Namespace) -> dict:
    expression = args.deformation_jacobian_expression
    if (
        not isinstance(expression, str)
        or not expression.strip()
        or len(expression) > 256
        or any(ord(character) < 32 for character in expression)
    ):
        raise ValueError("deformation_jacobian_expression must be bounded printable text")
    threshold = float(args.minimum_relative_jacobian)
    if not math.isfinite(threshold) or threshold < 0.0:
        raise ValueError(
            "minimum_relative_jacobian must be a caller-supplied finite nonnegative number"
        )
    return {
        "jacobian_expression": expression.strip(),
        "minimum_relative_jacobian": threshold,
        "comparison": "strictly_greater_than",
        "scope": "fresh_forward_finalist_deformed_geometry",
    }


def _deformation_feasibility_evidence(values, policy: dict) -> dict:
    series = _numeric_series(values)
    minimum = min(series)
    maximum = max(series)
    threshold = policy["minimum_relative_jacobian"]
    return {
        "sample_count": len(series),
        "minimum_relative_jacobian": minimum,
        "maximum_relative_jacobian": maximum,
        "threshold": threshold,
        "passed": minimum > threshold,
    }


def _configure_solver_move_limit(
    model, study, move_limit: float, optimizer_iterations: int
) -> dict:
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
    feature.set("movelimit", f"{move_limit:.17g}")
    feature.set("mmamaxiteractive", "on")
    feature.set("mmamaxiter", str(optimizer_iterations))
    return {
        "solution_tag": solution_tag,
        "feature_tag": feature_tag,
        "movelimitactive": str(feature.getString("movelimitactive")),
        "movelimit": str(feature.getString("movelimit")),
        "mmamaxiteractive": str(feature.getString("mmamaxiteractive")),
        "mmamaxiter": str(feature.getString("mmamaxiter")),
    }


def run(args: argparse.Namespace) -> dict:
    spec = structural._spec(args)
    requested_iterations = _requested_optimizer_iterations(args, spec["optimizer"]["budget"])
    requested_move_limit = _requested_move_limit(args)
    deformation_policy = _deformation_feasibility_policy(args)
    optimizer = dict(spec["optimizer"])
    optimizer.pop("optimizer_fingerprint", None)
    optimizer["move_limit"] = requested_move_limit
    spec["optimizer"] = structural.normalize_native_optimizer_configuration(optimizer)
    source_before = structural._sha(spec["source"])
    for path in (spec["base_copy"], spec["configured_copy"]):
        path.unlink(missing_ok=True)
    support = structural._support(spec)
    receipt = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "source_revision": structural._git_identity()["revision"],
        "source_sha256": source_before,
        "optimizer_method": spec["optimizer"]["method"],
        "budget": spec["optimizer"]["budget"],
        "requested_optimizer_iterations": requested_iterations,
        "requested_move_limit": requested_move_limit,
        "objective_expression": support["objective"]["expression"],
        "points": [],
        "mesh_admission_policy": {
            "max_elements_per_model": args.max_elements_per_model,
            "minimum_element_quality": args.minimum_element_quality,
            "scope": "baseline_and_explicit_finalist_remesh",
            "internal_optimizer_remesh_callback": False,
        },
        "deformation_feasibility_policy": deformation_policy,
    }
    client = None
    private_error = None
    phase = "startup"
    started = time.monotonic()
    try:
        import mph

        client = mph.Client(cores=spec["cores"], version="6.4")
        source_model = client.load(str(spec["source"]))
        source_model.java.save(str(spec["base_copy"]), True)
        client.remove(source_model)
        model = client.load(str(spec["base_copy"]))
        structural.configure_native_adjoint(
            structural.ClientapiAdjointStudyBackend(model), support, structural._optimizer(spec)
        )
        gradient._configure_selected_sensitivity(
            model,
            support,
            [item["variable_id"] for item in support["variables"]],
        )
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        baseline_model = client.load(str(spec["configured_copy"]))
        phase = "baseline_solve"
        baseline_study = baseline_model.java.study("std1")
        baseline_study.feature().remove("sens_a71")
        baseline_mesh = _mesh_statistics(baseline_model)
        _admit_mesh(
            baseline_mesh,
            max_elements=args.max_elements_per_model,
            minimum_quality=args.minimum_element_quality,
        )
        receipt["baseline_mesh"] = baseline_mesh
        baseline_study.run()
        baseline_dataset = _forward_sweep_dataset(baseline_model)
        baseline_dataset_tag = str(baseline_dataset.tag())
        baseline_values = baseline_model.evaluate(
            receipt["objective_expression"], dataset=baseline_dataset, outer=1
        )
        baseline_series = _numeric_series(baseline_values)
        baseline = baseline_series[-1]
        phase = "optimization_solve"
        client.remove(baseline_model)
        model = client.load(str(spec["configured_copy"]))
        std2 = model.java.study("std2")
        optimization = std2.feature("opt_a71")
        optimization.set("nsolvemax", str(spec["optimizer"]["budget"]["max_solves"]))
        receipt["solver_move_limit"] = _configure_solver_move_limit(
            model,
            std2,
            spec["optimizer"]["move_limit"],
            requested_iterations,
        )
        if not math.isclose(
            float(receipt["solver_move_limit"]["movelimit"]),
            requested_move_limit,
            rel_tol=1e-12,
            abs_tol=0.0,
        ):
            raise ValueError("native optimizer solver move-limit readback drifted")
        if time.monotonic() - started > spec["optimizer"]["budget"]["max_wall_time_seconds"]:
            raise TimeoutError("native optimizer wall budget exhausted before optimization")
        std2.run()
        phase = "final_objective"
        optimizer_dataset = _dataset_for_solution(
            model, receipt["solver_move_limit"]["solution_tag"]
        )
        variables = [item["variable_id"] for item in support["variables"]]
        evaluated = model.evaluate(
            [receipt["objective_expression"], *variables], dataset=optimizer_dataset
        )
        optimizer_series = _numeric_series(evaluated[0])
        final = optimizer_series[-1]
        final_variables = {
            name: _numeric_series(values)[-1]
            for name, values in zip(variables, evaluated[1:], strict=True)
        }
        global_parameter_readback = {name: str(model.parameters()[name]) for name in variables}
        optimizer_dataset_tag = str(optimizer_dataset.tag())
        client.remove(model)
        phase = "final_fresh_forward"
        finalist = client.load(str(spec["configured_copy"]))
        for name, value in final_variables.items():
            finalist.java.param().set(name, f"{value:.17g}[m]")
        finalist_study = finalist.java.study("std1")
        finalist_study.feature().remove("sens_a71")
        mesh = finalist.java.component("comp1").mesh("mesh1")
        mesh_before = _mesh_statistics(finalist)
        mesh.run()
        mesh_after = _mesh_statistics(finalist)
        _admit_mesh(
            mesh_after,
            max_elements=args.max_elements_per_model,
            minimum_quality=args.minimum_element_quality,
        )
        finalist_study.run()
        finalist_dataset = _forward_sweep_dataset(finalist)
        finalist_dataset_tag = str(finalist_dataset.tag())
        fresh_series = _numeric_series(
            finalist.evaluate(receipt["objective_expression"], dataset=finalist_dataset, outer=1)
        )
        fresh_final = fresh_series[-1]
        deformation_evidence = _deformation_feasibility_evidence(
            finalist.evaluate(
                deformation_policy["jacobian_expression"],
                dataset=finalist_dataset,
                outer=1,
            ),
            deformation_policy,
        )
        receipt["deformation_feasibility"] = deformation_evidence
        if deformation_evidence["passed"] is not True:
            raise ValueError("fresh-forward deformation feasibility is below the caller threshold")
        physical_expressions = [
            "ewfd.Rtotal",
            "ewfd.Ttotal",
            "ewfd.Atotal",
            "wl",
            "c_const/freq",
        ]
        physical_values = finalist.evaluate(physical_expressions, dataset=finalist_dataset, outer=1)
        physical = {
            name: _numeric_series(value)[-1]
            for name, value in zip(physical_expressions, physical_values, strict=True)
        }
        closure = physical["ewfd.Rtotal"] + physical["ewfd.Ttotal"] + physical["ewfd.Atotal"]
        if not math.isclose(closure, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("remeshed finalist R/T/A closure is outside tolerance")
        requested_wavelength = support["objective"]["wavelength_um"] * 1e-6
        if not math.isclose(physical["wl"], requested_wavelength, rel_tol=1e-12, abs_tol=1e-15):
            raise ValueError("remeshed finalist evaluated wavelength changed")
        if not math.isclose(
            physical["c_const/freq"], requested_wavelength, rel_tol=1e-12, abs_tol=1e-15
        ):
            raise ValueError("remeshed finalist solved wavelength changed")
        receipt.update(
            {
                "baseline_objective": baseline,
                "baseline_objective_series": baseline_series,
                "baseline_dataset_tag": baseline_dataset_tag,
                "final_objective": final,
                "optimizer_objective_series": optimizer_series,
                "objective_delta": final - baseline,
                "final_variables_si": final_variables,
                "fresh_forward_objective": fresh_final,
                "fresh_forward_objective_series": fresh_series,
                "fresh_forward_dataset_tag": finalist_dataset_tag,
                "fresh_forward_delta": fresh_final - baseline,
                "remesh": {
                    "explicit_rebuild": True,
                    "before": mesh_before,
                    "after": mesh_after,
                },
                "physical_evidence": {
                    "reflectance": physical["ewfd.Rtotal"],
                    "transmittance": physical["ewfd.Ttotal"],
                    "absorption": physical["ewfd.Atotal"],
                    "closure": closure,
                    "requested_wavelength_m": requested_wavelength,
                    "evaluated_wavelength_m": physical["wl"],
                    "solved_wavelength_m": physical["c_const/freq"],
                    "branch_disposition": "single_fixed_state_no_continuation_claim",
                    "robustness_disposition": "multi_state_deferred_to_alpha7_2",
                },
                "global_parameter_readback": global_parameter_readback,
                "optimizer_dataset_tag": optimizer_dataset_tag,
            }
        )
        if not math.isclose(fresh_final, final, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("fresh forward objective differs from native optimizer result")
        client.remove(finalist)
        model = None
        phase = "parameter_readback"
        receipt.update(
            {
                "success": True,
                "elapsed_seconds": time.monotonic() - started,
                "configured_copy_sha256": structural._sha(spec["configured_copy"]),
            }
        )
    except Exception as exc:
        receipt["error"] = {"code": "native_optimizer_failed", "type": type(exc).__name__}
        private_error = {"phase": phase, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        cleanup = {
            "client_clear": False,
            "source_unchanged": structural._sha(spec["source"]) == source_before,
        }
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                cleanup["client_clear_error_type"] = type(exc).__name__
        receipt["cleanup"] = cleanup
        receipt["elapsed_seconds"] = time.monotonic() - started
        receipt["success"] = receipt.get("success") is True and all(cleanup.values())
    structural.atomic_write_json(spec["root"] / "native-optimizer-receipt.json", receipt)
    structural.atomic_write_json(
        spec["root"] / "native-optimizer-private.json",
        {"error": private_error} if private_error is not None else {"error": None},
    )
    return receipt


if __name__ == "__main__":
    arguments = _parser().parse_args()
    result = run(arguments)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result["success"] else 1)
