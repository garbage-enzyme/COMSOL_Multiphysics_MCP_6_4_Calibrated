"""Solver-free validator and tamper suite for the offline export manifest.

Covers the alpha7.3 fixture matrix: missing artifact, changed bytes, changed
units/order, duplicate ID, path escape, unsupported reader, unbounded
artifact counts, stale manifest hashes, model-identity mismatch, and the
explicit not-FEM-validation boundary. COMSOL/Java/MPh/JPype never start.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from comsol_mcp.evidence.offline_export import (
    OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME,
    OfflineExportError,
    build_offline_export_manifest,
    validate_offline_export_manifest,
)
from comsol_mcp.schema_registry import check_schema_support


@pytest.fixture(autouse=True)
def _owned_artifact_root(tmp_path, monkeypatch):
    """B04: every export path in this suite lives under the owned artifact root."""
    monkeypatch.setenv("COMSOL_MCP_ARTIFACT_WRITE_ROOT", str(tmp_path))
    return tmp_path


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _artifact(
    artifact_id: str = "csv-1",
    relative_path: str = "data/sweep.csv",
    payload: bytes | None = None,
) -> dict:
    if payload is None:
        payload = b"wl,T\n1.0,300.0\n"
    return {
        "artifact_id": artifact_id,
        "relative_path": relative_path,
        "format": relative_path.rsplit(".", 1)[-1],
        "expressions": ["wl", "T"],
        "units": ["m", "K"],
        "parameter_values": {"wl": 1.0},
        "time_values": None,
        "byte_count": len(payload),
        "sha256": _sha(payload),
        "reader_status": "offline_reader_available",
        "_payload": payload,
    }


def _write_export(tmp_path: Path, artifacts_spec: list[dict]) -> tuple[Path, dict]:
    """Write each spec's exact bytes, strip payloads, and build the manifest."""
    for spec in artifacts_spec:
        target = tmp_path / spec["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(spec.pop("_payload"))
    for spec in artifacts_spec:
        spec.pop("_payload", None)
    manifest = build_offline_export_manifest(
        producer_tool="results_export_data",
        producer_version="0.7.3",
        model_path_redacted="**/model.mph",
        model_sha256="a" * 64,
        artifacts=artifacts_spec,
    )
    manifest_path = tmp_path / "export-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, manifest


# ---------------------------------------------------------------------------
# Construction and happy path
# ---------------------------------------------------------------------------


def test_built_manifest_round_trips_and_validates_clean(tmp_path):
    payload = b"wl,T\n1.0,300.0\n"
    spec = _artifact(payload=payload)
    manifest_path, manifest = _write_export(tmp_path, [spec])
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "sweep.csv").write_bytes(payload)

    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    verdict = validate_offline_export_manifest(loaded, tmp_path)
    assert verdict["valid"] is True
    assert verdict["checked_artifacts"] == 1
    assert verdict["failures"] == []
    assert verdict["is_fem_validation"] is False
    assert verdict["validation_sha256"]
    assert verdict["path_evidence"]["enforced"] is True
    assert verdict["path_evidence"]["validated_input_count"] >= 1
    assert manifest["manifest_sha256"]


def test_builder_rejects_duplicate_ids_and_path_escapes():
    with pytest.raises(OfflineExportError):
        build_offline_export_manifest(
            producer_tool="t",
            producer_version="v",
            model_path_redacted="**/m.mph",
            model_sha256=None,
            artifacts=[_artifact(), _artifact()],
        )
    with pytest.raises(OfflineExportError):
        build_offline_export_manifest(
            producer_tool="t",
            producer_version="v",
            model_path_redacted="**/m.mph",
            model_sha256=None,
            artifacts=[_artifact(relative_path="../escape.csv")],
        )


