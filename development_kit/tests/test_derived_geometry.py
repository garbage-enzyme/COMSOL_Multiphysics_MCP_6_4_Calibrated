"""derived geometry typed derived-geometry edit gates without COMSOL."""

from __future__ import annotations

from src.tools.derived_geometry import (
    DerivedGeometryRecord,
    _DERIVED,
    _snapshot,
    _state_hash,
    apply_blocks,
    apply_fin,
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
    model, geom, record, state = fixture(fail_second="size")
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
    _DERIVED[record.derived_model_id] = record
    try:
        status = derived_model_validation_status("dirty-clone")
        assert status["tracked"] is True
        assert status["validation_allowed"] is False
        assert status["dirty_reason"] == "rollback unproven"
    finally:
        _DERIVED.pop(record.derived_model_id, None)
