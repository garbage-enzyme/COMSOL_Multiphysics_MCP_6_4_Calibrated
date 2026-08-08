"""Bounded manifest submission and expansion for adjoint optimization jobs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from comsol_mcp.durable import domain_sha256_v2, validate_finite_json
from comsol_mcp.jobs.resource_admission import normalize_resource_policy
from comsol_mcp.research.derivative_support import normalize_derivative_support
from comsol_mcp.research.gradient_contracts import normalize_native_optimizer_configuration

ADJOINT_MANIFEST_SCHEMA_NAME = "comsol_mcp.adjoint_optimization_manifest"
ADJOINT_MANIFEST_SCHEMA_VERSION = "1.0.0"
ADJOINT_SUBMISSION_SCHEMA_NAME = "comsol_mcp.adjoint_optimization_submission"
ADJOINT_SUBMISSION_SCHEMA_VERSION = "1.0.0"
MAX_MANIFEST_BYTES = 512 * 1024


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value.lower()


def _manifest_path(value: object) -> Path:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError("submission_manifest_path must be a nonempty ASCII path")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.suffix.casefold() != ".json":
        raise ValueError("submission_manifest_path must be an absolute JSON path")
    if path.is_symlink() or not path.is_file():
        raise ValueError("submission_manifest_path must name a regular file")
    return path.resolve()


def normalize_adjoint_optimization_submission(value: object) -> dict[str, Any]:
    """Normalize only the bounded public submission envelope; no file read occurs."""
    if not isinstance(value, dict):
        raise ValueError("adjoint optimization submission must be an object")
    fields = {
        "job_type",
        "submission_manifest_path",
        "submission_manifest_sha256",
        "cores",
        "version",
        "resource_policy",
    }
    if set(value) != fields:
        raise ValueError("adjoint optimization submission fields are invalid")
    if value["job_type"] != "adjoint_optimization":
        raise ValueError("adjoint optimization submission discriminator is invalid")
    cores = value["cores"]
    if isinstance(cores, bool) or not isinstance(cores, int) or not 1 <= cores <= 1024:
        raise ValueError("adjoint optimization cores must be explicitly bounded")
    version = value["version"]
    if not isinstance(version, str) or not version.strip() or len(version) > 32:
        raise ValueError("adjoint optimization version must be bounded")
    policy = normalize_resource_policy(value["resource_policy"])
    if policy is None:
        raise ValueError("adjoint optimization resource_policy is required")
    return {
        "job_type": "adjoint_optimization",
        "submission_manifest_path": str(_manifest_path(value["submission_manifest_path"])),
        "submission_manifest_sha256": _digest(
            value["submission_manifest_sha256"], "submission_manifest_sha256"
        ),
        "cores": cores,
        "version": version.strip(),
        "resource_policy": policy,
        "schema_name": ADJOINT_SUBMISSION_SCHEMA_NAME,
        "schema_version": ADJOINT_SUBMISSION_SCHEMA_VERSION,
    }


def expand_adjoint_optimization_manifest(submission: object) -> dict[str, Any]:
    """Read, hash-pin, and normalize one complete manifest before worker startup."""
    envelope = normalize_adjoint_optimization_submission(submission)
    path = Path(envelope["submission_manifest_path"])
    payload = path.read_bytes()
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("adjoint optimization manifest exceeds its byte limit")
    observed_hash = hashlib.sha256(payload).hexdigest()
    if observed_hash != envelope["submission_manifest_sha256"]:
        raise ValueError("adjoint optimization manifest SHA-256 changed")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("adjoint optimization manifest is not strict UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("adjoint optimization manifest must be an object")
    fields = {
        "schema_name",
        "schema_version",
        "source_model_path",
        "source_model_sha256",
        "support",
        "optimizer",
        "initial_values",
        "synthetic_mode",
    }
    if set(raw) != fields:
        raise ValueError("adjoint optimization manifest fields are invalid")
    if (
        raw["schema_name"] != ADJOINT_MANIFEST_SCHEMA_NAME
        or raw["schema_version"] != ADJOINT_MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError("adjoint optimization manifest schema is unsupported")
    source_text = raw["source_model_path"]
    if not isinstance(source_text, str) or not source_text.isascii():
        raise ValueError("adjoint optimization source path must be ASCII")
    source = Path(source_text).expanduser()
    if (
        not source.is_absolute()
        or source.suffix.casefold() != ".mph"
        or source.is_symlink()
        or not source.is_file()
    ):
        raise ValueError("adjoint optimization source must be a regular absolute MPH file")
    source = source.resolve()
    source_hash = _digest(raw["source_model_sha256"], "source_model_sha256")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != source_hash:
        raise ValueError("adjoint optimization source SHA-256 changed")
    support = normalize_derivative_support(raw["support"])
    if support["source_identity"] != source_hash:
        raise ValueError("adjoint support source identity differs from manifest source")
    optimizer = normalize_native_optimizer_configuration(raw["optimizer"])
    values = raw["initial_values"]
    if not isinstance(values, list) or len(values) != len(support["variables"]):
        raise ValueError("initial_values must match the support variable count")
    normalized_values = [float(item) for item in values]
    for item, variable in zip(normalized_values, support["variables"], strict=True):
        if not variable["lower"] <= item <= variable["upper"]:
            raise ValueError("initial_values must remain within support bounds")
    if not isinstance(raw["synthetic_mode"], bool):
        raise ValueError("synthetic_mode must be boolean")
    body = {
        **envelope,
        "schema_name": ADJOINT_MANIFEST_SCHEMA_NAME,
        "schema_version": ADJOINT_MANIFEST_SCHEMA_VERSION,
        "source_model_path": str(source),
        "source_model_sha256": source_hash,
        "support": support,
        "optimizer": optimizer,
        "initial_values": normalized_values,
        "synthetic_mode": raw["synthetic_mode"],
    }
    validate_finite_json(body)
    body["spec_fingerprint"] = domain_sha256_v2(ADJOINT_MANIFEST_SCHEMA_NAME, body)
    return body


__all__ = [
    "ADJOINT_MANIFEST_SCHEMA_NAME",
    "ADJOINT_MANIFEST_SCHEMA_VERSION",
    "ADJOINT_SUBMISSION_SCHEMA_NAME",
    "ADJOINT_SUBMISSION_SCHEMA_VERSION",
    "expand_adjoint_optimization_manifest",
    "normalize_adjoint_optimization_submission",
]
