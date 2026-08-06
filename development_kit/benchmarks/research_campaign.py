"""Frozen solver-free benchmarks for bounded research-campaign development."""

from __future__ import annotations

from copy import deepcopy
from statistics import fmean, median
from typing import Any, Mapping

from comsol_mcp.durable import domain_sha256_v2, validate_finite_json
from comsol_mcp.research.optimizers import (
    DeterministicGridOptimizer,
    DeterministicLatinHypercubeOptimizer,
    DeterministicRandomOptimizer,
)

BENCHMARK_SUITE_SCHEMA = "comsol_mcp.synthetic_research_benchmark_suite"
BENCHMARK_SUITE_VERSION = "1.0.0"
OPTIMIZER_BENCHMARK_SCHEMA = "comsol_mcp.synthetic_optimizer_benchmark_receipt"
OPTIMIZER_BENCHMARK_VERSION = "1.0.0"
OPTIMIZER_BENCHMARK_SEEDS = (17001, 17002, 17003, 17004, 17005, 17006, 17007, 17008)
_MATERIAL_STATES = {"gold_reference", "gold_low_loss"}


def fake_mim_response(candidate: Mapping[str, object]) -> dict[str, float | str]:
    """Evaluate a deterministic metasurface-like response without a solver."""
    if set(candidate) != {"patch_length_x", "patch_length_y", "material_state"}:
        raise ValueError("candidate fields must match the frozen fake MIM space")
    x = candidate["patch_length_x"]
    y = candidate["patch_length_y"]
    material = candidate["material_state"]
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError("patch_length_x must be numeric")
    if isinstance(y, bool) or not isinstance(y, (int, float)):
        raise ValueError("patch_length_y must be numeric")
    if material not in _MATERIAL_STATES:
        raise ValueError("material_state is outside the frozen material ledger")
    x_value = float(x)
    y_value = float(y)
    if not 75.0 <= x_value <= 125.0 or not 60.0 <= y_value <= 100.0:
        raise ValueError("candidate is outside the frozen +/-25 percent design space")
    dx = x_value / 100.0 - 1.0
    dy = y_value / 80.0 - 1.0
    material_peak = 12.0 if material == "gold_low_loss" else 0.0
    material_q = 4.0 if material == "gold_low_loss" else 0.0
    peak_nm = 1550.0 + 240.0 * dx + 120.0 * dy + 80.0 * dx * dy + material_peak
    q_factor = 24.0 - 90.0 * (dx - 0.08) ** 2 - 70.0 * (dy + 0.06) ** 2 + material_q
    return {
        "schema_name": "comsol_mcp.synthetic_mim_response",
        "peak_wavelength_nm": peak_nm,
        "q_factor": q_factor,
        "power_closure": 1.0,
        "material_state": str(material),
    }


def _benchmark_specs() -> list[dict[str, Any]]:
    hidden = {
        "patch_length_x": 108.0,
        "patch_length_y": 75.2,
        "material_state": "gold_reference",
    }
    hidden_response = fake_mim_response(hidden)
    return [
        {
            "benchmark_id": "feasible_hidden_target_v1",
            "kind": "feasible_hidden_target",
            "seed": 17001,
            "evaluation_budget": 32,
            "hidden_candidate": hidden,
            "target": {
                "peak_wavelength_nm": hidden_response["peak_wavelength_nm"],
                "q_factor": hidden_response["q_factor"],
            },
            "success_tolerances": {"peak_wavelength_nm": 5.0, "q_factor": 1.0},
        },
        {
            "benchmark_id": "impossible_target_v1",
            "kind": "impossible_target",
            "seed": 17002,
            "evaluation_budget": 32,
            "hidden_candidate": None,
            "target": {"peak_wavelength_nm": 2000.0, "q_factor": 80.0},
            "success_tolerances": {"peak_wavelength_nm": 5.0, "q_factor": 1.0},
        },
        {
            "benchmark_id": "multi_objective_v1",
            "kind": "multi_objective",
            "seed": 17003,
            "evaluation_budget": 32,
            "hidden_candidate": None,
            "target": {"peak_wavelength_nm": 1550.0, "q_factor": "maximize"},
            "success_tolerances": None,
        },
        {
            "benchmark_id": "material_choice_v1",
            "kind": "material_choice",
            "seed": 17004,
            "evaluation_budget": 32,
            "hidden_candidate": None,
            "target": {"peak_wavelength_nm": 1570.0, "q_factor": "maximize"},
            "allowed_material_states": sorted(_MATERIAL_STATES),
            "success_tolerances": None,
        },
        {
            "benchmark_id": "crash_resume_v1",
            "kind": "crash_resume",
            "seed": 17005,
            "evaluation_budget": 32,
            "hidden_candidate": hidden,
            "target": {
                "peak_wavelength_nm": hidden_response["peak_wavelength_nm"],
                "q_factor": hidden_response["q_factor"],
            },
            "success_tolerances": {"peak_wavelength_nm": 5.0, "q_factor": 1.0},
            "interrupt_after_completed_evaluations": 3,
        },
    ]


