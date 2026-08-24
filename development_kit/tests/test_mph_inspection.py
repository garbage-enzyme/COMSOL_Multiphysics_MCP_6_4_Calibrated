"""Solver-free contract suite for offline MPH archive inspection."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from comsol_mcp.contracts.mph_inspection import MphInspectionLimits
from comsol_mcp.evidence.inspection.archive import (
    MphInspectionError,
    inspect_archive_inventory,
    size_breakdown,
)
from comsol_mcp.evidence.inspection.summary import build_mph_inspection_summary
from comsol_mcp.tools.mph_inspection import register_mph_inspection_tools

_FILEVERSION_TEMPLATE = "{marker}:COMSOL {version}\n"

_MODELINFO_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<modelInfo comsolVersion="{version}" modelType="MODEL" nodeType="{node_type}"
 isRunnable="{runnable}" title="{title}" description="" startMode="edit"
 lastComputationTime="" lastComputationDate="">
  <historyInfo createdIn="COMSOL Multiphysics {version}" author=""/>
  <licenseInfo products="COMSOL##WAVEOPTICS"/>
  <physicsInfo physics="Electromagnetic_waves_frequency_domain"/>
  <geometryInfo>
    <geom tag="geom1" dimension="3"/>
  </geometryInfo>
</modelInfo>
"""

_DMODEL_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Model>
  <ModelParam>{params}</ModelParam>
  <PhysicsList><Physics tag="ewfd" op="ElectromagneticWavesFrequencyDomain" name="ewfd"/></PhysicsList>
  <MaterialList><Material tag="m1" op="Common" name="mat"/></MaterialList>
  <StudyList><Study tag="std1" name="Study 1"/></StudyList>
  <SolverSequenceList><SolverSequence tag="sol1" name="Solution 1"/></SolverSequenceList>
  <GeomList><Geom tag="geom1"/></GeomList>
  <MeshList><Mesh tag="mesh1"/></MeshList>
</Model>
"""

_FILEIDS_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<FileIDs>
  <SavePoint tag="savepoint1" fileid="a6e1a8f360ecb2a4" purgeable="true"/>
  <BinaryResource file="mesh1.mphbin" fileid="23a47fc5c59f51bc" binarytype="MESH" purgeable="true"/>
</FileIDs>
"""


def _write_valid_mph(
    path: Path,
    *,
    version: str = "6.4.0.293",
    marker: int = 2092,
    node_type: str = "solved",
    runnable: str = "false",
    title: str = "fixture",
    params: tuple[tuple[str, str], ...] = (("wl", "1.0[um]"),),
    with_savepoint: bool = True,
    include_dmodel: bool = True,
) -> Path:
    rendered_params = "".join(
        f'<expressions name="{name}" expr="{expr}"/>' for name, expr in params
    )
    payload = {
        "fileversion": _FILEVERSION_TEMPLATE.format(marker=marker, version=version).encode(),
        "modelinfo.xml": _MODELINFO_TEMPLATE.format(
            version=version,
            node_type=node_type,
            runnable=runnable,
            title=title,
        ).encode(),
        "usedlicenses.txt": b"COMSOL\nWAVEOPTICS\n",
        "fileids.xml": _FILEIDS_TEMPLATE.encode(),
        "dmodel.xml": _DMODEL_TEMPLATE.format(params=rendered_params).encode(),
        "mesh1.mphbin": b"\x00" * 32,
    }
    if with_savepoint:
        payload["savepoint1/savepoint.xml"] = b"<savepoint/>"
    if not include_dmodel:
        payload.pop("dmodel.xml")
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(payload):
            archive.writestr(name, payload[name])
    return path


def _tools() -> dict:
    server = MCPServer("mph-inspection-test")
    register_mph_inspection_tools(server)
    return server._tool_manager._tools


def _refuse(path: Path, reason_code: str, limits: MphInspectionLimits | None = None) -> None:
    with pytest.raises(MphInspectionError) as excinfo:
        build_mph_inspection_summary(path, limits)
    assert excinfo.value.reason_code == reason_code


