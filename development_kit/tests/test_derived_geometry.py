"""derived geometry typed derived-geometry edit gates without COMSOL."""

from __future__ import annotations

from pathlib import Path

import pytest
import src.tools.derived_geometry as derived_geometry_module
from src.tools.derived_geometry import (
    _DERIVED,
    DerivedGeometryRecord,
    _create_registered_derived_geometry_clone,
    _snapshot,
    _state_hash,
    apply_blocks,
    apply_fin,
    create_derived_geometry_clone,
    derived_model_validation_status,
    preview_blocks,
    preview_fin,
)


class Container:
    def __init__(self, items):
        self.items = items

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[str(tag)]


class Feature:
    def __init__(self, kind, props, fail=None):
        self.kind = kind
        self.props = {key: list(value) if isinstance(value, list) else value for key, value in props.items()}
        self.fail = fail

    def getType(self):
        return self.kind

    def properties(self):
        return list(self.props)

    def getValueType(self, name):
        return "StringArray" if isinstance(self.props[name], list) else "String"

    def label(self):
        return self.kind

    def getString(self, name):
        value = self.props[name]
        return " ".join(value) if isinstance(value, list) else value

    def getStringArray(self, name):
        value = self.props[name]
        if not isinstance(value, list):
            raise RuntimeError("not array")
        return value

    def set(self, name, value):
        if self.fail == name or self.fail == "all":
            raise RuntimeError(f"forced {name} failure")
        if self.kind == "FormUnion" and name in {"imprint", "createpairs"} and isinstance(value, bool):
            value = "on" if value else "off"
        self.props[name] = list(value) if isinstance(value, list) else value


class Geometry:
    def __init__(self, features, run_failure=False):
        self.features = Container(features)
        self.run_failure = run_failure
        self.run_count = 0

    def feature(self):
        return self.features

    def run(self):
        self.run_count += 1
        if self.run_failure:
            raise RuntimeError("forced geometry failure")

    def getNDomains(self):
        return 2

    def getNBoundaries(self):
        return 12


class Component:
    def __init__(self, geom):
        self.geometries = Container({"geom1": geom})

    def geom(self):
        return self.geometries


class JavaModel:
    def __init__(self, geom):
        self.components = Container({"comp1": Component(geom)})

    def component(self):
        return self.components


class Model:
    def __init__(self, geom):
        self.java = JavaModel(geom)


def fixture(fail_second=None, run_failure=False):
    fin = Feature("FormUnion", {"action": "union", "imprint": "off", "createpairs": "off"})
    blk1 = Feature("Block", {"size": ["1[mm]", "2[mm]", "3[mm]"], "pos": ["0[mm]", "0[mm]", "0[mm]"]})
    blk2 = Feature("Block", {"size": ["2[mm]", "2[mm]", "2[mm]"], "pos": ["1[mm]", "1[mm]", "1[mm]"]}, fail=fail_second)
    geom = Geometry({"blk1": blk1, "blk2": blk2, "fin": fin}, run_failure=run_failure)
    model = Model(geom)
    record = DerivedGeometryRecord("derived-test", "clone", "source.mph", "a" * 64, "clone.mph", "b" * 64)
    state = _state_hash(record, _snapshot(model, "comp1", "geom1"))
    return model, geom, record, state


def edits():
    return [
        {"block_tag": "blk1", "size": ["2[mm]", "2[mm]", "3[mm]"], "pos": ["-1[mm]", "0[mm]", "0[mm]"]},
        {"block_tag": "blk2", "size": ["3[mm]", "2[mm]", "2[mm]"], "pos": ["1[mm]", "1[mm]", "1[mm]"]},
    ]


def test_fin_preview_is_read_only_and_apply_runs_geometry():
    model, geom, record, state = fixture()
    preview = preview_fin(model, record, expected_state_sha256=state, component_tag="comp1", geometry_tag="geom1", action="assembly", imprint=True, create_pairs=False)
    assert preview["mutated"] is False
    assert geom.features.get("fin").props["action"] == "union"

    result = apply_fin(model, record, preview, "comp1", "geom1")
    assert result["success"] is True
    assert result["after"] == {"action": "assembly", "imprint": "on", "createpairs": "off"}
    assert geom.run_count == 1


