"""MCP adapters for solver-free visual-review contracts."""

from __future__ import annotations

from typing import Any, Literal, Optional

from mcp.server.fastmcp import FastMCP

from comsol_mcp.evidence.visual_review import (
    build_visual_review_receipt,
    build_visual_review_request,
    evaluate_dual_visual_review,
    normalize_codex_capability,
    normalize_opencode_capability,
)


def register_visual_review_tools(mcp: FastMCP) -> None:
    """Register bounded host-evidence normalization and review contracts."""

    @mcp.tool()
    def visual_review_capability_normalize(
        adapter: Literal["codex", "opencode"],
        provider: Optional[str] = None,
        model: Optional[str] = None,
        provider_metadata: Optional[dict[str, Any]] = None,
        cli_attachment_supported: bool = False,
        attachment_part_confirmed: bool = False,
        delivered_artifacts: Optional[list[dict[str, Any]]] = None,
        view_image_available: bool = False,
        view_image_results: Optional[list[dict[str, Any]]] = None,
        calibration: Optional[dict[str, Any]] = None,
        self_reported_image_input: Optional[bool] = None,
        max_images: int = 16,
        max_total_bytes: int = 268435456,
        original_resolution_support: bool = False,
    ) -> dict[str, Any]:
        """Normalize host-confirmed Codex or opencode image capability evidence."""
        try:
            if adapter == "codex":
                return normalize_codex_capability(
                    view_image_available=view_image_available,
                    view_image_results=view_image_results,
                    calibration=calibration,
                    self_reported_image_input=self_reported_image_input,
                    max_images=max_images,
                    max_total_bytes=max_total_bytes,
                    original_resolution_support=original_resolution_support,
                )
            if provider is None or model is None or provider_metadata is None:
                raise ValueError("opencode adapter requires provider, model, and provider_metadata")
            return normalize_opencode_capability(
                provider=provider,
                model=model,
                provider_metadata=provider_metadata,
                cli_attachment_supported=cli_attachment_supported,
                attachment_part_confirmed=attachment_part_confirmed,
                delivered_artifacts=delivered_artifacts,
                calibration=calibration,
                self_reported_image_input=self_reported_image_input,
                max_images=max_images,
                max_total_bytes=max_total_bytes,
                original_resolution_support=original_resolution_support,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def visual_review_request_create(
        request_id: str,
        configuration_sha256: str,
        artifacts: list[dict[str, Any]],
        views: list[dict[str, Any]],
        numerical_summary: dict[str, Any],
        questions: list[str],
        review_mode: Literal["single", "dual_blind"] = "single",
    ) -> dict[str, Any]:
        """Create a bounded immutable visual-review request manifest."""
        try:
            return build_visual_review_request(
                request_id=request_id,
                configuration_sha256=configuration_sha256,
                artifacts=artifacts,
                views=views,
                numerical_summary=numerical_summary,
                questions=questions,
                review_mode=review_mode,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def visual_review_receipt_create(
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
        """Create a receipt that stays incomplete until every host and artifact gate passes."""
        try:
            return build_visual_review_receipt(
                review_id=review_id,
                request=request,
                capability=capability,
                session_id=session_id,
                received_artifacts=received_artifacts,
                visual_inspection_performed=visual_inspection_performed,
                findings=findings,
                uncertainties=uncertainties,
                rejected_claims=rejected_claims,
                prior_review_exposure=prior_review_exposure,
                timestamp=timestamp,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def visual_review_dual_evaluate(
        request: dict[str, Any],
        first_receipt: dict[str, Any],
        second_receipt: dict[str, Any],
        comparison: Literal["agreement", "disagreement", "not_compared"],
    ) -> dict[str, Any]:
        """Check blind dual-review identity and route disagreements to adjudication."""
        try:
            return evaluate_dual_visual_review(
                request=request,
                first_receipt=first_receipt,
                second_receipt=second_receipt,
                comparison=comparison,
            )
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": str(exc)}


__all__ = ["register_visual_review_tools"]