def test_compact_metadata_archive_produces_a_complete_summary(tmp_path):
    fixture = _write_valid_mph(tmp_path / "compact.mph")
    first = build_mph_inspection_summary(fixture)
    second = build_mph_inspection_summary(fixture)

    assert first["schema_name"] == "comsol_mcp.mph_inspection_summary"
    assert first["schema_version"] == "1.0.0"
    assert first["format_family"] == "mph_zip"
    assert first["comsol_version"] == "6.4.0.293"
    assert first["schema_marker"] == "2092"
    assert first["node_type"] == "MODEL"
    assert first["solved_state"] == "solved"
    assert first["runnable_state"] == "false"
    assert first["preview_state"] == "savepoint_present"
    assert first["title"] == "fixture"
    assert first["model_tags"] == ["Model"]
    assert first["parameter_summary"]["parameters"] == [
        {"name": "wl", "expression": "1.0[um]"}
    ]
    assert [row["tag"] for row in first["physics_tags"]] == ["ewfd"]
    assert [row["tag"] for row in first["study_tags"]] == ["std1"]
    assert [row["tag"] for row in first["material_tags"]] == ["m1"]
    assert [row["tag"] for row in first["solution_tags"]] == ["sol1"]
    assert first["geometry_summary"]["geoms"] == [{"tag": "geom1", "dimension": 3}]
    assert first["mesh_summary"]["binary_resources"] == [
        {"file": "mesh1.mphbin", "binarytype": "MESH"}
    ]
    assert first["savepoint_summary"]["present"] is True
    assert first["zip_valid"] is True
    assert first["warnings"] == []
    assert first["inspection_fingerprint"]
    assert first == second


def test_fingerprint_changes_when_declared_content_changes(tmp_path):
    baseline = _write_valid_mph(tmp_path / "base.mph")
    changed = _write_valid_mph(
        tmp_path / "changed.mph", params=(("wl", "1.55[um]"),)
    )
    first = build_mph_inspection_summary(baseline)
    second = build_mph_inspection_summary(changed)
    assert first["inspection_fingerprint"] != second["inspection_fingerprint"]


def test_no_savepoint_archives_report_the_absent_preview_state(tmp_path):
    fixture = _write_valid_mph(tmp_path / "plain.mph", with_savepoint=False)
    summary = build_mph_inspection_summary(fixture)
    assert summary["preview_state"] == "no_savepoint"
    assert summary["savepoint_summary"]["present"] is False


def test_truncated_zip_is_refused_without_guessing(tmp_path):
    fixture = _write_valid_mph(tmp_path / "truncated.mph")
    raw = fixture.read_bytes()
    fixture.write_bytes(raw[: len(raw) - 64])
    _refuse(fixture, "mph_invalid_zip")


def test_corrupt_central_directory_is_refused(tmp_path):
    fixture = _write_valid_mph(tmp_path / "central.mph")
    raw = bytearray(fixture.read_bytes())
    raw[-96:-32] = b"\x00" * 64
    fixture.write_bytes(bytes(raw))
    _refuse(fixture, "mph_invalid_zip")


def test_case_folded_duplicate_entry_names_are_refused(tmp_path):
    fixture = tmp_path / "duplicate.mph"
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("a/One.txt", b"x")
        archive.writestr("A/ONE.TXT", b"y")
    _refuse(fixture, "mph_duplicate_entry_name")


@pytest.mark.parametrize(
    "member",
    ["../escape.xml", "/absolute.xml", "C:/drive.xml"],
)
def test_unsafe_entry_paths_are_refused(tmp_path, member):
    fixture = tmp_path / "unsafe.mph"
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr(member, b"x")
    _refuse(fixture, "mph_entry_path_unsafe")


