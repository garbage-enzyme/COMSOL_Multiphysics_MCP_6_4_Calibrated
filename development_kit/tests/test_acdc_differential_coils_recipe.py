"""Behavioral and structural checks for the differential-coil recipe."""

import ast
import math
import runpy
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
RECIPE = ROOT / "recipes" / "acdc_2d_differential_coils.py"


def _tree():
    return ast.parse(RECIPE.read_text(encoding="utf-8"), filename=str(RECIPE))


def _function(tree, name):
    return next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _executable_strings(tree):
    docstrings = {
        id(statement.value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.body
        and isinstance((statement := node.body[0]), ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_recipe_has_structured_runtime_inputs_and_solve_branch():
    tree = _tree()
    declarations = {}
    for node in ast.walk(_function(tree, "parse_args")):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            declarations[node.args[0].value] = {item.arg: item.value for item in node.keywords}

    for flag in ("--baseline-model", "--output-model"):
        assert isinstance(declarations[flag]["type"], ast.Name)
        assert declarations[flag]["type"].id == "Path"
        assert declarations[flag]["required"].value is True
    for flag in ("--solve", "--overwrite-output"):
        assert declarations[flag]["action"].value == "store_true"
    assert any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and isinstance(node.test.value, ast.Name)
        and node.test.value.id == "args"
        and node.test.attr == "solve"
        for node in ast.walk(_function(tree, "main"))
    )


def test_recipe_executable_literals_have_no_absolute_or_fixed_model_path():
    literals = _executable_strings(_tree())
    assert not [
        value
        for value in literals
        if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute()
    ]
    assert not [value for value in literals if value.casefold().endswith(".mph")]


def test_recipe_structurally_binds_air_properties_and_frequency_study():
    tree = _tree()
    replace_physics = _function(tree, "replace_physics")
    air_sets = {
        tuple(argument.value for argument in node.args)
        for node in ast.walk(replace_physics)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ampere_air"
        and node.func.attr == "set"
        and all(isinstance(argument, ast.Constant) for argument in node.args)
    }
    assert {("mur", "1"), ("epsilonr", "1")} <= air_sets
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create"
        and [getattr(argument, "value", None) for argument in node.args] == ["freq", "Frequency"]
        for node in ast.walk(_function(tree, "replace_mesh_and_study"))
    )


def test_recipe_rebuilds_geometry_with_finalization_last(monkeypatch):
    namespace = _behavior_namespace(monkeypatch)
    events = []

    class Feature:
        def __init__(self, tag):
            self.tag = tag

        def set(self, *_args):
            return None

        def label(self, value):
            events.append(("label", self.tag, value))

    class Features:
        def __init__(self):
            self.values = ["old", "fin"]

        def tags(self):
            return list(self.values)

        def remove(self, tag):
            events.append(("remove", str(tag)))
            self.values.remove(str(tag))

        def create(self, tag, kind):
            events.append(("create", tag, kind))
            self.values.append(tag)
            return Feature(tag)

        def move(self, tag, position):
            events.append(("move", tag, position))
            self.values.remove(tag)
            self.values.insert(position, tag)

        def size(self):
            return len(self.values)

    class Geometry:
        features = Features()

        def feature(self):
            return self.features

        @staticmethod
        def run():
            events.append(("run",))

    namespace["replace_geometry"](Geometry())

    creates = [event for event in events if event[0] == "create"]
    assert events[0] == ("remove", "old")
    assert creates == [
        ("create", "coil_positive", "Rectangle"),
        ("create", "coil_negative", "Rectangle"),
        ("create", "air", "Rectangle"),
    ]
    assert ("move", "fin", 3) in events
    assert events[-1] == ("run",)


def test_recipe_removes_every_nonrequired_baseline_physics_feature(monkeypatch):
    namespace = _behavior_namespace(monkeypatch)
    removed = []

    class Selection:
        @staticmethod
        def named(_name):
            return None

    class Feature:
        @staticmethod
        def set(*_args):
            return None

        @staticmethod
        def label(_value):
            return None

        @staticmethod
        def selection():
            return Selection()

    class Features:
        def __init__(self):
            self.values = {
                "fsp1": Feature(),
                "mi1": Feature(),
                "init1": Feature(),
                "baseline_extra": Feature(),
            }

        def tags(self):
            return list(self.values)

        def remove(self, tag):
            removed.append(str(tag))
            del self.values[str(tag)]

        def get(self, tag):
            return self.values[tag]

        def create(self, tag, _kind, _dimension):
            feature = Feature()
            self.values[tag] = feature
            return feature

    class Physics:
        def __init__(self):
            self.features = Features()

        def feature(self):
            return self.features

    physics = Physics()

    class PhysicsList:
        @staticmethod
        def get(tag):
            assert tag == "mf"
            return physics

    class Component:
        @staticmethod
        def physics():
            return PhysicsList()

    namespace["replace_physics"](Component(), 1.0)

    assert removed == ["baseline_extra"]
    assert set(physics.features.values) == {
        "fsp1",
        "mi1",
        "init1",
        "ampere_air",
        "coil_positive",
        "coil_negative",
    }


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_recipe_rejects_nonfinite_or_nonpositive_frequency(monkeypatch, value):
    namespace = _behavior_namespace(monkeypatch)
    with pytest.raises(ValueError, match="finite and positive"):
        namespace["validate_frequency_khz"](value)


def _behavior_namespace(monkeypatch):
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace())
    loaded = runpy.run_path(str(RECIPE), run_name="recipe_behavior_test")
    return loaded["main"].__globals__


