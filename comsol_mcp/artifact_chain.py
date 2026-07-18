"""Solver-free integrity verification for bounded JSON artifact dependency chains."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from comsol_mcp import __version__
from comsol_mcp.durable import canonical_json_v1, canonical_sha256_v1, sha256_file_bounded
from comsol_mcp.schema_registry import check_schema_support


ARTIFACT_CHAIN_SCHEMA = "comsol_mcp.artifact_chain"
ARTIFACT_CHAIN_VERIFICATION_SCHEMA = "comsol_mcp.artifact_chain_verification"
ARTIFACT_CHAIN_SCHEMA_VERSION = "1.0.0"
MAX_CHAIN_ARTIFACTS = 256
MAX_CHAIN_MANIFEST_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_CHAIN_BYTES = 256 * 1024 * 1024

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_ROLES = {
    "raw_evidence",
    "derived_spectral",
    "derived_convergence",
    "derived_branch",
    "receipt",
}
_ARTIFACT_FIELDS = {
    "artifact_id",
    "role",
    "relative_path",
    "sha256",
    "byte_count",
    "schema_name",
    "schema_version",
    "parents",
}
_PARENT_FIELDS = {"artifact_id", "sha256"}


def _canonical_bytes(value: Any) -> bytes:
    try:
        return canonical_json_v1(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact chain must contain finite JSON values") from exc


def _sha256(value: Any) -> str:
    _canonical_bytes(value)
    return canonical_sha256_v1(value)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return dict(value)


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a bounded portable identifier")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value.lower()):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return value.lower()


def _relative_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{label} must be a bounded relative path")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"{label} must be relative and traversal-free")
    return normalized


def _normalize_artifact(value: Any, index: int) -> dict[str, Any]:
    label = f"artifacts[{index}]"
    item = _mapping(value, label)
    if set(item) != _ARTIFACT_FIELDS:
        raise ValueError(f"{label} fields are invalid")
    artifact_id = _identifier(item["artifact_id"], f"{label}.artifact_id")
    role = item["role"]
    if role not in _ROLES:
        raise ValueError(f"{label}.role is unsupported")
    byte_count = item["byte_count"]
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or not 0 < byte_count <= MAX_ARTIFACT_BYTES
    ):
        raise ValueError(f"{label}.byte_count is out of bounds")
    schema_name = item["schema_name"]
    schema_version = item["schema_version"]
    support = check_schema_support(schema_name, schema_version)
    if not support["supported"]:
        raise ValueError(
            f"{label} schema is unsupported: {support['reason_code']}"
        )
    parents = item["parents"]
    if not isinstance(parents, list) or len(parents) > MAX_CHAIN_ARTIFACTS:
        raise ValueError(f"{label}.parents must be a bounded list")
    normalized_parents = []
    for parent_index, parent_value in enumerate(parents):
        parent_label = f"{label}.parents[{parent_index}]"
        parent = _mapping(parent_value, parent_label)
        if set(parent) != _PARENT_FIELDS:
            raise ValueError(f"{parent_label} fields are invalid")
        normalized_parents.append(
            {
                "artifact_id": _identifier(
                    parent["artifact_id"], f"{parent_label}.artifact_id"
                ),
                "sha256": _hash(parent["sha256"], f"{parent_label}.sha256"),
            }
        )
    parent_ids = [parent["artifact_id"] for parent in normalized_parents]
    if len(parent_ids) != len(set(parent_ids)):
        raise ValueError(f"{label}.parents contain duplicate artifact IDs")
    if role == "raw_evidence" and normalized_parents:
        raise ValueError("raw evidence artifacts cannot declare parents")
    if role != "raw_evidence" and not normalized_parents:
        raise ValueError("derived artifacts and receipts must declare parents")
    return {
        "artifact_id": artifact_id,
        "role": role,
        "relative_path": _relative_path(item["relative_path"], f"{label}.relative_path"),
        "sha256": _hash(item["sha256"], f"{label}.sha256"),
        "byte_count": byte_count,
        "schema_name": schema_name,
        "schema_version": schema_version,
        "parents": sorted(normalized_parents, key=lambda parent: parent["artifact_id"]),
    }


def _validate_graph(
    artifacts: list[dict[str, Any]], terminal_artifact_ids: list[str]
) -> None:
    by_id = {item["artifact_id"]: item for item in artifacts}
    if len(by_id) != len(artifacts):
        raise ValueError("artifact IDs must be unique")
    if not terminal_artifact_ids or len(terminal_artifact_ids) != len(set(terminal_artifact_ids)):
        raise ValueError("terminal_artifact_ids must be nonempty and unique")
    if not set(terminal_artifact_ids) <= set(by_id):
        raise ValueError("terminal_artifact_ids reference missing artifacts")
    for item in artifacts:
        for parent in item["parents"]:
            actual = by_id.get(parent["artifact_id"])
            if actual is None:
                raise ValueError("artifact parent reference is missing")
            if actual["sha256"] != parent["sha256"]:
                raise ValueError("artifact parent hash does not match its manifest entry")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(artifact_id: str) -> None:
        if artifact_id in visiting:
            raise ValueError("artifact dependency graph contains a cycle")
        if artifact_id in visited:
            return
        visiting.add(artifact_id)
        for parent in by_id[artifact_id]["parents"]:
            visit(parent["artifact_id"])
        visiting.remove(artifact_id)
        visited.add(artifact_id)

    for terminal in terminal_artifact_ids:
        visit(terminal)
    if visited != set(by_id):
        raise ValueError("artifact chain contains entries not reachable from a terminal")
    roots = {artifact_id for artifact_id in visited if not by_id[artifact_id]["parents"]}
    if not roots or any(by_id[artifact_id]["role"] != "raw_evidence" for artifact_id in roots):
        raise ValueError("every artifact chain must resolve to raw evidence roots")


def build_artifact_chain_manifest(
    *,
    chain_id: str,
    artifacts: list[Mapping[str, Any]],
    terminal_artifact_ids: list[str],
) -> dict[str, Any]:
    """Normalize and hash one immutable artifact-chain manifest."""
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_CHAIN_ARTIFACTS:
        raise ValueError(f"artifacts must contain 1..{MAX_CHAIN_ARTIFACTS} entries")
    normalized = [_normalize_artifact(item, index) for index, item in enumerate(artifacts)]
    normalized.sort(key=lambda item: item["artifact_id"])
    terminals = sorted(
        _identifier(item, f"terminal_artifact_ids[{index}]")
        for index, item in enumerate(terminal_artifact_ids)
    )
    _validate_graph(normalized, terminals)
    body = {
        "schema_name": ARTIFACT_CHAIN_SCHEMA,
        "schema_version": ARTIFACT_CHAIN_SCHEMA_VERSION,
        "chain_id": _identifier(chain_id, "chain_id"),
        "producer": {"package": "comsol-mcp", "version": __version__},
        "artifacts": normalized,
        "terminal_artifact_ids": terminals,
    }
    if len(_canonical_bytes(body)) > MAX_CHAIN_MANIFEST_BYTES:
        raise ValueError("artifact chain manifest is oversized")
    return {**body, "manifest_sha256": _sha256(body)}


def validate_artifact_chain_manifest(value: Any) -> dict[str, Any]:
    """Validate one canonical chain manifest without reading artifact files."""
    item = _mapping(value, "artifact_chain")
    expected = {
        "schema_name",
        "schema_version",
        "chain_id",
        "producer",
        "artifacts",
        "terminal_artifact_ids",
        "manifest_sha256",
    }
    if set(item) != expected:
        raise ValueError("artifact chain fields are invalid")
    if (
        item["schema_name"] != ARTIFACT_CHAIN_SCHEMA
        or item["schema_version"] != ARTIFACT_CHAIN_SCHEMA_VERSION
    ):
        raise ValueError("artifact chain schema is unsupported")
    producer = _mapping(item["producer"], "artifact_chain.producer")
    if set(producer) != {"package", "version"} or producer["package"] != "comsol-mcp":
        raise ValueError("artifact chain producer is invalid")
    supplied_hash = _hash(item["manifest_sha256"], "artifact_chain.manifest_sha256")
    rebuilt = build_artifact_chain_manifest(
        chain_id=item["chain_id"],
        artifacts=item["artifacts"],
        terminal_artifact_ids=item["terminal_artifact_ids"],
    )
    rebuilt["producer"] = producer
    unhashed = dict(rebuilt)
    unhashed.pop("manifest_sha256")
    rebuilt["manifest_sha256"] = _sha256(unhashed)
    if rebuilt["manifest_sha256"] != supplied_hash or rebuilt != item:
        raise ValueError("artifact chain is noncanonical or its hash does not match")
    return deepcopy(rebuilt)


def verify_artifact_chain(value: Any, *, artifact_root: str | Path) -> dict[str, Any]:
    """Verify exact bytes, schema identities, and graph closure under one root."""
    manifest = validate_artifact_chain_manifest(value)
    root = Path(artifact_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("artifact_root must be a directory")
    verified_bytes = 0
    verified_hashes = []
    for item in manifest["artifacts"]:
        candidate = root.joinpath(*PurePosixPath(item["relative_path"]).parts)
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError("artifact path escapes artifact_root") from exc
        if candidate.is_symlink() or not resolved.is_file():
            raise ValueError("artifact path must be a regular non-symlink file")
        receipt = sha256_file_bounded(resolved, max_bytes=MAX_ARTIFACT_BYTES)
        verified_bytes += receipt["byte_count"]
        if verified_bytes > MAX_CHAIN_BYTES:
            raise ValueError("artifact chain exceeds the total byte limit")
        if receipt["byte_count"] != item["byte_count"]:
            raise ValueError("artifact byte count does not match")
        if receipt["sha256"] != item["sha256"]:
            raise ValueError("artifact SHA-256 does not match")
        payload = resolved.read_bytes()
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("artifact must be UTF-8 JSON") from exc
        if not isinstance(document, dict):
            raise ValueError("artifact JSON must contain one object")
        if (
            document.get("schema_name") != item["schema_name"]
            or document.get("schema_version") != item["schema_version"]
        ):
            raise ValueError("artifact embedded schema identity does not match")
        verified_hashes.append(
            {"artifact_id": item["artifact_id"], "sha256": receipt["sha256"]}
        )

    receipt_body = {
        "schema_name": ARTIFACT_CHAIN_VERIFICATION_SCHEMA,
        "schema_version": ARTIFACT_CHAIN_SCHEMA_VERSION,
        "manifest_sha256": manifest["manifest_sha256"],
        "chain_id": manifest["chain_id"],
        "verification_state": "verified",
        "content_validation": "schema_identity_and_hash_chain",
        "artifact_count": len(manifest["artifacts"]),
        "verified_byte_count": verified_bytes,
        "terminal_artifact_ids": manifest["terminal_artifact_ids"],
        "verified_artifacts": verified_hashes,
        "paths_included": False,
    }
    return {**receipt_body, "receipt_sha256": _sha256(receipt_body)}


__all__ = [
    "ARTIFACT_CHAIN_SCHEMA",
    "ARTIFACT_CHAIN_SCHEMA_VERSION",
    "ARTIFACT_CHAIN_VERIFICATION_SCHEMA",
    "build_artifact_chain_manifest",
    "validate_artifact_chain_manifest",
    "verify_artifact_chain",
]