def test_backslash_and_encrypted_metadata_are_refused_by_the_entry_guard():
    from types import SimpleNamespace

    from comsol_mcp.evidence.inspection.archive import reject_entry_metadata

    with pytest.raises(MphInspectionError) as backslash:
        reject_entry_metadata(SimpleNamespace(filename="back\\slash.xml"))
    assert backslash.value.reason_code == "mph_entry_path_unsafe"

    encrypted = SimpleNamespace(
        filename="secret.xml",
        external_attr=0,
        flag_bits=0x1,
    )
    with pytest.raises(MphInspectionError) as flag:
        # CPython's ZIP writer normalizes this declared bit away on write, so
        # the guard is exercised directly against the raw metadata contract.
        reject_entry_metadata(encrypted)
    assert flag.value.reason_code == "mph_encrypted_entry"


def test_symlink_like_entries_are_refused(tmp_path):
    fixture = tmp_path / "link.mph"
    with zipfile.ZipFile(fixture, "w") as archive:
        info = zipfile.ZipInfo("linked.xml")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "target")
    _refuse(fixture, "mph_symlink_like_entry")


def test_oversized_entries_honor_caller_limits(tmp_path):
    fixture = tmp_path / "oversized.mph"
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("big.bin", b"x" * 128)
    limits = MphInspectionLimits(max_entry_uncompressed_bytes=16)
    _refuse(fixture, "mph_oversized_entry", limits)


def test_excessive_compression_ratio_guard_uses_caller_limit(tmp_path):
    fixture = tmp_path / "ratio.mph"
    with zipfile.ZipFile(fixture, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("zeros.bin", b"\x00" * 20_000)
    limits = MphInspectionLimits(max_compression_ratio=4)
    _refuse(fixture, "mph_excessive_compression_ratio", limits)


def test_entry_count_limits_are_enforced(tmp_path):
    fixture = tmp_path / "many.mph"
    with zipfile.ZipFile(fixture, "w") as archive:
        for index in range(6):
            archive.writestr(f"part{index}.txt", b"x")
    limits = MphInspectionLimits(max_entries=3)
    _refuse(fixture, "mph_too_many_entries", limits)


def test_missing_required_markers_fail_closed_as_unsupported(tmp_path):
    fixture = _write_valid_mph(tmp_path / "nodmodel.mph", include_dmodel=False)
    _refuse(fixture, "mph_unsupported_format")

    plain_zip = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain_zip, "w") as archive:
        archive.writestr("readme.txt", b"not a model")
    _refuse(plain_zip, "mph_unsupported_format")


@pytest.mark.parametrize(
    "marker_text",
    [b"", b"nonsense\n", b"2092:COMSOL six.four\n", b"2092:COMSOL 6.4.0.293\nextra\n"],
)
def test_ambiguous_version_markers_are_refused(tmp_path, marker_text):
    fixture = _write_valid_mph(tmp_path / "version.mph")
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("fileversion", marker_text)
        archive.writestr("modelinfo.xml", b"<modelInfo/>")
    _refuse(fixture, "mph_version_marker_ambiguous")


def test_conflicting_declared_versions_are_refused(tmp_path):
    fixture = _write_valid_mph(tmp_path / "conflict.mph", version="6.4.0.293")
    replacement = _FILEVERSION_TEMPLATE.format(marker=2092, version="6.3.0.180").encode()
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("fileversion", replacement)
        archive.writestr(
            "modelinfo.xml",
            _MODELINFO_TEMPLATE.format(
                version="6.4.0.293", node_type="solved", runnable="false", title="t"
            ).encode(),
        )
        archive.writestr("dmodel.xml", b"<Model/>")
    _refuse(fixture, "mph_conflicting_version_markers")


def test_malformed_marker_xml_is_refused(tmp_path):
    fixture = _write_valid_mph(tmp_path / "brokenxml.mph")
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("fileversion", b"2092:COMSOL 6.4.0.293\n")
        archive.writestr("modelinfo.xml", b"<modelInfo><unclosed>")
        archive.writestr("dmodel.xml", b"<Model/>")
    _refuse(fixture, "mph_marker_xml_invalid")