def frozen_benchmark_suite() -> dict[str, Any]:
    """Return a defensive copy of the canonical alpha7 solver-free benchmark suite."""
    body = {
        "schema_name": BENCHMARK_SUITE_SCHEMA,
        "schema_version": BENCHMARK_SUITE_VERSION,
        "structure_family": "periodic_mim_patch_v1",
        "mutable_variables": ["patch_length_x", "patch_length_y"],
        "fixed_inputs": [
            "incidence",
            "layer_stack",
            "materials_except_declared_state",
            "period",
            "topology",
        ],
        "bounds": {
            "patch_length_x": {"baseline": 100.0, "lower": 75.0, "upper": 125.0},
            "patch_length_y": {"baseline": 80.0, "lower": 60.0, "upper": 100.0},
        },
        "benchmarks": _benchmark_specs(),
    }
    validate_finite_json(body)
    body["suite_fingerprint"] = domain_sha256_v2(BENCHMARK_SUITE_SCHEMA, body)
    return deepcopy(body)


def optimizer_benchmark_design_space() -> dict[str, Any]:
    """Return the frozen two-variable design space used by the S3 baseline gate."""
    variables = [
        {
            "variable_id": "patch_length_x",
            "kind": "continuous",
            "unit": "nm",
            "baseline": 100.0,
            "lower": 75.0,
            "upper": 125.0,
            "allowed_values": None,
            "dependency_class": "geometry",
            "adapter_path": "geom.patch_length_x",
        },
        {
            "variable_id": "patch_length_y",
            "kind": "continuous",
            "unit": "nm",
            "baseline": 80.0,
            "lower": 60.0,
            "upper": 100.0,
            "allowed_values": None,
            "dependency_class": "geometry",
            "adapter_path": "geom.patch_length_y",
        },
    ]
    return {
        "schema_name": "comsol_mcp.design_space",
        "schema_version": "1.0.0",
        "space_id": "synthetic-mim-optimizer-benchmark-v1",
        "structure_family": "periodic_mim_patch_v1",
        "template_identity": {
            "kind": "solver_free_fixture",
            "suite_fingerprint": frozen_benchmark_suite()["suite_fingerprint"],
        },
        "variables": variables,
        "constraints": [],
        "canonicalization": {"float_digits": 12, "relative_tolerance": 1e-12},
        "adapter_mappings": [
            {
                "variable_id": item["variable_id"],
                "adapter_path": item["adapter_path"],
                "unit": item["unit"],
            }
            for item in variables
        ],
    }


