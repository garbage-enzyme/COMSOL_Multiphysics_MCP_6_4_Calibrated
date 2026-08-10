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
    parser.add_argument("--max-wall-time-seconds", type=int, required=True)
    parser.add_argument("--max-commit-fraction", type=float, required=True)
    parser.add_argument("--max-disk-bytes", type=int, required=True)
    parser.add_argument("--max-review-items", type=int, required=True)
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
        "objective_expression": support["objective"]["expression"],
        "points": [],
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
            spec["optimizer"]["budget"]["max_iterations"],
        )
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
        phase = "parameter_readback"
        receipt.update(
            {
                "success": True,
                "baseline_objective": baseline,
                "baseline_objective_series": baseline_series,
                "baseline_dataset_tag": baseline_dataset_tag,
                "final_objective": final,
                "optimizer_objective_series": optimizer_series,
                "objective_delta": final - baseline,
                "final_variables_si": final_variables,
                "global_parameter_readback": {
                    name: str(model.parameters()[name]) for name in variables
                },
                "optimizer_dataset_tag": str(optimizer_dataset.tag()),
                "elapsed_seconds": time.monotonic() - started,
                "configured_copy_sha256": structural._sha(spec["configured_copy"]),
            }
        )
    except Exception as exc:
        receipt["error"] = {"code": "native_optimizer_failed", "type": type(exc).__name__}
        private_error = {"phase": phase, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        cleanup = {"client_clear": False, "source_unchanged": structural._sha(spec["source"]) == source_before}
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception:
                pass
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
