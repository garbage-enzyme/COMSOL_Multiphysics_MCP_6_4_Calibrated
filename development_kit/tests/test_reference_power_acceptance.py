"""Solver-free reference-power licensed-gate contract and preflight tests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.evidence.reference_power_acceptance import (
    build_reference_power_dry_run_receipt,
    validate_reference_power_acceptance_contract,
    validate_reference_power_execution_spec,
)


ROOT = Path(__file__).parents[2]
CONTRACT_PATH = (
    ROOT
    / "development_kit"
    / "release"
    / "integration_fixtures"
    / "reference_power_evidence.json"
)


def _contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def _plane(expression, selection, coordinate, normal, sign):
    return {
        "expression": expression,
        "selection_ids": [selection],
        "plane_coordinate_m": coordinate,
        "normal": normal,
        "medium_id": "lossless_air",
        "positive_power_sign": sign,
    }


def _spec(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable")
    return {
        "schema_name": "comsol_mcp.h1_execution_spec",
        "schema_version": "1.0.0",
        "config_id": "unit-reference-power-gate",
        "source_model_path": str(source.resolve()),
        "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "artifact_dir": "D:/reference_power_gate_unit",
        "model": {
            "component_tag": "comp1",
            "physics_tag": "ewfd",
            "study_tag": "std1",
            "study_step_tag": "wl_step",
            "study_step_property": "plist"
        },
        "wavelength": {"value": 4.37, "unit": "um", "parameter": "wl"},
        "reference_air": {
            "expected_material_tags": ["mat1", "mat2"],
            "all_domain_ids": [1, 2, 3],
            "top_air_domain_ids": [3],
            "top_air_coordinate_range": {"x": [0, 1], "y": [0, 1], "z": [0.8, 1]},
            "target_axis": "x",
            "aggregation": "rms_abs",
            "r_expression": "ewfd.Rtotal",
            "t_expression": "ewfd.Ttotal"
        },
        "declared_plane_flux": {
            "incident": _plane("inc_flux", 10, 1e-6, [0, 0, -1], -1),
            "reflected": _plane("ref_flux", 11, 1e-6, [0, 0, 1], 1),
            "transmitted": _plane("trn_flux", 12, -1e-6, [0, 0, -1], -1)
        }
    }


def test_frozen_reference_power_contract_is_strict_sanitized_and_bounded():
    contract = validate_reference_power_acceptance_contract(_contract())

    assert contract["fixture_id"] == "reference_power_evidence"
    serialized = json.dumps(contract, ensure_ascii=False)
    assert "C:\\Users\\" not in serialized
    assert "D:/" not in serialized
    assert contract["limits"]["max_spec_bytes"] <= 256 * 1024


def test_execution_spec_normalizes_exact_declarations_and_redacts_paths(tmp_path):
    spec = _spec(tmp_path)
    normalized = validate_reference_power_execution_spec(spec, _contract(), verify_files=True)
    receipt = build_reference_power_dry_run_receipt(_contract(), spec, verify_files=True)

    assert normalized["reference_air"]["top_air_domain_ids"] == [3]
    assert normalized["declared_plane_flux"]["incident"]["positive_power_sign"] == -1
    assert receipt["real_comsol_started"] is False
    assert receipt["spec_valid"] is True
    assert receipt["paths_redacted"] is True
    assert "source_model_path" not in receipt
    assert "artifact_dir" not in receipt


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value.update({"unknown": True}), "fields mismatch"),
        (lambda value: value["reference_air"].update({"top_air_domain_ids": [9]}), "subset"),
        (lambda value: value["reference_air"].update({"aggregation": "maximum"}), "rms_abs or median_abs"),
        (lambda value: value["declared_plane_flux"]["incident"].update({"normal": [0, 0, 2]}), "unit vector"),
        (lambda value: value.update({"artifact_dir": str(Path("C:/Users/nonascii/测试"))}), "ASCII-only"),
    ],
)
def test_execution_spec_fails_closed_on_ambiguous_inputs(tmp_path, mutation, match):
    value = _spec(tmp_path)
    mutation(value)
    with pytest.raises(ValueError, match=match):
        validate_reference_power_execution_spec(value, _contract())


def test_preflight_cli_validates_fixture_without_importing_mph():
    completed = subprocess.run(
        [sys.executable, "development_kit/scripts/reference_power_gate_preflight.py"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["contract_valid"] is True
    assert receipt["spec_valid"] is None
    assert receipt["real_comsol_started"] is False
    assert "mph" not in (
        ROOT / "development_kit" / "scripts" / "reference_power_gate_preflight.py"
    ).read_text(encoding="utf-8")
