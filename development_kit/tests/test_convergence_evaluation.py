"""Solver-free ordered convergence evidence regression tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from mcp.server.fastmcp import FastMCP

from src.evidence.convergence_evaluation import (
    build_convergence_ladder,
    evaluate_convergence,
    validate_convergence_evaluation,
    validate_convergence_ladder,
)
from src.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
)
from src.tools.convergence_evaluation import register_convergence_evaluation_tools


MATERIAL_SHA256 = "d" * 64
INCIDENCE_SHA256 = "e" * 64


def _spectral_artifacts(index: int, center: float, amplitude: float = 0.9):
    configuration = f"{index + 1:x}" * 64
    wavelengths = [center + offset * 0.05e-6 for offset in range(-3, 4)]
    values = [0.1, 0.3, 0.5, amplitude, 0.5, 0.3, 0.1]
    rows = []
    for row_index, (wavelength, absorption) in enumerate(zip(wavelengths, values)):
        raw = {"level": index, "row": row_index, "wavelength": wavelength}
        rows.append({
            "row_id": f"level-{index}-point-{row_index}",
            "raw_row_sha256": hashlib.sha256(
                json.dumps(raw, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "configuration_sha256": configuration,
            "requested_wavelength_m": wavelength,
            "evaluated_wavelength_m": wavelength,
            "frequency_wavelength_m": wavelength,
            "R": 0.95 - absorption,
            "T": 0.05,
            "A": absorption,
        })
    bundle = build_spectral_point_bundle(
        bundle_id=f"spectrum-level-{index}",
        source_model={
            "relative_identity": f"fixtures/source-{index}.mph",
            "sha256": f"{index + 5:x}" * 64,
        },
        configuration_sha256=configuration,
        parameter_state={"mesh_level": index},
        wavelength_convention={
            "unit": "m",
            "requested_field": "requested_wavelength_m",
            "evaluated_field": "evaluated_wavelength_m",
            "frequency_derived_field": "frequency_wavelength_m",
            "frequency_relation": "c_const/frequency",
        },
        expressions={"R": "R", "T": "T", "A": "1-R-T"},
        rows=rows,
    )
    policy = {
        "response_quantity": "A",
        "candidate_polarity": "maximum",
        "passivity_abs_tolerance": 1e-12,
        "closure_abs_tolerance": 1e-12,
        "wavelength_sync_abs_m": 1e-15,
        "flat_response_abs_tolerance": 1e-12,
        "minimum_point_count": 5,
    }
    measurement = {
        "peak_method": "measured_grid",
        "baseline_rule": "declared_response",
        "baseline_response_value": 0.1,
        "fwhm_definition": "half_prominence",
        "fit_support_points": None,
        "fit_support_sensitivity_points": [],
        "local_polynomial_degree": None,
        "fit_max_evaluations": None,
    }
    decision = build_spectral_analysis_decision(bundle, policy)
    characterization = build_spectral_characterization(bundle, decision, measurement)
    return bundle, decision, characterization


def _level(index: int, center: float, predecessor: str | None):
    bundle, decision, characterization = _spectral_artifacts(index, center)
    return {
        "level_id": f"mesh-{index}",
        "ordinal": index,
        "declared_predecessor_level_id": predecessor,
        "source_model_sha256": bundle["source_model"]["sha256"],
        "configuration_sha256": bundle["configuration_sha256"],
        "mesh_counts": {
            "element_count": 1000 * (index + 1),
            "vertex_count": 500 * (index + 1),
        },
        "material_identity_sha256": MATERIAL_SHA256,
        "incidence_identity_sha256": INCIDENCE_SHA256,
        "spectral_bundle": bundle,
        "analysis_decision": decision,
        "candidate_measurements": characterization,
        "optional_field_metrics": {
            "field_integral": {
                "value": 1.0 + 0.1 * index,
                "unit": "J",
                "evidence_artifact_sha256": f"{index + 9:x}" * 64,
            }
        },
        "fixed_reference_diagnostics": {
            "fixed_wavelength_amplitude": {
                "value": 0.8 + 0.01 * index,
                "unit": "1",
                "evidence_artifact_sha256": f"{index + 12:x}" * 64,
            }
        },
    }


def _levels():
    return [
        _level(0, 5.00e-6, None),
        _level(1, 5.02e-6, "mesh-0"),
        _level(2, 5.025e-6, "mesh-1"),
    ]


def test_ordered_ladder_binds_exact_identities_artifacts_and_own_peak_measurements():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())

    assert validate_convergence_ladder(ladder) == ladder
    assert ladder["level_count"] == 3
    assert [level["ordinal"] for level in ladder["levels"]] == [0, 1, 2]
    assert [level["declared_predecessor_level_id"] for level in ladder["levels"]] == [
        None, "mesh-0", "mesh-1",
    ]
    assert ladder["levels"][2]["measurements"]["peak_wavelength_m"] == 5.025e-6
    assert ladder["levels"][2]["evidence_state"] == "complete_own_peak"
    assert len(ladder["levels"][2]["spectral_artifacts"]["raw_row_sha256s"]) == 7
    assert ladder["levels"][0]["fixed_reference_diagnostics"][
        "fixed_wavelength_amplitude"
    ]["value"] == 0.8


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda levels: levels.__setitem__(1, {**levels[1], "ordinal": 2}), "ordinal"),
        (
            lambda levels: levels.__setitem__(2, {
                **levels[2], "declared_predecessor_level_id": "mesh-0"
            }),
            "adjacency",
        ),
        (
            lambda levels: levels.__setitem__(2, {
                **levels[2], "configuration_sha256": levels[1]["configuration_sha256"]
            }),
            "does not match|duplicate",
        ),
        (
            lambda levels: levels.__setitem__(2, {
                **levels[2], "material_identity_sha256": "f" * 64
            }),
            "material identity",
        ),
        (
            lambda levels: levels.__setitem__(1, {
                **levels[1], "incidence_identity_sha256": "f" * 64
            }),
            "incidence identity",
        ),
    ],
)
def test_reordering_bad_adjacency_duplicates_and_identity_changes_fail_closed(mutation, match):
    levels = _levels()
    mutation(levels)
    with pytest.raises(ValueError, match=match):
        build_convergence_ladder(ladder_id="invalid-ladder", levels=levels)


def test_source_configuration_and_artifact_hash_mismatches_fail_closed():
    for field, replacement, match in (
        ("source_model_sha256", "f" * 64, "source model hash"),
        ("configuration_sha256", "f" * 64, "configuration hash"),
    ):
        levels = _levels()
        levels[1][field] = replacement
        with pytest.raises(ValueError, match=match):
            build_convergence_ladder(ladder_id="hash-mismatch", levels=levels)

    levels = _levels()
    levels[1]["candidate_measurements"] = deepcopy(levels[1]["candidate_measurements"])
    levels[1]["candidate_measurements"]["bundle_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="noncanonical|hash"):
        build_convergence_ladder(ladder_id="artifact-mismatch", levels=levels)


def test_ladder_hash_tampering_and_duplicate_spectral_artifacts_fail_closed():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    tampered = deepcopy(ladder)
    tampered["levels"][2]["mesh_counts"]["element_count"] += 1
    with pytest.raises(ValueError, match="hash"):
        validate_convergence_ladder(tampered)

    levels = _levels()
    for field in ("spectral_bundle", "analysis_decision", "candidate_measurements"):
        levels[2][field] = levels[1][field]
    levels[2]["source_model_sha256"] = levels[1]["source_model_sha256"]
    levels[2]["configuration_sha256"] = levels[1]["configuration_sha256"]
    with pytest.raises(ValueError, match="duplicate"):
        build_convergence_ladder(ladder_id="duplicate-artifacts", levels=levels)


def _metric(
    name: str,
    unit: str,
    *,
    absolute: float | None = None,
    relative: float | None = None,
):
    return {
        "metric": name,
        "unit": unit,
        "absolute_tolerance": absolute,
        "relative_tolerance": relative,
    }


def _policy(**overrides):
    value = {
        "policy_id": "declared-convergence-policy",
        "metrics": [
            _metric("peak_wavelength_m", "m", absolute=10e-9),
            _metric("peak_response_value", "1", absolute=1e-6),
        ],
        "minimum_level_count": 3,
        "governing_pairs": "final_pair",
        "relative_denominator": "previous_abs",
        "declared_cap_reached": False,
    }
    value.update(overrides)
    return value


def test_three_level_final_pair_passes_only_declared_metrics_and_tolerances():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    evaluation = evaluate_convergence(ladder, _policy())

    assert evaluation["scientific_disposition"] == "accepted"
    assert evaluation["governing_pair_indices"] == [1]
    assert evaluation["pair_comparisons"][0]["passed"] is False
    assert evaluation["pair_comparisons"][1]["passed"] is True
    final_peak = evaluation["pair_comparisons"][1]["comparisons"][0]
    assert final_peak["absolute_change"] == pytest.approx(5e-9)
    assert final_peak["absolute_passed"] is True
    assert final_peak["previous_level_sha256"] == ladder["levels"][1]["level_sha256"]
    assert validate_convergence_evaluation(evaluation, ladder=ladder) == evaluation


def test_all_adjacent_policy_fails_when_earlier_pair_misses_the_same_gate():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    evaluation = evaluate_convergence(
        ladder, _policy(governing_pairs="all_adjacent")
    )

    assert evaluation["governing_pair_indices"] == [0, 1]
    assert evaluation["scientific_disposition"] == "residual"
    assert evaluation["reason_code"] == "governing_metric_checks_failed"
    assert evaluation["undeclared_configuration_started"] is False


def test_stable_amplitude_cannot_hide_excessive_own_peak_shift():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    policy = _policy(metrics=[
        _metric("peak_response_value", "1", absolute=1e-6),
        _metric("peak_wavelength_m", "m", absolute=1e-9),
    ])
    evaluation = evaluate_convergence(ladder, policy)
    comparisons = {
        item["metric"]: item
        for item in evaluation["pair_comparisons"][-1]["comparisons"]
    }

    assert comparisons["peak_response_value"]["passed"] is True
    assert comparisons["peak_wavelength_m"]["passed"] is False
    assert evaluation["scientific_disposition"] == "residual"


def test_failed_final_pair_at_explicit_cap_is_unresolved_not_execution_failure():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    evaluation = evaluate_convergence(
        ladder,
        _policy(
            metrics=[_metric("peak_wavelength_m", "m", absolute=1e-9)],
            declared_cap_reached=True,
        ),
    )

    assert evaluation["scientific_disposition"] == "unresolved_at_declared_cap"
    assert evaluation["reason_code"] == "governing_metric_checks_failed_at_declared_cap"
    assert evaluation["undeclared_configuration_started"] is False


def test_absolute_relative_and_optional_field_metrics_use_declared_units():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    evaluation = evaluate_convergence(
        ladder,
        _policy(metrics=[
            _metric("peak_wavelength_m", "m", relative=0.002),
            _metric("field:field_integral", "J", absolute=0.11),
        ]),
    )

    final = evaluation["pair_comparisons"][-1]["comparisons"]
    assert final[0]["relative_change"] == pytest.approx(5e-9 / 5.02e-6)
    assert final[0]["relative_passed"] is True
    assert final[1]["absolute_change"] == pytest.approx(0.1)
    assert final[1]["passed"] is True


def test_missing_metric_or_minimum_levels_returns_invalid_evidence():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    missing = evaluate_convergence(
        ladder,
        _policy(metrics=[_metric("field:not_present", "1", absolute=0.1)]),
    )
    minimum = evaluate_convergence(ladder, _policy(minimum_level_count=4))

    assert missing["scientific_disposition"] == "invalid_evidence"
    assert missing["evidence_issues"] == ["declared_metric_evidence_incomplete"]
    assert minimum["scientific_disposition"] == "invalid_evidence"
    assert minimum["evidence_issues"] == ["minimum_level_count_not_met"]


@pytest.mark.parametrize(
    "policy,match",
    [
        (_policy(metrics=[_metric("peak_wavelength_m", "m")]), "tolerance"),
        (_policy(metrics=[_metric("peak_wavelength_m", "nm", absolute=1.0)]), "unit"),
        (_policy(metrics=[_metric("fixed_reference_amplitude", "1", absolute=0.1)]), "fixed-reference"),
        (_policy(metrics=[_metric("diagnostic:amplitude", "1", absolute=0.1)]), "fixed-reference"),
        (_policy(governing_pairs="best_pair"), "governing_pairs"),
    ],
)
def test_undeclared_ambiguous_and_fixed_reference_rules_fail_closed(policy, match):
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    with pytest.raises(ValueError, match=match):
        evaluate_convergence(ladder, policy)


def test_evaluation_hash_tampering_fails_closed():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    evaluation = evaluate_convergence(ladder, _policy())
    tampered = deepcopy(evaluation)
    tampered["scientific_disposition"] = "residual"

    with pytest.raises(ValueError, match="noncanonical|hash"):
        validate_convergence_evaluation(tampered, ladder=ladder)


def _fitted_level(index: int, center: float, predecessor: str | None):
    configuration = f"{index + 7:x}" * 64
    wavelengths = [5.0e-6 + (point - 10) * 0.02e-6 for point in range(21)]
    rows = []
    for point, wavelength in enumerate(wavelengths):
        coordinate = (wavelength - center) / 0.06e-6
        absorption = 0.1 + 0.8 / (1.0 + coordinate * coordinate)
        rows.append({
            "row_id": f"fit-{index}-point-{point}",
            "raw_row_sha256": hashlib.sha256(f"fit-{index}-{point}".encode()).hexdigest(),
            "configuration_sha256": configuration,
            "requested_wavelength_m": wavelength,
            "evaluated_wavelength_m": wavelength,
            "frequency_wavelength_m": wavelength,
            "R": 0.95 - absorption,
            "T": 0.05,
            "A": absorption,
        })
    bundle = build_spectral_point_bundle(
        bundle_id=f"fitted-spectrum-{index}",
        source_model={
            "relative_identity": f"fixtures/fitted-source-{index}.mph",
            "sha256": ("a" if index == 0 else "b") * 64,
        },
        configuration_sha256=configuration,
        parameter_state={"mesh_level": index},
        wavelength_convention={
            "unit": "m", "requested_field": "requested_wavelength_m",
            "evaluated_field": "evaluated_wavelength_m",
            "frequency_derived_field": "frequency_wavelength_m",
            "frequency_relation": "c_const/frequency",
        },
        expressions={"R": "R", "T": "T", "A": "A"},
        rows=rows,
    )
    decision = build_spectral_analysis_decision(bundle, {
        "response_quantity": "A", "candidate_polarity": "maximum",
        "passivity_abs_tolerance": 1e-12, "closure_abs_tolerance": 1e-12,
        "wavelength_sync_abs_m": 1e-15, "flat_response_abs_tolerance": 1e-12,
        "minimum_point_count": 5,
    })
    characterization = build_spectral_characterization(bundle, decision, {
        "peak_method": "local_polynomial_fit",
        "baseline_rule": "declared_response", "baseline_response_value": 0.1,
        "fwhm_definition": "half_prominence", "fit_support_points": 9,
        "fit_support_sensitivity_points": [5, 7, 9, 11, 13],
        "local_polynomial_degree": 2, "fit_max_evaluations": 20000,
    })
    return {
        "level_id": f"fitted-mesh-{index}", "ordinal": index,
        "declared_predecessor_level_id": predecessor,
        "source_model_sha256": bundle["source_model"]["sha256"],
        "configuration_sha256": configuration,
        "mesh_counts": {
            "element_count": 2000 * (index + 1),
            "vertex_count": 1000 * (index + 1),
        },
        "material_identity_sha256": MATERIAL_SHA256,
        "incidence_identity_sha256": INCIDENCE_SHA256,
        "spectral_bundle": bundle, "analysis_decision": decision,
        "candidate_measurements": characterization,
        "optional_field_metrics": {}, "fixed_reference_diagnostics": {},
    }


def test_fit_support_outcome_change_blocks_automatic_acceptance():
    levels = [
        _fitted_level(0, 5.0e-6, None),
        _fitted_level(1, 5.006e-6, "fitted-mesh-0"),
    ]
    ladder = build_convergence_ladder(ladder_id="fit-sensitive-ladder", levels=levels)
    evaluation = evaluate_convergence(
        ladder,
        _policy(
            metrics=[_metric("peak_wavelength_m", "m", absolute=4.4e-9)],
            minimum_level_count=2,
        ),
    )
    comparison = evaluation["pair_comparisons"][0]["comparisons"][0]
    sensitivity = comparison["fit_support_sensitivity"]

    assert comparison["passed"] is True
    assert sensitivity["state"] == "compared"
    assert sensitivity["common_support_point_counts"] == [5, 7, 9, 11, 13]
    assert {item["passed"] for item in sensitivity["comparisons"]} == {False, True}
    assert sensitivity["outcome_changed_by_support"] is True
    assert sensitivity["policy_authority"] is False
    assert evaluation["fit_sensitive"] is True
    assert evaluation["scientific_disposition"] == "residual"
    assert evaluation["reason_code"] == "fit_support_sensitive"


def test_monotonicity_is_observation_and_fixed_reference_is_diagnostic_only():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    evaluation = evaluate_convergence(ladder, _policy())
    observations = {
        item["metric"]: item for item in evaluation["monotonicity_observations"]
    }

    assert observations["peak_wavelength_m"]["state"] == "nondecreasing"
    assert observations["peak_response_value"]["state"] == "constant"
    assert observations["mesh:element_count"]["state"] == "nondecreasing"
    assert all(item["convergence_proof"] is False for item in observations.values())
    assert all(item["policy_authority"] is False for item in observations.values())
    assert evaluation["scientific_disposition"] == "accepted"
    assert len(evaluation["fixed_reference_diagnostics"]) == 2
    for pair in evaluation["fixed_reference_diagnostics"]:
        assert pair["governs_convergence"] is False
        assert pair["comparisons"][0]["diagnostic_only"] is True
        assert pair["comparisons"][0]["policy_authority"] is False


def test_public_tool_returns_separate_ladder_and_policy_artifacts():
    server = FastMCP("convergence-evaluation-test")
    register_convergence_evaluation_tools(server)
    result = server._tool_manager._tools["convergence_evaluate"].fn(
        ladder_spec={"ladder_id": "three-mesh-ladder", "levels": _levels()},
        convergence_policy=_policy(),
    )

    assert result["success"] is True
    assert result["scientific_disposition"] == "accepted"
    assert result["artifact_separation"] == {
        "ordered_evidence": "convergence_ladder",
        "policy_decision": "convergence_evaluation",
    }
    assert result["convergence_evaluation"]["ladder_sha256"] == result[
        "convergence_ladder"
    ]["ladder_sha256"]
    assert result["fixed_reference_governs"] is False
    assert result["monotonicity_proves_convergence"] is False
    assert result["undeclared_configuration_started"] is False
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False


def test_public_tool_accepts_canonical_ladder_and_rejects_ambiguous_input():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    server = FastMCP("convergence-input-test")
    register_convergence_evaluation_tools(server)
    tool = server._tool_manager._tools["convergence_evaluate"]

    accepted = tool.fn(convergence_ladder=ladder, convergence_policy=_policy())
    rejected = tool.fn(convergence_policy=_policy())

    assert accepted["success"] is True
    assert rejected["success"] is False
    assert rejected["scientific_disposition"] == "invalid_evidence"
    assert "exactly one" in rejected["error"]
    assert rejected["solver_started"] is False


def test_public_convergence_tool_never_constructs_a_comsol_client():
    code = """
