"""Solver-free verification of outcome summaries against hashed evidence chains."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.artifact_chain import validate_artifact_chain_manifest, verify_artifact_chain

from .contracts import canonical_json_bytes, canonical_sha256
from .outcome_contract import validate_outcome_contract


PORTFOLIO_REQUEST_SCHEMA_NAME = "comsol_mcp.portfolio_evidence_request"
PORTFOLIO_VERIFICATION_SCHEMA_NAME = "comsol_mcp.portfolio_evidence_verification"
PORTFOLIO_SCHEMA_VERSION = "1.0.0"

CLAIM_DIMENSIONS = frozenset({"configuration", "mesh", "fit", "wavelength"})
_DIMENSION_POINTER_KEYWORDS = {
    "configuration": ("config", "configuration"),
    "mesh": ("mesh",),
    "fit": ("fit", "fwhm", "quality_factor", "q_factor"),
    "wavelength": ("wavelength", "frequency"),
}
_REQUEST_FIELDS = {
    "schema_name",
    "schema_version",
    "portfolio_id",
    "cases",
    "request_sha256",
}
_CASE_FIELDS = {"case_id", "outcome", "artifact_chain", "summary_claims"}
_CLAIM_FIELDS = {"claim_id", "dimension", "value", "citation"}
_CITATION_FIELDS = {"artifact_id", "artifact_sha256", "json_pointer"}
_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-/"
)
_MAX_CASES = 64
_MAX_CLAIMS_PER_CASE = 256
_MAX_IDENTIFIER_LENGTH = 192


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _reject_unknown(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    unknown = sorted(set(value) - fields)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {unknown}")


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or value[0] not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        or any(character not in _IDENTIFIER_CHARS for character in value)
    ):
        raise ValueError(f"{label} must be a bounded identifier")
    return value


def _hash64(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _decode_json_pointer(pointer: Any, label: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 2048:
        raise ValueError(f"{label} must be a bounded non-root JSON Pointer")
    tokens = []
    for raw in pointer[1:].split("/"):
        decoded = ""
        index = 0
        while index < len(raw):
            if raw[index] != "~":
                decoded += raw[index]
                index += 1
                continue
            if index + 1 >= len(raw) or raw[index + 1] not in {"0", "1"}:
                raise ValueError(f"{label} contains an invalid JSON Pointer escape")
            decoded += "~" if raw[index + 1] == "0" else "/"
            index += 2
        if not decoded:
            raise ValueError(f"{label} cannot contain an empty token")
        tokens.append(decoded)
    return tokens


def _pointer_value(document: Any, tokens: list[str], label: str) -> Any:
    current = document
    for token in tokens:
        if isinstance(current, dict):
            if token not in current:
                raise ValueError(f"{label} does not exist in the cited artifact")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ValueError(f"{label} contains an invalid array index")
            index = int(token)
            if index >= len(current):
                raise ValueError(f"{label} array index is out of bounds")
            current = current[index]
        else:
            raise ValueError(f"{label} traverses through a scalar value")
    return current


def _validate_claim(value: Any, case_index: int, claim_index: int) -> dict[str, Any]:
    label = f"cases[{case_index}].summary_claims[{claim_index}]"
    claim = _mapping(value, label)
    _reject_unknown(claim, _CLAIM_FIELDS, label)
    if set(claim) != _CLAIM_FIELDS:
        raise ValueError(f"{label} fields are incomplete")
    _identifier(claim["claim_id"], f"{label}.claim_id")
    dimension = claim["dimension"]
    if dimension not in CLAIM_DIMENSIONS:
        raise ValueError(f"{label}.dimension must be one of {sorted(CLAIM_DIMENSIONS)}")
    canonical_json_bytes(claim["value"])
    citation = _mapping(claim["citation"], f"{label}.citation")
    _reject_unknown(citation, _CITATION_FIELDS, f"{label}.citation")
    if set(citation) != _CITATION_FIELDS:
        raise ValueError(f"{label}.citation fields are incomplete")
    _identifier(citation["artifact_id"], f"{label}.citation.artifact_id")
    _hash64(citation["artifact_sha256"], f"{label}.citation.artifact_sha256")
    tokens = _decode_json_pointer(citation["json_pointer"], f"{label}.citation.json_pointer")
    pointer_identity = "/".join(tokens).casefold().replace("-", "_")
    if not any(
        keyword in pointer_identity for keyword in _DIMENSION_POINTER_KEYWORDS[dimension]
    ):
        raise ValueError(f"{label} JSON Pointer does not identify its declared dimension")
    return claim


def validate_portfolio_evidence_request(
    value: Any,
    *,
    verify_hash: bool = True,
    validate_outcomes: bool = True,
    validate_artifact_chains: bool = True,
) -> dict[str, Any]:
    """Validate a bounded policy-free portfolio evidence request."""
    request = _mapping(value, "portfolio_request")
    _reject_unknown(request, _REQUEST_FIELDS, "portfolio_request")
    if set(request) != _REQUEST_FIELDS:
        raise ValueError("portfolio_request fields are incomplete")
    if request["schema_name"] != PORTFOLIO_REQUEST_SCHEMA_NAME:
        raise ValueError("portfolio_request.schema_name is unsupported")
    if request["schema_version"] != PORTFOLIO_SCHEMA_VERSION:
        raise ValueError("portfolio_request.schema_version is unsupported")
    _identifier(request["portfolio_id"], "portfolio_request.portfolio_id")
    cases = request["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= _MAX_CASES:
        raise ValueError(f"portfolio_request.cases must contain 1..{_MAX_CASES} cases")
    case_ids = []
    for case_index, raw_case in enumerate(cases):
        label = f"cases[{case_index}]"
        case = _mapping(raw_case, label)
        _reject_unknown(case, _CASE_FIELDS, label)
        if set(case) != _CASE_FIELDS:
            raise ValueError(f"{label} fields are incomplete")
        case_ids.append(_identifier(case["case_id"], f"{label}.case_id"))
        if validate_outcomes:
            validate_outcome_contract(case["outcome"])
        else:
            _mapping(case["outcome"], f"{label}.outcome")
        if validate_artifact_chains:
            validate_artifact_chain_manifest(case["artifact_chain"])
        else:
            _mapping(case["artifact_chain"], f"{label}.artifact_chain")
        claims = case["summary_claims"]
        if not isinstance(claims, list) or len(claims) > _MAX_CLAIMS_PER_CASE:
            raise ValueError(f"{label}.summary_claims must be a bounded list")
        claim_ids = [
            _validate_claim(claim, case_index, claim_index)["claim_id"]
            for claim_index, claim in enumerate(claims)
        ]
        if claim_ids != sorted(claim_ids) or len(claim_ids) != len(set(claim_ids)):
            raise ValueError(f"{label}.summary_claims must have sorted unique claim IDs")
    if case_ids != sorted(case_ids) or len(case_ids) != len(set(case_ids)):
        raise ValueError("portfolio_request.cases must have sorted unique case IDs")
    supplied_hash = _hash64(request["request_sha256"], "portfolio_request.request_sha256")
    without_hash = dict(request)
    without_hash.pop("request_sha256")
    if verify_hash and supplied_hash != canonical_sha256(without_hash):
        raise ValueError("portfolio_request.request_sha256 does not match")
    canonical_json_bytes(request)
    return deepcopy(request)


def build_portfolio_evidence_request(value: Mapping[str, Any]) -> dict[str, Any]:
    """Sort and hash a policy-free portfolio evidence request."""
    request = deepcopy(dict(value))
    if "request_sha256" in request:
        raise ValueError("build_portfolio_evidence_request computes request_sha256")
    cases = request.get("cases")
    if isinstance(cases, list):
        for case in cases:
            if isinstance(case, dict) and isinstance(case.get("summary_claims"), list):
                case["summary_claims"].sort(
                    key=lambda claim: claim.get("claim_id", "")
                    if isinstance(claim, dict)
                    else ""
                )
        cases.sort(
            key=lambda case: case.get("case_id", "") if isinstance(case, dict) else ""
        )
    request["request_sha256"] = canonical_sha256(request)
    return validate_portfolio_evidence_request(request)


def _read_cited_artifact(
    *,
    root: Path,
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    path = (root / artifact["relative_path"]).resolve(strict=True)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("cited artifact path escapes its artifact root") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError("cited artifact must be a regular non-symlink file")
    payload = path.read_bytes()
    if len(payload) != artifact["byte_count"]:
        raise ValueError("cited artifact byte count changed after chain verification")
    if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
        raise ValueError("cited artifact hash changed after chain verification")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cited artifact must contain UTF-8 JSON") from exc
    return _mapping(document, "cited artifact")


def verify_portfolio_evidence(
    value: Any,
    *,
    artifact_roots: Mapping[str, str | Path],
) -> dict[str, Any]:
    """Verify every cited summary value against an exact hash-bound artifact."""
    request = validate_portfolio_evidence_request(value)
    expected_case_ids = [case["case_id"] for case in request["cases"]]
    if set(artifact_roots) != set(expected_case_ids):
        raise ValueError("artifact_roots must map every and only requested case ID")

    verified_cases = []
    total_claims = 0
    for case in request["cases"]:
        case_id = case["case_id"]
        outcome = validate_outcome_contract(case["outcome"])
        chain = validate_artifact_chain_manifest(case["artifact_chain"])
        root = Path(artifact_roots[case_id]).resolve(strict=True)
        chain_receipt = verify_artifact_chain(chain, artifact_root=root)
        by_id = {artifact["artifact_id"]: artifact for artifact in chain["artifacts"]}
        chain_raw_ids = sorted(
            artifact["artifact_id"]
            for artifact in chain["artifacts"]
            if artifact["role"] == "raw_evidence"
        )
        if outcome["evidence"]["raw_artifact_ids"] != chain_raw_ids:
            raise ValueError(
                f"case {case_id} outcome raw artifact IDs do not match the evidence chain"
            )

        documents: dict[str, dict[str, Any]] = {}
        for claim in case["summary_claims"]:
            citation = claim["citation"]
            artifact = by_id.get(citation["artifact_id"])
            if artifact is None:
                raise ValueError(
                    f"case {case_id} claim {claim['claim_id']} cites a missing artifact"
                )
            if citation["artifact_sha256"] != artifact["sha256"]:
                raise ValueError(
                    f"case {case_id} claim {claim['claim_id']} cites the wrong artifact hash"
                )
            document = documents.get(artifact["artifact_id"])
            if document is None:
                document = _read_cited_artifact(root=root, artifact=artifact)
                documents[artifact["artifact_id"]] = document
            tokens = _decode_json_pointer(
                citation["json_pointer"],
                f"case {case_id} claim {claim['claim_id']} JSON Pointer",
            )
            measured = _pointer_value(
                document,
                tokens,
                f"case {case_id} claim {claim['claim_id']} JSON Pointer",
            )
            if canonical_json_bytes(measured) != canonical_json_bytes(claim["value"]):
                raise ValueError(
                    f"case {case_id} claim {claim['claim_id']} is absent from the cited evidence"
                )
        total_claims += len(case["summary_claims"])
        verified_cases.append(
            {
                "case_id": case_id,
                "execution_state": outcome["execution"]["state"],
                "evidence_state": outcome["evidence"]["state"],
                "scientific_disposition": outcome["scientific"]["disposition"],
                "claim_count": len(case["summary_claims"]),
                "artifact_count": chain_receipt["artifact_count"],
                "chain_manifest_sha256": chain["manifest_sha256"],
            }
        )

    body = {
        "schema_name": PORTFOLIO_VERIFICATION_SCHEMA_NAME,
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "portfolio_id": request["portfolio_id"],
        "request_sha256": request["request_sha256"],
        "case_count": len(verified_cases),
        "claim_count": total_claims,
        "verified_cases": verified_cases,
        "policy_applied": False,
        "source_mutation_performed": False,
        "paths_included": False,
    }
    return {**body, "verification_sha256": canonical_sha256(body)}


def verify_portfolio_evidence_checks(
    value: Any,
    *,
    artifact_roots: Mapping[str, str | Path],
    check_outcome_contract: bool,
    check_artifact_chain: bool,
    check_summary_claims: bool,
) -> dict[str, Any]:
    """Run an explicit subset of deterministic checks for exploration opt-out."""
    for name, enabled in {
        "check_outcome_contract": check_outcome_contract,
        "check_artifact_chain": check_artifact_chain,
        "check_summary_claims": check_summary_claims,
    }.items():
        if not isinstance(enabled, bool):
            raise ValueError(f"{name} must be a boolean")
    if not any((check_outcome_contract, check_artifact_chain, check_summary_claims)):
        raise ValueError("at least one portfolio evidence check must be selected")

    request = validate_portfolio_evidence_request(
        value,
        validate_outcomes=check_outcome_contract,
        validate_artifact_chains=check_artifact_chain or check_summary_claims,
    )
    expected_case_ids = [case["case_id"] for case in request["cases"]]
    filesystem_checks = check_artifact_chain or check_summary_claims
    if filesystem_checks and set(artifact_roots) != set(expected_case_ids):
        raise ValueError("artifact_roots must map every and only requested case ID")
    if not filesystem_checks and artifact_roots:
        raise ValueError("artifact_roots must be empty when no filesystem check is selected")

    case_receipts = []
    total_claims = 0
    total_artifacts = 0
    for case in request["cases"]:
        case_id = case["case_id"]
        outcome = (
            validate_outcome_contract(case["outcome"])
            if check_outcome_contract
            else None
        )
        chain = (
            validate_artifact_chain_manifest(case["artifact_chain"])
            if filesystem_checks
            else None
        )
        root = (
            Path(artifact_roots[case_id]).resolve(strict=True)
            if filesystem_checks
            else None
        )
        chain_receipt = (
            verify_artifact_chain(chain, artifact_root=root)
            if check_artifact_chain
            else None
        )

        if check_outcome_contract and check_artifact_chain:
            chain_raw_ids = sorted(
                artifact["artifact_id"]
                for artifact in chain["artifacts"]
                if artifact["role"] == "raw_evidence"
            )
            if outcome["evidence"]["raw_artifact_ids"] != chain_raw_ids:
                raise ValueError(
                    f"case {case_id} outcome raw artifact IDs do not match the evidence chain"
                )

        if check_summary_claims:
            by_id = {artifact["artifact_id"]: artifact for artifact in chain["artifacts"]}
            documents: dict[str, dict[str, Any]] = {}
            for claim in case["summary_claims"]:
                citation = claim["citation"]
                artifact = by_id.get(citation["artifact_id"])
                if artifact is None:
                    raise ValueError(
                        f"case {case_id} claim {claim['claim_id']} cites a missing artifact"
                    )
                if citation["artifact_sha256"] != artifact["sha256"]:
                    raise ValueError(
                        f"case {case_id} claim {claim['claim_id']} cites the wrong artifact hash"
                    )
                document = documents.get(artifact["artifact_id"])
                if document is None:
                    document = _read_cited_artifact(root=root, artifact=artifact)
                    documents[artifact["artifact_id"]] = document
                tokens = _decode_json_pointer(
                    citation["json_pointer"],
                    f"case {case_id} claim {claim['claim_id']} JSON Pointer",
                )
                measured = _pointer_value(
                    document,
                    tokens,
                    f"case {case_id} claim {claim['claim_id']} JSON Pointer",
                )
                if canonical_json_bytes(measured) != canonical_json_bytes(claim["value"]):
                    raise ValueError(
                        f"case {case_id} claim {claim['claim_id']} is absent from the cited evidence"
                    )
            total_claims += len(case["summary_claims"])

        artifact_count = chain_receipt["artifact_count"] if chain_receipt else 0
        total_artifacts += artifact_count
        case_receipts.append(
            {
                "case_id": case_id,
                "outcome_contract": "passed" if check_outcome_contract else "not_selected",
                "artifact_chain": "passed" if check_artifact_chain else "not_selected",
                "summary_claims": "passed" if check_summary_claims else "not_selected",
                "claim_count": len(case["summary_claims"]) if check_summary_claims else 0,
                "artifact_count": artifact_count,
            }
        )

    body = {
        "schema_name": PORTFOLIO_VERIFICATION_SCHEMA_NAME,
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "portfolio_id": request["portfolio_id"],
        "request_sha256": request["request_sha256"],
        "selected_checks": {
            "outcome_contract_validation": check_outcome_contract,
            "artifact_chain_verification": check_artifact_chain,
            "summary_claim_verification": check_summary_claims,
        },
        "case_count": len(case_receipts),
        "claim_count": total_claims,
        "artifact_count": total_artifacts,
        "case_receipts": case_receipts,
        "paths_included": False,
        "source_mutation_performed": False,
    }
    return {**body, "verification_sha256": canonical_sha256(body)}


__all__ = [
    "CLAIM_DIMENSIONS",
    "PORTFOLIO_REQUEST_SCHEMA_NAME",
    "PORTFOLIO_SCHEMA_VERSION",
    "PORTFOLIO_VERIFICATION_SCHEMA_NAME",
    "build_portfolio_evidence_request",
    "validate_portfolio_evidence_request",
    "verify_portfolio_evidence",
    "verify_portfolio_evidence_checks",
]
