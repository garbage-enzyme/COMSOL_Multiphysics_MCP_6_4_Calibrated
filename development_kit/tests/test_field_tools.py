from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from src.evidence.field_bundle import normalize_field_evidence_request
from src.tools.field_evidence import register_field_evidence_tools

from development_kit.tests.test_field_bundle import _request
from development_kit.tests.test_field_dataset import _Model as _DatasetModel


class _Node:
    def __init__(self, name, tag, kind=None, properties=(), solution=None, empty=False):
        self._name = name
        self._tag = tag
        self._kind = kind
        self._properties = properties
        self._solution = solution
        self.java = type("JavaNode", (), {"isEmpty": lambda _self: empty})()

    def name(self):
        return self._name

    def tag(self):
        return self._tag

    def type(self):
        return self._kind

    def properties(self):
        return list(self._properties)

    def property(self, name):
        if name != "solution":
            raise KeyError(name)
        return self._solution


class _Model:
    def __truediv__(self, group):
        return {
            "components": [_Node("Component 1", "comp1")],
            "solutions": [_Node("Solution 1", "sol1", empty=False)],
            "datasets": [
                _Node(
                    "研究 1//解 1",
                    "dset1",
                    kind="Solution",
                    properties=("solution",),
                    solution="sol1",
                )
            ],
        }[group]


def _tool(name="wave_optics_field_datasets"):
    server = MCPServer("field-evidence-tools-test")
    register_field_evidence_tools(server)
    return server._tool_manager._tools[name].fn


def test_public_field_dataset_discovery_is_locale_safe_and_read_only(monkeypatch):
    from src.tools import field_evidence

    model = _Model()
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: model)
    monkeypatch.setattr(
        field_evidence.session_manager, "preflight_long_operation", lambda: {"ready": True}
    )

    result = _tool()(model_name="fixture", max_datasets=4, max_components=2)

    assert result["success"] is True, result
    assert result["datasets"][0]["dataset_name"] == "研究 1//解 1"
    assert result["datasets"][0]["dataset_tag"] == "dset1"
    assert result["datasets"][0]["field_evaluation_eligible"] is True
    assert result["ownership_checked"] is True
    assert result["solver_started_by_tool"] is False
    assert result["study_run"] is False
    assert result["model_mutated"] is False


def test_public_field_dataset_discovery_fails_closed_on_incomplete_ownership(monkeypatch):
    from src.tools import field_evidence

    monkeypatch.setattr(
        field_evidence.session_manager,
        "get_model",
        lambda _name: pytest.fail("failed preflight must precede model lookup"),
    )
    monkeypatch.setattr(
        field_evidence.session_manager,
        "preflight_long_operation",
        lambda: {"ready": False, "blockers": ["solver collision"]},
    )

    result = _tool()(model_name="fixture")

    assert result["success"] is False
    assert result["blockers"] == ["solver collision"]


def test_public_field_dataset_discovery_rejects_missing_model_and_limits(monkeypatch):
    from src.tools import field_evidence

    monkeypatch.setattr(
        field_evidence.session_manager,
        "preflight_long_operation",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: None)
    assert _tool()(model_name="missing") == {
        "success": False,
        "error": "Model not found: missing",
    }

    monkeypatch.setattr(
        field_evidence.session_manager,
        "get_model",
        lambda _name: pytest.fail("invalid limits must precede model lookup"),
    )
    monkeypatch.setattr(
        field_evidence.session_manager,
        "preflight_long_operation",
        lambda: pytest.fail("invalid limits must precede ownership preflight"),
    )
    invalid = _tool()(model_name="fixture", max_datasets=0)
    assert invalid["success"] is False
    assert "max_datasets" in invalid["error"]


def _extraction_request(source: Path):
    raw = _request(paired=False, png=False)
    raw["views"][0]["source"] = {
        "kind": "existing_dataset",
        "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "component_tag": "comp1",
        "dataset_name": "研究 1//解 1",
        "dataset_tag": "dset_on",
        "solution_tag": "sol_on",
        "solution_number": 1,
    }
    raw["grid"]["shape"] = [9, 11]
    raw["limits"]["max_grid_points"] = 200
    return raw