def test_block_preview_and_apply_never_run_geometry_or_mesh():
    model, geom, record, state = fixture()
    preview = preview_blocks(model, record, expected_state_sha256=state, component_tag="comp1", geometry_tag="geom1", block_edits=edits())
    result = apply_blocks(model, record, preview, "comp1", "geom1")
    assert result["success"] is True
    assert result["geometry_run"] is False and result["mesh_run"] is False
    assert geom.run_count == 0
    assert result["after"]["blk1"]["size"][0] == "2[mm]"


def test_stale_hash_invalid_feature_partial_vectors_and_nonpositive_size_fail():
    model, _geom, record, state = fixture()
    cases = [
        ([{"block_tag": "missing", "size": ["1[mm]"] * 3, "pos": ["0[mm]"] * 3}], "missing"),
        ([{"block_tag": "blk1", "size": ["1[mm]"] * 2, "pos": ["0[mm]"] * 3}], "complete"),
        ([{"block_tag": "blk1", "size": ["0[mm]", "1[mm]", "1[mm]"], "pos": ["0[mm]"] * 3}], "positive"),
    ]
    for block_edits, text in cases:
        try:
            preview_blocks(model, record, expected_state_sha256=state, component_tag="comp1", geometry_tag="geom1", block_edits=block_edits)
        except ValueError as exc:
            assert text in str(exc)
        else:
            raise AssertionError("invalid edit accepted")
    try:
        preview_fin(model, record, expected_state_sha256="0" * 64, component_tag="comp1", geometry_tag="geom1", action="union", imprint=False, create_pairs=False)
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale hash accepted")


def test_partial_block_failure_rolls_back_when_setters_remain_available():
    model, _geom, record, state = fixture(fail_second="size")
    preview = preview_blocks(model, record, expected_state_sha256=state, component_tag="comp1", geometry_tag="geom1", block_edits=edits())
    before = _snapshot(model, "comp1", "geom1")
    result = apply_blocks(model, record, preview, "comp1", "geom1")
    assert result["success"] is False
    # The permanently failing blk2 setter makes rollback unprovable and marks dirty.
    assert result["rollback_proved"] is False
    assert record.dirty is True
    assert _snapshot(model, "comp1", "geom1")["blocks"]["blk1"] == before["blocks"]["blk1"]


def test_fin_geometry_failure_restores_properties_but_reports_unproven_build():
    model, geom, record, state = fixture(run_failure=True)
    preview = preview_fin(model, record, expected_state_sha256=state, component_tag="comp1", geometry_tag="geom1", action="assembly", imprint=True, create_pairs=True)
    result = apply_fin(model, record, preview, "comp1", "geom1")
    assert result["success"] is False
    assert geom.features.get("fin").props == {"action": "union", "imprint": "off", "createpairs": "off"}
    assert result["rollback_proved"] is False
    assert record.dirty is True


def test_dirty_derived_record_is_forbidden_from_validation():
    record = DerivedGeometryRecord("derived-dirty", "dirty-clone", "source.mph", "a" * 64, "clone.mph", "b" * 64, dirty=True, dirty_reason="rollback unproven")
    previous = _DERIVED.get(record.derived_model_id)
    _DERIVED[record.derived_model_id] = record
    try:
        status = derived_model_validation_status("dirty-clone")
        assert status["tracked"] is True
        assert status["validation_allowed"] is False
        assert status["dirty_reason"] == "rollback unproven"
    finally:
        if previous is None:
            _DERIVED.pop(record.derived_model_id, None)
        else:
            _DERIVED[record.derived_model_id] = previous