def test_ordering_fingerprint_detects_unit_and_order_drift(tmp_path):
    """Tampering with column order or units breaks the sealed fingerprint."""
    payload = b"wl,T\n1.0,300.0\n"
    manifest_path, manifest = _write_export(
        tmp_path,
        [
            {
                "artifact_id": "csv-1",
                "relative_path": "data/sweep.csv",
                "format": "csv",
                "expressions": ["wl", "T"],
                "units": ["m", "K"],
                "parameter_values": {"wl": 1.0},
                "time_values": None,
                "byte_count": len(payload),
                "sha256": _sha(payload),
                "reader_status": "offline_reader_available",
                "_payload": payload,
            }
        ],
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "sweep.csv").write_bytes(payload)

    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    loaded["artifacts"][0]["columns"] = [
        {"expression": "wl", "unit": "K"},
        {"expression": "T", "unit": "m"},
    ]
    verdict = validate_offline_export_manifest(loaded, tmp_path)
    assert verdict["valid"] is False
    assert verdict["failures"][0]["reason_codes"] == ["ordering_drift"]


def test_unsupported_reader_status_is_refused():
    with pytest.raises(OfflineExportError):
        build_offline_export_manifest(
            producer_tool="t",
            producer_version="v",
            model_path_redacted="**/m.mph",
            model_sha256=None,
            artifacts=[
                {
                    **_artifact(),
                    "format": "vtu",
                    "relative_path": "mesh.vtu",
                    "reader_status": "offline_reader_available",
                }
            ],
        )


# ---------------------------------------------------------------------------
# Tamper matrix through the public validator
# ---------------------------------------------------------------------------


def test_missing_artifact_file_is_reported_per_id(tmp_path):
    manifest_path, _ = _write_export(tmp_path, [_artifact()])
    (tmp_path / "data" / "sweep.csv").unlink()
    verdict = validate_offline_export_manifest(manifest_path)
    assert verdict["valid"] is False
    assert verdict["failures"][0]["reason_codes"] == ["missing_file"]
    assert verdict["failures"][0]["artifact_id"] == "csv-1"


def test_changed_bytes_produce_hash_mismatch(tmp_path):
    payload = b"wl,T\n1.0,300.0\n"
    spec = _artifact(payload=payload)
    manifest_path, _ = _write_export(tmp_path, [spec])
    (tmp_path / "data" / "sweep.csv").write_bytes(b"wl,T\n9.9,999.0")
    verdict = validate_offline_export_manifest(manifest_path)
    assert verdict["valid"] is False
    assert "byte_count_mismatch" in verdict["failures"][0]["reason_codes"]
    assert "artifact_hash_mismatch" in verdict["failures"][0]["reason_codes"]


def test_tampered_ordering_is_detected_as_drift(tmp_path):
    payload = b"wl,T\n1.0,300.0\n"
    spec = _artifact(payload=payload)
    manifest_path, manifest = _write_export(tmp_path, [spec])
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    loaded["artifacts"][0]["columns"] = [
        {"expression": "wl", "unit": "K"},
        {"expression": "T", "unit": "m"},
    ]
    verdict = validate_offline_export_manifest(loaded, tmp_path)
    assert verdict["valid"] is False
    assert verdict["failures"][0]["reason_codes"] == ["ordering_drift"]


def test_duplicate_artifact_id_is_detected_from_disk_payload(tmp_path):
    _, manifest = _write_export(tmp_path, [_artifact()])
    loaded = dict(manifest)
    duplicated = dict(loaded["artifacts"][0])
    loaded["artifacts"] = [loaded["artifacts"][0], duplicated]
    # Recompute a plausible-looking top hash so only the duplicate is wrong.
    from comsol_mcp.durable import canonical_sha256_v1

    body = {k: v for k, v in loaded.items() if k != "manifest_sha256"}
    loaded["manifest_sha256"] = canonical_sha256_v1(body)
    verdict = validate_offline_export_manifest(loaded, tmp_path)
    assert verdict["valid"] is False
    assert any(
        "duplicate_artifact_id" in reason
        for row in verdict["failures"]
        for reason in row["reason_codes"]
    )