import mph
mph.Client = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('Client called'))
from mcp.server.fastmcp import FastMCP
from src.tools.convergence_evaluation import register_convergence_evaluation_tools
server = FastMCP('solver-free-convergence-subprocess')
register_convergence_evaluation_tools(server)
result = server._tool_manager._tools['convergence_evaluate'].fn(convergence_policy={})
assert result['success'] is False
assert result['solver_started'] is False
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2], capture_output=True, text=True,
        timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_self_rehashed_malformed_level_summary_still_fails_closed():
    ladder = build_convergence_ladder(ladder_id="three-mesh-ladder", levels=_levels())
    malformed = deepcopy(ladder)
    level = malformed["levels"][1]
    level["measurements"]["peak_wavelength_m"] = "5.02e-6"
    level_body = dict(level)
    level_body.pop("level_sha256")
    level["level_sha256"] = _canonical_hash(level_body)
    ladder_body = dict(malformed)
    ladder_body.pop("ladder_sha256")
    malformed["ladder_sha256"] = _canonical_hash(ladder_body)

    with pytest.raises(ValueError, match="numeric"):
        validate_convergence_ladder(malformed)


def test_missing_middle_level_and_non_governing_missing_evidence_fail_closed():
    levels = _levels()
    del levels[1]
    with pytest.raises(ValueError, match="ordinal|adjacency"):
        build_convergence_ladder(ladder_id="missing-middle", levels=levels)

    levels = _levels()
    levels[0]["optional_field_metrics"] = {}
    ladder = build_convergence_ladder(ladder_id="missing-earlier-field", levels=levels)
    evaluation = evaluate_convergence(
        ladder,
        _policy(metrics=[_metric("field:field_integral", "J", absolute=0.11)]),
    )
    assert evaluation["governing_pair_indices"] == [1]
    assert evaluation["pair_comparisons"][1]["evidence_complete"] is True
    assert evaluation["scientific_disposition"] == "invalid_evidence"
    assert evaluation["evidence_issues"] == ["declared_metric_evidence_incomplete"]
