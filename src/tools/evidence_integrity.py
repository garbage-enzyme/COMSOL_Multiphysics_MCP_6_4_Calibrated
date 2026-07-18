"""MCP adapters for default-on solver-free evidence-integrity verification."""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from src.path_policy import PathPolicy


def register_evidence_integrity_tools(mcp: FastMCP) -> None:
    """Register path-redacted settings discovery and formal verification."""

    @mcp.tool()
    def evidence_integrity_status() -> dict[str, Any]:
        """Report every effective default-on check and the settings fingerprint."""
        from src.evidence.integrity_controls import load_evidence_integrity_status

        return load_evidence_integrity_status()

    @mcp.tool()
    def evidence_integrity_verify(
        portfolio_request: dict[str, Any],
        artifact_roots: dict[str, str],
        resumed: bool = False,
        producer_compatibility: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Verify exact outcomes, artifact bytes, summary citations, and resume identity."""
        from src.evidence.integrity_controls import (
            load_evidence_integrity_status,
            warning_fields,
        )
        from src.evidence.integrity_verifier import verify_evidence_integrity

        status = load_evidence_integrity_status()
        if status.get("configuration_state") != "valid":
            return verify_evidence_integrity(
                portfolio_request=portfolio_request,
                artifact_roots={},
                resumed=resumed,
                producer_compatibility=producer_compatibility,
                settings_status=status,
            )
        try:
            policy = PathPolicy.from_environment()
            filesystem_checks_enabled = any(
                status["checks"][name]["enabled"]
                for name in (
                    "artifact_chain_verification",
                    "summary_claim_verification",
                )
            )
            normalized_roots: dict[str, str] = {}
            root_ids: set[str] = set()
            if filesystem_checks_enabled:
                for case_id, value in artifact_roots.items():
                    if not isinstance(case_id, str) or not case_id or len(case_id) > 192:
                        raise ValueError("artifact_roots keys must be bounded case IDs")
                    decision = policy.validate_artifact_read_root(value)
                    normalized_roots[case_id] = str(decision.normalized_path)
                    root_ids.add(decision.root_id)
            elif artifact_roots:
                raise ValueError(
                    "artifact_roots must be empty when filesystem checks are disabled"
                )
            result = verify_evidence_integrity(
                portfolio_request=portfolio_request,
                artifact_roots=normalized_roots,
                resumed=resumed,
                producer_compatibility=producer_compatibility,
                settings_status=status,
            )
            result["artifact_root_validation"] = {
                "enforced": True,
                "validated_root_count": len(normalized_roots),
                "root_ids": sorted(root_ids),
                "paths_included": False,
            }
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            result = {
                "success": False,
                "verification_state": "blocked",
                "strictly_verified": False,
                "reason_code": "artifact_root_rejected",
                "error_type": type(exc).__name__,
                "error": str(exc)[:1024],
                "artifact_root_validation": {
                    "enforced": True,
                    "accepted": False,
                    "paths_included": False,
                },
            }
            result.update(warning_fields(status))
            return result


__all__ = ["register_evidence_integrity_tools"]
