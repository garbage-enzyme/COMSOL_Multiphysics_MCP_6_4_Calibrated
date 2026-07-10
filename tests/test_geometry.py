"""Unit tests for geometry helpers without a COMSOL client."""

from src.tools.geometry import add_geometry_feature, list_geometry_features


class FakeFeature:
    def __init__(self, failing_property=None):
        self.properties = {}
        self.failing_property = failing_property

    def set(self, name, value):
        if name == self.failing_property:
            raise ValueError("unsupported property")
        self.properties[name] = value

    def label(self):
        return "Geometry Feature"


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


def test_add_geometry_feature_reports_property_errors():
    geometry = FakeGeometry(failing_property="bad")
    model = FakeModel(geometry)

    result = add_geometry_feature(
        model,
        "Sphere",
        feature_name="sph1",
        properties={"r": "1", "bad": "value"},
    )

    assert result["success"] is True
    assert result["property_errors"] == {"bad": "unsupported property"}
    assert "warning" in result


def test_add_geometry_feature_validates_type():
    result = add_geometry_feature(FakeModel(FakeGeometry()), "  ")

    assert result == {"success": False, "error": "feature_type must not be empty."}


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
