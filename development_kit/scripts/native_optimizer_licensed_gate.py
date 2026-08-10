"""Run one bounded native COMSOL optimization with fresh forward evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from development_kit.scripts import native_adjoint_licensed_gate as structural

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


def _objective(model, expression: str) -> float:
    value = model.evaluate(expression)
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise ValueError("native objective must be scalar")
        value = value[0]
    return float(value)


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
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        baseline_model = client.load(str(spec["configured_copy"]))
        baseline_study = baseline_model.java.study("std1")
        baseline_study.feature().remove("sens_a71")
        baseline_study.run()
        baseline = _objective(baseline_model, receipt["objective_expression"])
        client.remove(baseline_model)
        model = client.load(str(spec["configured_copy"]))
        std2 = model.java.study("std2")
        optimization = std2.feature("opt_a71")
        optimization.set("nsolvemax", str(spec["optimizer"]["budget"]["max_solves"]))
        try:
            optimization.set("maxiter", str(spec["optimizer"]["budget"]["max_iterations"]))
        except Exception:
            pass
        if time.monotonic() - started > spec["optimizer"]["budget"]["max_wall_time_seconds"]:
            raise TimeoutError("native optimizer wall budget exhausted before optimization")
        std2.run()
        final = _objective(model, receipt["objective_expression"])
        receipt.update(
            {
                "success": True,
                "baseline_objective": baseline,
                "final_objective": final,
                "objective_delta": final - baseline,
                "parameters": {name: str(model.parameters()[name]) for name in ("patch_length_x", "patch_length_y")},
                "elapsed_seconds": time.monotonic() - started,
                "configured_copy_sha256": structural._sha(spec["configured_copy"]),
            }
        )
    except Exception as exc:
        receipt["error"] = {"code": "native_optimizer_failed", "type": type(exc).__name__}
        private_error = f"{type(exc).__name__}: {exc}"
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
