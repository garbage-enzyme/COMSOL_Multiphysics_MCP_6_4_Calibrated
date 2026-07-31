"""Public failures retain stable codes without backend or filesystem details."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from src.evidence.integrity_controls import load_evidence_integrity_status
from src.evidence.integrity_verifier import verify_evidence_integrity
from src.resources.model_resources import register_model_resources
from src.tools.evidence_integrity import register_evidence_integrity_tools
from src.tools.field_evidence import register_field_evidence_tools


SECRET = r"C:\private\model.mph"


def test_integrity_tool_redacts_artifact_root_exception(monkeypatch):
    from src.tools import evidence_integrity

    server = FastMCP("redaction-integrity")
    register_evidence_integrity_tools(server)
    monkeypatch.setattr(
        evidence_integrity.PathPolicy,
        "from_environment",
        lambda: (_ for _ in ()).throw(OSError(SECRET)),
    )
    result = server._tool_manager._tools["evidence_integrity_verify"].fn(
        {}, {"case": SECRET}
    )

    assert result["reason_code"] == "artifact_root_rejected"
    assert SECRET not in json.dumps(result)


def test_field_extraction_redacts_backend_exception(monkeypatch):
    from src.tools import field_evidence

    server = FastMCP("redaction-field")
    register_field_evidence_tools(server)
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda _name: object())
    monkeypatch.setattr(
        field_evidence,
        "_normalize_public_field_request",
        lambda _request: (_ for _ in ()).throw(OSError(SECRET)),
    )
    result = server._tool_manager._tools["wave_optics_field_extract"].fn(
        "fixture", {}, "view"
    )

    assert result["reason_code"] == "field_extraction_failed"
    assert SECRET not in json.dumps(result)


def test_integrity_receipt_redacts_check_exception(monkeypatch):
    from src.evidence import integrity_verifier

    monkeypatch.setattr(
        integrity_verifier,
        "verify_portfolio_evidence_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(SECRET)),
    )
    result = verify_evidence_integrity(
        portfolio_request={},
        artifact_roots={},
        settings_status=load_evidence_integrity_status({}),
    )

    assert result["success"] is False
    assert SECRET not in json.dumps(result)


def test_model_resource_redacts_backend_exception(monkeypatch):
    from src.resources import model_resources

    class BrokenModel:
        def name(self):
            raise RuntimeError(SECRET)

    server = FastMCP("redaction-resource")
    register_model_resources(server)
    monkeypatch.setattr(
        model_resources.session_manager, "get_model", lambda _name: BrokenModel()
    )
    resource = next(
        item
        for item in server._resource_manager._templates.values()
        if "model/{name}/tree" in item.uri_template
    )
    result = resource.fn("fixture")

    assert result == "# Error\n\nModel tree is unavailable."
    assert SECRET not in result