@pytest.mark.parametrize(
    ("member", "payload"),
    [
        ("modelinfo.xml", b"<!DOCTYPE modelInfo [<!ENTITY a \"b\">]><modelInfo>&a;</modelInfo>"),
        ("dmodel.xml", b"<?xml version=\"1.0\"?><!ENTITY x SYSTEM \"file.xml\"><Model/>"),
    ],
)
def test_dtd_or_entity_markers_are_refused(tmp_path, member, payload):
    fixture = _write_valid_mph(tmp_path / "dtd.mph")
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("fileversion", b"2092:COMSOL 6.4.0.293\n")
        if member != "modelinfo.xml":
            archive.writestr(
                "modelinfo.xml",
                _MODELINFO_TEMPLATE.format(
                    version="6.4.0.293", node_type="solved", runnable="false", title="t"
                ).encode(),
            )
        archive.writestr(member, payload)
    _refuse(fixture, "mph_marker_dtd_rejected")


def test_unknown_runnable_state_is_refused(tmp_path):
    fixture = _write_valid_mph(tmp_path / "runnable.mph", runnable="maybe")
    _refuse(fixture, "mph_runnable_state_ambiguous")


def test_non_utf8_marker_is_refused(tmp_path):
    fixture = _write_valid_mph(tmp_path / "encoding.mph")
    with zipfile.ZipFile(fixture, "w") as archive:
        archive.writestr("fileversion", b"2092:COMSOL 6.4.0.293\n\xff\xfe")
        archive.writestr("modelinfo.xml", b"<modelInfo/>")
        archive.writestr("dmodel.xml", b"<Model/>")
    _refuse(fixture, "mph_marker_encoding_invalid")


def test_non_ascii_external_name_stays_redacted_inside_an_ascii_root(tmp_path):
    fixture = _write_valid_mph(tmp_path / "\u6a21\u578b.mph")
    summary = build_mph_inspection_summary(fixture)
    serialized = json.dumps(summary, ensure_ascii=False)
    assert summary["source_path_redacted"] == "**/\u6a21\u578b.mph"
    assert str(tmp_path) not in serialized
    assert "\\" not in serialized


def test_size_breakdown_totals_match_inventory(tmp_path):
    fixture = _write_valid_mph(tmp_path / "sizes.mph")
    inventory = inspect_archive_inventory(fixture)
    breakdown = size_breakdown(inventory)
    assert breakdown["totals"]["entry_count"] == len(inventory.entries)
    assert breakdown["totals"]["uncompressed_bytes"] == inventory.total_uncompressed_bytes
    declared = sum(
        bucket["uncompressed_bytes"]
        for key, bucket in breakdown.items()
        if key != "totals"
    )
    assert declared <= breakdown["totals"]["uncompressed_bytes"]
    assert breakdown["binary_resource"]["entry_count"] >= 1


def test_public_dispatch_reports_success_and_structured_refusals(tmp_path):
    tools = _tools()
    fixture = _write_valid_mph(tmp_path / "dispatch.mph")
    result = tools["mph_inspect"].fn(str(fixture))
    assert result["success"] is True
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert result["summary"]["format_family"] == "mph_zip"

    missing = tools["mph_inspect"].fn(str(tmp_path / "absent.mph"))
    assert missing["success"] is False
    assert missing["reason_code"] == "mph_source_unavailable"
    assert missing["solver_started"] is False

    rejected = tools["mph_inspect"].fn(str(fixture), MphInspectionLimits(max_entries=1))
    assert rejected["success"] is False
    assert rejected["reason_code"] == "mph_too_many_entries"


def test_inspection_modules_never_import_solver_or_process_dependencies():
    sources = [
        Path("comsol_mcp") / "contracts" / "mph_inspection.py",
        Path("comsol_mcp") / "evidence" / "inspection" / "archive.py",
        Path("comsol_mcp") / "evidence" / "inspection" / "summary.py",
        Path("comsol_mcp") / "evidence" / "inspection" / "__init__.py",
        Path("comsol_mcp") / "tools" / "mph_inspection.py",
    ]
    forbidden_prefixes = (
        "import mph",
        "from mph",
        "import jpype",
        "from jpype",
        "import subprocess",
        "from comsol_mcp.tools.session import",
    )
    for relative in sources:
        text = relative.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith(forbidden_prefixes), (relative, stripped)
            assert "comsol_start(" not in stripped, (relative, stripped)