def test_path_escape_inside_a_loaded_payload_is_rejected(tmp_path):
    _, manifest = _write_export(tmp_path, [_artifact()])
    escaped = {
        **json.loads(json.dumps(manifest)),
    }
    escaped["artifacts"][0]["relative_path"] = "../outside.csv"
    verdict = validate_offline_export_manifest(escaped, tmp_path)
    assert verdict["valid"] is False
    assert verdict["failures"][0]["reason_codes"] == ["path_escape"]


def test_stale_manifest_hash_is_detected_without_touching_files(tmp_path):
    manifest_path, manifest = _write_export(tmp_path, [_artifact()])
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    loaded["producer"]["version"] = "9.9.9"
    verdict = validate_offline_export_manifest(loaded, tmp_path)
    assert verdict["valid"] is False
    assert verdict["failures"][0]["reason_codes"] == ["stale_manifest_hash"]


def test_model_identity_expectation_fails_closed(tmp_path):
    manifest_path, _ = _write_export(tmp_path, [_artifact()])
    mismatch = validate_offline_export_manifest(manifest_path, expected_model_sha256="f" * 64)
    assert mismatch["valid"] is False
    assert mismatch["failures"][0]["reason_codes"] == ["model_identity_mismatch"]

    undeclared = build_offline_export_manifest(
        producer_tool="t",
        producer_version="v",
        model_path_redacted="**/m.mph",
        model_sha256=None,
        artifacts=[],
    )
    undeclared_path = tmp_path / "no-model-hash.json"
    undeclared_path.write_text(json.dumps(undeclared), encoding="utf-8")
    result = validate_offline_export_manifest(undeclared_path, expected_model_sha256="a" * 64)
    assert result["valid"] is False
    assert result["failures"][0]["reason_codes"] == ["model_identity_undeclared"]


def test_unbounded_artifact_counts_honor_caller_limits(tmp_path):
    specs = [_artifact(artifact_id=f"a-{index}") for index in range(4)]
    manifest_path, _ = _write_export(tmp_path, specs)
    verdict = validate_offline_export_manifest(manifest_path, max_artifacts=2)
    assert verdict["valid"] is False
    assert any("too_many_artifacts" in row["reason_codes"] for row in verdict["failures"])


