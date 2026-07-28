"""Solver-free artifact chain integrity tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import src.artifact_chain as artifact_chain_module
from src.artifact_chain import (
    build_artifact_chain_manifest,
    validate_artifact_chain_manifest,
    verify_artifact_chain,
)


def _write(root: Path, name: str, schema_name: str, schema_version: str) -> dict:
    payload = json.dumps(
        {
            "schema_name": schema_name,
            "schema_version": schema_version,
            "artifact_id": name,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path = root / f"{name}.json"
    path.write_bytes(payload)
    return {
        "artifact_id": name,
        "relative_path": path.name,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "schema_name": schema_name,
        "schema_version": schema_version,
    }


def _chain(root: Path):
    raw = _write(root, "raw", "comsol_mcp.environment_identity", "1.0.0")
    spectrum = _write(root, "spectrum", "comsol_mcp.physical_evidence", "1.1.0")
    convergence = _write(root, "convergence", "comsol_mcp.runtime_compatibility", "1.0.0")
    branch = _write(root, "branch", "comsol_mcp.schema_registry", "1.0.0")
    receipt = _write(root, "receipt", "comsol_mcp.visual_review_receipt", "1.0.0")
    artifacts = [
        {**raw, "role": "raw_evidence", "parents": []},
        {
            **spectrum,
            "role": "derived_spectral",
            "parents": [{"artifact_id": "raw", "sha256": raw["sha256"]}],
        },
        {
            **convergence,
            "role": "derived_convergence",
            "parents": [{"artifact_id": "spectrum", "sha256": spectrum["sha256"]}],
        },
        {
            **branch,
            "role": "derived_branch",
            "parents": [{"artifact_id": "convergence", "sha256": convergence["sha256"]}],
        },
        {
            **receipt,
            "role": "receipt",
            "parents": [{"artifact_id": "branch", "sha256": branch["sha256"]}],
        },
    ]
    return build_artifact_chain_manifest(
        chain_id="bounded-chain",
        artifacts=artifacts,
        terminal_artifact_ids=["receipt"],
    )


def _rehash_manifest(manifest: dict) -> None:
    body = dict(manifest)
    body.pop("manifest_sha256")
    manifest["manifest_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def test_complete_chain_verifies_exact_bytes_and_returns_path_redacted_receipt(tmp_path):
    manifest = _chain(tmp_path)

    assert validate_artifact_chain_manifest(manifest) == manifest
    receipt = verify_artifact_chain(manifest, artifact_root=tmp_path)

    assert receipt["verification_state"] == "verified"
    assert receipt["artifact_count"] == 5
    assert receipt["terminal_artifact_ids"] == ["receipt"]
    assert receipt["paths_included"] is False
    assert str(tmp_path) not in json.dumps(receipt)
    assert len(receipt["receipt_sha256"]) == 64


def test_chain_rejects_tampered_artifact_bytes(tmp_path):
    manifest = _chain(tmp_path)
    (tmp_path / "spectrum.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="byte count|SHA-256"):
        verify_artifact_chain(manifest, artifact_root=tmp_path)


def test_chain_rejects_parent_hash_mismatch_cycle_or_orphan(tmp_path):
    manifest = _chain(tmp_path)
    artifacts = deepcopy(manifest["artifacts"])
    receipt = next(item for item in artifacts if item["artifact_id"] == "receipt")
    receipt["parents"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="parent hash"):
        build_artifact_chain_manifest(
            chain_id="bad-parent",
            artifacts=artifacts,
            terminal_artifact_ids=["receipt"],
        )

    artifacts = deepcopy(manifest["artifacts"])
    raw = next(item for item in artifacts if item["artifact_id"] == "raw")
    raw["role"] = "receipt"
    raw["parents"] = [
        {
            "artifact_id": "receipt",
            "sha256": next(
                item["sha256"] for item in artifacts if item["artifact_id"] == "receipt"
            ),
        }
    ]
    with pytest.raises(ValueError, match="cycle"):
        build_artifact_chain_manifest(
            chain_id="cycle",
            artifacts=artifacts,
            terminal_artifact_ids=["receipt"],
        )

    artifacts = deepcopy(manifest["artifacts"])
    artifacts.append(
        {
            **_write(tmp_path, "orphan", "comsol_mcp.environment_identity", "1.0.0"),
            "role": "raw_evidence",
            "parents": [],
        }
    )
    with pytest.raises(ValueError, match="not reachable"):
        build_artifact_chain_manifest(
            chain_id="orphan",
            artifacts=artifacts,
            terminal_artifact_ids=["receipt"],
        )


def test_chain_rejects_future_schema_and_path_traversal(tmp_path):
    artifact = _write(tmp_path, "raw", "comsol_mcp.environment_identity", "1.0.0")
    future = {**artifact, "role": "raw_evidence", "parents": [], "schema_version": "99.0.0"}
    with pytest.raises(ValueError, match="unsupported_schema_version"):
        build_artifact_chain_manifest(
            chain_id="future",
            artifacts=[future],
            terminal_artifact_ids=["raw"],
        )

    traversal = {**artifact, "role": "raw_evidence", "parents": [], "relative_path": "../raw.json"}
    with pytest.raises(ValueError, match="traversal-free"):
        build_artifact_chain_manifest(
            chain_id="traversal",
            artifacts=[traversal],
            terminal_artifact_ids=["raw"],
        )


@pytest.mark.parametrize("version", [None, "", {"unbounded": True}, "x" * 129])
def test_chain_rejects_invalid_producer_version(tmp_path, version):
    manifest = _chain(tmp_path)
    manifest["producer"]["version"] = version
    _rehash_manifest(manifest)

    with pytest.raises(ValueError, match="producer"):
        validate_artifact_chain_manifest(manifest)


def test_chain_rechecks_size_after_validating_supplied_producer(tmp_path, monkeypatch):
    manifest = _chain(tmp_path)
    manifest["producer"]["version"] = "release-candidate"
    _rehash_manifest(manifest)
    unhashed = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    exact_size = len(artifact_chain_module._canonical_bytes(unhashed))
    monkeypatch.setattr(artifact_chain_module, "MAX_CHAIN_MANIFEST_BYTES", exact_size - 1)

    with pytest.raises(ValueError, match="oversized"):
        validate_artifact_chain_manifest(manifest)