def test_recipe_main_executes_ordered_derived_build_and_cleanup(tmp_path, monkeypatch):
    namespace = _behavior_namespace(monkeypatch)
    baseline = tmp_path / "baseline.mph"
    baseline.write_bytes(b"immutable")
    output = tmp_path / "derived.mph"
    staging = tmp_path / ".derived.staging"
    events = []
    model, java_model, component, geometry = object(), object(), object(), object()

    class Client:
        def load(self, path):
            events.append(("load", Path(path)))
            return model

        def remove(self, value):
            assert value is model
            events.append(("remove", value))

    namespace["parse_args"] = lambda: SimpleNamespace(
        baseline_model=baseline,
        output_model=output,
        frequency_khz=1.0,
        solve=False,
        overwrite_output=False,
    )
    namespace["mph"] = SimpleNamespace(Client=lambda version: Client())
    namespace["sha256_file"] = lambda path: events.append(("hash", Path(path))) or "a" * 64
    namespace["require_baseline_contract"] = lambda value: (
        events.append(("contract", value)) or (java_model, component, geometry)
    )
    for name in (
        "replace_geometry",
        "replace_physics",
        "replace_materials",
        "replace_mesh_and_study",
    ):
        namespace[name] = lambda *args, _name=name: events.append((_name, args))
    namespace["configure_results"] = lambda value: events.append(("configure_results", value))
    namespace["save_staged_model"] = lambda value, destination: (
        events.append(("save", value, destination)) or staging
    )
    namespace["publish_staged_model"] = lambda source, destination, *, overwrite: events.append(
        ("publish", source, destination, overwrite)
    )

    namespace["main"]()

    assert [event[0] for event in events] == [
        "hash",
        "load",
        "contract",
        "replace_geometry",
        "replace_physics",
        "replace_materials",
        "replace_mesh_and_study",
        "configure_results",
        "save",
        "remove",
        "hash",
        "publish",
    ]


def test_recipe_main_removes_loaded_model_after_intermediate_failure(tmp_path, monkeypatch):
    namespace = _behavior_namespace(monkeypatch)
    baseline = tmp_path / "baseline.mph"
    baseline.write_bytes(b"immutable")
    events = []
    model = object()

    class Client:
        def load(self, _path):
            return model

        def remove(self, value):
            events.append(("remove", value))

    namespace["parse_args"] = lambda: SimpleNamespace(
        baseline_model=baseline,
        output_model=tmp_path / "derived.mph",
        frequency_khz=1.0,
        solve=False,
        overwrite_output=False,
    )
    namespace["mph"] = SimpleNamespace(Client=lambda version: Client())
    namespace["sha256_file"] = lambda _path: "a" * 64
    namespace["require_baseline_contract"] = lambda _model: (object(), object(), object())
    namespace["replace_geometry"] = lambda _geometry: (_ for _ in ()).throw(
        RuntimeError("injected build failure")
    )
    namespace["publish_staged_model"] = lambda *_args, **_kwargs: pytest.fail(
        "failed builds must not publish"
    )

    with pytest.raises(RuntimeError, match="injected build failure"):
        namespace["main"]()
    assert events == [("remove", model)]


def test_recipe_main_preserves_build_failure_when_client_remove_fails(tmp_path, monkeypatch):
    namespace = _behavior_namespace(monkeypatch)
    baseline = tmp_path / "baseline.mph"
    baseline.write_bytes(b"immutable")
    model = object()

    class Client:
        def load(self, _path):
            return model

        def remove(self, _value):
            raise OSError("remove failed")

    namespace["parse_args"] = lambda: SimpleNamespace(
        baseline_model=baseline,
        output_model=tmp_path / "derived.mph",
        frequency_khz=1.0,
        solve=False,
        overwrite_output=False,
    )
    namespace["mph"] = SimpleNamespace(Client=lambda version: Client())
    namespace["sha256_file"] = lambda _path: "a" * 64
    namespace["require_baseline_contract"] = lambda _model: (object(), object(), object())
    namespace["replace_geometry"] = lambda _geometry: (_ for _ in ()).throw(
        RuntimeError("injected build failure")
    )

    with pytest.raises(RuntimeError, match="injected build failure") as caught:
        namespace["main"]()
    assert any("client.remove cleanup failed" in note for note in caught.value.__notes__)


def test_recipe_staging_never_overwrites_a_competing_output(tmp_path, monkeypatch):
    namespace = _behavior_namespace(monkeypatch)
    output = tmp_path / "derived.mph"

    class FakeJava:
        def save(self, staging):
            Path(staging).write_bytes(b"ours")

    staging = namespace["save_staged_model"](FakeJava(), output)
    output.write_bytes(b"competitor")
    with pytest.raises(FileExistsError):
        namespace["publish_staged_model"](staging, output, overwrite=False)
    assert output.read_bytes() == b"competitor"
    assert not staging.exists()
