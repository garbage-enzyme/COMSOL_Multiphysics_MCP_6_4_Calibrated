"""Unit tests for model management helpers without a COMSOL client."""

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from src.tools import model as model_module
from src.tools.model import (
    _clone_model,
    _list_model_components,
    _save_model_file,
    _save_model_version_bundle,
    create_model_component,
    register_model_tools,
)


class FakeJavaModel:
    def __init__(self):
        self.saved = []

    def save(self, file_path, copy=False):
        self.saved.append((file_path, copy))
        Path(file_path).write_bytes(b"saved-model")


class FakeModel:
    def __init__(self, current_file=None):
        self.java = FakeJavaModel()
        self.current_file = current_file
        self.high_level_saves = []

    def file(self):
        return self.current_file

    def save(self, path=None, format=None):
        self.high_level_saves.append((path, format))
        Path(path).write_bytes(b"saved-export")


def test_save_mph_uses_java_clientapi_for_unicode_path(tmp_path):
    model = FakeModel()
    requested = tmp_path / "中文目录" / "模型.mph"

    saved = _save_model_file(model, str(requested))

    assert saved == str(requested.resolve())
    assert len(model.java.saved) == 1
    assert Path(model.java.saved[0][0]).name.startswith(f".{requested.name}.")
    assert model.java.saved[0][1] is True
    assert model.high_level_saves == []
    assert requested.parent.is_dir()


def test_save_mph_uses_existing_model_file(tmp_path):
    current = tmp_path / "existing.mph"
    model = FakeModel(current_file=str(current))

    saved = _save_model_file(model)

    assert saved == str(current.resolve())
    assert len(model.java.saved) == 1
    assert current.read_bytes() == b"saved-model"


def test_save_source_export_keeps_mph_format_api(tmp_path):
    model = FakeModel()
    requested = tmp_path / "model.java"

    saved = _save_model_file(model, str(requested), format="Java")

    assert saved == str(requested)
    assert len(model.high_level_saves) == 1
    assert model.high_level_saves[0][1] == "Java"
    assert model.java.saved == []


def test_save_never_overwrites_a_target_created_during_native_staging(tmp_path):
    requested = tmp_path / "model.mph"

    class CompetingJava:
        def save(self, staging, copy):
            assert copy is True
            Path(staging).write_bytes(b"ours")
            requested.write_bytes(b"competitor")

    model = FakeModel()
    model.java = CompetingJava()

    with pytest.raises(FileExistsError):
        _save_model_file(model, str(requested))

    assert requested.read_bytes() == b"competitor"
    assert not list(tmp_path.glob(".*.save"))


class CloneJava:
    def __init__(self):
        self.saved = []
        self.model_label = None

    def save(self, path, copy):
        self.saved.append((path, copy))

    def label(self, value):
        self.model_label = value


class CloneModel:
    def __init__(self, name="Source"):
        self._name = name
        self.java = CloneJava()

    def name(self):
        return self._name


class CloneClient:
    def __init__(self, cloned):
        self.cloned = cloned
        self.loaded = []
        self.removed = []

    def load(self, path):
        self.loaded.append(path)
        return self.cloned

    def remove(self, model):
        self.removed.append(model)


def test_clone_model_uses_clientapi_save_copy_and_load(tmp_path):
    source = CloneModel()
    cloned = CloneModel("Loaded")
    client = CloneClient(cloned)

    clone_root = tmp_path / "model_clones"
    result, cleanup_path = _clone_model(
        client,
        source,
        "Independent Copy",
        clone_root=clone_root,
    )

    assert result is cloned
    assert source.java.saved[0][1] is True
    assert client.loaded == [source.java.saved[0][0]]
    assert cleanup_path == source.java.saved[0][0]
    assert Path(cleanup_path).parent.parent == clone_root
    assert cloned.java.model_label == "Independent Copy"
    Path(cleanup_path).parent.rmdir()


@pytest.mark.parametrize("failure", ["save", "load", "label"])
def test_clone_failures_remove_loaded_model_and_backing_artifacts(tmp_path, failure):
    source = CloneModel()
    cloned = CloneModel("Loaded")
    client = CloneClient(cloned)

    def save(path, copy):
        if failure == "save":
            raise RuntimeError("save failure")
        Path(path).write_bytes(b"clone")

    source.java.save = save
    if failure == "load":
        client.load = lambda _path: (_ for _ in ()).throw(RuntimeError("load failure"))
    if failure == "label":
        cloned.java.label = lambda _value: (_ for _ in ()).throw(
            RuntimeError("label failure")
        )

    root = tmp_path / "clones"
    with pytest.raises(RuntimeError, match=failure):
        _clone_model(client, source, "Copy", clone_root=root)

    assert client.removed == ([cloned] if failure == "label" else [])
    assert list(root.glob("comsol_mcp_clone_*")) == []


def test_clone_rejects_name_collision_before_save(tmp_path):
    source = CloneModel()
    client = CloneClient(CloneModel("Loaded"))

    with pytest.raises(ValueError, match="already exists"):
        _clone_model(
            client,
            source,
            "Existing",
            clone_root=tmp_path,
            existing_names={"Existing"},
        )

    assert source.java.saved == []


