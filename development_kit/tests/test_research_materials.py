"""Research material catalog and campaign-binding contracts."""

from __future__ import annotations

import copy

import pytest

from comsol_mcp.research import compile_campaign_manifest, normalize_material_catalog
from development_kit.tests.test_research_contracts import _approval, _goal, _space
from development_kit.tests.test_research_workflow import _capsule


def _entry(material_id: str = "gold_reference", *, approved: bool = True) -> dict:
    return {
        "material_id": material_id,
        "display_name": "Gold reference",
        "composition": "Au",
        "phase": "solid",
        "sample_state": "bulk reference film",
        "source": {
            "citation": "Reviewed optical table",
            "locator": "table-1",
            "accessed_at": "2026-08-06T00:00:00Z",
            "source_sha256": "d" * 64,
            "license": "caller-supplied",
        },
        "validity": {"wavelength_nm": [400.0, 2000.0], "temperature_k": [250.0, 400.0]},
        "optical_data": {
            "representation": "complex_refractive_index",
            "table_sha256": "e" * 64,
            "phasor_sign": "n_plus_ik",
            "interpolation": "linear",
            "extrapolation": "forbidden",
            "passive_expected": True,
        },
        "evidence_status": "measured",
        "uncertainty": {"status": "quantified", "description": "Tabulated uncertainty."},
        "comsol_mapping": {"property_group": "RefractiveIndex", "function_ids": ["n", "k"]},
        "readback_expectations": ["n", "k"],
        "allowed_formulations": ["domain_material"],
        "compatibility_constraints": ["Use only inside the wavelength validity interval."],
        "fabrication_constraints": ["Film state must match the declared source."],
        "caller_approved": approved,
    }


def _catalog() -> dict:
    return {
        "schema_name": "comsol_mcp.research_material_catalog",
        "schema_version": "1.0.0",
        "catalog_id": "mim-materials",
        "created_at": "2026-08-06T00:00:00Z",
        "entries": [_entry()],
    }


def test_catalog_is_canonical_defensive_and_strictly_verified():
    value = _catalog()
    normalized = normalize_material_catalog(value)
    value["entries"][0]["composition"] = "changed"
    assert normalized["entries"][0]["composition"] == "Au"
    assert normalized["caller_approved_material_ids"] == ["gold_reference"]
    assert normalized["strictly_verified_material_ids"] == ["gold_reference"]
    assert len(normalized["catalog_fingerprint"]) == 64


def test_assumed_or_unapproved_material_is_not_strictly_verified():
    catalog = _catalog()
    catalog["entries"][0]["evidence_status"] = "assumed"
    catalog["entries"][0]["caller_approved"] = False
    normalized = normalize_material_catalog(catalog)
    assert normalized["caller_approved_material_ids"] == []
    assert normalized["strictly_verified_material_ids"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entry: entry["optical_data"].update({"extrapolation": "linear"}),
        lambda entry: entry["optical_data"].update({"phasor_sign": "unknown"}),
        lambda entry: entry["validity"].update({"wavelength_nm": [2000.0, 400.0]}),
        lambda entry: entry["source"].update({"source_sha256": "bad"}),
        lambda entry: entry.update({"caller_approved": 1}),
    ],
)
def test_catalog_rejects_promotion_unsafe_material_contracts(mutate):
    value = _catalog()
    mutate(value["entries"][0])
    with pytest.raises(ValueError):
        normalize_material_catalog(value)


def test_catalog_rejects_duplicate_material_ids():
    value = _catalog()
    value["entries"].append(copy.deepcopy(value["entries"][0]))
    with pytest.raises(ValueError, match="material_id values must be unique"):
        normalize_material_catalog(value)


def test_compiler_binds_ready_workflow_and_material_catalog():
    manifest = compile_campaign_manifest(
        _goal(),
        _space(),
        _approval(),
        workflow_capsule=_capsule(),
        material_catalog=_catalog(),
    )
    assert manifest["workflow_capsule"]["exploration_ready"] is True
    assert manifest["material_catalog"]["caller_approved_material_ids"] == ["gold_reference"]


def test_compiler_rejects_unready_workflow_and_unapproved_material_search():
    capsule = _capsule()
    capsule["baseline_receipt"] = None
    with pytest.raises(ValueError, match="workflow capsule must be exploration-ready"):
        compile_campaign_manifest(
            _goal(), _space(), _approval(), workflow_capsule=capsule, material_catalog=_catalog()
        )
    space = _space()
    space["variables"][0] = {
        "variable_id": "material_state",
        "kind": "categorical",
        "unit": "1",
        "baseline": "unapproved",
        "lower": None,
        "upper": None,
        "allowed_values": ["unapproved"],
        "dependency_class": "material",
        "adapter_path": "material.state",
    }
    space["adapter_mappings"][0] = {
        "variable_id": "material_state",
        "adapter_path": "material.state",
        "unit": "1",
    }
    catalog = _catalog()
    catalog["entries"].append(_entry("unapproved", approved=False))
    with pytest.raises(ValueError, match="caller-approved catalog entries"):
        compile_campaign_manifest(
            _goal(), space, _approval(), workflow_capsule=_capsule(), material_catalog=catalog
        )
