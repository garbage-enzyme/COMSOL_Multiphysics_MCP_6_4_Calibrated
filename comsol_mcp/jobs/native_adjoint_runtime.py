"""Licensed fixed-topology native adjoint optimization runtime."""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.adapter.adjoint import (
    ClientapiAdjointStudyBackend,
    _native_objective_type,
    configure_native_adjoint,
)
from comsol_mcp.durable import atomic_write_json
from comsol_mcp.jobs.adjoint_optimization import normalize_native_execution_policy
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


def _freeze_wavelength(
    model: Any, support: Mapping[str, Any], initial_values: object | None = None
) -> None:
    from comsol_mcp.adapter.adjoint import _variable_strings

    initial = _variable_strings(support, initial_values)["values"]
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
        initial,
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


def _validate_wavelengths(
    requested_wavelength_m: float, evaluated_wavelength_m: float, solved_wavelength_m: float
) -> None:
    """Reject finalist evidence when either wavelength identity drifts."""
    for name, value in (
        ("requested", requested_wavelength_m),
        ("evaluated", evaluated_wavelength_m),
        ("solved", solved_wavelength_m),
    ):
        if not math.isfinite(value):
            raise ValueError(f"{name} wavelength is nonfinite")
    if not math.isclose(
        evaluated_wavelength_m, requested_wavelength_m, rel_tol=1e-12, abs_tol=1e-15
    ):
        raise ValueError("remeshed finalist evaluated wavelength changed")
    if not math.isclose(solved_wavelength_m, requested_wavelength_m, rel_tol=1e-12, abs_tol=1e-15):
        raise ValueError("remeshed finalist solved wavelength changed")


def _optimizer_solve_budget(total_solves: object, *, finalist_solves: int = 1) -> int:
    """Reserve the baseline and every explicitly declared finalist solve."""
    if (
        isinstance(finalist_solves, bool)
        or not isinstance(finalist_solves, int)
        or not 1 <= finalist_solves <= 32
    ):
        raise ValueError("finalist_solves must be an integer in [1, 32]")
    if (
        isinstance(total_solves, bool)
        or not isinstance(total_solves, int)
        or total_solves < finalist_solves + 2
    ):
        raise ValueError("max_solves must cover baseline, optimization, and finalist solves")
    return total_solves - 1 - finalist_solves


def _initial_values_si(support: Mapping[str, Any], values: list[float]) -> dict[str, float]:
    scales = {"m": 1.0, "um": 1e-6, "nm": 1e-9}
    initial: dict[str, float] = {}
    for item, value in zip(support["variables"], values, strict=True):
        try:
            factor = scales[item["unit"]]
        except KeyError as exc:
            raise ValueError(
                f"control variable {item['variable_id']!r} carries unsupported unit "
                f"{item['unit']!r}; the native runtime converts only m, um, and nm"
            ) from exc
        initial[item["variable_id"]] = value * factor
    return initial


def _check_execution_budget(started: float, root: Path, budget: Mapping[str, Any]) -> None:
    if time.monotonic() - started > budget["max_wall_time_seconds"]:
        raise TimeoutError("native optimization exceeded its caller wall budget")
    size = sum(
        path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    )
    if size > budget["max_disk_bytes"]:
        raise ValueError("native optimization exceeded its caller disk budget")


def _run_loading_solution(model: Any, study: Any) -> tuple[str, dict[str, str]]:
    study.createAutoSequences("all")
    tags = [str(tag) for tag in study.getSolverSequences("all")]
    if len(tags) != 1:
        raise ValueError("finalist loading solver identity is ambiguous")
    solution = model.java.sol(tags[0])
    node = solution.feature("v1")
    expected = {"initmethod": "sol", "initsol": tags[0], "solnum": "last"}
    for key, value in expected.items():
        node.set(key, value)
    observed = {key: str(node.getString(key)) for key in expected}
    if observed != expected:
        raise ValueError("finalist initialization readback differs")
    solution.runAll()
    return tags[0], observed


def _admit_policy_mesh(model: Any, spec: Mapping[str, Any], policy: Mapping[str, Any]) -> None:
    mesh = model.java.component("comp1").mesh("mesh1")
    mesh.run()
    evidence = _mesh_statistics(model)
    if evidence["element_count"] > spec["resource_policy"]["rules"]["max_mesh_elements"]:
        raise ValueError("native mesh exceeds caller element limit")
    quality = evidence["minimum_quality"]
    if not math.isfinite(quality) or quality < policy["minimum_element_quality"]:
        raise ValueError("native mesh violates caller quality limit")


