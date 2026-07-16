"""Unit tests for model management helpers without a COMSOL client."""

import json
from pathlib import Path

from src.tools.model import _clone_model, _list_model_components, _save_model_file


class FakeJavaModel:
    def __init__(self):
        self.saved = []

    def save(self, file_path):
        self.saved.append(file_path)


class FakeModel:
    def __init__(self, current_file=None):
        self.java = FakeJavaModel()
        self.current_file = current_file
        self.high_level_saves = []

    def file(self):
        return self.current_file

    def save(self, path=None, format=None):
        self.high_level_saves.append((path, format))


def test_save_mph_uses_java_clientapi_for_unicode_path(tmp_path):
    model = FakeModel()
    requested = tmp_path / "中文目录" / "模型.mph"

    saved = _save_model_file(model, str(requested))

    assert saved == str(requested.resolve())
    assert model.java.saved == [str(requested.resolve())]
    assert model.high_level_saves == []
    assert requested.parent.is_dir()


def test_save_mph_uses_existing_model_file(tmp_path):
    current = tmp_path / "existing.mph"
    model = FakeModel(current_file=str(current))

    saved = _save_model_file(model)

    assert saved == str(current.resolve())
    assert model.java.saved == [str(current.resolve())]


def test_save_source_export_keeps_mph_format_api(tmp_path):
    model = FakeModel()
    requested = tmp_path / "model.java"

    saved = _save_model_file(model, str(requested), format="Java")

    assert saved == str(requested)
    assert model.high_level_saves == [(str(requested), "Java")]
    assert model.java.saved == []


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

    def load(self, path):
        self.loaded.append(path)
        return self.cloned


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
