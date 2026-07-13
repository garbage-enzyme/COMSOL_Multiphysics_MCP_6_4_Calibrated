"""Solver-free visual-review capability, request, and receipt contracts."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import PurePosixPath
import re
from typing import Any, Mapping

from .contracts import canonical_json_bytes, canonical_sha256


VISUAL_CAPABILITY_SCHEMA = "comsol_mcp.visual_reviewer_capability"
VISUAL_REQUEST_SCHEMA = "comsol_mcp.visual_review_request"
VISUAL_RECEIPT_SCHEMA = "comsol_mcp.visual_review_receipt"
VISUAL_DUAL_REVIEW_SCHEMA = "comsol_mcp.visual_dual_review"
VISUAL_REVIEW_SCHEMA_VERSION = "1.0.0"

MAX_ARTIFACTS = 16
MAX_TOTAL_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_QUESTIONS = 32
MAX_FINDINGS = 64
MAX_TEXT = 4096
ALLOWED_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_CAPABILITY_FIELDS = {
    "schema_name", "schema_version", "client", "provider", "model",
    "transport", "image_input", "supported_media_types", "max_images",
    "max_total_bytes", "original_resolution_support",
    "host_capability_confirmed", "delivery_confirmed", "delivered_artifacts",
    "self_reported_image_input", "host_evidence", "calibration",
    "scientific_review_eligible", "capability_state", "inspection_status",
    "contract_sha256",
}
_HOST_EVIDENCE_FIELDS = {
    "evidence_kind", "metadata_image_input", "transport_available",
    "image_content_results_confirmed", "model_identity_confirmed",
}
_CALIBRATION_FIELDS = {
    "calibration_id", "artifact_sha256", "axis_direction_passed",
    "labels_passed", "colorbar_order_passed", "shared_limits_passed",
    "localized_feature_passed", "completed_at", "passed",
}
_ARTIFACT_REF_FIELDS = {"artifact_id", "sha256"}
_ARTIFACT_FIELDS = {
    "artifact_id", "sha256", "media_type", "byte_count", "relative_path", "role",
}
_VIEW_FIELDS = {
    "artifact_id", "slice_axis", "slice_value", "slice_unit", "grid_shape",
    "x_range", "y_range", "coordinate_unit", "color_limits", "color_scale",
    "quantity", "quantity_unit", "wavelength_m", "config_sha256",
}
_REQUEST_FIELDS = {
    "schema_name", "schema_version", "request_id", "configuration_sha256",
    "artifacts", "views", "required_artifact_ids", "numerical_summary",
    "questions", "review_mode", "status", "contract_sha256",
}
_FINDING_FIELDS = {"question", "observation", "confidence", "uncertainty"}
_REVIEWER_FIELDS = {"client", "provider", "model", "session_id"}
_RECEIPT_FIELDS = {
    "schema_name", "schema_version", "review_id", "request_sha256",
    "capability_sha256", "reviewer", "received_artifacts",
    "visual_inspection_performed", "findings", "uncertainties",
    "rejected_claims", "prior_review_exposure", "timestamp",
    "inspection_status", "status", "incomplete_reasons",
    "numerical_policy_authority", "host_capability_evidence", "contract_sha256",
}
_RECEIPT_HOST_FIELDS = {
    "capability_state", "transport", "delivery_confirmed",
    "scientific_review_eligible", "delivered_artifacts", "calibration_id",
}


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _unknown(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    fields = sorted(set(mapping) - allowed)
    if fields:
        raise ValueError(f"{label} contains unknown fields: {fields}")


def _text(value: Any, label: str, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{label} exceeds {MAX_TEXT} characters")
    if identifier and not _ID.fullmatch(value):
        raise ValueError(f"{label} is not a valid bounded identifier")
    return value


def _hash(value: Any, label: str) -> str:
    text = _text(value, label).lower()
    if not _HEX64.fullmatch(text):
        raise ValueError(f"{label} must be exactly 64 hexadecimal characters")
    return text


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _strings(value: Any, label: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded string list")
    return [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _artifact_ref(value: Any, label: str) -> dict[str, str]:
    item = _mapping(value, label)
    _unknown(item, _ARTIFACT_REF_FIELDS, label)
    return {
        "artifact_id": _text(item.get("artifact_id"), f"{label}.artifact_id", identifier=True),
        "sha256": _hash(item.get("sha256"), f"{label}.sha256"),
    }


def _artifact(value: Any, label: str) -> dict[str, Any]:
    item = _mapping(value, label)
    _unknown(item, _ARTIFACT_FIELDS, label)
    relative_path = _text(item.get("relative_path"), f"{label}.relative_path").replace("\\", "/")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", relative_path):
        raise ValueError(f"{label}.relative_path must be relative and traversal-free")
    media_type = _text(item.get("media_type"), f"{label}.media_type").lower()
    if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
        raise ValueError(f"{label}.media_type is not a supported image type")
    byte_count = item.get("byte_count")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count <= 0:
        raise ValueError(f"{label}.byte_count must be a positive integer")
    return {
        "artifact_id": _text(item.get("artifact_id"), f"{label}.artifact_id", identifier=True),
        "sha256": _hash(item.get("sha256"), f"{label}.sha256"),
        "media_type": media_type,
        "byte_count": byte_count,
        "relative_path": relative_path,
        "role": _text(item.get("role"), f"{label}.role"),
    }


def _calibration(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    item = _mapping(value, "calibration")
    _unknown(item, _CALIBRATION_FIELDS, "calibration")
    result = {
        "calibration_id": _text(item.get("calibration_id"), "calibration.calibration_id", identifier=True),
        "artifact_sha256": _hash(item.get("artifact_sha256"), "calibration.artifact_sha256"),
        "completed_at": _text(item.get("completed_at"), "calibration.completed_at"),
    }
    for field in (
        "axis_direction_passed", "labels_passed", "colorbar_order_passed",
        "shared_limits_passed", "localized_feature_passed",
    ):
        if not isinstance(item.get(field), bool):
            raise ValueError(f"calibration.{field} must be boolean")
        result[field] = item[field]
    result["passed"] = all(result[field] for field in result if field.endswith("_passed"))
    if "passed" in item and item["passed"] != result["passed"]:
        raise ValueError("calibration.passed conflicts with the individual gates")
    return result


def _capability_payload(
    *,
    client: str,
    provider: str | None,
    model: str | None,
    transport: str,
    image_input: bool,
    supported_media_types: list[str],
    max_images: int,
    max_total_bytes: int,
    original_resolution_support: bool,
    host_capability_confirmed: bool,
    delivery_confirmed: bool,
    delivered_artifacts: list[dict[str, str]],
    self_reported_image_input: bool | None,
    host_evidence: dict[str, Any],
    calibration: dict[str, Any] | None,
) -> dict[str, Any]:
    scientific = bool(host_capability_confirmed and calibration and calibration.get("passed"))
    state = (
        "delivery_confirmed" if delivery_confirmed
        else ("host_capability_confirmed" if host_capability_confirmed else "unavailable")
    )
    payload = {
        "schema_name": VISUAL_CAPABILITY_SCHEMA,
        "schema_version": VISUAL_REVIEW_SCHEMA_VERSION,
        "client": client,
        "provider": provider,
        "model": model,
        "transport": transport,
        "image_input": image_input,
        "supported_media_types": supported_media_types,
        "max_images": max_images,
        "max_total_bytes": max_total_bytes,
        "original_resolution_support": original_resolution_support,
        "host_capability_confirmed": host_capability_confirmed,
        "delivery_confirmed": delivery_confirmed,
        "delivered_artifacts": delivered_artifacts,
        "self_reported_image_input": self_reported_image_input,
        "host_evidence": host_evidence,
        "calibration": calibration,
        "scientific_review_eligible": scientific,
        "capability_state": state,
        "inspection_status": "ready_for_inspection" if delivery_confirmed else "not_ready",
    }
    payload["contract_sha256"] = canonical_sha256(payload)
    return validate_reviewer_capability(payload)


def normalize_opencode_capability(
    *,
    provider: str,
    model: str,
    provider_metadata: dict[str, Any],
    cli_attachment_supported: bool,
    attachment_part_confirmed: bool,
    delivered_artifacts: list[dict[str, Any]] | None = None,
    calibration: dict[str, Any] | None = None,
    self_reported_image_input: bool | None = None,
    max_images: int = MAX_ARTIFACTS,
    max_total_bytes: int = MAX_TOTAL_ARTIFACT_BYTES,
    original_resolution_support: bool = False,
) -> dict[str, Any]:
    """Normalize live opencode metadata without inferring capability from model name."""
    metadata = _mapping(provider_metadata, "provider_metadata")
    canonical_json_bytes(metadata)
    capabilities = metadata.get("capabilities", {})
    inputs = capabilities.get("input", {}) if isinstance(capabilities, dict) else {}
    metadata_image_input = bool(inputs.get("image")) if isinstance(inputs, dict) else False
    metadata_identity = metadata.get("id") or metadata.get("model") or metadata.get("name")
    model_identity_confirmed = bool(
        isinstance(metadata_identity, str)
        and (metadata_identity == model or metadata_identity.endswith(f"/{model}"))
    )
    refs = [_artifact_ref(item, f"delivered_artifacts[{index}]") for index, item in enumerate(delivered_artifacts or [])]
    host_confirmed = bool(metadata_image_input and cli_attachment_supported and model_identity_confirmed)
    delivery = bool(host_confirmed and attachment_part_confirmed and refs)
    return _capability_payload(
        client="opencode",
        provider=_text(provider, "provider", identifier=True),
        model=_text(model, "model"),
        transport="file_attachment",
        image_input=metadata_image_input,
        supported_media_types=sorted(ALLOWED_IMAGE_MEDIA_TYPES),
        max_images=max_images,
        max_total_bytes=max_total_bytes,
        original_resolution_support=original_resolution_support,
        host_capability_confirmed=host_confirmed,
        delivery_confirmed=delivery,
        delivered_artifacts=refs,
        self_reported_image_input=self_reported_image_input,
        host_evidence={
            "evidence_kind": "opencode_provider_metadata_and_input_part",
            "metadata_image_input": metadata_image_input,
            "transport_available": bool(cli_attachment_supported),
            "image_content_results_confirmed": bool(attachment_part_confirmed and refs),
            "model_identity_confirmed": model_identity_confirmed,
        },
        calibration=_calibration(calibration),
    )


def normalize_codex_capability(
    *,
    view_image_available: bool,
    view_image_results: list[dict[str, Any]] | None = None,
    calibration: dict[str, Any] | None = None,
    self_reported_image_input: bool | None = None,
    max_images: int = MAX_ARTIFACTS,
    max_total_bytes: int = MAX_TOTAL_ARTIFACT_BYTES,
    original_resolution_support: bool = True,
) -> dict[str, Any]:
    """Normalize Codex host-tool evidence; self-identification cannot grant vision."""
    refs = []
    confirmed_results = 0
    for index, value in enumerate(view_image_results or []):
        item = _mapping(value, f"view_image_results[{index}]")
        _unknown(item, {"artifact_id", "sha256", "image_content_returned"}, f"view_image_results[{index}]")
        if not isinstance(item.get("image_content_returned"), bool):
            raise ValueError(f"view_image_results[{index}].image_content_returned must be boolean")
        if item["image_content_returned"]:
            confirmed_results += 1
            refs.append(_artifact_ref({"artifact_id": item.get("artifact_id"), "sha256": item.get("sha256")}, f"view_image_results[{index}]"))
    host_confirmed = bool(view_image_available)
    delivery = bool(host_confirmed and refs and confirmed_results == len(view_image_results or []))
    return _capability_payload(
        client="codex",
        provider=None,
        model=None,
        transport="view_image_tool",
        image_input=host_confirmed,
        supported_media_types=sorted(ALLOWED_IMAGE_MEDIA_TYPES),
        max_images=max_images,
        max_total_bytes=max_total_bytes,
        original_resolution_support=original_resolution_support,
        host_capability_confirmed=host_confirmed,
        delivery_confirmed=delivery,
        delivered_artifacts=refs,
        self_reported_image_input=self_reported_image_input,
        host_evidence={
            "evidence_kind": "codex_view_image_tool_result",
            "metadata_image_input": None,
            "transport_available": host_confirmed,
            "image_content_results_confirmed": bool(delivery),
            "model_identity_confirmed": True,
        },
        calibration=_calibration(calibration),
    )


def validate_reviewer_capability(value: Any) -> dict[str, Any]:
    item = _mapping(value, "reviewer_capability")
    _unknown(item, _CAPABILITY_FIELDS, "reviewer_capability")
    if item.get("schema_name") != VISUAL_CAPABILITY_SCHEMA or item.get("schema_version") != VISUAL_REVIEW_SCHEMA_VERSION:
        raise ValueError("reviewer_capability schema is unsupported")
    client = _text(item.get("client"), "reviewer_capability.client", identifier=True)
    if item.get("transport") not in {"file_attachment", "view_image_tool"}:
        raise ValueError("reviewer_capability.transport is unsupported")
    for field in (
        "image_input", "original_resolution_support", "host_capability_confirmed",
        "delivery_confirmed", "scientific_review_eligible",
    ):
        if not isinstance(item.get(field), bool):
            raise ValueError(f"reviewer_capability.{field} must be boolean")
    if item.get("self_reported_image_input") is not None and not isinstance(item.get("self_reported_image_input"), bool):
        raise ValueError("reviewer_capability.self_reported_image_input must be boolean or null")
    media = _strings(item.get("supported_media_types"), "reviewer_capability.supported_media_types", 16)
    if any(media_type not in ALLOWED_IMAGE_MEDIA_TYPES for media_type in media):
        raise ValueError("reviewer_capability contains an unsupported media type")
    max_images = item.get("max_images")
    max_bytes = item.get("max_total_bytes")
    if isinstance(max_images, bool) or not isinstance(max_images, int) or not 1 <= max_images <= MAX_ARTIFACTS:
        raise ValueError("reviewer_capability.max_images is out of bounds")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_TOTAL_ARTIFACT_BYTES:
        raise ValueError("reviewer_capability.max_total_bytes is out of bounds")
    refs = [_artifact_ref(ref, f"reviewer_capability.delivered_artifacts[{index}]") for index, ref in enumerate(item.get("delivered_artifacts", []))]
    if len(refs) > max_images or len({ref["artifact_id"] for ref in refs}) != len(refs):
        raise ValueError("reviewer_capability.delivered_artifacts is oversized or duplicated")
    host_evidence = _mapping(item.get("host_evidence"), "reviewer_capability.host_evidence")
    _unknown(host_evidence, _HOST_EVIDENCE_FIELDS, "reviewer_capability.host_evidence")
    if item.get("delivery_confirmed") and (not item.get("host_capability_confirmed") or not refs):
        raise ValueError("delivery_confirmed requires host capability and delivered artifacts")
    calibration = _calibration(item.get("calibration"))
    expected_scientific = bool(item.get("host_capability_confirmed") and calibration and calibration.get("passed"))
    if item.get("scientific_review_eligible") != expected_scientific:
        raise ValueError("scientific_review_eligible conflicts with host/calibration evidence")
    expected_state = "delivery_confirmed" if item.get("delivery_confirmed") else ("host_capability_confirmed" if item.get("host_capability_confirmed") else "unavailable")
    if item.get("capability_state") != expected_state:
        raise ValueError("reviewer_capability.capability_state is inconsistent")
    expected_inspection = "ready_for_inspection" if item.get("delivery_confirmed") else "not_ready"
    if item.get("inspection_status") != expected_inspection:
        raise ValueError("reviewer_capability.inspection_status is inconsistent")
    supplied = _hash(item.get("contract_sha256"), "reviewer_capability.contract_sha256")
    unhashed = dict(item); unhashed.pop("contract_sha256", None)
    if supplied != canonical_sha256(unhashed):
        raise ValueError("reviewer_capability.contract_sha256 does not match")
    canonical_json_bytes(item)
    return deepcopy(item)


def _view(value: Any, label: str) -> dict[str, Any]:
    item = _mapping(value, label)
    _unknown(item, _VIEW_FIELDS, label)
    if item.get("slice_axis") not in {"x", "y", "z"}:
        raise ValueError(f"{label}.slice_axis must be x, y, or z")
    grid = item.get("grid_shape")
    if not isinstance(grid, list) or len(grid) != 2 or any(isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 8192 for v in grid):
        raise ValueError(f"{label}.grid_shape must contain two bounded positive integers")
    def pair(name: str, ordered: bool = False) -> list[float]:
        raw = item.get(name)
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError(f"{label}.{name} must contain two finite values")
        result = [_finite(raw[0], f"{label}.{name}[0]"), _finite(raw[1], f"{label}.{name}[1]")]
        if ordered and result[0] > result[1]:
            raise ValueError(f"{label}.{name} must be ordered")
        return result
    color_scale = item.get("color_scale")
    if color_scale not in {"linear", "log"}:
        raise ValueError(f"{label}.color_scale must be linear or log")
    return {
        "artifact_id": _text(item.get("artifact_id"), f"{label}.artifact_id", identifier=True),
        "slice_axis": item["slice_axis"],
        "slice_value": _finite(item.get("slice_value"), f"{label}.slice_value"),
        "slice_unit": _text(item.get("slice_unit"), f"{label}.slice_unit"),
        "grid_shape": grid,
        "x_range": pair("x_range", ordered=True),
        "y_range": pair("y_range", ordered=True),
        "coordinate_unit": _text(item.get("coordinate_unit"), f"{label}.coordinate_unit"),
        "color_limits": pair("color_limits", ordered=True),
        "color_scale": color_scale,
        "quantity": _text(item.get("quantity"), f"{label}.quantity"),
        "quantity_unit": _text(item.get("quantity_unit"), f"{label}.quantity_unit"),
        "wavelength_m": _finite(item.get("wavelength_m"), f"{label}.wavelength_m", positive=True),
        "config_sha256": _hash(item.get("config_sha256"), f"{label}.config_sha256"),
    }


def build_visual_review_request(
    *,
    request_id: str,
    configuration_sha256: str,
    artifacts: list[dict[str, Any]],
    views: list[dict[str, Any]],
    numerical_summary: dict[str, Any],
    questions: list[str],
    review_mode: str = "single",
) -> dict[str, Any]:
    if review_mode not in {"single", "dual_blind"}:
        raise ValueError("review_mode must be single or dual_blind")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_ARTIFACTS:
        raise ValueError(f"artifacts must contain 1..{MAX_ARTIFACTS} entries")
    normalized_artifacts = [_artifact(item, f"artifacts[{index}]") for index, item in enumerate(artifacts)]
    ids = [item["artifact_id"] for item in normalized_artifacts]
    if len(ids) != len(set(ids)):
        raise ValueError("artifact IDs must be unique")
    if sum(item["byte_count"] for item in normalized_artifacts) > MAX_TOTAL_ARTIFACT_BYTES:
        raise ValueError("artifact byte total exceeds the contract bound")
    normalized_views = [_view(item, f"views[{index}]") for index, item in enumerate(views)]
    if {item["artifact_id"] for item in normalized_views} != set(ids):
        raise ValueError("views must describe every artifact exactly once")
    normalized_config = _hash(configuration_sha256, "configuration_sha256")
    if any(view["config_sha256"] != normalized_config for view in normalized_views):
        raise ValueError("every view must reference the request configuration_sha256")
    normalized_questions = _strings(questions, "questions", MAX_QUESTIONS)
    if not normalized_questions:
        raise ValueError("questions must not be empty")
    summary = _mapping(numerical_summary, "numerical_summary")
    canonical_json_bytes(summary)
    payload = {
        "schema_name": VISUAL_REQUEST_SCHEMA,
        "schema_version": VISUAL_REVIEW_SCHEMA_VERSION,
        "request_id": _text(request_id, "request_id", identifier=True),
        "configuration_sha256": normalized_config,
        "artifacts": normalized_artifacts,
        "views": normalized_views,
        "required_artifact_ids": ids,
        "numerical_summary": deepcopy(summary),
        "questions": normalized_questions,
        "review_mode": review_mode,
        "status": "visual_review_required",
    }
    payload["contract_sha256"] = canonical_sha256(payload)
    return validate_visual_review_request(payload)


def validate_visual_review_request(value: Any) -> dict[str, Any]:
    item = _mapping(value, "visual_review_request")
    _unknown(item, _REQUEST_FIELDS, "visual_review_request")
    if item.get("schema_name") != VISUAL_REQUEST_SCHEMA or item.get("schema_version") != VISUAL_REVIEW_SCHEMA_VERSION:
        raise ValueError("visual_review_request schema is unsupported")
    if item.get("status") != "visual_review_required":
        raise ValueError("visual_review_request.status must remain visual_review_required")
    _text(item.get("request_id"), "visual_review_request.request_id", identifier=True)
    _hash(item.get("configuration_sha256"), "visual_review_request.configuration_sha256")
    if item.get("review_mode") not in {"single", "dual_blind"}:
        raise ValueError("visual_review_request.review_mode is unsupported")
    artifacts = item.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= MAX_ARTIFACTS:
        raise ValueError(f"visual_review_request.artifacts must contain 1..{MAX_ARTIFACTS} entries")
    normalized_artifacts = [_artifact(value, f"visual_review_request.artifacts[{index}]") for index, value in enumerate(artifacts)]
    ids = [artifact["artifact_id"] for artifact in normalized_artifacts]
    if len(ids) != len(set(ids)) or sum(artifact["byte_count"] for artifact in normalized_artifacts) > MAX_TOTAL_ARTIFACT_BYTES:
        raise ValueError("visual_review_request artifacts are duplicated or oversized")
    views = item.get("views")
    if not isinstance(views, list):
        raise ValueError("visual_review_request.views must be a list")
    normalized_views = [_view(value, f"visual_review_request.views[{index}]") for index, value in enumerate(views)]
    if len(normalized_views) != len(ids) or {view["artifact_id"] for view in normalized_views} != set(ids):
        raise ValueError("visual_review_request.views must describe every artifact exactly once")
    if any(view["config_sha256"] != item.get("configuration_sha256") for view in normalized_views):
        raise ValueError("visual_review_request view configuration does not match the request")
    if item.get("required_artifact_ids") != ids:
        raise ValueError("visual_review_request.required_artifact_ids is not canonical")
    questions = _strings(item.get("questions"), "visual_review_request.questions", MAX_QUESTIONS)
    if not questions or len(questions) != len(set(questions)):
        raise ValueError("visual_review_request.questions must be non-empty and unique")
    canonical_json_bytes(_mapping(item.get("numerical_summary"), "visual_review_request.numerical_summary"))
    supplied = _hash(item.get("contract_sha256"), "visual_review_request.contract_sha256")
    unhashed = dict(item); unhashed.pop("contract_sha256", None)
    if supplied != canonical_sha256(unhashed):
        raise ValueError("visual_review_request.contract_sha256 does not match")
    if normalized_artifacts != artifacts or normalized_views != views or questions != item.get("questions"):
        raise ValueError("visual_review_request is not canonical")
    canonical_json_bytes(item)
    return deepcopy(item)


def _finding(value: Any, label: str) -> dict[str, Any]:
    item = _mapping(value, label)
    _unknown(item, _FINDING_FIELDS, label)
    confidence = _finite(item.get("confidence"), f"{label}.confidence")
    if not 0 <= confidence <= 1:
        raise ValueError(f"{label}.confidence must be within [0,1]")
    return {
        "question": _text(item.get("question"), f"{label}.question"),
        "observation": _text(item.get("observation"), f"{label}.observation"),
        "confidence": confidence,
        "uncertainty": _text(item.get("uncertainty"), f"{label}.uncertainty"),
    }


def build_visual_review_receipt(
    *,
    review_id: str,
    request: dict[str, Any],
    capability: dict[str, Any],
    session_id: str,
    received_artifacts: list[dict[str, Any]],
    visual_inspection_performed: bool,
    findings: list[dict[str, Any]],
    uncertainties: list[str],
    rejected_claims: list[str],
    prior_review_exposure: bool,
    timestamp: str,
) -> dict[str, Any]:
    request = validate_visual_review_request(request)
    capability = validate_reviewer_capability(capability)
    if not isinstance(visual_inspection_performed, bool) or not isinstance(prior_review_exposure, bool):
        raise ValueError("inspection and prior-review flags must be boolean")
    refs = [_artifact_ref(item, f"received_artifacts[{index}]") for index, item in enumerate(received_artifacts)]
    normalized_findings = [_finding(item, f"findings[{index}]") for index, item in enumerate(findings)]
    if len(normalized_findings) > MAX_FINDINGS:
        raise ValueError(f"findings exceeds {MAX_FINDINGS} entries")
    expected = {item["artifact_id"]: item["sha256"] for item in request["artifacts"]}
    received = {item["artifact_id"]: item["sha256"] for item in refs}
    delivered = {item["artifact_id"]: item["sha256"] for item in capability["delivered_artifacts"]}
    finding_questions = {item["question"] for item in normalized_findings}
    reasons = []
    if not capability["host_capability_confirmed"]:
        reasons.append("host_capability_unconfirmed")
    if not capability["delivery_confirmed"]:
        reasons.append("host_delivery_unconfirmed")
    if not capability["scientific_review_eligible"]:
        reasons.append("known_answer_calibration_incomplete")
    if received != expected:
        reasons.append("received_artifacts_incomplete_or_mismatched")
    if any(delivered.get(key) != value for key, value in expected.items()):
        reasons.append("capability_delivery_does_not_cover_request")
    if not visual_inspection_performed:
        reasons.append("visual_inspection_not_performed")
    if set(request["questions"]) - finding_questions:
        reasons.append("findings_do_not_cover_all_questions")
    if finding_questions - set(request["questions"]):
        reasons.append("findings_reference_unknown_questions")
    status = "visual_review_complete" if not reasons else "visual_review_required"
    payload = {
        "schema_name": VISUAL_RECEIPT_SCHEMA,
        "schema_version": VISUAL_REVIEW_SCHEMA_VERSION,
        "review_id": _text(review_id, "review_id", identifier=True),
        "request_sha256": request["contract_sha256"],
        "capability_sha256": capability["contract_sha256"],
        "reviewer": {
            "client": capability["client"], "provider": capability["provider"],
            "model": capability["model"],
            "session_id": _text(session_id, "session_id", identifier=True),
        },
        "received_artifacts": refs,
        "visual_inspection_performed": visual_inspection_performed,
        "findings": normalized_findings,
        "uncertainties": _strings(uncertainties, "uncertainties", MAX_FINDINGS),
        "rejected_claims": _strings(rejected_claims, "rejected_claims", MAX_FINDINGS),
        "prior_review_exposure": prior_review_exposure,
        "timestamp": _text(timestamp, "timestamp"),
        "inspection_status": "performed" if visual_inspection_performed else "not_performed",
        "status": status,
        "incomplete_reasons": reasons,
        "numerical_policy_authority": False,
        "host_capability_evidence": {
            "capability_state": capability["capability_state"],
            "transport": capability["transport"],
            "delivery_confirmed": capability["delivery_confirmed"],
            "scientific_review_eligible": capability["scientific_review_eligible"],
            "delivered_artifacts": capability["delivered_artifacts"],
            "calibration_id": (
                capability["calibration"]["calibration_id"]
                if capability.get("calibration") else None
            ),
        },
    }
    payload["contract_sha256"] = canonical_sha256(payload)
    return validate_visual_review_receipt(payload)


def validate_visual_review_receipt(value: Any) -> dict[str, Any]:
    item = _mapping(value, "visual_review_receipt")
    _unknown(item, _RECEIPT_FIELDS, "visual_review_receipt")
    if item.get("schema_name") != VISUAL_RECEIPT_SCHEMA or item.get("schema_version") != VISUAL_REVIEW_SCHEMA_VERSION:
        raise ValueError("visual_review_receipt schema is unsupported")
    reviewer = _mapping(item.get("reviewer"), "visual_review_receipt.reviewer")
    _unknown(reviewer, _REVIEWER_FIELDS, "visual_review_receipt.reviewer")
    for field in ("visual_inspection_performed", "prior_review_exposure"):
        if not isinstance(item.get(field), bool):
            raise ValueError(f"visual_review_receipt.{field} must be boolean")
    if item.get("numerical_policy_authority") is not False:
        raise ValueError("visual review cannot have numerical policy authority")
    if not _TIMESTAMP.fullmatch(_text(item.get("timestamp"), "visual_review_receipt.timestamp")):
        raise ValueError("visual_review_receipt.timestamp must be UTC ISO-8601")
    if item.get("status") not in {"visual_review_required", "visual_review_complete"}:
        raise ValueError("visual_review_receipt.status is unsupported")
    _hash(item.get("request_sha256"), "visual_review_receipt.request_sha256")
    _hash(item.get("capability_sha256"), "visual_review_receipt.capability_sha256")
    [_artifact_ref(ref, f"visual_review_receipt.received_artifacts[{index}]") for index, ref in enumerate(item.get("received_artifacts", []))]
    [_finding(finding, f"visual_review_receipt.findings[{index}]") for index, finding in enumerate(item.get("findings", []))]
    _strings(item.get("uncertainties"), "visual_review_receipt.uncertainties", MAX_FINDINGS)
    _strings(item.get("rejected_claims"), "visual_review_receipt.rejected_claims", MAX_FINDINGS)
    _strings(item.get("incomplete_reasons"), "visual_review_receipt.incomplete_reasons", 16)
    host = _mapping(item.get("host_capability_evidence"), "visual_review_receipt.host_capability_evidence")
    _unknown(host, _RECEIPT_HOST_FIELDS, "visual_review_receipt.host_capability_evidence")
    if not isinstance(host.get("delivery_confirmed"), bool) or not isinstance(host.get("scientific_review_eligible"), bool):
        raise ValueError("visual_review_receipt host capability booleans are invalid")
    [_artifact_ref(ref, f"visual_review_receipt.host_capability_evidence.delivered_artifacts[{index}]") for index, ref in enumerate(host.get("delivered_artifacts", []))]
    if item.get("status") == "visual_review_complete" and item.get("incomplete_reasons"):
        raise ValueError("complete visual review cannot contain incomplete reasons")
    supplied = _hash(item.get("contract_sha256"), "visual_review_receipt.contract_sha256")
    unhashed = dict(item); unhashed.pop("contract_sha256", None)
    if supplied != canonical_sha256(unhashed):
        raise ValueError("visual_review_receipt.contract_sha256 does not match")
    canonical_json_bytes(item)
    return deepcopy(item)


def evaluate_dual_visual_review(
    *,
    request: dict[str, Any],
    first_receipt: dict[str, Any],
    second_receipt: dict[str, Any],
    comparison: str,
) -> dict[str, Any]:
    request = validate_visual_review_request(request)
    first = validate_visual_review_receipt(first_receipt)
    second = validate_visual_review_receipt(second_receipt)
    if comparison not in {"agreement", "disagreement", "not_compared"}:
        raise ValueError("comparison is unsupported")
    reasons = []
    if request["review_mode"] != "dual_blind":
        reasons.append("request_not_dual_blind")
    if first["request_sha256"] != request["contract_sha256"] or second["request_sha256"] != request["contract_sha256"]:
        reasons.append("receipt_request_mismatch")
    if first["reviewer"]["session_id"] == second["reviewer"]["session_id"]:
        reasons.append("review_sessions_not_independent")
    if first["reviewer"]["client"] == second["reviewer"]["client"]:
        reasons.append("review_clients_not_distinct")
    if first["prior_review_exposure"] or second["prior_review_exposure"]:
        reasons.append("blind_review_contaminated")
    if first["received_artifacts"] != second["received_artifacts"]:
        reasons.append("artifact_sets_differ")
    if first["status"] != "visual_review_complete" or second["status"] != "visual_review_complete":
        reasons.append("one_or_more_reviews_incomplete")
    if reasons:
        state = "visual_review_required"
    elif comparison == "disagreement":
        state = "adjudication_required"
    elif comparison == "not_compared":
        state = "dual_review_complete_pending_comparison"
    else:
        state = "dual_review_complete"
    payload = {
        "schema_name": VISUAL_DUAL_REVIEW_SCHEMA,
        "schema_version": VISUAL_REVIEW_SCHEMA_VERSION,
        "request_sha256": request["contract_sha256"],
        "receipt_sha256s": [first["contract_sha256"], second["contract_sha256"]],
        "comparison": comparison,
        "state": state,
        "reasons": reasons,
        "numerical_policy_authority": False,
    }
    payload["contract_sha256"] = canonical_sha256(payload)
    return payload


__all__ = [
    "ALLOWED_IMAGE_MEDIA_TYPES", "VISUAL_CAPABILITY_SCHEMA",
    "VISUAL_DUAL_REVIEW_SCHEMA", "VISUAL_RECEIPT_SCHEMA",
    "VISUAL_REQUEST_SCHEMA", "VISUAL_REVIEW_SCHEMA_VERSION",
    "build_visual_review_receipt", "build_visual_review_request",
    "evaluate_dual_visual_review", "normalize_codex_capability",
    "normalize_opencode_capability", "validate_reviewer_capability",
    "validate_visual_review_receipt", "validate_visual_review_request",
]