def _mapping_evidence(
    model: Any,
    dataset: Any,
    policy: Mapping[str, Any],
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = evidence if evidence is not None else {}
    for kind in ("geometry", "material"):
        expression = policy[f"{kind}_jacobian_expression"]
        values = _numeric_series(model.evaluate(expression, dataset=dataset, outer=1, inner="last"))
        minimum = min(values)
        result[kind] = {
            "expression": expression,
            "minimum": minimum,
            "maximum": max(values),
            "sample_count": len(values),
        }
        if minimum <= policy[f"minimum_{kind}_jacobian"]:
            raise ValueError(f"native {kind} mapping violates caller limits")
    return result


def _constraint_evidence(
    model: Any,
    dataset: Any,
    support: Mapping[str, Any],
    tolerance: float,
    *,
    evidence: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows = evidence if evidence is not None else []
    for item in support["constraints"]:
        values = _numeric_series(
            model.evaluate(
                item["expression"], unit=item["unit"], dataset=dataset, outer=1, inner="last"
            )
        )
        if len(values) != 1:
            raise ValueError("native constraint must return one scalar")
        value = values[0]
        violation = max(
            0.0,
            0.0 if item["lower"] is None else item["lower"] - value,
            0.0 if item["upper"] is None else value - item["upper"],
        )
        rows.append(
            {
                "constraint_id": item["constraint_id"],
                "value": value,
                "unit": item["unit"],
                "violation": violation,
                "tolerance": tolerance,
            }
        )
        if violation > tolerance:
            raise ValueError("native constraint violates caller tolerance")
    return rows


def _solved_dimensions(
    model: Any, dataset: Any, domain: int, controls: Mapping[str, float]
) -> dict[str, float]:
    numerical = model.java.result().numerical()
    extrema = {}
    for kind in ("MaxVolume", "MinVolume"):
        tag = "finalist_" + kind
        if tag in [str(value) for value in numerical.tags()]:
            raise ValueError("finalist dimension probe tag already exists")
        node = numerical.create(tag, kind)
        try:
            node.selection().set([domain])
            node.set("data", str(dataset.tag()))
            _set_vector(node, "expr", ["x", "y"])
            extrema[kind] = _numeric_series([list(row) for row in node.getReal()])
        finally:
            numerical.remove(tag)
    if any(len(value) != 2 for value in extrema.values()):
        raise ValueError("finalist dimensions are not a single solved state")
    measured = {
        name: extrema["MaxVolume"][index] - extrema["MinVolume"][index]
        for index, name in enumerate(("patch_length_x", "patch_length_y"))
    }
    if any(
        not math.isclose(value, controls[name], rel_tol=1e-10, abs_tol=1e-18)
        for name, value in measured.items()
    ):
        raise ValueError("finalist solved dimensions differ from controls")
    return measured


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
    _native_objective_type(support)
    optimizer = spec["optimizer"]
    if optimizer["method"] != "gcmma":
        raise ValueError("licensed product runtime accepts only validated gcmma")
    if spec["cores"] != optimizer["budget"]["cores"]:
        raise ValueError("submission cores differ from optimizer budget")
    policy = (
        normalize_native_execution_policy(spec["execution_policy"])
        if "execution_policy" in spec
        else None
    )
    optimizer_solves = _optimizer_solve_budget(
        optimizer["budget"]["max_solves"],
        finalist_solves=len(policy["finalist_fractions"]) if policy else 1,
    )
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
        adapter_receipt = configure_native_adjoint(
            ClientapiAdjointStudyBackend(model),
            support,
            optimizer,
            initial_values=spec["initial_values"],
        )
        if policy is not None:
            deformation = model.java.component("comp1").physics("dg_a71")
            for group, key, expected in (
                ("FreeDeformationSettings", "smoothingType", policy["smoothing_type"]),
                ("FrameSettings", "geometryShapeOrder", policy["geometry_shape_order"]),
            ):
                node = deformation.prop(group)
                node.set(key, expected)
                if str(node.getString(key)) != expected:
                    raise ValueError("native deformation policy readback differs")
            receipt["execution_policy"] = policy
        _freeze_wavelength(model, support, spec["initial_values"])
        model.java.save(str(configured), True)
        client.remove(model)

        baseline_model = client.load(str(configured))
        baseline_study = baseline_model.java.study("std1")
        baseline_study.feature().remove("sens_a71")
        if policy is not None:
            _check_execution_budget(started, workdir, optimizer["budget"])
            _admit_policy_mesh(baseline_model, spec, policy)
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
        study.feature("opt_a71").set("nsolvemax", str(optimizer_solves))
        solver = _configure_solver(model, study, optimizer)
        if policy is not None:
            feature = model.java.sol(solver["solution_tag"]).feature(solver["feature_tag"])
            feature.set("gradientmma", policy["gradient_method"])
            if str(feature.getString("gradientmma")) != policy["gradient_method"]:
                raise ValueError("native optimizer gradient readback differs")
        if policy is not None:
            _check_execution_budget(started, workdir, optimizer["budget"])
            _admit_policy_mesh(model, spec, policy)
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
        if policy is not None:
            final_variables = {
                name: _numeric_series(
                    model.evaluate(name, unit="m", dataset=dataset, outer=1, inner="last")
                )[-1]
                for name in variables
            }
            receipt["optimizer_constraints"] = []
            _constraint_evidence(
                model,
                dataset,
                support,
                optimizer["constraint_tolerance"],
                evidence=receipt["optimizer_constraints"],
            )
            receipt["optimizer_geometry"] = {}
            _mapping_evidence(model, dataset, policy, evidence=receipt["optimizer_geometry"])
        client.remove(model)

        finalist = client.load(str(configured))
        if policy is None:
            for name, value in final_variables.items():
                finalist.java.param().set(name, f"{value:.17g}[m]")
        finalist_study = finalist.java.study("std1")
        finalist_study.feature().remove("sens_a71")
        mesh = finalist.java.component("comp1").mesh("mesh1")
        mesh.run()
        mesh_evidence = _mesh_statistics(finalist)
        if not math.isfinite(mesh_evidence["minimum_quality"]) or (
            mesh_evidence["minimum_quality"] < policy["minimum_element_quality"]
            if policy
            else mesh_evidence["minimum_quality"] <= 0.1
        ):
            raise ValueError("remeshed finalist minimum quality is not acceptable")
        if policy is None:
            finalist_study.run()
            finalist_dataset = _forward_sweep_dataset(finalist)
        else:
            maximum = spec["resource_policy"]["rules"]["max_mesh_elements"]
            if mesh_evidence["element_count"] > maximum:
                raise ValueError("finalist mesh exceeds the caller element limit")
            steps: list[dict[str, Any]] = []
            receipt["finalist_continuation"] = steps
            initial = _initial_values_si(support, spec["initial_values"])
            for fraction in policy["finalist_fractions"]:
                _check_execution_budget(started, workdir, optimizer["budget"])
                controls = {
                    name: value
                    if fraction == 1.0
                    else initial[name] + fraction * (value - initial[name])
                    for name, value in final_variables.items()
                }
                for name, value in controls.items():
                    finalist.java.param().set(name, f"{value:.17g}[m]")
                solution_tag, initialization = _run_loading_solution(finalist, finalist_study)
                finalist_dataset = _dataset_for_solution(finalist, solution_tag)
                step = {
                    "fraction": fraction,
                    "controls_si": controls,
                    "initialization": initialization,
                    "solution_tag": solution_tag,
                    "dataset_tag": str(finalist_dataset.tag()),
                }
                steps.append(step)
                step["geometry"] = {}
                _mapping_evidence(finalist, finalist_dataset, policy, evidence=step["geometry"])
                step["dimensions"] = _solved_dimensions(
                    finalist,
                    finalist_dataset,
                    adapter_receipt["controls"]["deformed_geometry"]["patch_domain"],
                    controls,
                )
                atomic_write_json(receipt_path, receipt)
            receipt["finalist_constraints"] = []
            _constraint_evidence(
                finalist,
                finalist_dataset,
                support,
                optimizer["constraint_tolerance"],
                evidence=receipt["finalist_constraints"],
            )
            _check_execution_budget(started, workdir, optimizer["budget"])
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
        receipt["physical_values"] = physical
        if policy is not None:
            tolerance = policy["passivity_absolute_tolerance"]
            if any(
                not -tolerance <= physical[name] <= 1 + tolerance
                for name in ("ewfd.Rtotal", "ewfd.Ttotal", "ewfd.Atotal")
            ):
                raise ValueError("finalist violates caller passivity limits")
        fresh = physical[support["objective"]["expression"]]
        _validate_wavelengths(
            support["objective"]["wavelength_um"] * 1e-6,
            physical["wl"],
            physical["c_const/freq"],
        )
        closure = physical["ewfd.Rtotal"] + physical["ewfd.Ttotal"] + physical["ewfd.Atotal"]
        if not math.isclose(fresh, final, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError("fresh forward objective differs from native optimizer result")
        if not math.isclose(
            closure,
            1.0,
            rel_tol=0.0,
            abs_tol=policy["closure_absolute_tolerance"] if policy else 1e-6,
        ):
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