def _baseline_score(values: Mapping[str, object], spec: Mapping[str, Any]) -> dict[str, Any]:
    response = fake_mim_response({**values, "material_state": "gold_reference"})
    target = spec["target"]
    tolerances = spec["success_tolerances"]
    peak_loss = (
        abs(response["peak_wavelength_nm"] - target["peak_wavelength_nm"])
        / tolerances["peak_wavelength_nm"]
    )
    q_loss = abs(response["q_factor"] - target["q_factor"]) / tolerances["q_factor"]
    return {
        "peak_loss": peak_loss,
        "q_loss": q_loss,
        "total_loss": peak_loss + q_loss,
        "success": peak_loss <= 1.0 and q_loss <= 1.0,
        "response": response,
    }


def _evaluate_baseline(name: str, optimizer: Any, spec: Mapping[str, Any], seed: int) -> dict:
    rows = []
    while optimizer.state()["remaining_proposals"] and len(rows) < spec["evaluation_budget"]:
        proposal = optimizer.ask()
        rows.append({"proposal": proposal, "score": _baseline_score(proposal["values"], spec)})
    best = min(
        rows,
        key=lambda item: (item["score"]["total_loss"], item["proposal"]["proposal_fingerprint"]),
    )
    return {
        "backend": name,
        "seed": seed,
        "backend_identity": optimizer.backend_identity,
        "evaluation_count": len(rows),
        "best_proposal": best["proposal"],
        "best_score": best["score"],
    }


def frozen_optimizer_baseline_benchmark() -> dict[str, Any]:
    """Compare grid, random, and LHS baselines over the frozen multi-seed matrix."""
    suite = frozen_benchmark_suite()
    spec = next(item for item in suite["benchmarks"] if item["kind"] == "feasible_hidden_target")
    space = optimizer_benchmark_design_space()
    runs = []
    for seed in OPTIMIZER_BENCHMARK_SEEDS:
        runs.extend(
            [
                _evaluate_baseline(
                    "deterministic_grid", DeterministicGridOptimizer(space, levels=5), spec, seed
                ),
                _evaluate_baseline(
                    "deterministic_random",
                    DeterministicRandomOptimizer(space, seed=seed),
                    spec,
                    seed,
                ),
                _evaluate_baseline(
                    "deterministic_latin_hypercube",
                    DeterministicLatinHypercubeOptimizer(
                        space, seed=seed, sample_count=spec["evaluation_budget"]
                    ),
                    spec,
                    seed,
                ),
            ]
        )
    aggregates = {}
    for backend in sorted({run["backend"] for run in runs}):
        selected = [run for run in runs if run["backend"] == backend]
        losses = [run["best_score"]["total_loss"] for run in selected]
        aggregates[backend] = {
            "run_count": len(selected),
            "total_evaluations": sum(run["evaluation_count"] for run in selected),
            "success_count": sum(run["best_score"]["success"] for run in selected),
            "mean_best_total_loss": fmean(losses),
            "median_best_total_loss": median(losses),
            "minimum_best_total_loss": min(losses),
            "maximum_best_total_loss": max(losses),
        }
    body = {
        "schema_name": OPTIMIZER_BENCHMARK_SCHEMA,
        "schema_version": OPTIMIZER_BENCHMARK_VERSION,
        "suite_fingerprint": suite["suite_fingerprint"],
        "benchmark_id": spec["benchmark_id"],
        "seeds": list(OPTIMIZER_BENCHMARK_SEEDS),
        "evaluation_budget": spec["evaluation_budget"],
        "fixed_material_state": "gold_reference",
        "runs": runs,
        "aggregates": aggregates,
    }
    validate_finite_json(body)
    body["benchmark_fingerprint"] = domain_sha256_v2(OPTIMIZER_BENCHMARK_SCHEMA, body)
    return deepcopy(body)


__all__ = [
    "BENCHMARK_SUITE_SCHEMA",
    "BENCHMARK_SUITE_VERSION",
    "OPTIMIZER_BENCHMARK_SCHEMA",
    "OPTIMIZER_BENCHMARK_SEEDS",
    "OPTIMIZER_BENCHMARK_VERSION",
    "fake_mim_response",
    "frozen_benchmark_suite",
    "frozen_optimizer_baseline_benchmark",
    "optimizer_benchmark_design_space",
]
