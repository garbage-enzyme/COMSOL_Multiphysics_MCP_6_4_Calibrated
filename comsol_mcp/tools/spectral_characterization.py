"""MCP adapter for bounded solver-free spectral characterization."""

from __future__ import annotations

import math
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

def _nonfinite_row_summary(bundle_spec: dict[str, Any]) -> dict[str, Any] | None:
    rows = bundle_spec.get("rows")
    if not isinstance(rows, list):
        return None
    numeric_fields = (
        "requested_wavelength_m",
        "evaluated_wavelength_m",
        "frequency_wavelength_m",
        "R",
        "T",
        "A",
    )
    invalid = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        if any(
            isinstance(row.get(field), (int, float))
            and not isinstance(row.get(field), bool)
            and not math.isfinite(float(row[field]))
            for field in numeric_fields
        ):
            invalid.append(
                {
                    "row_id": row.get("row_id")
                    if isinstance(row.get("row_id"), str)
                    else f"row-index-{index}",
                    "raw_row_sha256": row.get("raw_row_sha256")
                    if isinstance(row.get("raw_row_sha256"), str)
                    else None,
                }
            )
    if not invalid:
        return None
    return {
        "success": False,
        "classification": "non_finite",
        "reason_code": "raw_spectrum_contains_non_finite_values",
        "invalid_rows": invalid,
        "raw_bundle": None,
        "analysis_decision": None,
        "candidate_measurements": None,
        "solver_started": False,
        "filesystem_modified": False,
    }


def register_spectral_characterization_tools(mcp: FastMCP) -> None:
    """Register one solver-free tool that returns three separate artifacts."""

    @mcp.tool()
    def spectral_characterize(
        analysis_policy: dict[str, Any],
        measurement_configuration: dict[str, Any],
        bundle_spec: Optional[dict[str, Any]] = None,
        spectral_bundle: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Validate raw spectral evidence and derive bounded candidate measurements."""
        from comsol_mcp.evidence.spectral_characterization import (
            build_spectral_analysis_decision,
            build_spectral_characterization,
            build_spectral_point_bundle,
            validate_spectral_point_bundle,
        )

        try:
            if (bundle_spec is None) == (spectral_bundle is None):
                raise ValueError(
                    "provide exactly one of bundle_spec or spectral_bundle"
                )
            if bundle_spec is not None:
                nonfinite = _nonfinite_row_summary(bundle_spec)
                if nonfinite is not None:
                    return nonfinite
                bundle = build_spectral_point_bundle(**bundle_spec)
            else:
                bundle = validate_spectral_point_bundle(spectral_bundle)
            decision = build_spectral_analysis_decision(bundle, analysis_policy)
            characterization = build_spectral_characterization(
                bundle, decision, measurement_configuration
            )
            return {
                "success": True,
                "classification": decision["classification"],
                "raw_bundle": bundle,
                "analysis_decision": decision,
                "candidate_measurements": characterization,
                "artifact_separation": {
                    "raw_measurements": "raw_bundle",
                    "policy_decisions": "analysis_decision",
                    "derived_measurements": "candidate_measurements",
                },
                "solver_started": False,
                "filesystem_modified": False,
            }
        except (TypeError, ValueError) as exc:
            return {
                "success": False,
                "classification": "invalid_input",
                "reason_code": "spectral_input_rejected",
                "error": str(exc)[:2048],
                "solver_started": False,
                "filesystem_modified": False,
            }


__all__ = ["register_spectral_characterization_tools"]
