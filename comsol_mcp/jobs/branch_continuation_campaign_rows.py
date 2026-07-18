"""Hash-chained state evidence for durable branch-continuation campaigns."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.evidence.spectral_characterization import (
    validate_spectral_analysis_decision,
    validate_spectral_characterization,
    validate_spectral_point_bundle,
)

from .spectral_rows import read_spectral_rows
from .store import read_json


BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_NAME = (
    "comsol_mcp.branch_continuation_campaign_state"
)
BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_VERSION = "1.0.0"
MAX_BRANCH_CONTINUATION_CAMPAIGN_ROW_BYTES = 128 * 1024

_ARTIFACT_PATHS = {
    "spectral_summary": "analysis/summary.json",
    "spectral_bundle": "analysis/spectral_point_bundle.json",
    "spectral_decision": "analysis/spectral_analysis_decision.json",
    "spectral_characterization": "analysis/spectral_characterization.json",
    "spectral_rows": "spectral_rows.jsonl",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _descriptor(path: Path, root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("continuation state artifact escapes the campaign directory") from exc
    return {
        "relative_path": relative,
        "sha256": _sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _verify_descriptor(value: object, root: Path, name: str) -> Path:
    if not isinstance(value, Mapping) or set(value) != {"relative_path", "sha256", "size_bytes"}:
        raise ValueError(f"{name} artifact descriptor is invalid")
    relative = value["relative_path"]
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError(f"{name} artifact path is invalid")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{name} artifact escapes the campaign directory") from exc
    if not path.is_file():
        raise ValueError(f"{name} artifact is missing")
    size = value["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int) or size < 0 or path.stat().st_size != size:
        raise ValueError(f"{name} artifact size does not match")
    if _sha256_file(path) != value["sha256"]:
        raise ValueError(f"{name} artifact hash does not match")
    return path


def _load_state_artifacts(
    campaign_root: Path,
    state_dir: Path,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    spectral_spec = state["spectral_job"]
    paths = {name: state_dir / relative for name, relative in _ARTIFACT_PATHS.items()}
    if any(not path.is_file() for path in paths.values()):
        raise ValueError("completed continuation state is missing spectral artifacts")
    summary = read_json(paths["spectral_summary"])
    if (
        summary.get("execution_state") != "completed"
        or summary.get("spec_fingerprint") != spectral_spec["spec_fingerprint"]
        or summary.get("source_model_sha256") != spectral_spec["source_model_sha256"]
        or summary.get("configuration_sha256") != spectral_spec["configuration_sha256"]
    ):
        raise ValueError("spectral summary does not match the declared continuation state")
    bundle = validate_spectral_point_bundle(read_json(paths["spectral_bundle"]))
    decision = validate_spectral_analysis_decision(
        read_json(paths["spectral_decision"]), bundle=bundle
    )
    characterization = validate_spectral_characterization(
        read_json(paths["spectral_characterization"]), bundle=bundle, decision=decision
    )
    if (
        bundle["source_model"]["sha256"] != spectral_spec["source_model_sha256"]
        or bundle["configuration_sha256"] != spectral_spec["configuration_sha256"]
    ):
        raise ValueError("spectral bundle identity does not match the continuation state")
    rows = read_spectral_rows(paths["spectral_rows"], spectral_spec, artifact_root=state_dir)
    if len(rows) != summary.get("row_count"):
        raise ValueError("spectral row count does not match the completed summary")
    mesh_counts = {(row["mesh_element_count"], row["mesh_vertex_count"]) for row in rows}
    if len(mesh_counts) != 1:
        raise ValueError("one continuation state must use one observed mesh")
    element_count, vertex_count = mesh_counts.pop()
    requested_wavelengths = [row["requested_wavelength_m"] for row in bundle["rows"]]
    expansion_count = len(
        {
            row["stage_index"]
            for row in rows
            if row["stage_kind"] == "window_expansion"
        }
    )
    return {
        "summary": summary,
        "bundle": bundle,
        "decision": decision,
        "characterization": characterization,
        "search_window_m": {
            "lower_m": min(requested_wavelengths),
            "upper_m": max(requested_wavelengths),
        },
        "expansion_count": expansion_count,
        "mesh_counts": {"element_count": element_count, "vertex_count": vertex_count},
        "artifacts": {name: _descriptor(path, campaign_root) for name, path in paths.items()},
    }


def _validate_row(
    value: object,
    spec: Mapping[str, Any],
    *,
    expected_ordinal: int,
    previous_row_sha256: str | None,
    artifact_root: Path,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("branch-continuation campaign state row must be an object")
    row = dict(value)
    expected_fields = {
        "schema_name", "schema_version", "spec_fingerprint", "attempt",
        "ordinal", "state_id", "child_spec_fingerprint", "source_model_sha256",
        "configuration_sha256", "coordinate_identity_sha256", "polarization",
        "material_identity_sha256", "incidence_readback_sha256", "execution_state",
        "scientific_disposition", "reason_code", "search_window_m", "expansion_count", "mesh_counts",
        "artifacts", "previous_row_sha256", "row_sha256",
    }
    if set(row) != expected_fields:
        raise ValueError("branch-continuation campaign state row fields are invalid")
    if (
        row["schema_name"] != BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_NAME
        or row["schema_version"] != BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_VERSION
        or row["spec_fingerprint"] != spec["spec_fingerprint"]
        or row["ordinal"] != expected_ordinal
        or row["previous_row_sha256"] != previous_row_sha256
    ):
        raise ValueError("branch-continuation campaign state row chain identity is invalid")
    if isinstance(row["attempt"], bool) or not isinstance(row["attempt"], int) or row["attempt"] <= 0:
        raise ValueError("branch-continuation campaign state attempt is invalid")
    state = spec["states"][expected_ordinal]
    spectral_spec = state["spectral_job"]
    expected_identity = {
        "state_id": state["state_id"],
        "child_spec_fingerprint": spectral_spec["spec_fingerprint"],
        "source_model_sha256": spectral_spec["source_model_sha256"],
        "configuration_sha256": spectral_spec["configuration_sha256"],
        "coordinate_identity_sha256": state["coordinate"]["identity_sha256"],
        "polarization": state["polarization"],
        "material_identity_sha256": state["material_identity_sha256"],
        "incidence_readback_sha256": state["incidence_readback"]["evidence_sha256"],
    }
    if any(row[key] != expected for key, expected in expected_identity.items()):
        raise ValueError("branch-continuation campaign state row does not match the immutable state")
    if row["execution_state"] != "completed":
        raise ValueError("only completed spectral states may enter the continuation row chain")
    if row["scientific_disposition"] not in {
        "accepted", "residual", "unresolved_at_declared_cap", "invalid_evidence"
    }:
        raise ValueError("branch-continuation campaign state scientific disposition is invalid")
    if not isinstance(row["reason_code"], str) or not row["reason_code"]:
        raise ValueError("branch-continuation campaign state reason code is invalid")
    window = row["search_window_m"]
    if (
        not isinstance(window, Mapping)
        or set(window) != {"lower_m", "upper_m"}
        or any(isinstance(number, bool) or not isinstance(number, (int, float)) for number in window.values())
        or not 0.0 < float(window["lower_m"]) < float(window["upper_m"])
    ):
        raise ValueError("branch-continuation campaign state search window is invalid")
    if (
        isinstance(row["expansion_count"], bool)
        or not isinstance(row["expansion_count"], int)
        or not 0 <= row["expansion_count"] <= state["spectral_job"]["expansion_policy"]["maximum_expansions"]
    ):
        raise ValueError("branch-continuation campaign state expansion count is invalid")
    if (
        not isinstance(row["mesh_counts"], Mapping)
        or set(row["mesh_counts"]) != {"element_count", "vertex_count"}
        or any(
            isinstance(count, bool) or not isinstance(count, int) or count <= 0
            for count in row["mesh_counts"].values()
        )
    ):
        raise ValueError("branch-continuation campaign state mesh counts are invalid")
    if not isinstance(row["artifacts"], Mapping) or set(row["artifacts"]) != set(_ARTIFACT_PATHS):
        raise ValueError("branch-continuation campaign state artifact inventory is invalid")
    artifact_paths = {
        name: _verify_descriptor(descriptor, artifact_root, name)
        for name, descriptor in row["artifacts"].items()
    }
    state_dir = artifact_paths["spectral_rows"].parent
    loaded = _load_state_artifacts(artifact_root, state_dir, state)
    if (
        loaded["search_window_m"] != dict(row["search_window_m"])
        or loaded["expansion_count"] != row["expansion_count"]
        or loaded["mesh_counts"] != dict(row["mesh_counts"])
        or loaded["summary"]["scientific_disposition"] != row["scientific_disposition"]
        or loaded["summary"]["reason_code"] != row["reason_code"]
        or loaded["artifacts"] != dict(row["artifacts"])
    ):
        raise ValueError("branch-continuation campaign state row does not replay from its artifacts")
    body = dict(row)
    supplied = body.pop("row_sha256")
    if _fingerprint(body) != supplied:
        raise ValueError("branch-continuation campaign state row hash does not match")
    return row


def read_branch_continuation_campaign_states(
    path: str | Path,
    spec: Mapping[str, Any],
    *,
    artifact_root: str | Path,
) -> list[dict[str, Any]]:
    journal = Path(path)
    if not journal.exists():
        return []
    if journal.stat().st_size > len(spec["states"]) * MAX_BRANCH_CONTINUATION_CAMPAIGN_ROW_BYTES:
        raise ValueError("branch-continuation campaign state journal exceeds its bound")
    values = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(values) > len(spec["states"]):
        raise ValueError("branch-continuation campaign has more rows than declared states")
    result = []
    previous = None
    root = Path(artifact_root).resolve()
    for ordinal, value in enumerate(values):
        row = _validate_row(
            value,
            spec,
            expected_ordinal=ordinal,
            previous_row_sha256=previous,
            artifact_root=root,
        )
        result.append(row)
        previous = row["row_sha256"]
    return result


def append_branch_continuation_campaign_state(
    path: str | Path,
    spec: Mapping[str, Any],
    *,
    attempt: int,
    state_dir: str | Path,
    artifact_root: str | Path,
) -> dict[str, Any]:
    root = Path(artifact_root).resolve()
    existing = read_branch_continuation_campaign_states(path, spec, artifact_root=root)
    ordinal = len(existing)
    if ordinal >= len(spec["states"]):
        raise ValueError("all declared branch-continuation states are already complete")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise ValueError("attempt must be a positive integer")
    state = spec["states"][ordinal]
    loaded = _load_state_artifacts(root, Path(state_dir).resolve(), state)
    summary = loaded["summary"]
    body = {
        "schema_name": BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_NAME,
        "schema_version": BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_VERSION,
        "spec_fingerprint": spec["spec_fingerprint"],
        "attempt": attempt,
        "ordinal": ordinal,
        "state_id": state["state_id"],
        "child_spec_fingerprint": state["spectral_job"]["spec_fingerprint"],
        "source_model_sha256": state["spectral_job"]["source_model_sha256"],
        "configuration_sha256": state["spectral_job"]["configuration_sha256"],
        "coordinate_identity_sha256": state["coordinate"]["identity_sha256"],
        "polarization": state["polarization"],
        "material_identity_sha256": state["material_identity_sha256"],
        "incidence_readback_sha256": state["incidence_readback"]["evidence_sha256"],
        "execution_state": "completed",
        "scientific_disposition": summary["scientific_disposition"],
        "reason_code": summary["reason_code"],
        "search_window_m": loaded["search_window_m"],
        "expansion_count": loaded["expansion_count"],
        "mesh_counts": loaded["mesh_counts"],
        "artifacts": loaded["artifacts"],
        "previous_row_sha256": existing[-1]["row_sha256"] if existing else None,
    }
    row = {**body, "row_sha256": _fingerprint(body)}
    if len(_canonical_bytes(row)) > MAX_BRANCH_CONTINUATION_CAMPAIGN_ROW_BYTES:
        raise ValueError("branch-continuation campaign state row exceeds its bound")
    journal = Path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("ab") as handle:
        handle.write(_canonical_bytes(row) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())
    replayed = read_branch_continuation_campaign_states(path, spec, artifact_root=root)
    if replayed[-1] != row:
        raise RuntimeError("branch-continuation campaign state row did not replay after append")
    return row


__all__ = [
    "BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_NAME",
    "BRANCH_CONTINUATION_CAMPAIGN_STATE_SCHEMA_VERSION",
    "append_branch_continuation_campaign_state",
    "read_branch_continuation_campaign_states",
]