def test_model_clone_cleans_unregistered_clone_after_session_rejection(
    tmp_path, monkeypatch
):
    source = CloneModel("Source")
    cloned = CloneModel("Clone")
    client = CloneClient(cloned)
    backing_dir = tmp_path / "comsol_mcp_clone_failed_registration"
    backing_dir.mkdir()
    backing = backing_dir / "clone.mph"
    backing.write_bytes(b"clone")

    class Session:
        models = {}
        current_model = "Source"

        def __init__(self):
            self.client = client

        def get_model(self, name=None):
            return source if name in {None, "Source"} else None

        def add_model(self, _model, *, cleanup_path=None):
            assert cleanup_path == str(backing)
            raise ValueError("registration rejected")

        def remove_model(self, _name):
            return False

    monkeypatch.setattr(model_module, "session_manager", Session())
    monkeypatch.setattr(
        model_module,
        "_clone_model",
        lambda *_args, **_kwargs: (cloned, str(backing)),
    )
    server = MCPServer("model-clone-registration-failure-test")
    register_model_tools(server)

    result = server._tool_manager._tools["model_clone"].fn(model_name="Source")

    assert result["success"] is False
    assert "registration rejected" in result["error"]
    assert client.removed == [cloned]
    assert not backing.exists()
    assert not backing_dir.exists()
    assert client.loaded == []


def test_version_bundle_uses_one_save_copy_and_persists_metadata(tmp_path):
    model = FakeModel()
    model.name = lambda: "Model"
    version = tmp_path / "Model_1.mph"
    latest = tmp_path / "Model_latest.mph"

    result = _save_model_version_bundle(
        model, str(version), str(latest), description="accepted state"
    )

    assert len(model.java.saved) == 1
    assert model.java.saved[0][1] is True
    assert version.read_bytes() == latest.read_bytes() == b"saved-model"
    version_metadata = json.loads(Path(result["version_metadata_path"]).read_text())
    latest_metadata = json.loads(Path(result["latest_metadata_path"]).read_text())
    assert version_metadata == latest_metadata
    assert version_metadata["description"] == "accepted state"


def test_version_bundle_failure_restores_existing_latest_and_removes_version(
    tmp_path, monkeypatch
):
    model = FakeModel()
    model.name = lambda: "Model"
    version = tmp_path / "Model_1.mph"
    latest = tmp_path / "Model_latest.mph"
    latest_metadata = latest.with_suffix(".metadata.json")
    latest.write_bytes(b"previous")
    latest_metadata.write_text('{"previous": true}\n', encoding="utf-8")
    original_publish = __import__(
        "src.tools.model", fromlist=["publish_file_exclusive"]
    ).publish_file_exclusive
    calls = 0

    def fail_third(staging, target):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("latest publication failure")
        return original_publish(staging, target)

    monkeypatch.setattr("src.tools.model.publish_file_exclusive", fail_third)

    with pytest.raises(OSError, match="latest publication failure"):
        _save_model_version_bundle(model, str(version), str(latest), description=None)

    assert not version.exists()
    assert not version.with_suffix(".metadata.json").exists()
    assert latest.read_bytes() == b"previous"
    assert latest_metadata.read_text(encoding="utf-8") == '{"previous": true}\n'
    assert not list(tmp_path.glob(".*.stage"))
    assert not list(tmp_path.glob(".*.backup"))


def test_save_requires_target_and_cleans_native_failure_staging(tmp_path):
    with pytest.raises(ValueError, match="file_path"):
        _save_model_file(FakeModel())

    model = FakeModel()

    def fail_save(_path, _copy):
        raise RuntimeError("save failure")

    model.java.save = fail_save
    with pytest.raises(RuntimeError, match="save failure"):
        _save_model_file(model, str(tmp_path / "target.mph"))
    assert not list(tmp_path.glob(".*.save"))


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class ComponentNode:
    def __init__(self, tag, label):
        self._tag = JavaStringLike(tag)
        self._label = JavaStringLike(label)

    def tag(self):
        return self._tag

    def label(self):
        return self._label


class ComponentCollection:
    def __init__(self):
        self.nodes = {"comp1": ComponentNode("comp1", "Component 1")}

    def tags(self):
        return [JavaStringLike("comp1")]

    def get(self, tag):
        return self.nodes[str(tag)]


class ComponentJavaModel:
    def __init__(self):
        self.components = ComponentCollection()

    def component(self):
        return self.components


def test_list_components_normalizes_clientapi_strings_for_json():
    model = type("Model", (), {"java": ComponentJavaModel()})()

    components = _list_model_components(model)

    assert components == [{"name": "comp1", "label": "Component 1"}]
    assert json.loads(json.dumps(components)) == components


class MutableComponentCollection:
    def __init__(self):
        self.created = []

    def tags(self):
        return [name for name, _flag in self.created]

    def create(self, name, flag):
        self.created.append((name, flag))


def test_component_creation_does_not_claim_geometry_dimension_was_applied():
    components = MutableComponentCollection()
    java = type("Java", (), {"component": lambda _self: components})()
    model = type("Model", (), {"java": java})()

    result = create_model_component(model, "comp1", 3)

    assert components.created == [("comp1", True)]
    assert result["success"] is True
    assert result["requested_geometry_space_dimension"] == 3
    assert result["space_dimension_applied"] is False
    assert "space_dimension" not in result
