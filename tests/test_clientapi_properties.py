"""Mock gates for constrained existing-object clientapi property access."""

from __future__ import annotations

import math

import pytest

from src.tools.properties import get_existing_property, set_existing_property


class FakeFeature:
    def __init__(self, values, value_types):
        self.values = dict(values)
        self.value_types = dict(value_types)
        self.set_calls = []

    def properties(self):
        return list(self.values)

    def getValueType(self, name):
        return self.value_types[name]

    def getString(self, name):
        return self.values[name]

    def getStringArray(self, name):
        return self.values[name]

    def getStringMatrix(self, name):
        return self.values[name]

    def getDouble(self, name):
        return self.values[name]

    def getDoubleArray(self, name):
        return self.values[name]

    def getInt(self, name):
        return self.values[name]

    def getIntArray(self, name):
        return self.values[name]

    def getBoolean(self, name):
        return self.values[name]

    def getBooleanArray(self, name):
        return self.values[name]

    def set(self, name, value):
        self.set_calls.append((name, value))
        self.values[name] = value


class FakeFeatureList:
    def __init__(self, feature):
        self.feature = feature

    def get(self, tag):
        return self.feature if tag == "child1" else None


class FakeParent:
    def __init__(self, feature):
        self.features = FakeFeatureList(feature)

    def feature(self):
        return self.features


class FakeContainerList:
    def __init__(self, feature):
        self.feature = feature

    def __call__(self, tag):
        return FakeParent(self.feature) if tag == "parent1" else None


class FakeComponent:
    def __init__(self, feature):
        self.feature = feature

    def geom(self, tag):
        return FakeParent(self.feature)

    def physics(self, tag):
        return FakeParent(self.feature)

    def mesh(self, tag):
        return FakeParent(self.feature)


class FakeJava:
    def __init__(self, feature):
        self.feature = feature
        self.component_node = FakeComponent(feature)
        self.study = FakeContainerList(feature)
        self.result = FakeContainerList(feature)

    def component(self, tag):
        return self.component_node if tag == "comp1" else None


class FakeModel:
    def __init__(self, feature):
        self.java = FakeJava(feature)


@pytest.fixture
def feature():
    return FakeFeature(
        {
            "label": "old",
            "size": ["1", "2", "3"],
            "basis": [["1", "0"], ["0", "1"]],
            "scale": 0.25,
            "indices": [1, 2],
            "active": True,
        },
        {
            "label": "String",
            "size": "StringArray",
            "basis": "StringMatrix",
            "scale": "Double",
            "indices": "IntArray",
            "active": "Boolean",
        },
    )


@pytest.mark.parametrize(
    "container",
    ["geometry_feature", "physics_feature", "mesh_feature", "study_step", "result_feature"],
)
def test_property_get_resolves_each_allowlisted_container(feature, container):
    result = get_existing_property(
        FakeModel(feature), "comp1", container, "parent1/child1", "size"
    )

    assert result["success"] is True
    assert result["value"] == ["1", "2", "3"]
    assert result["target"].endswith("parent1/child1/size")


@pytest.mark.parametrize(
    "property_name, new_value",
    [
        ("label", "new"),
        ("size", ["4", "5", "6"]),
        ("basis", [["0", "1"], ["1", "0"]]),
        ("scale", 0.5),
        ("indices", [3, 4]),
        ("active", False),
    ],
)
def test_property_set_returns_normalized_old_and_new_values(
    feature, property_name, new_value
):
    old_value = feature.values[property_name]

    result = set_existing_property(
        FakeModel(feature),
        "comp1",
        "geometry_feature",
        "parent1/child1",
        property_name,
        new_value,
    )

    assert result["success"] is True
    assert result["old_value"] == old_value
    assert result["new_value"] == new_value
    assert feature.set_calls == [(property_name, new_value)]


def test_property_access_rejects_unknown_targets_and_properties(feature):
    model = FakeModel(feature)

    assert get_existing_property(
        model, "comp1", "arbitrary_java", "parent1/child1", "label"
    )["success"] is False
    assert get_existing_property(
        model, "comp1", "geometry_feature", "parent1/child1/run", "label"
    )["success"] is False
    result = get_existing_property(
        model, "comp1", "geometry_feature", "parent1/child1", "missing"
    )
    assert result["success"] is False
    assert "unknown property" in result["error"]


def test_property_set_rejects_nonfinite_before_resolving_clientapi():
    class UntouchableModel:
        @property
        def java(self):
            raise AssertionError("clientapi must not be touched")

    result = set_existing_property(
        UntouchableModel(),
        "comp1",
        "geometry_feature",
        "parent1/child1",
        "scale",
        math.nan,
    )

    assert result["success"] is False
    assert "finite" in result["error"]


def test_property_access_rejects_file_and_callable_property_names(feature):
    for property_name in ("filename", "method", "run()"):
        result = get_existing_property(
            FakeModel(feature),
            "comp1",
            "geometry_feature",
            "parent1/child1",
            property_name,
        )
        assert result["success"] is False
        assert not feature.set_calls
