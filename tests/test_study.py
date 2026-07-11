"""Unit tests for study helpers without a COMSOL client."""

import pytest

from src.tools.study import _resolve_study_tag, list_studies


class FakeEntity:
    def __init__(self, label, features=None):
        self._label = label
        self.features = dict(features or {})

    def label(self):
        return self._label

    def feature(self):
        return FakeEntityList(self.features)


class FakeEntityList:
    def __init__(self, entities):
        self.entities = entities

    def tags(self):
        return list(self.entities)

    def get(self, tag):
        return self.entities[tag]


class JavaStringLike:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class JavaEntityList(FakeEntityList):
    def tags(self):
        return [JavaStringLike(tag) for tag in self.entities]


class JavaEntity(FakeEntity):
    def feature(self):
        return JavaEntityList(self.features)


class FakeJava:
    def __init__(self, studies):
        self.studies = studies

    def study(self):
        return FakeEntityList(self.studies)


class FakeModel:
    def __init__(self, studies):
        self.java = FakeJava(studies)


class JavaTagJava(FakeJava):
    def study(self):
        return JavaEntityList(self.studies)


def make_model():
    return FakeModel(
        {
            "std1": FakeEntity(
                "研究 1",
                {
                    "stat": FakeEntity("Stationary"),
                    "param": FakeEntity("Parametric Sweep"),
                },
            ),
            "std2": FakeEntity("Study 2", {"time": FakeEntity("Transient")}),
        }
    )


def test_list_studies_returns_tags_labels_and_steps():
    result = list_studies(make_model())

    assert result == {
        "success": True,
        "studies": [
            {
                "tag": "std1",
                "label": "研究 1",
                "steps": [
                    {"tag": "stat", "label": "Stationary"},
                    {"tag": "param", "label": "Parametric Sweep"},
                ],
            },
            {
                "tag": "std2",
                "label": "Study 2",
                "steps": [{"tag": "time", "label": "Transient"}],
            },
        ],
        "count": 2,
    }


def test_resolve_study_tag_accepts_tag_or_unicode_label():
    model = make_model()

    assert _resolve_study_tag(model, "std1") == "std1"
    assert _resolve_study_tag(model, "研究 1") == "std1"


def test_resolve_study_tag_reports_available_tags():
    with pytest.raises(ValueError, match="std1"):
        _resolve_study_tag(make_model(), "missing")


def test_study_helpers_normalize_java_string_tags():
    model = FakeModel(
        {"std1": JavaEntity("研究 1", {"step1": FakeEntity("稳态 1")})}
    )
    model.java = JavaTagJava(model.java.studies)

    result = list_studies(model)

    assert result["studies"][0]["tag"] == "std1"
    assert result["studies"][0]["steps"][0]["tag"] == "step1"
    assert _resolve_study_tag(model, "研究 1") == "std1"
