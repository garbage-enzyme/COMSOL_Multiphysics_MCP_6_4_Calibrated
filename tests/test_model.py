"""Unit tests for model management helpers without a COMSOL client."""

from src.tools.model import _clone_model, _save_model_file


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


def test_clone_model_uses_clientapi_save_copy_and_load():
    source = CloneModel()
    cloned = CloneModel("Loaded")
    client = CloneClient(cloned)

    result = _clone_model(client, source, "Independent Copy")

    assert result is cloned
    assert source.java.saved[0][1] is True
    assert client.loaded == [source.java.saved[0][0]]
    assert cloned.java.model_label == "Independent Copy"
