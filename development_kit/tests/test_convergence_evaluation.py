"""Solver-free ordered convergence evidence regression tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from src.evidence.convergence_evaluation import (
    build_convergence_ladder,
    validate_convergence_ladder,
)
from src.evidence.spectral_characterization import (
    build_spectral_analysis_decision,
    build_spectral_characterization,
    build_spectral_point_bundle,
)


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
