"""Run independent three-step central finite-difference validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from importlib import import_module
from pathlib import Path
from typing import Any

from . import native_gradient_licensed_gate as _native

_structural = import_module("development_kit.scripts.native_adjoint_licensed_gate")
_durable = import_module("comsol_mcp.durable")
_derived_geometry = import_module("comsol_mcp.tools.derived_geometry")

atomic_write_json = _durable.atomic_write_json
configure_native_adjoint = _native.configure_native_adjoint
ClientapiAdjointStudyBackend = _native.ClientapiAdjointStudyBackend
_set_vector = _derived_geometry._set_vector

SCHEMA_NAME = "comsol_mcp.native_gradient_fd_licensed_gate"
SCHEMA_VERSION = "1.0.0"
STEPS = (0.01, 0.003, 0.001)
WAVELENGTH_EXPRESSION = _native.WAVELENGTH_EXPRESSION


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = _structural._parser()
    parser.description = __doc__
    parser.add_argument("--mode", choices=("full-vector",), default="full-vector")
    parser.add_argument("--native-receipt", type=Path, required=True)
    parser.add_argument("--relative-steps", default=",".join(str(item) for item in STEPS))
    return parser


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode != "full-vector":
        raise ValueError("finite-difference gate requires --mode full-vector")
    spec = _native._spec(args)
    native_receipt = args.native_receipt.resolve(strict=True)
    if not str(native_receipt).isascii():
        raise ValueError("native receipt must use an ASCII path")
    try:
        steps = tuple(float(item.strip()) for item in str(args.relative_steps).split(","))
    except ValueError as exc:
        raise ValueError("relative steps must be finite comma-separated numbers") from exc
    if steps != STEPS:
        raise ValueError("alpha7.1 finite-difference steps are exactly 0.01, 0.003, 0.001")
    native = json.loads(native_receipt.read_text(encoding="utf-8"))
    if (
        native.get("schema_name") != "comsol_mcp.native_gradient_licensed_gate"
        or native.get("success") is not True
        or native.get("mode") != "full-vector"
        or native.get("source_sha256") != spec["source_sha256"]
    ):
        raise ValueError("native receipt does not bind the exact successful full-vector source")
    gradients = {
        item["variable_id"]: float(item["accepted_real"])
        for item in native["derivatives"]
    }
    if set(gradients) != set(spec["selected_variables"]):
        raise ValueError("native receipt derivative variables differ from the canonical vector")
    spec.update(
        {
            "native_receipt": native_receipt,
            "native_receipt_sha256": _sha(native_receipt),
            "native_gradients": gradients,
            "steps": steps,
            "base_copy": spec["root"] / "base.mph",
            "configured_copy": spec["root"] / "configured.mph",
            "points": spec["root"] / "points.jsonl",
            "receipt": spec["root"] / "fd-receipt.json",
            "private_receipt": spec["root"] / "fd-private.json",
        }
    )
    return spec


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "mode": "full-vector",
        "variables": spec["selected_variables"],
        "relative_steps": list(spec["steps"]),
        "forward_point_count": len(spec["selected_variables"]) * len(spec["steps"]) * 2,
        "wavelength_expression": WAVELENGTH_EXPRESSION,
        "native_receipt_sha256": spec["native_receipt_sha256"],
        "solver_started": False,
        "paths_included": False,
    }


def _append(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        import os

        os.fsync(handle.fileno())


def _set_forward_state(model: Any, values: dict[str, float]) -> None:
    java = model.java
    study = java.study("std1")
    study.feature().remove("sens_a71")
    sweep = study.feature("sweep1")
    wavelength = study.feature("step1")
    for variable, value in values.items():
        java.param().set(variable, f"{value:.17g}[m]")
    java.param().set("wl", WAVELENGTH_EXPRESSION)
    _set_vector(sweep, "pname", ["wl"])
    _set_vector(sweep, "plistarr", [WAVELENGTH_EXPRESSION])
    _set_vector(sweep, "punit", ["m"])
    wavelength.set("plist", WAVELENGTH_EXPRESSION)


def _baseline_values(support: dict[str, Any], variables: list[str]) -> dict[str, float]:
    by_id = {item["variable_id"]: item for item in support["variables"]}
    return {variable: float(by_id[variable]["baseline"]) for variable in variables}


def _dataset_by_tag(model: Any, tag: str) -> Any:
    return _native._dataset_by_tag(model, tag)


def _forward_objective(model: Any) -> float:
    evaluated = model.evaluate(
        "comp1.ewfd.Torder_0_0",
        dataset=_dataset_by_tag(model, "dset1"),
        outer=1,
    )
    objective = _native._complex_scalar(evaluated)
    if abs(objective.imag) > 1e-12:
        raise ValueError("forward objective is not real-valued")
    return objective.real


def _forward_point(client: Any, spec: dict[str, Any], values: dict[str, float]) -> dict[str, Any]:
    model = client.load(str(spec["configured_copy"]))
    try:
        _set_forward_state(model, values)
        started = time.monotonic()
        model.java.study("std1").run()
        elapsed = time.monotonic() - started
        datasets = [str(item) for item in list(model.java.result().dataset().tags())]
        if "dset1" not in datasets:
            raise ValueError("forward point did not generate dset1")
        return {
            "values_m": values,
            "objective": _forward_objective(model),
            "dataset": "dset1",
            "dataset_solution": "sol1",
            "solve_elapsed_seconds": elapsed,
        }
    finally:
        client.remove(model)


def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _native._git_identity()
    if not git["clean"]:
        raise RuntimeError("finite-difference gate requires a clean source tree")
    source_before = _sha(spec["source"])
    resource_preflight = _native._resource_preflight(
        spec["optimizer"]["budget"]["max_commit_fraction"]
    )
    if resource_preflight["admitted"] is not True:
        raise RuntimeError("caller-declared commit ceiling does not admit this run")
    import mph
    for path in (spec["base_copy"], spec["configured_copy"], spec["points"], spec["receipt"]):
        path.unlink(missing_ok=True)
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "dry_run": False,
        "source_revision": git["revision"],
        "source_sha256": source_before,
        "native_receipt_sha256": spec["native_receipt_sha256"],
        "relative_steps": list(spec["steps"]),
        "variables": spec["selected_variables"],
        "wavelength_expression": WAVELENGTH_EXPRESSION,
        "points_fsync": True,
        "resource_preflight": resource_preflight,
        "points": [],
    }
    private = {
        "source_model": str(spec["source"]),
        "native_receipt": str(spec["native_receipt"]),
        "base_copy": str(spec["base_copy"]),
        "configured_copy": str(spec["configured_copy"]),
    }
    client = None
    try:
        client = mph.Client(cores=spec["cores"], version="6.4")
        source_model = client.load(str(spec["source"]))
        source_model.java.save(str(spec["base_copy"]), True)
        client.remove(source_model)
        model = client.load(str(spec["base_copy"]))
        support = _native._support(spec)
        adapter_receipt = configure_native_adjoint(
            ClientapiAdjointStudyBackend(model), support, dict(spec["optimizer"])
        )
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        baseline_values = _baseline_values(support, spec["selected_variables"])
        run_started = time.monotonic()
        for variable in spec["selected_variables"]:
            baseline = baseline_values[variable]
            for relative_step in spec["steps"]:
                delta = baseline * relative_step
                for direction, sign in (("plus", 1.0), ("minus", -1.0)):
                    if len(receipt["points"]) >= spec["optimizer"]["budget"]["max_solves"]:
                        raise RuntimeError("finite-difference solve budget was exhausted")
                    if (
                        time.monotonic() - run_started
                        > spec["optimizer"]["budget"]["max_wall_time_seconds"]
                    ):
                        raise TimeoutError("finite-difference gate exceeded its wall-time budget")
                    values = dict(baseline_values)
                    values[variable] = baseline + sign * delta
                    row = _forward_point(client, spec, values)
                    row.update(
                        {
                            "variable_id": variable,
                            "relative_step": relative_step,
                            "direction": direction,
                        }
                    )
                    _append(spec["points"], row)
                    receipt["points"].append(row)
        by_key = {
            (row["variable_id"], row["relative_step"], row["direction"]): row
            for row in receipt["points"]
        }
        derivatives = []
        for variable in spec["selected_variables"]:
            native = spec["native_gradients"][variable]
            baseline = baseline_values[variable]
            step_rows = []
            for relative_step in spec["steps"]:
                plus = by_key[(variable, relative_step, "plus")]["objective"]
                minus = by_key[(variable, relative_step, "minus")]["objective"]
                observed = (plus - minus) / (2.0 * baseline * relative_step)
                error = abs(observed - native) / max(abs(observed), abs(native), 1.0)
                step_rows.append(
                    {
                        "relative_step": relative_step,
                        "central_derivative_1_per_m": observed,
                        "absolute_error_1_per_m": abs(observed - native),
                        "relative_error": error,
                        "sign_agreement": (
                            observed == 0.0
                            or math.copysign(1.0, observed) == math.copysign(1.0, native)
                        ),
                    }
                )
            selected = min(step_rows, key=lambda item: item["relative_error"])
            derivatives.append(
                {
                    "variable_id": variable,
                    "native_1_per_m": native,
                    "steps": step_rows,
                    "selected": selected,
                    "step_sensitivity": max(item["relative_error"] for item in step_rows)
                    - min(item["relative_error"] for item in step_rows),
                }
            )
        native_vector = [spec["native_gradients"][item] for item in spec["selected_variables"]]
        fd_vector = [item["selected"]["central_derivative_1_per_m"] for item in derivatives]
        native_norm = math.sqrt(sum(item * item for item in native_vector))
        fd_norm = math.sqrt(sum(item * item for item in fd_vector))
        cosine = sum(a * b for a, b in zip(native_vector, fd_vector, strict=True)) / (
            native_norm * fd_norm
        )
        receipt.update(
            {
                "derivatives": derivatives,
                "finite_difference_vector": fd_vector,
                "cosine_similarity": cosine,
                "checks": {
                    "all_selected_relative_errors_below_0.1": all(
                        item["selected"]["relative_error"] <= 0.1 for item in derivatives
                    ),
                    "all_signs_agree": all(
                        item["selected"]["sign_agreement"] for item in derivatives
                    ),
                    "cosine_above_0.9": cosine >= 0.9,
                    "step_sensitivity_below_0.1": all(
                        item["step_sensitivity"] <= 0.1 for item in derivatives
                    ),
                },
                "source_unchanged": _sha(spec["source"]) == source_before,
                "success": True,
            }
        )
        receipt["success"] = all(receipt["checks"].values())
        private["adapter_receipt"] = adapter_receipt
    except Exception as exc:
        receipt["error"] = {"code": "finite_difference_failed", "type": type(exc).__name__}
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.clear()
                receipt["cleanup"] = {
                    "client_clear": True,
                    "source_unchanged": _sha(spec["source"]) == source_before,
                }
            except Exception as exc:
                receipt["cleanup"] = {
                    "client_clear": False,
                    "source_unchanged": _sha(spec["source"]) == source_before,
                }
                private["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        else:
            receipt["cleanup"] = {
                "client_clear": True,
                "source_unchanged": _sha(spec["source"]) == source_before,
            }
        receipt["success"] = receipt.get("success") is True and all(receipt["cleanup"].values())
    return receipt, private


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec = _spec(args)
    if args.dry_run:
        print(json.dumps(_dry_run(spec), ensure_ascii=False, sort_keys=True))
        return 0
    receipt, private = _run(spec)
    atomic_write_json(spec["receipt"], receipt)
    atomic_write_json(spec["private_receipt"], private)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
