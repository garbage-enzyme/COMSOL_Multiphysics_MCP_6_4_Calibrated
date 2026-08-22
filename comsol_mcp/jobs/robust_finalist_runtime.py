"""Licensed finalist remesh, convergence, off-design, and fidelity execution."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.durable import atomic_write_json, domain_sha256_v2
from comsol_mcp.research.external_validation import normalize_external_validation_receipt
from comsol_mcp.research.robust_condition_controls import normalize_robust_condition_controls
from comsol_mcp.research.robust_conditions import normalize_optimization_condition_table
from comsol_mcp.research.robust_finalist_evidence import assess_robust_finalist_validation
from comsol_mcp.research.robust_objectives import evaluate_robust_absolute_contrast

from .robust_shape_native_runtime import execute_lin2025_conditions


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_spec(
    spec: Mapping[str, Any],
    values: list[float],
    *,
    mesh_reference_value: str,
    condition_table: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = copy.deepcopy(dict(spec))
    candidate["initial_values"] = list(values)
    controls = copy.deepcopy(
        candidate["adapter_configuration"]["configuration"]["condition_controls"]
    )
    controls.pop("controls_fingerprint", None)
    controls["mesh_reference_value"] = mesh_reference_value
    candidate["adapter_configuration"]["configuration"]["condition_controls"] = (
        normalize_robust_condition_controls(controls)
    )
    if condition_table is not None:
        candidate["condition_table"] = normalize_optimization_condition_table(condition_table)
    candidate["spec_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.robust_finalist_candidate_spec",
        {
            "base_spec_fingerprint": spec["spec_fingerprint"],
            "initial_values": values,
            "mesh_reference_value": mesh_reference_value,
            "condition_table_fingerprint": candidate["condition_table"][
                "condition_table_fingerprint"
            ],
        },
    )
    return candidate


def _shape_evidence(result: Mapping[str, Any], directory: Path) -> dict[str, Any]:
    application = result.get("shape_application")
    if not isinstance(application, dict):
        raise ValueError("finalist fresh remesh did not produce shape application evidence")
    shape_path = directory / "shape-application.json"
    if not shape_path.is_file():
        raise ValueError("finalist fresh remesh shape receipt is missing")
    if application.get("receipt_fingerprint") != domain_sha256_v2(
        "comsol_mcp.robust_shape_application",
        {key: value for key, value in application.items() if key != "receipt_fingerprint"},
    ):
        raise ValueError("finalist shape application fingerprint is invalid")
    solved = application.get("solved_shape")
    if (
        not isinstance(solved, list)
        or len(solved) != 2
        or any(not isinstance(item, dict) or item.get("matches") is not True for item in solved)
    ):
        raise ValueError("finalist solved-shape readback is incomplete")
    return application


def _mesh_sha256(application: Mapping[str, Any], controls: Mapping[str, Any]) -> str:
    digest = domain_sha256_v2(
        "comsol_mcp.robust_finalist_mesh_identity",
        {
            "shape_application_fingerprint": application["receipt_fingerprint"],
            "mesh_elements": application["mesh_elements"],
            "minimum_mesh_quality": application["minimum_mesh_quality"],
            "mesh_reference": controls.get("mesh_reference_value"),
        },
    )
    if not isinstance(digest, str):
        raise ValueError("robust finalist mesh identity digest is not a string")
    return digest


def _ordered_observations_by_condition_id(
    observations: list[dict[str, Any]],
    condition_table: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    expected = [
        row["condition_id"]
        for row in condition_table["conditions"]
        if row["active"] and row["objective_role"] == "objective"
    ]
    observed = [row.get("condition_id") for row in observations]
    if observed != expected:
        raise ValueError(f"{label} condition observations are missing, duplicated, or reordered")
    return {str(row["condition_id"]): row for row in observations}


def _branch_evidence(
    baseline_objective: Mapping[str, Any],
    finer_objective: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    if policy["mode"] == "not_applicable":
        return {
            "mode": "not_applicable",
            "observable_id": None,
            "baseline_branch_id": None,
            "finer_branch_id": None,
            "baseline_mode_order": None,
            "finer_mode_order": None,
            "ambiguous": False,
            "disappeared": False,
            "evidence_sha256": None,
        }

    def winner(objective: Mapping[str, Any]) -> Mapping[str, Any]:
        pairs = objective["pairs"]
        minimum = min(pair["smooth_absolute_contrast"] for pair in pairs)
        winners = [pair for pair in pairs if pair["smooth_absolute_contrast"] == minimum]
        if len(winners) != 1:
            raise ValueError("finalist branch is ambiguous at the objective minimum")
        winner = winners[0]
        if not isinstance(winner, dict):
            raise ValueError("finalist branch winner is not an object")
        return winner

    baseline_winner = winner(baseline_objective)
    finer_winner = winner(finer_objective)
    finer_pair_ids = {str(pair["pair_id"]) for pair in finer_objective["pairs"]}
    body = {
        "mode": "required",
        "observable_id": policy["observable_id"],
        "baseline_branch_id": baseline_winner["pair_id"],
        "finer_branch_id": finer_winner["pair_id"],
        "baseline_mode_order": int(baseline_winner["pair_id"].split("-")[-1]),
        "finer_mode_order": int(finer_winner["pair_id"].split("-")[-1]),
        "ambiguous": False,
        "disappeared": str(baseline_winner["pair_id"]) not in finer_pair_ids,
    }
    return {**body, "evidence_sha256": domain_sha256_v2("comsol_mcp.robust_finalist_branch", body)}


def _off_design_table(
    condition_table: Mapping[str, Any], policy: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, tuple[str, str, float]]]:
    rows: list[dict[str, Any]] = []
    mapping: dict[str, tuple[str, str, float]] = {}
    active = [
        row
        for row in condition_table["conditions"]
        if row["active"] and row["objective_role"] == "objective"
    ]
    order = 0
    for base in active:
        for axis, offsets in (
            ("wavelength_relative", policy["wavelength_relative_offsets"]),
            ("angle_deg", policy["angle_offsets_deg"]),
        ):
            for offset in offsets:
                condition = copy.deepcopy(base)
                condition_id = f"finalist-{order:04d}"
                if axis == "wavelength_relative":
                    condition["wavelength_m"] = base["wavelength_m"] * (1.0 + offset)
                else:
                    condition["incidence_elevation_deg"] = base["incidence_elevation_deg"] + offset
                condition.update({"condition_id": condition_id, "order": order})
                rows.append(condition)
                mapping[condition_id] = (base["condition_id"], axis, offset)
                order += 1
    table = {
        "schema_name": condition_table["schema_name"],
        "schema_version": condition_table["schema_version"],
        "table_id": f"{condition_table['table_id']}-finalist-off-design",
        "material_states": copy.deepcopy(condition_table["material_states"]),
        "conditions": rows,
        "completeness": {
            "mode": "explicit_sparse",
            "sparse_justification": (
                "caller-declared finalist validation-only off-design coordinates"
            ),
        },
    }
    return normalize_optimization_condition_table(table), mapping


def _manufacturability(spec: Mapping[str, Any], application: Mapping[str, Any]) -> dict[str, Any]:
    fixture = spec["adapter_configuration"]["configuration"]["fixture"]
    geometry = fixture["geometry"]
    radii = [float(item["observed_radius_m"]) for item in application["solved_shape"]]
    gap = float(geometry["period_um"]) * 1e-6 - 2.0 * max(radii)
    thickness = float(geometry["pedot_height_um"]) * 1e-6
    support = spec["adapter_configuration"]["configuration"]["shape_support"]
    shape_receipt = application["shape_controls"]
    shape_controls = shape_receipt["controls"]["deformed_geometry"]
    selections_preserved = all(
        shape_controls[name] == support[name]
        for name in ("free_domains", "fixed_boundaries", "pedot_boundaries")
    )
    topology_preserved = bool(
        shape_receipt["shape_support_fingerprint"] == support["support_fingerprint"]
        and selections_preserved
        and shape_controls["height_preserved"] is True
        and shape_controls["center_preserved"] is True
        and support["domain_count"] >= max(shape_controls["free_domains"])
        and support["boundary_count"]
        >= max(shape_controls["fixed_boundaries"] + shape_controls["pedot_boundaries"])
    )
    return {
        "shape_policy_fingerprint": spec["shape_policy"]["policy_fingerprint"],
        "minimum_gap_m": gap,
        "minimum_thickness_m": thickness,
        "minimum_radius_m": min(radii),
        "topology_preserved": topology_preserved,
        "selections_preserved": selections_preserved,
        "positive_dimensions": all(value > 0.0 for value in radii),
        "self_intersection_absent": bool(application["minimum_mesh_quality"] > 0.0),
        "evidence_sha256": application["receipt_fingerprint"],
    }


def run_licensed_finalist(
    spec: Mapping[str, Any],
    directory: Path,
    *,
    attempt: int,
    candidate_values: list[float],
    candidate_fingerprint: str,
    accepted_observations: list[dict[str, Any]],
    accepted_objective_value: float,
    optimizer_execution_fingerprint: str,
    client_factory: Callable[..., Any],
    java_environment_reader: Callable[[str], str | None],
    cancel_requested: Callable[[], bool],
) -> dict[str, Any]:
    """Run all caller-declared finalist checks on fresh derived model copies."""
    policy = spec["finalist_validation_policy"]
    if policy["schema_version"] != "1.1.0":
        raise ValueError("licensed finalist runtime requires policy 1.1.0")
    mesh_policy = policy["mesh_convergence"]
    directory.mkdir(parents=True, exist_ok=True)

    def execute(
        label: str,
        candidate: Mapping[str, Any],
        *,
        aggregate_objective: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        target = directory / label
        target.mkdir(parents=True, exist_ok=True)
        result = execute_lin2025_conditions(
            candidate,
            target,
            attempt=attempt,
            client_factory=client_factory,
            java_environment_reader=java_environment_reader,
            cancel_requested=cancel_requested,
            include_gradients=False,
        )
        application = _shape_evidence(result, target)
        objective = None
        if aggregate_objective:
            objective = evaluate_robust_absolute_contrast(
                candidate["objective"],
                candidate["condition_table"],
                result["observations"],
            )
        return result, {"application": application, "objective": objective, "directory": target}

    baseline_spec = _candidate_spec(
        spec,
        candidate_values,
        mesh_reference_value=mesh_policy["baseline_mesh_reference_value"],
    )
    baseline_result, baseline = execute("baseline", baseline_spec)
    finer_spec = _candidate_spec(
        spec,
        candidate_values,
        mesh_reference_value=mesh_policy["finer_mesh_reference_value"],
    )
    finer_result, finer = execute("finer", finer_spec)

    off_table, off_mapping = _off_design_table(spec["condition_table"], policy["off_design"])
    off_spec = _candidate_spec(
        spec,
        candidate_values,
        mesh_reference_value=mesh_policy["baseline_mesh_reference_value"],
        condition_table=off_table,
    )
    off_result, _ = execute("off-design", off_spec, aggregate_objective=False)
    observed_validation_solves = (
        len(baseline_result["observations"])
        + len(finer_result["observations"])
        + len(off_result["observations"])
    )
    if observed_validation_solves != spec["finalist_validation_solve_count"]:
        raise ValueError("finalist validation solve count differs from the manifest bound")
    off_rows = []
    for observation in off_result["observations"]:
        base_id, axis, offset = off_mapping[observation["condition_id"]]
        off_rows.append(
            {
                "base_condition_id": base_id,
                "axis": axis,
                "offset": offset,
                "status": "measured",
                "objective_value": observation["value"],
                "evidence_sha256": observation["evidence_sha256"],
            }
        )

    controls = baseline_spec["adapter_configuration"]["configuration"]["condition_controls"]
    baseline_app = baseline["application"]
    finer_app = finer["application"]
    baseline_model = _file_sha256(baseline["directory"] / "robust-working.mph")
    finer_model = _file_sha256(finer["directory"] / "robust-working.mph")
    baseline_mesh = _mesh_sha256(baseline_app, controls)
    finer_mesh = _mesh_sha256(
        finer_app,
        finer_spec["adapter_configuration"]["configuration"]["condition_controls"],
    )
    baseline_objective = baseline["objective"]
    finer_objective = finer["objective"]
    accepted_by_id = _ordered_observations_by_condition_id(
        accepted_observations,
        spec["condition_table"],
        label="accepted optimizer",
    )
    baseline_by_id = _ordered_observations_by_condition_id(
        baseline_result["observations"],
        spec["condition_table"],
        label="independent baseline",
    )
    external_deltas = {
        condition_id: abs(accepted_by_id[condition_id]["value"] - observation["value"])
        for condition_id, observation in baseline_by_id.items()
    }
    maximum_delta = max(external_deltas.values())
    external_body = {
        "schema_name": "comsol_mcp.external_fidelity_validation_receipt",
        "schema_version": "1.0.0",
        "validation_id": "lin2025-finalist-independent-comsol",
        "backend": {
            "kind": "independent_comsol",
            "provider": "COMSOL",
            "version": str(spec["version"]),
            "execution_location": "local",
            "license_authority": "caller_authorized",
        },
        "fallback": {
            "mode": "primary",
            "prior_backend": None,
            "prior_receipt_sha256": None,
            "authorization_sha256": None,
        },
        "environment_identity_sha256": domain_sha256_v2(
            "comsol_mcp.robust_finalist_environment",
            {"version": spec["version"], "cores": spec["cores"]},
        ),
        "geometry_mapping_sha256": baseline_app["shape_controls"]["shape_support_fingerprint"],
        "material_mapping_sha256": spec["material_tensor_binding"]["binding_fingerprint"],
        "excitation_mapping_sha256": spec["condition_table"]["condition_table_fingerprint"],
        "known_non_equivalences": [],
        "sampling_sha256": spec["condition_table"]["condition_table_fingerprint"],
        "discretization_sha256": baseline_mesh,
        "convergence_sha256": domain_sha256_v2(
            "comsol_mcp.robust_finalist_convergence",
            {
                "baseline": baseline_objective["receipt_fingerprint"],
                "finer": finer_objective["receipt_fingerprint"],
            },
        ),
        "raw_artifact_sha256": [baseline_model, finer_model, baseline_app["receipt_fingerprint"]],
        "comparison_metrics": {
            "maximum_absolute_condition_delta": maximum_delta,
            "baseline_objective": baseline_objective["smooth_worst_case_absolute_contrast"],
            "accepted_objective": accepted_objective_value,
        },
        "comparison_tolerances": {
            "maximum_absolute_condition_delta": policy["external_fidelity"][
                "maximum_absolute_condition_delta"
            ]
        },
        "disposition": "validated"
        if maximum_delta <= policy["external_fidelity"]["maximum_absolute_condition_delta"]
        else "disagreed",
        "retention_disposition": "hash_bound_local_only",
    }
    external = normalize_external_validation_receipt(external_body)
    atomic_write_json(directory / "external-validation.json", external)
    evidence = {
        "candidate_fingerprint": candidate_fingerprint,
        "optimizer_execution_fingerprint": optimizer_execution_fingerprint,
        "manufacturability": _manufacturability(spec, baseline_app),
        "fresh_remesh": {
            "candidate_fingerprint": candidate_fingerprint,
            "explicit_rebuild": True,
            "optimizer_state_reused": False,
            "model_sha256": baseline_model,
            "mesh_sha256": baseline_mesh,
            "element_count": baseline_app["mesh_elements"],
            "minimum_element_quality": baseline_app["minimum_mesh_quality"],
            "quality_measure": mesh_policy["quality_measure"],
            "objective_evidence_sha256": baseline_objective["receipt_fingerprint"],
            "evidence_sha256": baseline_app["receipt_fingerprint"],
        },
        "mesh_convergence": {
            "levels": [
                {
                    "level_id": mesh_policy["baseline_level_id"],
                    "candidate_fingerprint": candidate_fingerprint,
                    "model_sha256": baseline_model,
                    "mesh_sha256": baseline_mesh,
                    "element_count": baseline_app["mesh_elements"],
                    "minimum_element_quality": baseline_app["minimum_mesh_quality"],
                    "quality_measure": mesh_policy["quality_measure"],
                    "objective_configuration_fingerprint": spec["objective"][
                        "objective_fingerprint"
                    ],
                    "objective_value": baseline_objective["smooth_worst_case_absolute_contrast"],
                    "objective_evidence_sha256": baseline_objective["receipt_fingerprint"],
                },
                {
                    "level_id": mesh_policy["finer_level_id"],
                    "candidate_fingerprint": candidate_fingerprint,
                    "model_sha256": finer_model,
                    "mesh_sha256": finer_mesh,
                    "element_count": finer_app["mesh_elements"],
                    "minimum_element_quality": finer_app["minimum_mesh_quality"],
                    "quality_measure": mesh_policy["quality_measure"],
                    "objective_configuration_fingerprint": spec["objective"][
                        "objective_fingerprint"
                    ],
                    "objective_value": finer_objective["smooth_worst_case_absolute_contrast"],
                    "objective_evidence_sha256": finer_objective["receipt_fingerprint"],
                },
            ],
            "evidence_sha256": domain_sha256_v2(
                "comsol_mcp.robust_finalist_mesh_convergence",
                {
                    "baseline": baseline_mesh,
                    "finer": finer_mesh,
                },
            ),
        },
        "branch_guard": _branch_evidence(
            baseline_objective, finer_objective, policy["branch_guard"]
        ),
        "off_design_rows": off_rows,
        "external_validation_receipt": external,
    }
    receipt = assess_robust_finalist_validation(
        policy, spec["condition_table"], spec["shape_policy"], evidence
    )
    atomic_write_json(directory / "finalist-validation.json", receipt)
    atomic_write_json(
        directory / "finalist-summary.json",
        {
            "candidate_fingerprint": candidate_fingerprint,
            "baseline_objective": baseline_objective["smooth_worst_case_absolute_contrast"],
            "finer_objective": finer_objective["smooth_worst_case_absolute_contrast"],
            "off_design_row_count": len(off_rows),
            "external_maximum_absolute_condition_delta": maximum_delta,
            "external_validation_receipt_fingerprint": external["receipt_fingerprint"],
            "receipt_fingerprint": receipt["receipt_fingerprint"],
        },
    )
    return receipt


__all__ = ["run_licensed_finalist"]
