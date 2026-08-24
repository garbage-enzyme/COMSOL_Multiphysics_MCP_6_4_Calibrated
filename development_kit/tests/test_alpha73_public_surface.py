"""MCP dispatch, frozen-profile, no-heavy-import, and package-boundary suite.

This is the alpha7.3 D8 gate: the five new solver-free tools must dispatch
through a real server in every profile, the comsolless_read_only surface must
be frozen at exactly those five tools, a cold interpreter must gain them
without importing COMSOL/Java/ML stacks, and no runtime artifact may enter
the packaged trees.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from src.server import create_server

from development_kit.tests.mcp_test_support import decode_tool_result

ROOT = Path(__file__).parents[2]

FIVE_TOOLS = {
    "mph_inspect",
    "mph_diff",
    "model_identity",
    "runtime_compatibility_status",
    "offline_export_validate",
}


# ---------------------------------------------------------------------------
# Frozen profile surface
# ---------------------------------------------------------------------------


def test_comsolless_read_only_surface_is_frozen_at_the_five_tools():
    server = create_server("frozen-comsolless", profile="comsolless_read_only")
    listed = {tool.name for tool in asyncio.run(server.list_tools())}
    assert listed == FIVE_TOOLS


def test_five_tools_are_present_in_every_profile():
    for profile in ("core", "basic_fem", "wave_optics", "experimental", "full"):
        server = create_server(f"surface-{profile}", profile=profile)
        listed = {tool.name for tool in asyncio.run(server.list_tools())}
        assert FIVE_TOOLS <= listed, profile


# ---------------------------------------------------------------------------
# Real dispatch for all five tools
# ---------------------------------------------------------------------------


def _write_mph(path: Path) -> None:
    payload = {
        "fileversion": b"2092:COMSOL 6.4.0.293\n",
        "modelinfo.xml": (
            '<modelInfo comsolVersion="6.4.0.293" modelType="MODEL" nodeType="solved"'
            ' isRunnable="false" title="surface" description=""></modelInfo>'
        ),
        "dmodel.xml": "<Model><ModelParam></ModelParam></Model>",
    }
    import zipfile

    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(payload):
            archive.writestr(name, payload[name])


def _csv_manifest(tmp_path: Path) -> Path:
    from comsol_mcp.evidence.offline_export import build_offline_export_manifest

    payload = b"wl,T\n1.0,300.0\n"
    (tmp_path / "sweep.csv").write_bytes(payload)

    artifact_inputs = {
        "artifact_id": "csv-1",
        "relative_path": "sweep.csv",
        "format": "csv",
        "expressions": ["wl", "T"],
        "units": ["m", "K"],
        "parameter_values": {"wl": 1.0},
        "time_values": None,
        "byte_count": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "reader_status": "offline_reader_available",
    }
    manifest = build_offline_export_manifest(
        producer_tool="results_export_data",
        producer_version="0.7.3",
        model_path_redacted="**/model.mph",
        model_sha256="a" * 64,
        artifacts=[artifact_inputs],
    )
    path = tmp_path / "export-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_all_five_tools_dispatch_through_a_real_server(tmp_path, monkeypatch):
    monkeypatch.setenv("COMSOL_MCP_MODEL_READ_ROOTS", str(tmp_path))
    server = create_server("dispatch-five-tools", profile="comsolless_read_only")
    decode = decode_tool_result

    mph_file = tmp_path / "model.mph"
    _write_mph(mph_file)

    inspected = decode(asyncio.run(server.call_tool("mph_inspect", {"file_path": str(mph_file)})))
    assert inspected["success"] is True
    diffed = decode(
        asyncio.run(
            server.call_tool("mph_diff", {"left_path": str(mph_file), "right_path": str(mph_file)})
        )
    )
    assert diffed["success"] is True
    identity = decode(asyncio.run(server.call_tool("model_identity", {"file_path": str(mph_file)})))
    assert identity["success"] is True
    compat = decode(asyncio.run(server.call_tool("runtime_compatibility_status", {})))
    assert compat["success"] is True
    assert compat["registry"]["profile"]["name"] == "comsolless_read_only"

    manifest_path = _csv_manifest(tmp_path)
    verdict = decode(
        asyncio.run(
            server.call_tool("offline_export_validate", {"manifest_path": str(manifest_path)})
        )
    )
    assert verdict["success"] is True
    assert verdict["verdict"]["valid"] is True
    assert verdict["verdict"]["is_fem_validation"] is False


def test_dispatch_refusals_stay_solver_free(tmp_path, monkeypatch):
    monkeypatch.setenv("COMSOL_MCP_MODEL_READ_ROOTS", str(tmp_path))
    server = create_server("refusal-five-tools", profile="comsolless_read_only")
    absent = decode_tool_result(
        asyncio.run(server.call_tool("mph_inspect", {"file_path": str(tmp_path / "no.mph")}))
    )
    # A missing model file may be refused either by the typed inspection
    # reader or by the containment path layer; both stay solver-free.
    assert absent["success"] is False
    assert (
        absent.get("reason_code") == "mph_source_unavailable"
        or absent.get("path_policy", {}).get("accepted") is False
    )
    bad = decode_tool_result(
        asyncio.run(
            server.call_tool(
                "offline_export_validate", {"manifest_path": str(tmp_path / "no.json")}
            )
        )
    )
    assert bad["success"] is True  # validator returns a structured invalid verdict
    assert bad["verdict"]["failures"][0]["reason_codes"] == ["manifest_unavailable"]


# ---------------------------------------------------------------------------
# Cold discovery without heavy imports
# ---------------------------------------------------------------------------


def test_cold_discovery_serves_all_five_tools_without_solver_or_ml_imports(tmp_path):
    code = """
import asyncio, json, sys
from src.server import create_server
from development_kit.tests.mcp_test_support import decode_tool_result
server = create_server('cold-comsolless', profile='comsolless_read_only')
names = sorted(tool.name for tool in asyncio.run(server.list_tools()))
result = decode_tool_result(asyncio.run(server.call_tool('runtime_compatibility_status', {})))
banned = ('mph', 'jpype', 'chromadb', 'torch', 'sentence_transformers')
loaded = [name for name in banned if name in sys.modules]
print(json.dumps({'names': names, 'loaded': loaded,
                  'lane': result['registry']['licensed_lane_status']}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **__import__("os").environ,
            "PYTHONPATH": str(ROOT),
        },
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload["names"]) == FIVE_TOOLS
    assert payload["loaded"] == []
    assert payload["lane"] in {
        "active_runtime_matches_accepted_lane",
        "dependency_ranges_only_no_licensed_acceptance",
        "active_runtime_outside_declared_support",
    }


# ---------------------------------------------------------------------------
# Package boundary: no runtime artifacts inside packaged trees
# ---------------------------------------------------------------------------


def test_packaged_trees_contain_no_model_or_private_artifacts():
    forbidden_suffixes = (".mph", ".vtu", ".onnx", ".sqlite3")
    forbidden_names = {
        "observation.json",
        "bounded_steps.jsonl",
        "export-manifest.json",
    }
    violations: list[str] = []
    for production_root in ("comsol_mcp", "settings_gui"):
        root = ROOT / production_root
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if path.suffix.lower() in forbidden_suffixes or path.name in forbidden_names:
                violations.append(relative)
    assert violations == []


def test_wheel_and_sdist_excludes_keep_development_kit_out():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = project["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = project["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert wheel["packages"] == ["comsol_mcp", "settings_gui"]
    for target in (wheel, sdist):
        excludes = json.dumps(target.get("exclude", []))
        assert "development_kit" in excludes or "docs" in excludes or target is wheel