@pytest.mark.parametrize("canonical_transport", [False, True])
def test_public_field_extract_binds_source_and_owned_runtime(
    tmp_path, ascii_tmp_path, monkeypatch, canonical_transport
):
    from src.tools import field_evidence

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable-mph-fixture")
    source_before = source.read_bytes()
    raw_request = _extraction_request(source)
    canonical_request = normalize_field_evidence_request(raw_request)
    request = canonical_request if canonical_transport else raw_request
    model = _DatasetModel()
    model.file = lambda: str(source)
    runtime = ascii_tmp_path / "runtime"
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: model)
    monkeypatch.setattr(
        field_evidence.session_manager, "preflight_long_operation", lambda: {"ready": True}
    )
    monkeypatch.setattr(field_evidence.ownership_manager, "runtime_dir", runtime)

    result = _tool("wave_optics_field_extract")(
        model_name="fixture",
        request=request,
        view_id="on",
    )

    assert result["success"] is True, result
    assert result["source_unchanged"] is True
    assert source.read_bytes() == source_before
    assert result["study_run"] is False
    assert result["model_mutated"] is False
    assert result["artifact_root_id"] == (
        f"field_evidence/{canonical_request['request_fingerprint']}"
    )
    root = runtime / Path(result["artifact_root_id"])
    for artifact in (result["array_artifact"], result["manifest_artifact"]):
        relative = Path(artifact["relative_path"])
        resolved = (root / relative).resolve(strict=True)
        assert not relative.is_absolute()
        assert resolved.is_relative_to(root.resolve())
        assert resolved.is_file()


def test_public_field_extract_rejects_source_mismatch_before_evaluation(tmp_path, monkeypatch):
    from src.tools import field_evidence

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable-mph-fixture")
    raw = _request(paired=False, png=False)
    raw["views"][0]["source"] = {
        "kind": "existing_dataset",
        "source_model_sha256": "0" * 64,
        "component_tag": "comp1",
        "dataset_name": "研究 1//解 1",
        "dataset_tag": "dset_on",
        "solution_tag": "sol_on",
        "solution_number": 1,
    }
    raw["grid"]["shape"] = [9, 11]
    raw["limits"]["max_grid_points"] = 200
    request = normalize_field_evidence_request(raw)
    model = _DatasetModel()
    model.file = lambda: str(source)
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: model)
    monkeypatch.setattr(
        field_evidence.session_manager, "preflight_long_operation", lambda: {"ready": True}
    )

    result = _tool("wave_optics_field_extract")(
        model_name="fixture",
        request=request,
        view_id="on",
    )

    assert result["success"] is False
    assert "loaded source SHA-256 does not match" in result["error"]
    assert model.calls == []


def test_public_field_extract_rehashes_source_after_collection_failure(
    tmp_path, ascii_tmp_path, monkeypatch
):
    from src.tools import field_evidence

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable-mph-fixture")
    request = normalize_field_evidence_request(_extraction_request(source))
    model = _DatasetModel()
    model.file = lambda: str(source)
    runtime = ascii_tmp_path / "runtime"
    real_hash = field_evidence._sha256_file
    hash_calls = []

    def observed_hash(path):
        hash_calls.append(Path(path))
        return real_hash(path)

    def fail_collection(**_kwargs):
        source.write_bytes(b"mutated-during-failed-collection")
        raise RuntimeError("injected collection failure")

    monkeypatch.setattr(field_evidence, "_sha256_file", observed_hash)
    monkeypatch.setattr(field_evidence, "collect_existing_dataset_field_evidence", fail_collection)
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda _name: model)
    monkeypatch.setattr(
        field_evidence.session_manager, "preflight_long_operation", lambda: {"ready": True}
    )
    monkeypatch.setattr(field_evidence.ownership_manager, "runtime_dir", runtime)

    result = _tool("wave_optics_field_extract")(model_name="fixture", request=request, view_id="on")

    assert result["success"] is False
    assert result["reason_code"] == "field_extraction_failed"
    assert hash_calls == [source.resolve(), source.resolve()]


