"""Policy-free summary verification against exact hashed evidence chains."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from src.artifact_chain import build_artifact_chain_manifest
from src.evidence.outcome_contract import (
    OUTCOME_SCHEMA_NAME,
    OUTCOME_SCHEMA_VERSION,
    build_outcome_contract,
)
from src.evidence.portfolio_verifier import (
    PORTFOLIO_REQUEST_SCHEMA_NAME,
    PORTFOLIO_SCHEMA_VERSION,
    build_portfolio_evidence_request,
    validate_portfolio_evidence_request,
    verify_portfolio_evidence,
)


def _write(root: Path, artifact_id: str, value: dict) -> dict:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = root / f"{artifact_id}.json"
    path.write_bytes(payload)
    return {
        "artifact_id": artifact_id,
        "relative_path": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "schema_name": value["schema_name"],
        "schema_version": value["schema_version"],
    }


def _fixture(root: Path) -> tuple[dict, dict, dict]:
    raw_value = {
        "schema_name": "comsol_mcp.physical_evidence",
        "schema_version": "1.1.0",
        "identity": {"config_sha256": "a" * 64},
        "model": {"mesh_element_count": 4096},
        "evidence": {"wavelength_m": 4.25e-6},
    }
    fit_value = {
        "schema_name": "comsol_mcp.runtime_compatibility",
        "schema_version": "1.0.0",
        "fit": {"quality_factor": 425.5},
    }
    raw = _write(root, "raw-point-001", raw_value)
    fit = _write(root, "fit-summary", fit_value)
    chain = build_artifact_chain_manifest(
        chain_id="case-chain",
        artifacts=[
            {**raw, "role": "raw_evidence", "parents": []},
            {
                **fit,
                "role": "derived_spectral",
                "parents": [{"artifact_id": raw["artifact_id"], "sha256": raw["sha256"]}],
            },
        ],
        terminal_artifact_ids=[fit["artifact_id"]],
    )
    outcome = build_outcome_contract(
        {
            "schema_name": OUTCOME_SCHEMA_NAME,
            "schema_version": OUTCOME_SCHEMA_VERSION,
            "subject_id": "case-one",
            "execution": {
                "state": "completed",
                "reason_code": "requested_work_completed",
                "completed_requested_work": True,
                "cleanup": {
                    "processes_absent": True,
                    "descendants_absent": True,
                    "port_closed": True,
                    "lease_absent": True,
                    "verified": True,
                },
            },
            "evidence": {
                "state": "complete",
                "missing_evidence": [],
                "raw_artifact_ids": [raw["artifact_id"]],
                "diagnostic_artifact_ids": [],
            },
            "scientific": {
                "disposition": "accepted",
                "reason_code": "scientific_gate_passed",
                "declared_cap_reached": False,
                "next_eligible_action": "none",
            },
        }
    )
    claims = [
        {
            "claim_id": "configuration",
            "dimension": "configuration",
            "value": "a" * 64,
            "citation": {
                "artifact_id": raw["artifact_id"],
                "artifact_sha256": raw["sha256"],
                "json_pointer": "/identity/config_sha256",
            },
        },
        {
            "claim_id": "fit",
            "dimension": "fit",
            "value": 425.5,
            "citation": {
                "artifact_id": fit["artifact_id"],
                "artifact_sha256": fit["sha256"],
                "json_pointer": "/fit/quality_factor",
            },
        },
        {
            "claim_id": "mesh",
            "dimension": "mesh",
            "value": 4096,
            "citation": {
                "artifact_id": raw["artifact_id"],
                "artifact_sha256": raw["sha256"],
                "json_pointer": "/model/mesh_element_count",
            },
        },
        {
            "claim_id": "wavelength",
            "dimension": "wavelength",
            "value": 4.25e-6,
            "citation": {
                "artifact_id": raw["artifact_id"],
                "artifact_sha256": raw["sha256"],
                "json_pointer": "/evidence/wavelength_m",
            },
        },
    ]
    request = build_portfolio_evidence_request(
        {
            "schema_name": PORTFOLIO_REQUEST_SCHEMA_NAME,
            "schema_version": PORTFOLIO_SCHEMA_VERSION,
            "portfolio_id": "bounded-portfolio",
            "cases": [
                {
                    "case_id": "case-one",
                    "outcome": outcome,
                    "artifact_chain": chain,
                    "summary_claims": claims,
                }
            ],
        }
    )
    return request, raw, fit


def _rehash_request(request: dict) -> dict:
    request = deepcopy(request)
    request.pop("request_sha256", None)
    return build_portfolio_evidence_request(request)


def test_exact_configuration_mesh_fit_and_wavelength_claims_verify(tmp_path):
    request, _raw, _fit = _fixture(tmp_path)
    original = deepcopy(request)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    receipt = verify_portfolio_evidence(
        request,
        artifact_roots={"case-one": tmp_path},
    )

    assert validate_portfolio_evidence_request(request) == request
    assert receipt["case_count"] == 1
    assert receipt["claim_count"] == 4
    assert receipt["policy_applied"] is False
    assert receipt["source_mutation_performed"] is False
    assert receipt["paths_included"] is False
    assert request == original
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}


@pytest.mark.parametrize("claim_id", ["configuration", "mesh", "fit", "wavelength"])
def test_summary_value_absent_from_cited_chain_is_rejected(tmp_path, claim_id):
    request, _raw, _fit = _fixture(tmp_path)
    claim = next(
        item for item in request["cases"][0]["summary_claims"] if item["claim_id"] == claim_id
    )
    claim["value"] = "not-the-measured-value"
    request = _rehash_request(request)

    with pytest.raises(ValueError, match="absent from the cited evidence"):
        verify_portfolio_evidence(request, artifact_roots={"case-one": tmp_path})


def test_missing_artifact_wrong_hash_pointer_or_dimension_fails_closed(tmp_path):
    request, _raw, _fit = _fixture(tmp_path)
    base = request["cases"][0]["summary_claims"][0]

    missing = deepcopy(request)
    missing["cases"][0]["summary_claims"][0]["citation"]["artifact_id"] = "absent"
    with pytest.raises(ValueError, match="missing artifact"):
        verify_portfolio_evidence(
            _rehash_request(missing), artifact_roots={"case-one": tmp_path}
        )

    wrong_hash = deepcopy(request)
    wrong_hash["cases"][0]["summary_claims"][0]["citation"]["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="wrong artifact hash"):
        verify_portfolio_evidence(
            _rehash_request(wrong_hash), artifact_roots={"case-one": tmp_path}
        )

    missing_pointer = deepcopy(request)
    missing_pointer["cases"][0]["summary_claims"][0]["citation"]["json_pointer"] = "/identity/config_missing"
    with pytest.raises(ValueError, match="does not exist"):
        verify_portfolio_evidence(
            _rehash_request(missing_pointer), artifact_roots={"case-one": tmp_path}
        )

    wrong_dimension = deepcopy(request)
    wrong_dimension["cases"][0]["summary_claims"][0] = {
        **base,
        "dimension": "mesh",
    }
    with pytest.raises(ValueError, match="does not identify"):
        _rehash_request(wrong_dimension)


def test_outcome_raw_ids_must_exactly_match_chain_roots(tmp_path):
    request, _raw, _fit = _fixture(tmp_path)
    request["cases"][0]["outcome"]["evidence"]["raw_artifact_ids"] = ["other-raw"]
    outcome = request["cases"][0]["outcome"]
    outcome.pop("outcome_sha256")
    request["cases"][0]["outcome"] = build_outcome_contract(outcome)

    with pytest.raises(ValueError, match="raw artifact IDs do not match"):
        verify_portfolio_evidence(
            _rehash_request(request), artifact_roots={"case-one": tmp_path}
        )


def test_artifact_byte_tampering_and_unrequested_policy_fields_are_rejected(tmp_path):
    request, raw, _fit = _fixture(tmp_path)
    (tmp_path / raw["relative_path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="byte count|SHA-256"):
        verify_portfolio_evidence(request, artifact_roots={"case-one": tmp_path})

    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    request, _raw, _fit = _fixture(clean_root)
    request["cases"][0]["paper_target"] = 0.99
    with pytest.raises(ValueError, match="unknown fields"):
        _rehash_request(request)


def test_artifact_root_mapping_is_exact(tmp_path):
    request, _raw, _fit = _fixture(tmp_path)
    with pytest.raises(ValueError, match="every and only"):
        verify_portfolio_evidence(request, artifact_roots={})
    with pytest.raises(ValueError, match="every and only"):
        verify_portfolio_evidence(
            request,
            artifact_roots={"case-one": tmp_path, "extra-case": tmp_path},
        )
