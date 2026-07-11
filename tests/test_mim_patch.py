"""Unit tests for MIM patch helpers without a COMSOL client."""

import json

from src.tools.mim_patch import _find_air_block_tag, _list_pair_metadata


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class PairNode:
    def label(self):
        return JavaStringLike("Identity Pair 1")


class PairCollection:
    def tags(self):
        return [JavaStringLike("pair1")]

    def get(self, tag):
        assert str(tag) == "pair1"
        return PairNode()


class Component:
    def pair(self):
        return PairCollection()


class GeometryFeature:
    def __init__(self, size):
        self.size = size

    def getString(self, name):
        assert name == "size"
        return JavaStringLike(self.size)


class GeometryFeatures:
    def __init__(self):
        self.nodes = {
            "thin": GeometryFeature("1e-6, 1e-6, 4e-8"),
            "air": GeometryFeature("1e-6, 1e-6, 2e-6"),
            "fin": GeometryFeature(""),
        }

    def tags(self):
        return [JavaStringLike(tag) for tag in self.nodes]

    def get(self, tag):
        return self.nodes[str(tag)]


class Geometry:
    def feature(self):
        return GeometryFeatures()


def test_pair_metadata_normalizes_clientapi_strings_for_json():
    pairs = _list_pair_metadata(Component())

    assert pairs == [{"tag": "pair1", "label": "Identity Pair 1"}]
    assert json.loads(json.dumps(pairs)) == pairs


def test_air_block_detection_returns_python_string_tag():
    tag = _find_air_block_tag(Geometry())

    assert tag == "air"
    assert type(tag) is str
