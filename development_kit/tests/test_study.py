"""Unit tests for study helpers without a COMSOL client."""

import pytest
from src.tools.study import _resolve_study_tag, create_study, list_studies


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


def test_resolve_study_tag_propagates_backend_label_failure():
    class FailingLabelEntity(FakeEntity):
        def label(self):
            raise RuntimeError("label backend unavailable")

    model = FakeModel({"std1": FailingLabelEntity("unused")})

    with pytest.raises(RuntimeError, match="std1.*label backend unavailable"):
        _resolve_study_tag(model, "Study 1")


def test_study_helpers_normalize_java_string_tags():
    model = FakeModel(
        {"std1": JavaEntity("研究 1", {"step1": FakeEntity("稳态 1")})}
    )
    model.java = JavaTagJava(model.java.studies)

    result = list_studies(model)

    assert result["studies"][0]["tag"] == "std1"
    assert result["studies"][0]["steps"][0]["tag"] == "step1"
    assert _resolve_study_tag(model, "研究 1") == "std1"


def test_list_studies_propagates_step_inventory_failure():
    class FailingFeatureInventory(FakeEntity):
        def feature(self):
            raise RuntimeError("step inventory unavailable")

    model = FakeModel({"std1": FailingFeatureInventory("Study 1")})

    with pytest.raises(RuntimeError, match="step inventory unavailable"):
        list_studies(model)


def test_resolve_study_tag_normalizes_java_labels_and_rejects_duplicates():
    first = FakeEntity(JavaStringLike("Shared"))
    model = FakeModel({"std1": first})
    assert _resolve_study_tag(model, "Shared") == "std1"

    model.java.studies["std2"] = FakeEntity(JavaStringLike("Shared"))
    with pytest.raises(ValueError, match="ambiguous"):
        _resolve_study_tag(model, "Shared")


class MutableStudyStep:
    def __init__(self, fail_set=False):
        self.fail_set = fail_set
        self.properties = {}

    def set(self, name, value):
        if self.fail_set:
            raise RuntimeError("property failure")
        self.properties[name] = value


class MutableStudy:
    def __init__(self, *, fail_create=False, fail_set=False):
        self.fail_create = fail_create
        self.step = MutableStudyStep(fail_set)

    def create(self, tag, step_type):
        if self.fail_create:
            raise RuntimeError("step failure")

    def feature(self, tag):
        assert tag == "step1"
        return self.step


class MutableStudyList:
    def __init__(self, existing=(), *, fail_create=False, fail_set=False):
        self.studies = {tag: object() for tag in existing}
        self.fail_create = fail_create
        self.fail_set = fail_set

    def tags(self):
        return list(self.studies)

    def create(self, tag):
        study = MutableStudy(
            fail_create=self.fail_create,
            fail_set=self.fail_set,
        )
        self.studies[tag] = study
        return study

    def remove(self, tag):
        del self.studies[tag]


class MutableStudyJava:
    def __init__(self, studies):
        self.studies = studies

    def study(self):
        return self.studies


class MutableStudyModel:
    def __init__(self, studies):
        self.java = MutableStudyJava(studies)

    def name(self):
        return "model"


def test_create_study_rolls_back_step_and_tlist_failures():
    for options in ({"fail_create": True}, {"fail_set": True}):
        studies = MutableStudyList(**options)
        result = create_study(
            MutableStudyModel(studies),
            study_type="Transient",
            time_list=[0, 1],
        )

        assert result["success"] is False
        assert result["rolled_back"] is True
        assert studies.studies == {}


def test_create_study_validates_before_creation_and_uses_first_free_tag():
    studies = MutableStudyList(existing=("std1", "std3"))
    model = MutableStudyModel(studies)

    invalid = create_study(model, time_list=[float("inf")])
    created = create_study(model)

    assert invalid["success"] is False
    assert list(studies.studies) == ["std1", "std3", "std2"]
    assert created["study"] == "std2"


def test_create_study_never_uses_collection_size_for_default_tag():
    class SizeTrapStudyList(MutableStudyList):
        def size(self):
            raise AssertionError("collection size must not select a study tag")

    studies = SizeTrapStudyList(existing=("std1", "std3"))

    result = create_study(MutableStudyModel(studies))

    assert result["success"] is True
    assert result["study"] == "std2"
