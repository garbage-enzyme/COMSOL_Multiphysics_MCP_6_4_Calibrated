"""visual review gates for host-confirmed visual-review contracts."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from mcp.server.fastmcp import FastMCP

from src.evidence.visual_review import (
    build_visual_review_receipt,
    build_visual_review_request,
    evaluate_dual_visual_review,
    normalize_codex_capability,
    normalize_opencode_capability,
    validate_reviewer_capability,
    validate_visual_review_receipt,
    validate_visual_review_request,
)
from src.tools.visual_review import register_visual_review_tools


CONFIG_HASH = "a" * 64
ON_HASH = "b" * 64
OFF_HASH = "c" * 64
CAL_HASH = "d" * 64


def _calibration(passed: bool = True):
    return {
        "calibration_id": "known-answer-v1",
        "artifact_sha256": CAL_HASH,
        "axis_direction_passed": passed,
        "labels_passed": passed,
        "colorbar_order_passed": passed,
        "shared_limits_passed": passed,
        "localized_feature_passed": passed,
        "completed_at": "2026-07-13T12:00:00Z",
    }


def _artifacts():
    return [
        {
            "artifact_id": "field.on",
            "sha256": ON_HASH,
            "media_type": "image/png",
            "byte_count": 1200,
            "relative_path": "visual/field_on.png",
            "role": "on_resonance",
        },
        {
            "artifact_id": "field.off",
            "sha256": OFF_HASH,
            "media_type": "image/png",
            "byte_count": 1100,
            "relative_path": "visual/field_off.png",
            "role": "off_resonance",
        },
    ]


def _views():
    base = {
        "slice_axis": "y",
        "slice_value": 0.0,
        "slice_unit": "um",
        "grid_shape": [200, 300],
        "x_range": [0.0, 3.0],
        "y_range": [-1.0, 2.0],
        "coordinate_unit": "um",
        "color_limits": [0.0, 1.0e8],
        "color_scale": "linear",
        "quantity": "ewfd.normE",
        "quantity_unit": "V/m",
        "config_sha256": CONFIG_HASH,
    }
    return [
        {**base, "artifact_id": "field.on", "wavelength_m": 5.292e-6},
        {**base, "artifact_id": "field.off", "wavelength_m": 5.270e-6},
    ]


def _request(review_mode: str = "single"):
    return build_visual_review_request(
        request_id="sun2025-field-v1",
        configuration_sha256=CONFIG_HASH,
        artifacts=_artifacts(),
        views=_views(),
        numerical_summary={"on_off_max_ratio": 4.2, "same_grid": True},
        questions=["Is the feature localized?", "Are shared color limits comparable?"],
        review_mode=review_mode,
    )


def _refs():
    return [
        {"artifact_id": "field.on", "sha256": ON_HASH},
        {"artifact_id": "field.off", "sha256": OFF_HASH},
    ]


def _codex(*, delivered: bool = True, calibrated: bool = True):
    results = [
        {**reference, "image_content_returned": delivered}
        for reference in _refs()
    ]
    return normalize_codex_capability(
        view_image_available=True,
        view_image_results=results,
        calibration=_calibration(calibrated),
        self_reported_image_input=False,
    )


def _findings():
    return [
        {
            "question": "Is the feature localized?",
            "observation": "A bounded bright region is visible in the declared slice.",
            "confidence": 0.9,
            "uncertainty": "Interpolation coverage is supplied by the upstream bundle.",
        },
        {
            "question": "Are shared color limits comparable?",
            "observation": "Both panels declare the same limits and color scale.",
            "confidence": 1.0,
            "uncertainty": "No material uncertainty for this display-only check.",
        },
    ]


def _receipt(capability, *, session="codex-session-1", received=None, inspected=True, exposure=False):
    return build_visual_review_receipt(
        review_id=f"review-{session}",
        request=_request("dual_blind"),
        capability=capability,
        session_id=session,
        received_artifacts=_refs() if received is None else received,
        visual_inspection_performed=inspected,
        findings=_findings() if inspected else [],
        uncertainties=["Mode identity still requires physical context."],
        rejected_claims=["No numerical passivity conclusion is made from the images."],
        prior_review_exposure=exposure,
        timestamp="2026-07-13T12:05:00Z",
    )


def test_text_only_route_is_rejected_from_metadata_even_if_model_self_reports_vision():
    capability = normalize_opencode_capability(
        provider="opencode-go",
        model="text-only-model",
        provider_metadata={
            "id": "opencode-go/text-only-model",
            "capabilities": {"input": {"image": False}},
            "attachment": False,
        },
        cli_attachment_supported=True,
        attachment_part_confirmed=True,
        delivered_artifacts=_refs(),
        calibration=_calibration(),
        self_reported_image_input=True,
    )

    assert capability["image_input"] is False
    assert capability["host_capability_confirmed"] is False
    assert capability["delivery_confirmed"] is False
    assert capability["capability_state"] == "unavailable"


def test_opencode_image_metadata_requires_a_confirmed_attachment_part():
    metadata = {
        "id": "opencode-go/future-vision-model",
        "capabilities": {"input": {"image": True}},
        "attachment": True,
    }
    declared = normalize_opencode_capability(
        provider="opencode-go", model="future-vision-model", provider_metadata=metadata,
        cli_attachment_supported=True, attachment_part_confirmed=False,
        delivered_artifacts=_refs(), calibration=_calibration(),
    )
    delivered = normalize_opencode_capability(
        provider="opencode-go", model="future-vision-model", provider_metadata=metadata,
        cli_attachment_supported=True, attachment_part_confirmed=True,
        delivered_artifacts=_refs(), calibration=_calibration(),
    )

    assert declared["capability_state"] == "host_capability_confirmed"
    assert declared["delivery_confirmed"] is False
    assert delivered["capability_state"] == "delivery_confirmed"
    assert delivered["scientific_review_eligible"] is True

    mismatched = normalize_opencode_capability(
        provider="opencode-go", model="different-model", provider_metadata=metadata,
        cli_attachment_supported=True, attachment_part_confirmed=True,
        delivered_artifacts=_refs(), calibration=_calibration(),
    )
    assert mismatched["host_capability_confirmed"] is False
    assert mismatched["host_evidence"]["model_identity_confirmed"] is False


def test_codex_requires_actual_image_content_results_not_self_identification():
    missing = normalize_codex_capability(
        view_image_available=False,
        view_image_results=[],
        calibration=_calibration(),
        self_reported_image_input=True,
    )
    partial = _codex(delivered=False)
    complete = _codex()

    assert missing["host_capability_confirmed"] is False
    assert partial["delivery_confirmed"] is False
    assert complete["delivery_confirmed"] is True
    assert complete["delivered_artifacts"] == _refs()


def test_failed_known_answer_calibration_blocks_scientific_eligibility():
    capability = _codex(calibrated=False)

    assert capability["calibration"]["passed"] is False
    assert capability["scientific_review_eligible"] is False


def test_capability_hash_and_unknown_fields_fail_closed():
    capability = _codex()
    tampered = deepcopy(capability)
    tampered["max_images"] -= 1
    with pytest.raises(ValueError, match="does not match"):
        validate_reviewer_capability(tampered)

    unknown = deepcopy(capability)
    unknown["model_says_it_can_see"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        validate_reviewer_capability(unknown)


def test_review_request_is_deterministic_bounded_and_contains_shared_view_evidence():
    first = _request()
    second = _request()

    assert first == second
    assert first["status"] == "visual_review_required"
    assert first["views"][0]["grid_shape"] == first["views"][1]["grid_shape"]
    assert first["views"][0]["color_limits"] == first["views"][1]["color_limits"]
    assert first["required_artifact_ids"] == ["field.on", "field.off"]


def test_request_rejects_absolute_paths_mismatched_views_and_hash_tampering():
    artifacts = _artifacts()
    artifacts[0]["relative_path"] = "C:/private/field.png"
    with pytest.raises(ValueError, match="relative"):
        build_visual_review_request(
            request_id="bad", configuration_sha256=CONFIG_HASH,
            artifacts=artifacts, views=_views(), numerical_summary={},
            questions=["Question?"],
        )

    with pytest.raises(ValueError, match="every artifact exactly once"):
        build_visual_review_request(
            request_id="bad", configuration_sha256=CONFIG_HASH,
            artifacts=_artifacts(), views=_views()[:1], numerical_summary={},
            questions=["Question?"],
        )

    tampered = _request()
    tampered["questions"][0] = "Changed?"
    with pytest.raises(ValueError, match="does not match"):
        validate_visual_review_request(tampered)


def test_complete_receipt_requires_calibration_delivery_hashes_inspection_and_findings():
    complete = _receipt(_codex())
    no_delivery = _receipt(_codex(delivered=False))
    no_calibration = _receipt(_codex(calibrated=False))
    missing_artifact = _receipt(_codex(), received=_refs()[:1])
    no_inspection = _receipt(_codex(), inspected=False)

    assert complete["status"] == "visual_review_complete"
    assert complete["numerical_policy_authority"] is False
    assert no_delivery["status"] == "visual_review_required"
    assert "host_delivery_unconfirmed" in no_delivery["incomplete_reasons"]
    assert "known_answer_calibration_incomplete" in no_calibration["incomplete_reasons"]
    assert "received_artifacts_incomplete_or_mismatched" in missing_artifact["incomplete_reasons"]
    assert "visual_inspection_not_performed" in no_inspection["incomplete_reasons"]


def test_visual_contract_hashes_survive_json_number_round_trip():
    request = _request("dual_blind")
    request["numerical_summary"]["integral_float"] = 4.0
    request.pop("contract_sha256")
    request = build_visual_review_request(
        request_id=request["request_id"],
        configuration_sha256=request["configuration_sha256"],
        artifacts=request["artifacts"],
        views=request["views"],
        numerical_summary=request["numerical_summary"],
        questions=request["questions"],
        review_mode=request["review_mode"],
    )
    transported_request = json.loads(json.dumps(request))
    transported_capability = json.loads(json.dumps(_codex()))

    assert validate_visual_review_request(transported_request)["contract_sha256"] == request["contract_sha256"]
    receipt = build_visual_review_receipt(
        review_id="json-round-trip-review",
        request=transported_request,
        capability=transported_capability,
        session_id="json-round-trip-session",
        received_artifacts=_refs(),
        visual_inspection_performed=True,
        findings=_findings(),
        uncertainties=["Transport-only regression fixture."],
        rejected_claims=["No numerical policy claim."],
        prior_review_exposure=False,
        timestamp="2026-07-13T12:05:00Z",
    )
    transported_receipt = json.loads(json.dumps(receipt))

    assert validate_visual_review_receipt(transported_receipt)["status"] == "visual_review_complete"


def test_dual_blind_review_requires_two_independent_complete_receipts():
    request = _request("dual_blind")
    codex = _receipt(_codex(), session="codex-session")
    opencode_capability = normalize_opencode_capability(
        provider="opencode-go", model="future-vision-model",
        provider_metadata={
            "id": "opencode-go/future-vision-model",
            "capabilities": {"input": {"image": True}},
        },
        cli_attachment_supported=True, attachment_part_confirmed=True,
        delivered_artifacts=_refs(), calibration=_calibration(),
    )
    opencode = _receipt(opencode_capability, session="opencode-vision-session")

    agreement = evaluate_dual_visual_review(
        request=request, first_receipt=codex, second_receipt=opencode, comparison="agreement"
    )
    disagreement = evaluate_dual_visual_review(
        request=request, first_receipt=codex, second_receipt=opencode, comparison="disagreement"
    )

    assert agreement["state"] == "dual_review_complete"
    assert disagreement["state"] == "adjudication_required"
    assert disagreement["numerical_policy_authority"] is False


def test_dual_review_remains_incomplete_after_prior_review_exposure():
    request = _request("dual_blind")
    first = _receipt(_codex(), session="first")
    second = _receipt(_codex(), session="second", exposure=True)
    result = evaluate_dual_visual_review(
        request=request, first_receipt=first, second_receipt=second,
        comparison="agreement",
    )

    assert result["state"] == "visual_review_required"
    assert "blind_review_contaminated" in result["reasons"]


def test_visual_contract_import_is_solver_free():
    code = """
import sys
from src.evidence.visual_review import build_visual_review_request
assert 'mph' not in sys.modules
assert not any(name.startswith('jpype') for name in sys.modules)
assert 'src.tools.ownership' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_public_visual_review_tools_fail_closed_without_starting_comsol():
    server = FastMCP("visual-review-tools-test")
    register_visual_review_tools(server)

    capability = server._tool_manager._tools["visual_review_capability_normalize"].fn(
        adapter="codex",
        view_image_available=False,
        self_reported_image_input=True,
    )
    invalid_request = server._tool_manager._tools["visual_review_request_create"].fn(
        request_id="bad",
        configuration_sha256=CONFIG_HASH,
        artifacts=[],
        views=[],
        numerical_summary={},
        questions=["Question?"],
    )

    assert capability["capability_state"] == "unavailable"
    assert invalid_request["success"] is False
    assert "1..16" in invalid_request["error"]