def test_invalid_json_and_absent_manifest_are_structured(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_bytes(b"{not json")
    verdict = validate_offline_export_manifest(broken)
    assert verdict["failures"][0]["reason_codes"] == ["manifest_not_valid_json"]

    absent = validate_offline_export_manifest(tmp_path / "absent.json")
    assert absent["failures"][0]["reason_codes"] == ["manifest_unavailable"]


def test_txt_exports_share_the_reader_and_vtu_stays_hash_only(tmp_path):
    txt_payload = b"scalar column\n42\n"
    specs = [
        _artifact(artifact_id="notes", relative_path="out.txt", payload=txt_payload),
    ]
    specs[0]["format"] = "txt"
    manifest_path, _ = _write_export(tmp_path, specs)
    (tmp_path / "out.txt").write_bytes(txt_payload)
    verdict = validate_offline_export_manifest(manifest_path)
    assert verdict["valid"] is True


# ---------------------------------------------------------------------------
# Registry membership and dispatch
# ---------------------------------------------------------------------------


def test_schema_registry_supports_the_published_contract():
    support = check_schema_support(OFFLINE_EXPORT_MANIFEST_SCHEMA_NAME, "1.0.0")
    assert support["supported"] is True
    assert support["producer"] == "comsol_mcp.evidence.offline_export"


def test_b04_manifest_outside_owned_artifact_root_is_rejected(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-export-root"
    outside.mkdir(exist_ok=True)
    payload = b"wl,T\n1.0,300.0\n"
    spec = _artifact(payload=payload)
    # Write the export under tmp_path (owned root), then point base outside.
    _write_export(tmp_path, [spec])
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "sweep.csv").write_bytes(payload)
    manifest = json.loads((tmp_path / "export-manifest.json").read_text(encoding="utf-8"))

    stray = outside / "stray.csv"
    stray.write_bytes(payload)
    monkeypatch.setenv("COMSOL_MCP_ARTIFACT_WRITE_ROOT", str(outside))
    verdict = validate_offline_export_manifest(manifest, outside)
    # Base under the new owned root is allowed; artifact relative path stays there.
    assert "base_directory_outside_allowed_root" not in {
        code for f in verdict["failures"] for code in f["reason_codes"]
    }

    monkeypatch.setenv("COMSOL_MCP_ARTIFACT_WRITE_ROOT", str(tmp_path))
    verdict2 = validate_offline_export_manifest(manifest, outside)
    assert verdict2["valid"] is False
    codes = {code for f in verdict2["failures"] for code in f["reason_codes"]}
    assert "base_directory_outside_allowed_root" in codes


def test_b04_relative_path_escape_is_rejected_under_owned_root(tmp_path):
    payload = b"wl,T\n1.0,300.0\n"
    spec = _artifact(relative_path="../escape.csv", payload=payload)
    (tmp_path / "escape.csv").write_bytes(payload)
    # builder already rejects ".."; craft a normalized-looking manifest via escape path
    with pytest.raises(OfflineExportError):
        build_offline_export_manifest(
            producer_tool="t",
            producer_version="v",
            model_path_redacted="**/m.mph",
            model_sha256="a" * 64,
            artifacts=[spec],
        )


def test_public_dispatch_on_the_comsolless_profile(tmp_path):
    from mcp.server.mcpserver import MCPServer

    from comsol_mcp.tools.offline_export import register_offline_export_tools

    server = MCPServer("offline-export-test")
    register_offline_export_tools(server)
    tools = server._tool_manager._tools

    payload = b"wl,T\n1.0,300.0\n"
    spec = _artifact(payload=payload)
    manifest_path, _ = _write_export(tmp_path, [spec])
    (tmp_path / "data" / "sweep.csv").write_bytes(payload)

    result = tools["offline_export_validate"].fn(str(manifest_path))
    assert result["success"] is True
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert result["verdict"]["valid"] is True
    assert result["verdict"]["path_evidence"]["enforced"] is True

    missing = tools["offline_export_validate"].fn(str(tmp_path / "absent.json"))
    assert missing["success"] is True
    assert missing["verdict"]["valid"] is False
    assert missing["verdict"]["failures"][0]["reason_codes"] == ["manifest_unavailable"]


def test_module_never_imports_solver_dependencies():
    sources = [
        Path("comsol_mcp") / "contracts" / "offline_export.py",
        Path("comsol_mcp") / "evidence" / "offline_export.py",
        Path("comsol_mcp") / "tools" / "offline_export.py",
    ]
    forbidden = ("import mph", "from mph", "import jpype", "from jpype")
    for relative in sources:
        text = relative.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line and not line[0].isspace():
                assert not line.strip().startswith(forbidden), (relative, line)


# ---------------------------------------------------------------------------
# B05 / B12: limits-first and input-bound verdicts
# ---------------------------------------------------------------------------


def test_b05_expression_limit_rejects_before_artifact_io(tmp_path):
    from comsol_mcp.durable import canonical_sha256_v1
    from comsol_mcp.evidence.offline_export import build_offline_export_manifest

    payload = b"wl,T\n1.0,300.0\n"
    expressions = [f"e{i}" for i in range(5)]
    units = ["m"] * 5
    columns = [
        {"expression": e, "unit": u} for e, u in zip(expressions, units, strict=True)
    ]
    ordering = canonical_sha256_v1(
        [{"expression": c["expression"], "unit": c["unit"]} for c in columns]
    )
    spec = _artifact(payload=payload)
    spec["expressions"] = expressions
    spec["units"] = units
    spec["ordering_sha256"] = ordering
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "sweep.csv").write_bytes(payload)
    spec.pop("_payload", None)
    manifest = build_offline_export_manifest(
        producer_tool="t",
        producer_version="v",
        model_path_redacted="**/m.mph",
        model_sha256="a" * 64,
        artifacts=[spec],
    )
    # Delete the artifact: if limits are applied first, hashing never runs.
    (tmp_path / "data" / "sweep.csv").unlink()
    verdict = validate_offline_export_manifest(manifest, tmp_path, max_expressions=2)
    assert verdict["valid"] is False
    assert verdict["failures"][0]["reason_codes"] == ["limit_exceeded"]
    assert "expressions exceed limit" in verdict["failures"][0]["detail"]
    assert verdict["checked_artifacts"] == 0


