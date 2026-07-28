"""Unit tests for geometry helpers without a COMSOL client."""

import pytest
from src.tools.geometry import (
    add_circle_feature,
    add_difference_feature,
    add_geometry_feature,
    add_import_feature,
    add_primitive_feature,
    add_union_feature,
    list_geometry_features,
)


class FakeFeature:
    def __init__(self, failing_property=None):
        self.properties = {}
        self.failing_property = failing_property
        self.selections = {}

    def set(self, name, value):
        if name == self.failing_property:
            raise ValueError("unsupported property")
        self.properties[name] = value

    def label(self):
        return "Geometry Feature"

    def selection(self, name):
        if name not in self.selections:
            self.selections[name] = FakeObjectSelection()
        return self.selections[name]


class FakeObjectSelection:
    def __init__(self):
        self.objects = None

    def set(self, objects):
        self.objects = objects


class FakeFeatureList:
    def __init__(self, failing_property=None):
        self.features = {}
        self.failing_property = failing_property

    def size(self):
        return len(self.features)

    def create(self, tag, feature_type):
        feature = FakeFeature(self.failing_property)
        self.features[tag] = (feature_type, feature)
        return feature

    def tags(self):
        return list(self.features)

    def get(self, tag):
        return self.features[tag][1]

    def remove(self, tag):
        del self.features[tag]


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class JavaTagFeatureList(FakeFeatureList):
    def tags(self):
        return [JavaStringLike(tag) for tag in self.features]


class FakeGeometry:
    def __init__(self, tag="geom1", failing_property=None):
        self._tag = tag
        self.features = FakeFeatureList(failing_property)

    def tag(self):
        return self._tag

    def feature(self):
        return self.features


class FakeGeometryList:
    def __init__(self, geometries):
        self.geometries = geometries

    def size(self):
        return len(self.geometries)

    def tags(self):
        return list(self.geometries)

    def get(self, tag):
        return self.geometries[tag]


class FakeComponent:
    def __init__(self, geometries):
        self.geometries = geometries

    def geom(self, tag=None):
        if tag is None:
            return FakeGeometryList(self.geometries)
        return self.geometries[tag]


class FakeJava:
    def __init__(self, component):
        self.component_node = component

    def component(self, tag):
        assert tag == "comp1"
        return self.component_node


class FakeModel:
    def __init__(self, geometry):
        self.java = FakeJava(FakeComponent({"geom1": geometry}))


def test_add_geometry_feature_uses_first_clientapi_geometry():
    geometry = FakeGeometry()
    model = FakeModel(geometry)

    result = add_geometry_feature(
        model,
        "Block",
        properties={"pos": ["0", "0", "0"], "size": ["1", "2", "3"]},
    )

    feature_type, feature = geometry.features.features["feat1"]
    assert result["success"] is True
    assert result["feature"]["geometry"] == "geom1"
    assert feature_type == "Block"
    assert feature.properties["size"] == ["1", "2", "3"]


def test_add_geometry_feature_rolls_back_property_errors():
    geometry = FakeGeometry(failing_property="bad")
    model = FakeModel(geometry)

    result = add_geometry_feature(
        model,
        "Sphere",
        feature_name="sph1",
        properties={"r": "1", "bad": "value"},
    )

    assert result["success"] is False
    assert result["property_errors"] == {"bad": "unsupported property"}
    assert result["rolled_back"] is True
    assert "sph1" not in geometry.features.features


def test_add_geometry_feature_chooses_first_unused_tag_and_rejects_collision():
    geometry = FakeGeometry()
    geometry.features.create("feat1", "Block")
    geometry.features.create("feat3", "Sphere")

    created = add_geometry_feature(FakeModel(geometry), "Cylinder")
    collision = add_geometry_feature(FakeModel(geometry), "Cylinder", feature_name="feat1")

    assert created["feature"]["name"] == "feat2"
    assert collision == {
        "success": False,
        "error": "Feature tag already exists: feat1",
    }


def test_add_geometry_feature_validates_type():
    result = add_geometry_feature(FakeModel(FakeGeometry()), "  ")

    assert result == {"success": False, "error": "feature_type must not be empty."}


def test_add_geometry_feature_rejects_invalid_properties_before_clientapi():
    class UntouchableModel:
        @property
        def java(self):
            raise AssertionError("clientapi must not be touched")

    result = add_geometry_feature(
        UntouchableModel(),
        "Block",
        properties={"filename": "unsafe.java"},
    )

    assert result["success"] is False
    assert "forbidden" in result["error"]


def test_list_geometry_features_returns_tags_and_labels():
    geometry = FakeGeometry()
    geometry.features.create("blk1", "Block")
    geometry.features.create("dif1", "Difference")

    result = list_geometry_features(FakeModel(geometry))

    assert result == {
        "success": True,
        "geometry": "geom1",
        "component": "comp1",
        "features": [
            {"tag": "blk1", "label": "Geometry Feature"},
            {"tag": "dif1", "label": "Geometry Feature"},
        ],
        "count": 2,
    }