def test_public_field_extract_never_publishes_artifacts_after_source_drift(
    tmp_path, ascii_tmp_path, monkeypatch
):
    from src.tools import field_evidence

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable-mph-fixture")
    request = normalize_field_evidence_request(_extraction_request(source))
    model = _DatasetModel()
    model.file = lambda: str(source)
    runtime = ascii_tmp_path / "runtime"

    def collect_then_drift(**kwargs):
        root = Path(kwargs["artifact_root"])
        (root / "arrays").mkdir(parents=True)
        (root / "arrays" / "field.npz").write_bytes(b"npz")
        (root / "manifest.json").write_text("{}", encoding="utf-8")
        source.write_bytes(b"mutated-after-artifact-write")
        return {
            "array_artifact": {"relative_path": "arrays/field.npz"},
            "manifest_artifact": {"relative_path": "manifest.json"},
        }

    monkeypatch.setattr(
        field_evidence,
        "collect_existing_dataset_field_evidence",
        collect_then_drift,
    )
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda _name: model)
    monkeypatch.setattr(
        field_evidence.session_manager,
        "preflight_long_operation",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(field_evidence.ownership_manager, "runtime_dir", runtime)

    result = _tool("wave_optics_field_extract")(
        model_name="fixture", request=request, view_id="on"
    )

    final_root = runtime / "field_evidence" / request["request_fingerprint"]
    assert result["success"] is False
    assert result["reason_code"] == "field_extraction_failed"
    assert not final_root.exists()
    assert not list(final_root.parent.glob(f".{final_root.name}.stage-*"))


@pytest.mark.parametrize("tamper", ["fingerprint", "schema"])
def test_public_field_extract_rejects_tampered_canonical_request(tmp_path, monkeypatch, tamper):
    from src.tools import field_evidence

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable-mph-fixture")
    request = normalize_field_evidence_request(_extraction_request(source))
    if tamper == "fingerprint":
        request["request_fingerprint"] = "0" * 64
    else:
        request.pop("schema_name")
    model = _DatasetModel()
    model.file = lambda: str(source)
    monkeypatch.setattr(field_evidence.session_manager, "get_model", lambda name: model)

    result = _tool("wave_optics_field_extract")(model_name="fixture", request=request, view_id="on")

    assert result["success"] is False
    assert model.calls == []


def test_public_field_extract_rejects_png_and_matrix_sources(tmp_path, monkeypatch):
    from src.tools import field_evidence

    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable-mph-fixture")
    monkeypatch.setattr(
        field_evidence.session_manager,
        "get_model",
        lambda _name: pytest.fail("unsupported mode must precede model lookup"),
    )

    png_raw = _request(paired=False, png=True)
    source_value = dict(
        normalize_field_evidence_request(_extraction_request(source))["views"][0]["source"]
    )
    source_value.pop("source_fingerprint")
    png_raw["views"][0]["source"] = source_value
    png_request = normalize_field_evidence_request(png_raw)
    png_result = _tool("wave_optics_field_extract")(
        model_name="fixture", request=png_request, view_id="on"
    )
    assert png_result["success"] is False
    assert "PNG rendering is not yet public" in png_result["error"]

    matrix_raw = _request(paired=False, png=False)
    matrix_raw["views"][0]["source"]["source_model_sha256"] = hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    matrix_request = normalize_field_evidence_request(matrix_raw)
    matrix_result = _tool("wave_optics_field_extract")(
        model_name="fixture", request=matrix_request, view_id="on"
    )
    assert matrix_result["success"] is False
    assert "validation-matrix source" in matrix_result["error"]
