"""Frozen solver-free benchmarks for bounded research-campaign development."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from comsol_mcp.durable import domain_sha256_v2, validate_finite_json

BENCHMARK_SUITE_SCHEMA = "comsol_mcp.synthetic_research_benchmark_suite"
BENCHMARK_SUITE_VERSION = "1.0.0"
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


__all__ = [
    "BENCHMARK_SUITE_SCHEMA",
    "BENCHMARK_SUITE_VERSION",
    "fake_mim_response",
    "frozen_benchmark_suite",
]