def test_list_geometry_features_normalizes_java_string_tags():
    geometry = FakeGeometry()
    geometry.features = JavaTagFeatureList()
    geometry.features.create("blk1", "Block")

    result = list_geometry_features(FakeModel(geometry))

    assert result["features"] == [{"tag": "blk1", "label": "Geometry Feature"}]


def test_add_circle_feature_uses_clientapi_properties():
    geometry = FakeGeometry()

    result = add_circle_feature(
        FakeModel(geometry),
        [1.0, 2.0],
        0.5,
        feature_name="c1",
    )

    feature_type, feature = geometry.features.features["c1"]
    assert feature_type == "Circle"
    assert feature.properties == {"pos": ["1.0", "2.0"], "r": "0.5"}
    assert result["feature"]["position"] == [1.0, 2.0]
    assert result["feature"]["radius"] == 0.5


@pytest.mark.parametrize(
    "position,radius",
    [
        ([0], 1),
        ([0, 0], 0),
        ([float("nan"), 0], 1),
        ([0, float("inf")], 1),
        ([0, 0], float("nan")),
        ([0, 0], float("inf")),
        ([0, 0], -float("inf")),
    ],
)
def test_add_circle_feature_validates_geometry_values_before_creation(position, radius):
    geometry = FakeGeometry()

    result = add_circle_feature(FakeModel(geometry), position, radius)

    assert result["success"] is False
    assert geometry.features.features == {}


def test_add_union_feature_sets_input_selection():
    geometry = FakeGeometry()

    result = add_union_feature(
        FakeModel(geometry),
        ["blk1", "blk2"],
        feature_name="uni1",
    )

    feature_type, feature = geometry.features.features["uni1"]
    assert feature_type == "Union"
    assert feature.selections["input"].objects == ["blk1", "blk2"]
    assert result["feature"]["input_objects"] == ["blk1", "blk2"]


def test_add_union_feature_requires_inputs():
    result = add_union_feature(FakeModel(FakeGeometry()), [])

    assert result["success"] is False


def test_add_import_feature_sets_absolute_filename(tmp_path):
    source = tmp_path / "part.step"
    source.write_text("dummy", encoding="utf-8")
    geometry = FakeGeometry()

    result = add_import_feature(
        FakeModel(geometry),
        str(source),
        feature_name="imp1",
    )

    feature_type, feature = geometry.features.features["imp1"]
    assert feature_type == "Import"
    assert feature.properties["filename"] == str(source.resolve())
    assert result["feature"]["file"] == str(source.resolve())


def test_add_import_feature_requires_existing_file(tmp_path):
    result = add_import_feature(
        FakeModel(FakeGeometry()),
        str(tmp_path / "missing.step"),
    )

    assert result["success"] is False


@pytest.mark.parametrize(
    ("feature_type", "position", "dimensions"),
    [
        ("Block", [0, 0], [1, 1, 1]),
        ("Block", [0, 0, 0], [1, 0, 1]),
        ("Cylinder", [0, 0, float("nan")], [1, 1]),
        ("Cylinder", [0, 0, 0], [-1, 1]),
        ("Sphere", [0, 0, 0], [False]),
        ("Rectangle", [0, float("inf")], [1, 1]),
    ],
)
def test_primitive_validation_precedes_feature_creation(feature_type, position, dimensions):
    geometry = FakeGeometry()

    result = add_primitive_feature(FakeModel(geometry), feature_type, position, dimensions)

    assert result["success"] is False
    assert geometry.features.features == {}


def test_primitive_property_failure_removes_created_feature():
    geometry = FakeGeometry(failing_property="size")

    result = add_primitive_feature(FakeModel(geometry), "Block", [0, 0, 0], [1, 1, 1])

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert geometry.features.features == {}


def test_difference_validates_inputs_before_creation_and_rolls_back_selection():
    geometry = FakeGeometry()
    geometry.features.create("blk1", "Block")
    missing = add_difference_feature(FakeModel(geometry), "blk1", ["missing"])
    assert missing["success"] is False
    assert set(geometry.features.features) == {"blk1"}

    class FailingSelection:
        def set(self, _objects):
            raise RuntimeError("controlled selection failure")

    class FailingDifference(FakeFeature):
        def selection(self, _name):
            return FailingSelection()

    original_create = geometry.features.create

    def create(tag, feature_type):
        if feature_type == "Difference":
            feature = FailingDifference()
            geometry.features.features[tag] = (feature_type, feature)
            return feature
        return original_create(tag, feature_type)

    geometry.features.create = create
    geometry.features.create("cyl1", "Cylinder")
    failed = add_difference_feature(FakeModel(geometry), "blk1", ["cyl1"])
    assert failed["rolled_back"] is True
    assert not any(tag.startswith("dif") for tag in geometry.features.features)


def test_import_filename_failure_removes_created_feature(tmp_path):
    source = tmp_path / "part.step"
    source.write_text("dummy", encoding="utf-8")
    geometry = FakeGeometry(failing_property="filename")

    result = add_import_feature(FakeModel(geometry), str(source))

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert geometry.features.features == {}
