"""Run a bounded native-adjoint derivative gate on the trusted alpha7.1 fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

_structural = import_module("development_kit.scripts.native_adjoint_licensed_gate")
_durable = import_module("comsol_mcp.durable")
_resource_admission = import_module("comsol_mcp.jobs.resource_admission")
_derived_geometry = import_module("comsol_mcp.tools.derived_geometry")

atomic_write_json = _durable.atomic_write_json
domain_sha256_v2 = _durable.domain_sha256_v2
configure_native_adjoint = _structural.configure_native_adjoint
ClientapiAdjointStudyBackend = _structural.ClientapiAdjointStudyBackend
_set_vector = _derived_geometry._set_vector

SCHEMA_NAME = "comsol_mcp.native_gradient_licensed_gate"
SCHEMA_VERSION = "1.0.0"
WAVELENGTH_EXPRESSION = "1.717657785e-6[m]"
VARIABLE_MODES = {
    "one-variable": ("patch_length_x",),
    "full-vector": ("patch_length_x", "patch_length_y"),
}


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = _structural._parser()
    parser.description = __doc__
    parser.add_argument("--mode", choices=tuple(VARIABLE_MODES), required=True)
    return parser


def _git_identity() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    revision = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603
        [git, "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return {"revision": revision, "clean": not status.strip()}


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    spec = _structural._spec(args)
    spec.update(
        {
            "mode": args.mode,
            "selected_variables": list(VARIABLE_MODES[args.mode]),
            "base_copy": spec["root"] / "base.mph",
            "configured_copy": spec["root"] / "configured.mph",
            "presolve_copy": spec["root"] / "presolve.mph",
            "solved_copy": spec["root"] / "solved.mph",
            "receipt": spec["root"] / "gradient-receipt.json",
            "private_receipt": spec["root"] / "gradient-private.json",
        }
    )
    if spec["optimizer"]["budget"]["max_solves"] < 1:
        raise ValueError("native gradient gate requires at least one solve")
    return spec


def _support(spec: dict[str, Any]) -> dict[str, Any]:
    return _structural._support(spec)


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    support = _support(spec)
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "source_sha256": spec["source_sha256"],
        "manifest_sha256": spec["manifest_sha256"],
        "tree_audit_sha256": spec["tree_audit_sha256"],
        "mode": spec["mode"],
        "selected_variables": spec["selected_variables"],
        "objective_expression": support["objective"]["expression"],
        "wavelength_expression": WAVELENGTH_EXPRESSION,
        "expected_solutions": ["sol1", "sol2", "sol3"],
        "expected_derivative_dataset": {"dataset": "dset2", "solution": "sol2"},
        "derivative_expression": "real(fsens(control_variable))",
        "budget": spec["optimizer"]["budget"],
        "solver_started": False,
        "filesystem_modified": False,
        "paths_included": False,
    }


def _tags(container: Any) -> list[str]:
    return [str(item) for item in list(container.tags())]


def _java_string(value: Any) -> str:
    try:
        return "".join(str(item) for item in list(value))
    except TypeError:
        return str(value)


def _dataset_by_tag(model: Any, tag: str) -> Any:
    for dataset in model / "datasets":
        if str(dataset.tag()) == tag:
            return dataset
    raise ValueError(f"dataset tag is absent: {tag}")


def _complex_scalar(value: Any) -> complex:
    import numpy as np

    array = np.asarray(value)
    if array.size != 1:
        raise ValueError(f"expected scalar evaluation, observed shape {array.shape}")
    result = complex(array.reshape(-1)[0])
    if not math.isfinite(result.real) or not math.isfinite(result.imag):
        raise ValueError("native derivative evaluation is nonfinite")
    return result


def _readback(model: Any, selected_variables: list[str]) -> dict[str, Any]:
    java = model.java
    study = java.study("std1")
    sweep = study.feature("sweep1")
    sensitivity = study.feature("sens_a71")
    wavelength = study.feature("step1")
    return {
        "wl_parameter": dict(model.parameters())["wl"],
        "parametric": {
            "pname": [str(item) for item in list(sweep.getStringArray("pname"))],
            "plistarr": [str(item) for item in list(sweep.getStringArray("plistarr"))],
            "punit": [str(item) for item in list(sweep.getStringArray("punit"))],
        },
        "wavelength_plist": str(wavelength.getString("plist")),
        "gradient_method": str(sensitivity.getString("gradientMethod")),
        "variable_order": [str(item) for item in list(sensitivity.getStringArray("pname"))],
        "variable_value_types": [
            str(item) for item in list(sensitivity.getStringArray("valuetype"))
        ],
        "objective": [str(item) for item in list(sensitivity.getStringArray("optobj"))],
        "selected_variables_match": [
            str(item) for item in list(sensitivity.getStringArray("pname"))
        ]
        == selected_variables,
    }


def _configure_selected_sensitivity(
    model: Any,
    support: dict[str, Any],
    selected_variables: list[str],
) -> None:
    java = model.java
    study = java.study("std1")
    sweep = study.feature("sweep1")
    sensitivity = study.feature("sens_a71")
    wavelength = study.feature("step1")
    by_id = {item["variable_id"]: item for item in support["variables"]}
    java.param().set("wl", WAVELENGTH_EXPRESSION)
    _set_vector(sweep, "pname", ["wl"])
    _set_vector(sweep, "plistarr", [WAVELENGTH_EXPRESSION])
    _set_vector(sweep, "punit", ["m"])
    wavelength.set("plist", WAVELENGTH_EXPRESSION)
    _set_vector(sensitivity, "pname", selected_variables)
    _set_vector(sensitivity, "punit", [by_id[item]["unit"] for item in selected_variables])
    _set_vector(
        sensitivity,
        "initval",
        [f"{by_id[item]['baseline']:.17g}[{by_id[item]['unit']}]" for item in selected_variables],
    )
    _set_vector(
        sensitivity,
        "scale",
        [f"{by_id[item]['scale']:.17g}[{by_id[item]['unit']}]" for item in selected_variables],
    )
    _set_vector(sensitivity, "valuetype", ["real"] * len(selected_variables))
    _set_vector(sensitivity, "optobj", [support["objective"]["expression"]])


def _generated_identity(model: Any) -> dict[str, Any]:
    java = model.java
    solutions = []
    for tag in _tags(java.sol()):
        solution = java.sol(tag)
        solutions.append(
            {
                "tag": tag,
                "study": _java_string(solution.study()),
                "empty": bool(solution.isEmpty()),
                "features": [
                    {
                        "tag": feature_tag,
                        "type": str(solution.feature(feature_tag).getType()),
                    }
                    for feature_tag in _tags(solution.feature())
                ],
            }
        )
    datasets = []
    for tag in _tags(java.result().dataset()):
        dataset = java.result().dataset(tag)
        datasets.append(
            {
                "tag": tag,
                "type": str(dataset.getType()),
                "solution": _java_string(dataset.getString("solution")),
            }
        )
    if [item["tag"] for item in solutions] != ["sol1", "sol2", "sol3"]:
        raise ValueError("native adjoint generated an unexpected solution identity")
    if [item["tag"] for item in datasets] != ["dset1", "dset2"]:
        raise ValueError("native adjoint generated an unexpected dataset identity")
    derivative_dataset = next(item for item in datasets if item["tag"] == "dset2")
    if derivative_dataset["solution"] != "sol2":
        raise ValueError("derivative dataset is not bound to the sensitivity solution")
    return {"solutions": solutions, "datasets": datasets}


def _resource_preflight(max_commit_fraction: float) -> dict[str, Any]:
    if sys.platform != "win32":
        return {"available": False, "admitted": False, "reason": "windows_commit_unavailable"}
    remaining, limit = _resource_admission._windows_commit_bytes()
    if remaining is None or limit is None or limit <= 0:
        return {"available": False, "admitted": False, "reason": "windows_commit_unavailable"}
    used_fraction = 1.0 - (remaining / limit)
    return {
        "available": True,
        "admitted": used_fraction <= max_commit_fraction,
        "used_fraction": used_fraction,
        "caller_ceiling": max_commit_fraction,
    }


def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["clean"]:
        raise RuntimeError("native gradient licensed gate requires a clean source tree")
    preflight = _resource_preflight(spec["optimizer"]["budget"]["max_commit_fraction"])
    if preflight["admitted"] is not True:
        raise RuntimeError("caller-declared commit ceiling does not admit this run")

    import mph

    source_before = _sha(spec["source"])
    support = _support(spec)
    for path in (
        spec["base_copy"],
        spec["configured_copy"],
        spec["presolve_copy"],
        spec["solved_copy"],
    ):
        path.unlink(missing_ok=True)
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "dry_run": False,
        "source_revision": git["revision"],
        "source_sha256": source_before,
        "manifest_sha256": spec["manifest_sha256"],
        "tree_audit_sha256": spec["tree_audit_sha256"],
        "mode": spec["mode"],
        "selected_variables": spec["selected_variables"],
        "budget": spec["optimizer"]["budget"],
        "resource_preflight": preflight,
        "solve_started": False,
        "paths_included": False,
    }
    private: dict[str, Any] = {
        "source_model": str(spec["source"]),
        "base_copy": str(spec["base_copy"]),
        "configured_copy": str(spec["configured_copy"]),
        "presolve_copy": str(spec["presolve_copy"]),
        "solved_copy": str(spec["solved_copy"]),
    }
    client = None
    model = None
    started = time.monotonic()
    try:
        client = mph.Client(cores=spec["cores"], version="6.4")
        source_model = client.load(str(spec["source"]))
        source_model.java.save(str(spec["base_copy"]), True)
        client.remove(source_model)
        model = client.load(str(spec["base_copy"]))
        adapter_receipt = configure_native_adjoint(
            ClientapiAdjointStudyBackend(model), support, dict(spec["optimizer"])
        )
        _configure_selected_sensitivity(model, support, spec["selected_variables"])
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        model = client.load(str(spec["configured_copy"]))
        readback = _readback(model, spec["selected_variables"])
        if not readback["selected_variables_match"]:
            raise ValueError("saved sensitivity variable order differs after reload")
        if readback["gradient_method"] != "adjoint":
            raise ValueError("saved sensitivity method is not adjoint")
        if readback["variable_value_types"] != ["real"] * len(spec["selected_variables"]):
            raise ValueError("saved sensitivity controls are not real-valued")
        if readback["objective"] != [support["objective"]["expression"]]:
            raise ValueError("saved sensitivity objective differs after reload")
        model.java.save(str(spec["presolve_copy"]), True)
        receipt["solve_started"] = True
        solve_started = time.monotonic()
        model.java.study("std1").run()
        solve_elapsed = time.monotonic() - solve_started
        model.java.save(str(spec["solved_copy"]), True)
        generated = _generated_identity(model)
        expressions = [support["objective"]["expression"]]
        for variable in spec["selected_variables"]:
            expressions.extend(
                [
                    f"fsens({variable})",
                    f"real(fsens({variable}))",
                    f"imag(fsens({variable}))",
                ]
            )
        values = model.evaluate(
            expressions,
            dataset=_dataset_by_tag(model, "dset2"),
            outer=1,
        )
        scalars = {
            expression: _complex_scalar(value)
            for expression, value in zip(expressions, values, strict=True)
        }
        objective = scalars[support["objective"]["expression"]]
        if abs(objective.imag) > 1e-12:
            raise ValueError("native objective is not real-valued")
        derivatives = []
        for variable in spec["selected_variables"]:
            raw = scalars[f"fsens({variable})"]
            accepted = scalars[f"real(fsens({variable}))"]
            if accepted.imag != 0.0 or accepted.real != raw.real:
                raise ValueError("accepted derivative component differs from raw native real part")
            derivatives.append(
                {
                    "variable_id": variable,
                    "raw": {"real": raw.real, "imaginary": raw.imag},
                    "accepted_real": accepted.real,
                    "unit": "1/m",
                }
            )
        identities = {
            "primal": domain_sha256_v2(
                "comsol_mcp.native_primal_identity", generated["solutions"][0]
            ),
            "adjoint": domain_sha256_v2(
                "comsol_mcp.native_adjoint_identity", generated["solutions"][1]
            ),
            "study": domain_sha256_v2("comsol_mcp.native_study_identity", readback),
            "solution": domain_sha256_v2(
                "comsol_mcp.native_solution_identity", generated["solutions"]
            ),
            "dataset": domain_sha256_v2(
                "comsol_mcp.native_dataset_identity", generated["datasets"]
            ),
        }
        receipt.update(
            {
                "success": True,
                "configured_copy_sha256": _sha(spec["configured_copy"]),
                "presolve_copy_sha256": _sha(spec["presolve_copy"]),
                "solved_copy_sha256": _sha(spec["solved_copy"]),
                "readback": readback,
                "generated_identity": generated,
                "identity_fingerprints": identities,
                "objective": {
                    "expression": support["objective"]["expression"],
                    "value": objective.real,
                },
                "derivatives": derivatives,
                "solve_elapsed_seconds": solve_elapsed,
                "wall_elapsed_seconds": time.monotonic() - started,
                "source_unchanged": _sha(spec["source"]) == source_before,
            }
        )
        if receipt["wall_elapsed_seconds"] > spec["optimizer"]["budget"]["max_wall_time_seconds"]:
            raise TimeoutError("native gradient gate exceeded its caller wall-time budget")
        private["adapter_receipt"] = adapter_receipt
    except Exception as exc:
        receipt["success"] = False
        receipt["error"] = {"code": "native_gradient_failed", "type": type(exc).__name__}
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = {
            "model_removed": model is None,
            "client_clear": False,
            "source_unchanged": _sha(spec["source"]) == source_before,
        }
        if client is not None:
            if model is not None:
                try:
                    client.remove(model)
                    cleanup["model_removed"] = True
                except Exception as exc:
                    private["model_remove_error"] = f"{type(exc).__name__}: {exc}"
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                private["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        receipt["cleanup"] = cleanup
        receipt["success"] = receipt.get("success") is True and all(cleanup.values())
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