def test_snapshot_covers_every_feature_and_property_and_hashes_their_values():
    model, geom, record, _state = fixture()
    geom.features.items["sel1"] = Feature("ExplicitSelection", {"entitydim": "2"})
    geom.features.items["blk1"].props["base"] = "center"

    before = _snapshot(model, "comp1", "geom1")
    before_hash = _state_hash(record, before)
    geom.features.items["sel1"].props["entitydim"] = "3"
    after = _snapshot(model, "comp1", "geom1")

    assert set(before["features"]) == {"blk1", "blk2", "fin", "sel1"}
    assert before["features"]["blk1"]["properties"]["base"]["value"] == "center"
    assert _state_hash(record, after) != before_hash


def test_block_inventory_uses_exact_feature_type_not_substring_heuristics():
    model, geom, record, _state = fixture()
    geom.features.items["block_notes"] = Feature(
        "Annotation", {"size": ["1[mm]"] * 3, "pos": ["0[mm]"] * 3}
    )
    geom.features.items["shape1"] = Feature(
        "Block", {"size": ["1[mm]"] * 3, "pos": ["0[mm]"] * 3}
    )

    snapshot = _snapshot(model, "comp1", "geom1")

    assert "block_notes" not in snapshot["blocks"]
    assert "shape1" in snapshot["blocks"]


def test_clone_label_failure_removes_loaded_clone_and_backing_artifact(tmp_path):
    source_path = tmp_path / "source.mph"
    source_path.write_bytes(b"source")

    class SourceJava:
        def save(self, target, _copy):
            Path(target).write_bytes(b"clone")

    class Source:
        java = SourceJava()

        def file(self):
            return str(source_path)

    class CloneJava:
        def label(self, _name):
            raise RuntimeError("label failed")

    class Clone:
        java = CloneJava()

    class Client:
        def __init__(self):
            self.clone = Clone()
            self.removed = []

        def load(self, _path):
            return self.clone

        def remove(self, model):
            self.removed.append(model)

    client = Client()
    with pytest.raises(RuntimeError, match="label failed"):
        create_derived_geometry_clone(
            Source(), client, new_name="derived", runtime_dir=tmp_path
        )

    assert client.removed == [client.clone]
    assert list(tmp_path.glob("comsol_mcp_clone_geometry_*")) == []


def test_geometry_clone_uses_exact_immutable_source_bytes_not_unsaved_live_state(tmp_path):
    source_path = tmp_path / "source.mph"
    source_path.write_bytes(b"immutable-source")

    class SourceJava:
        def save(self, _target, _copy):
            raise AssertionError("unsaved live state must not be cloned")

    class Source:
        java = SourceJava()

        def file(self):
            return str(source_path)

    class CloneJava:
        def label(self, _name):
            return None

    class Clone:
        java = CloneJava()

        def name(self):
            return "derived"

    class Client:
        def __init__(self):
            self.loaded_bytes = None

        def load(self, path):
            self.loaded_bytes = Path(path).read_bytes()
            return Clone()

    client = Client()
    clone, record = create_derived_geometry_clone(
        Source(), client, new_name="derived", runtime_dir=tmp_path
    )

    assert clone.name() == "derived"
    assert client.loaded_bytes == b"immutable-source"
    assert record.source_sha256 == record.backing_sha256
    assert Path(record.backing_path).read_bytes() == b"immutable-source"


def test_session_removal_discards_derived_registry_entry(monkeypatch):
    manager = derived_geometry_module.session_manager

    class Client:
        def remove(self, _model):
            return None

    class Model:
        def name(self):
            return "derived-session-model"

    monkeypatch.setattr(manager, "_client", Client())
    monkeypatch.setattr(manager, "_models", {})
    monkeypatch.setattr(manager, "_model_paths", {})
    monkeypatch.setattr(manager, "_model_revisions", {})
    monkeypatch.setattr(manager, "_model_cleanup_paths", {})
    monkeypatch.setattr(manager, "_current_model", None)
    monkeypatch.setattr(manager._ownership, "heartbeat", lambda **_kwargs: True)
    record = DerivedGeometryRecord(
        "derived-session-id", "derived-session-model", "source.mph", "a" * 64,
        "clone.mph", "b" * 64,
    )
    _DERIVED[record.derived_model_id] = record
    manager.add_model(Model())

    assert manager.remove_model(record.model_name) is True
    assert record.derived_model_id not in _DERIVED