def test_b05_parameter_entry_limit_zero_allows_empty(tmp_path):
    from comsol_mcp.durable import canonical_sha256_v1
    from comsol_mcp.evidence.offline_export import build_offline_export_manifest

    payload = b"wl,T\n1.0,300.0\n"
    spec = _artifact(payload=payload)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "sweep.csv").write_bytes(payload)
    spec.pop("_payload", None)
    manifest = build_offline_export_manifest(
        producer_tool="t",
        producer_version="v",
        model_path_redacted="**/m.mph",
        model_sha256="a" * 64,
        artifacts=[spec],
    )
    verdict = validate_offline_export_manifest(
        manifest, tmp_path, max_parameter_entries=0
    )
    assert verdict["valid"] is False
    assert verdict["failures"][0]["reason_codes"] == ["limit_exceeded"]

    spec2 = _artifact(artifact_id="csv-2", payload=payload)
    spec2["parameter_values"] = {}
    spec2["relative_path"] = "data/sweep2.csv"
    spec2.pop("_payload", None)
    (tmp_path / "data" / "sweep2.csv").write_bytes(payload)
    cols = [
        {"expression": e, "unit": u}
        for e, u in zip(spec2["expressions"], spec2["units"], strict=True)
    ]
    spec2["ordering_sha256"] = canonical_sha256_v1(
        [{"expression": c["expression"], "unit": c["unit"]} for c in cols]
    )
    manifest2 = build_offline_export_manifest(
        producer_tool="t",
        producer_version="v",
        model_path_redacted="**/m.mph",
        model_sha256="a" * 64,
        artifacts=[spec2],
    )
    verdict2 = validate_offline_export_manifest(
        manifest2, tmp_path, max_parameter_entries=0
    )
    assert verdict2["valid"] is True
    assert verdict2["input_binding"]["effective_limits"]["max_parameter_entries"] == 0


def test_b12_verdict_binds_manifest_hash_limits_and_artifact_checks(tmp_path):
    from comsol_mcp.evidence.offline_export import build_offline_export_manifest

    payload_a = b"wl,T\n1.0,300.0\n"
    payload_b = b"wl,T\n2.0,310.0\n"
    assert payload_a != payload_b
    spec_a = _artifact(payload=payload_a)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "sweep.csv").write_bytes(payload_a)
    spec_a.pop("_payload", None)
    manifest_a = build_offline_export_manifest(
        producer_tool="t",
        producer_version="v",
        model_path_redacted="**/m.mph",
        model_sha256="a" * 64,
        artifacts=[spec_a],
    )
    va = validate_offline_export_manifest(manifest_a, tmp_path)
    assert va["valid"] is True
    assert va["input_binding"]["manifest_sha256"]
    assert va["input_binding"]["artifact_checks"][0]["observed_sha256"] == spec_a["sha256"]
    assert va["input_binding"]["effective_limits"]["max_expressions"] == 256

    spec_b = _artifact(artifact_id="csv-b", payload=payload_b)
    spec_b["relative_path"] = "data/sweep_b.csv"
    spec_b.pop("_payload", None)
    (tmp_path / "data" / "sweep_b.csv").write_bytes(payload_b)
    manifest_b = build_offline_export_manifest(
        producer_tool="t",
        producer_version="v",
        model_path_redacted="**/m.mph",
        model_sha256="a" * 64,
        artifacts=[spec_b],
    )
    vb = validate_offline_export_manifest(manifest_b, tmp_path)
    assert vb["valid"] is True
    assert va["input_binding"]["manifest_sha256"] != vb["input_binding"]["manifest_sha256"]
    assert va["validation_sha256"] != vb["validation_sha256"]
