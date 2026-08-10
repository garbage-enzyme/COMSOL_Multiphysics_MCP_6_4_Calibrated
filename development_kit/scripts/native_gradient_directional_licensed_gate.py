"""Run an independent deterministic directional finite-difference gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

from . import native_gradient_fd_licensed_gate as _fd
from . import native_gradient_licensed_gate as _native

SCHEMA_NAME = "comsol_mcp.native_gradient_directional_licensed_gate"
SCHEMA_VERSION = "1.0.0"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = _fd._parser()
    parser.description = __doc__
    parser.add_argument("--direction-seed", type=int, default=71004)
    parser.add_argument("--direction-relative-step", type=float, default=0.001)
    return parser


def _direction(seed: int, count: int) -> list[float]:
    if isinstance(seed, bool) or not 0 <= seed <= (1 << 63) - 1:
        raise ValueError("direction seed must be a bounded nonnegative integer")
    generator = random.Random(seed)  # noqa: S311 - deterministic scientific direction
    vector = [generator.uniform(-1.0, 1.0) for _ in range(count)]
    norm = math.sqrt(sum(item * item for item in vector))
    if norm <= 1e-12:
        raise ValueError("generated direction is numerically zero")
    return [item / norm for item in vector]


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    spec = _fd._spec(args)
    step = float(args.direction_relative_step)
    if not math.isfinite(step) or not 0.0 < step <= 0.01:
        raise ValueError("direction relative step must be finite in (0, 0.01]")
    spec.update(
        {
            "direction_seed": args.direction_seed,
            "direction": _direction(args.direction_seed, len(spec["selected_variables"])),
            "direction_relative_step": step,
            "points": spec["root"] / "directional-points.jsonl",
            "receipt": spec["root"] / "directional-receipt.json",
            "private_receipt": spec["root"] / "directional-private.json",
        }
    )
    if spec["optimizer"]["budget"]["max_solves"] < 2:
        raise ValueError("directional gate requires at least two solves")
    return spec


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "variables": spec["selected_variables"],
        "direction_seed": spec["direction_seed"],
        "direction": spec["direction"],
        "direction_relative_step": spec["direction_relative_step"],
        "forward_point_count": 2,
        "native_receipt_sha256": spec["native_receipt_sha256"],
        "solver_started": False,
        "paths_included": False,
    }


def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _native._git_identity()
    if not git["clean"]:
        raise RuntimeError("directional gate requires a clean source tree")
    source_before = _sha(spec["source"])
    resource_preflight = _native._resource_preflight(
        spec["optimizer"]["budget"]["max_commit_fraction"]
    )
    if resource_preflight["admitted"] is not True:
        raise RuntimeError("caller-declared commit ceiling does not admit this run")
    import mph

    for path in (
        spec["base_copy"],
        spec["configured_copy"],
        spec["points"],
        spec["receipt"],
    ):
        path.unlink(missing_ok=True)
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "dry_run": False,
        "source_revision": git["revision"],
        "source_sha256": source_before,
        "native_receipt_sha256": spec["native_receipt_sha256"],
        "variables": spec["selected_variables"],
        "direction_seed": spec["direction_seed"],
        "direction": spec["direction"],
        "direction_relative_step": spec["direction_relative_step"],
        "resource_preflight": resource_preflight,
        "points": [],
        "points_fsync": True,
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
        adapter_receipt = _native.configure_native_adjoint(
            _native.ClientapiAdjointStudyBackend(model), support, dict(spec["optimizer"])
        )
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        baselines = _fd._baseline_values(support, spec["selected_variables"])
        reference_scale = min(baselines.values())
        physical_step = reference_scale * spec["direction_relative_step"]
        for label, sign in (("plus", 1.0), ("minus", -1.0)):
            values = {
                variable: baselines[variable] + sign * physical_step * component
                for variable, component in zip(
                    spec["selected_variables"], spec["direction"], strict=True
                )
            }
            row = _fd._forward_point(client, spec, values)
            row["direction_label"] = label
            _fd._append(spec["points"], row)
            receipt["points"].append(row)
        plus, minus = receipt["points"]
        observed = (plus["objective"] - minus["objective"]) / (2.0 * physical_step)
        native_vector = [spec["native_gradients"][item] for item in spec["selected_variables"]]
        predicted = sum(
            gradient * component
            for gradient, component in zip(native_vector, spec["direction"], strict=True)
        )
        relative_error = abs(predicted - observed) / max(abs(predicted), abs(observed), 1.0)
        sign_agreement = (
            observed == 0.0
            or predicted == 0.0
            or math.copysign(1.0, observed) == math.copysign(1.0, predicted)
        )
        receipt.update(
            {
                "physical_step_m": physical_step,
                "native_prediction_1_per_m": predicted,
                "observed_1_per_m": observed,
                "absolute_error_1_per_m": abs(predicted - observed),
                "relative_error": relative_error,
                "sign_agreement": sign_agreement,
                "checks": {
                    "relative_error_below_0.1": relative_error <= 0.1,
                    "sign_agreement": sign_agreement,
                },
                "source_unchanged": _sha(spec["source"]) == source_before,
            }
        )
        receipt["success"] = all(receipt["checks"].values())
        private["adapter_receipt"] = adapter_receipt
    except Exception as exc:
        receipt["error"] = {"code": "directional_gradient_failed", "type": type(exc).__name__}
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = {"client_clear": True, "source_unchanged": _sha(spec["source"]) == source_before}
        if client is not None:
            try:
                client.clear()
            except Exception as exc:
                cleanup["client_clear"] = False
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
    _fd.atomic_write_json(spec["receipt"], receipt)
    _fd.atomic_write_json(spec["private_receipt"], private)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
