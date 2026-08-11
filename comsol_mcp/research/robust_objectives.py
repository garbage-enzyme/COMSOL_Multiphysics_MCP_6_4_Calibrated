"""Differentiable robust multi-state objective contracts and scalarization."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _bounded_json, _finite, _identifier, _object, _sha256
from .robust_conditions import normalize_optimization_condition_table

ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_NAME = "comsol_mcp.robust_objective_configuration"
ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_VERSION = "1.0.0"
ROBUST_OBJECTIVE_RECEIPT_SCHEMA_NAME = "comsol_mcp.robust_objective_receipt"
ROBUST_OBJECTIVE_RECEIPT_SCHEMA_VERSION = "1.0.0"

_OBJECTIVE_KIND = "smooth_worst_case_absolute_contrast"
_MEASURED_DISPOSITION = "measured"


def normalize_robust_objective_configuration(value: object) -> dict[str, Any]:
    """Normalize the symmetric smooth absolute-contrast objective policy."""
    bounded = _bounded_json(value, "robust objective configuration", 64 * 1024)
    supplied = None
    if isinstance(bounded, dict) and "objective_fingerprint" in bounded:
        supplied = bounded.pop("objective_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "objective_id",
            "kind",
            "direction",
            "state_ids",
            "observable_id",
            "absolute_smoothing_epsilon",
            "worst_case_temperature",
        },
        "robust objective configuration",
    )
    if (
        raw["schema_name"] != ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_NAME
        or raw["schema_version"] != ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_VERSION
    ):
        raise ValueError("robust objective configuration schema identity is unsupported")
    if raw["kind"] != _OBJECTIVE_KIND or raw["direction"] != "maximize":
        raise ValueError("robust objective must maximize smooth worst-case absolute contrast")
    state_ids = raw["state_ids"]
    if not isinstance(state_ids, list) or len(state_ids) != 2:
        raise ValueError("robust objective state_ids must contain exactly two states")
    normalized_states = [_identifier(item, "state_ids") for item in state_ids]
    if len(set(normalized_states)) != 2:
        raise ValueError("robust objective state_ids must be distinct")
    body = {
        "schema_name": ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_NAME,
        "schema_version": ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_VERSION,
        "objective_id": _identifier(raw["objective_id"], "objective_id"),
        "kind": _OBJECTIVE_KIND,
        "direction": "maximize",
        "state_ids": normalized_states,
        "observable_id": _identifier(raw["observable_id"], "observable_id"),
        "absolute_smoothing_epsilon": _finite(
            raw["absolute_smoothing_epsilon"], "absolute_smoothing_epsilon", positive=True
        ),
        "worst_case_temperature": _finite(
            raw["worst_case_temperature"], "worst_case_temperature", positive=True
        ),
    }
    body["objective_fingerprint"] = domain_sha256_v2(
        ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_NAME, body
    )
    if supplied is not None and supplied != body["objective_fingerprint"]:
        raise ValueError("robust objective configuration fingerprint is invalid")
    return body


def _normalize_observations(value: object) -> dict[str, dict[str, Any]]:
    bounded = _bounded_json(value, "robust objective observations", 2 * 1024 * 1024)
    if not isinstance(bounded, list) or not bounded:
        raise ValueError("robust objective observations must be a bounded nonempty list")
    by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(bounded):
        name = f"observations[{index}]"
        raw = _object(
            item,
            {"condition_id", "observable_id", "value", "evidence_sha256", "disposition"},
            name,
        )
        condition_id = _identifier(raw["condition_id"], f"{name}.condition_id")
        if condition_id in by_id:
            raise ValueError("robust objective observation condition IDs must be unique")
        if raw["disposition"] != _MEASURED_DISPOSITION:
            raise ValueError("robust objective cannot aggregate an unmeasured condition")
        by_id[condition_id] = {
            "condition_id": condition_id,
            "observable_id": _identifier(raw["observable_id"], f"{name}.observable_id"),
            "value": _finite(raw["value"], f"{name}.value"),
            "evidence_sha256": _sha256(raw["evidence_sha256"], f"{name}.evidence_sha256"),
            "disposition": _MEASURED_DISPOSITION,
        }
    return by_id


def _pair_key(row: Mapping[str, Any]) -> tuple[float, float, float, str, str, str]:
    return (
        row["wavelength_m"],
        row["incidence_elevation_deg"],
        row["incidence_azimuth_deg"],
        row["polarization_basis_id"],
        row["excitation_sha256"],
        row["observable_id"],
    )


def evaluate_robust_absolute_contrast(
    configuration: object, condition_table: object, observations: object
) -> dict[str, Any]:
    """Evaluate a symmetric smooth absolute contrast and weighted smooth minimum."""
    objective = normalize_robust_objective_configuration(configuration)
    table = normalize_optimization_condition_table(condition_table)
    observed = _normalize_observations(observations)
    table_states = {item["state_id"] for item in table["material_states"]}
    if not set(objective["state_ids"]).issubset(table_states):
        raise ValueError("robust objective states are not declared by the condition table")
    active = [
        row for row in table["conditions"] if row["active"] and row["objective_role"] == "objective"
    ]
    expected_ids = {row["condition_id"] for row in active}
    if set(observed) != expected_ids:
        raise ValueError("observations must exactly cover every active objective condition")
    if any(row["observable_id"] != objective["observable_id"] for row in active):
        raise ValueError("condition observable differs from the robust objective")

    grouped: dict[tuple[float, float, float, str, str, str], dict[str, dict[str, Any]]] = {}
    for row in active:
        if row["material_state_id"] not in objective["state_ids"]:
            raise ValueError("active objective conditions contain an undeclared contrast state")
        states = grouped.setdefault(_pair_key(row), {})
        states[row["material_state_id"]] = row

    state_a, state_b = objective["state_ids"]
    epsilon = objective["absolute_smoothing_epsilon"]
    pairs = []
    for pair_index, (key, states) in enumerate(sorted(grouped.items())):
        if set(states) != {state_a, state_b}:
            raise ValueError("every robust condition coordinate must contain both contrast states")
        row_a, row_b = states[state_a], states[state_b]
        if row_a["weight"] != row_b["weight"]:
            raise ValueError("paired material-state conditions must use the same weight")
        observation_a = observed[row_a["condition_id"]]
        observation_b = observed[row_b["condition_id"]]
        if (
            observation_a["observable_id"] != objective["observable_id"]
            or observation_b["observable_id"] != objective["observable_id"]
        ):
            raise ValueError("observation observable differs from the robust objective")
        delta = observation_a["value"] - observation_b["value"]
        smooth_absolute = math.sqrt(delta * delta + epsilon * epsilon)
        pairs.append(
            {
                "pair_id": f"pair-{pair_index:04d}",
                "coordinate": {
                    "wavelength_m": key[0],
                    "incidence_elevation_deg": key[1],
                    "incidence_azimuth_deg": key[2],
                    "polarization_basis_id": key[3],
                    "excitation_sha256": key[4],
                },
                "state_condition_ids": {
                    state_a: row_a["condition_id"],
                    state_b: row_b["condition_id"],
                },
                "state_values": {
                    state_a: observation_a["value"],
                    state_b: observation_b["value"],
                },
                "state_evidence_sha256": {
                    state_a: observation_a["evidence_sha256"],
                    state_b: observation_b["evidence_sha256"],
                },
                "signed_delta": delta,
                "exact_absolute_contrast": abs(delta),
                "smooth_absolute_contrast": smooth_absolute,
                "smooth_absolute_derivative": delta / smooth_absolute,
                "weight": row_a["weight"],
            }
        )
    if not pairs:
        raise ValueError("robust objective has no active condition pairs")

    temperature = objective["worst_case_temperature"]
    minimum = min(pair["smooth_absolute_contrast"] for pair in pairs)
    weighted_terms = [
        pair["weight"] * math.exp(-(pair["smooth_absolute_contrast"] - minimum) / temperature)
        for pair in pairs
    ]
    total_weight = sum(pair["weight"] for pair in pairs)
    partition = sum(weighted_terms) / total_weight
    aggregate = minimum - temperature * math.log(partition)
    softmin_weights = [term / sum(weighted_terms) for term in weighted_terms]
    for pair, softmin_weight in zip(pairs, softmin_weights, strict=True):
        pair["smooth_worst_case_weight"] = softmin_weight

    body = {
        "schema_name": ROBUST_OBJECTIVE_RECEIPT_SCHEMA_NAME,
        "schema_version": ROBUST_OBJECTIVE_RECEIPT_SCHEMA_VERSION,
        "objective_fingerprint": objective["objective_fingerprint"],
        "condition_table_fingerprint": table["condition_table_fingerprint"],
        "pair_count": len(pairs),
        "pairs": pairs,
        "minimum_smooth_absolute_contrast": minimum,
        "smooth_worst_case_absolute_contrast": aggregate,
        "worst_case_approximation_offset": aggregate - minimum,
        "complete": True,
    }
    return {
        **body,
        "receipt_fingerprint": domain_sha256_v2(ROBUST_OBJECTIVE_RECEIPT_SCHEMA_NAME, body),
    }


__all__ = [
    "ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_NAME",
    "ROBUST_OBJECTIVE_CONFIGURATION_SCHEMA_VERSION",
    "ROBUST_OBJECTIVE_RECEIPT_SCHEMA_NAME",
    "ROBUST_OBJECTIVE_RECEIPT_SCHEMA_VERSION",
    "evaluate_robust_absolute_contrast",
    "normalize_robust_objective_configuration",
]