def test_rollback_requires_complete_snapshot_readback_not_only_setter_success():
    class SilentCorruptFeature(Feature):
        def __init__(self, kind, props):
            super().__init__(kind, props)
            self.changed = False

        def set(self, name, value):
            if name == "size" and self.changed and value == ["1[mm]", "2[mm]", "3[mm]"]:
                self.props[name] = ["9[mm]", "9[mm]", "9[mm]"]
                return
            super().set(name, value)
            if name == "size":
                self.changed = True

    fin = Feature("FormUnion", {"action": "union", "imprint": "off", "createpairs": "off"})
    blk1 = SilentCorruptFeature(
        "Block", {"size": ["1[mm]", "2[mm]", "3[mm]"], "pos": ["0[mm]"] * 3}
    )
    blk2 = Feature(
        "Block", {"size": ["2[mm]"] * 3, "pos": ["1[mm]"] * 3}, fail="size"
    )
    model = Model(Geometry({"blk1": blk1, "blk2": blk2, "fin": fin}))
    record = DerivedGeometryRecord(
        "derived-readback", "clone", "source.mph", "a" * 64, "clone.mph", "b" * 64
    )
    state = _state_hash(record, _snapshot(model, "comp1", "geom1"))
    preview = preview_blocks(
        model, record, expected_state_sha256=state, component_tag="comp1",
        geometry_tag="geom1", block_edits=edits(),
    )

    result = apply_blocks(model, record, preview, "comp1", "geom1")

    assert result["success"] is False
    assert result["rollback_proved"] is False
    assert any("complete pre-state" in error for error in result["rollback_errors"])
    assert record.dirty is True


def test_post_mutation_snapshot_failure_rolls_back_and_marks_state_dirty(monkeypatch):
    model, _geom, record, state = fixture()
    preview = preview_blocks(
        model, record, expected_state_sha256=state, component_tag="comp1",
        geometry_tag="geom1", block_edits=edits(),
    )
    original_snapshot = derived_geometry_module._snapshot
    calls = 0

    def fail_once_after_mutation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("post-mutation snapshot failed")
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(derived_geometry_module, "_snapshot", fail_once_after_mutation)
    result = apply_blocks(model, record, preview, "comp1", "geom1")

    assert result["success"] is False
    assert result["rollback_proved"] is True
    assert record.dirty is False
    assert _snapshot(model, "comp1", "geom1")["blocks"] == preview["before"]


def test_initial_snapshot_failure_rolls_back_session_registry_and_clone(monkeypatch, tmp_path):
    backing_dir = tmp_path / "comsol_mcp_clone_geometry_test"
    backing_dir.mkdir()
    backing = backing_dir / "clone.mph"
    backing.write_bytes(b"clone")

    class Clone:
        def name(self):
            return "registered-clone"

    clone = Clone()
    record = DerivedGeometryRecord(
        "derived-transaction", clone.name(), "source.mph", "a" * 64,
        str(backing), "b" * 64,
    )
    removed = []
    monkeypatch.setattr(
        derived_geometry_module,
        "create_derived_geometry_clone",
        lambda *_args, **_kwargs: (clone, record),
    )
    monkeypatch.setattr(
        derived_geometry_module.session_manager,
        "add_model",
        lambda _clone, cleanup_path=None: clone.name(),
    )

    def remove_model(name):
        removed.append(name)
        derived_geometry_module._discard_derived_model(name)
        derived_geometry_module._cleanup_clone_artifact(str(backing))
        return True

    monkeypatch.setattr(derived_geometry_module.session_manager, "remove_model", remove_model)
    monkeypatch.setattr(
        derived_geometry_module, "_snapshot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("snapshot failed")),
    )

    with pytest.raises(ValueError, match="snapshot failed"):
        _create_registered_derived_geometry_clone(object(), object(), new_name="clone")

    assert removed == [clone.name()]
    assert record.derived_model_id not in _DERIVED
    assert not backing.exists() and not backing_dir.exists()
