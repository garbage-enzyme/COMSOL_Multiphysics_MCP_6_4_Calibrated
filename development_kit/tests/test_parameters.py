"""Unit tests for parameter tools without a COMSOL client."""

import sys
from types import SimpleNamespace

from src.tools import parameters


class FakeSweep:
    def __init__(self, label="Parametric Sweep", kind="Parametric"):
        self._label = label
        self.kind = kind
        self.properties = {
            "pname": ["old"],
            "plistarr": ["1 2"],
            "punit": ["m"],
            "sweeptype": "sparse",
        }
        self.enabled = False
        self.fail_on = None

    def getType(self):
        return self.kind

    def label(self):
        return self._label

    def set(self, name, value):
        if name == self.fail_on:
            raise RuntimeError(f"forced {name} failure")
        self.properties[name] = value

    def active(self, enabled):
        self.enabled = enabled

    def isActive(self):
        return self.enabled

    def getStringArray(self, name):
        return self.properties[name]

    def getString(self, name):
        return self.properties[name]


class FakeFeatureList:
    def __init__(self, features):
        self.features = features

    def tags(self):
        return list(self.features)

    def get(self, tag):
        return self.features[tag]

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

    def get(self, tag):
        return self.features[str(tag)]


class FakeStudy:
    def __init__(self, features=None):
        self.features = dict(features or {})

    def feature(self):
        return FakeFeatureList(self.features)

    def create(self, tag, feature_type):
        assert feature_type == "Parametric"
        feature = FakeSweep()
        self.features[tag] = feature
        return feature

    def label(self):
        return "Study 1"


class JavaTagStudy(FakeStudy):
    def feature(self):
        return JavaTagFeatureList(self.features)


class FakeStudyList:
    def __init__(self, studies):
        self.studies = studies

    def tags(self):
        return list(self.studies)

    def get(self, tag):
        return self.studies[tag]


class JavaTagStudyList(FakeStudyList):
    def tags(self):
        return [JavaStringLike(tag) for tag in self.studies]


class FakeJava:
    def __init__(self, studies, java_study_tags=False):
        self.studies = studies
        self.java_study_tags = java_study_tags

    def study(self, tag=None):
        if tag is None:
            list_type = JavaTagStudyList if self.java_study_tags else FakeStudyList
            return list_type(self.studies)
        return self.studies[tag]


class FakeModel:
    def __init__(self, studies, java_study_tags=False):
        self.java = FakeJava(studies, java_study_tags)


def test_java_string_array_uses_jpype_string_component_and_coerces_values(monkeypatch):
    observed = {}

    class FakeJString:
        pass

    def fake_jarray(component):
        observed["component"] = component

        def construct(values):
            observed["values"] = list(values)
            return tuple(values)

        return construct

    monkeypatch.setitem(
        sys.modules,
        "jpype",
        SimpleNamespace(JArray=fake_jarray, JString=FakeJString),
    )

    assert parameters._java_string_array(["wl", 2]) == ("wl", "2")
    assert observed == {"component": FakeJString, "values": ["wl", "2"]}


def test_setup_parametric_sweep_uses_clientapi_properties(monkeypatch):
    study = FakeStudy()
    model = FakeModel({"std1": study})
    monkeypatch.setattr(parameters, "_java_string_array", list)

    result = parameters.setup_parametric_sweep(
        model,
        "wl",
        ["4.0e-6", "4.1e-6"],
        parameter_unit="m",
    )

    sweep = study.features["param1"]
    assert result["success"] is True
    assert result["study"] == "std1"
    assert sweep.properties == {
        "pname": ["wl"],
        "plistarr": ["4.0e-6 4.1e-6"],
        "punit": ["m"],
        "sweeptype": "sparse",
    }
    assert sweep.enabled is True


def test_setup_parametric_sweep_reuses_existing_feature(monkeypatch):
    existing = FakeSweep()
    study = FakeStudy({"sweep_custom": existing})
    model = FakeModel({"std1": study})
    monkeypatch.setattr(parameters, "_java_string_array", list)

    result = parameters.setup_parametric_sweep(model, "theta", [0, 10, 20])

    assert result["sweep_tag"] == "sweep_custom"
    assert list(study.features) == ["sweep_custom"]
    assert existing.properties["plistarr"] == ["0 10 20"]
    assert existing.properties["punit"] == [""]


def test_setup_parametric_sweep_accepts_java_string_tags(monkeypatch):
    existing = FakeSweep()
    study = JavaTagStudy({"parametric1": existing})
    model = FakeModel({"std1": study})
    monkeypatch.setattr(parameters, "_java_string_array", list)

    result = parameters.setup_parametric_sweep(model, "wl", [1, 2])

    assert result["success"] is True
    assert result["sweep_tag"] == "parametric1"
    assert existing.properties["plistarr"] == ["1 2"]


def test_setup_parametric_sweep_normalizes_default_java_study_tag(monkeypatch):
    study = FakeStudy()
    model = FakeModel({"std1": study}, java_study_tags=True)
    monkeypatch.setattr(parameters, "_java_string_array", list)

    result = parameters.setup_parametric_sweep(model, "wl", [1, 2])

    assert result["study"] == "std1"
    assert type(result["study"]) is str


def test_setup_parametric_sweep_validates_inputs():
    model = FakeModel({})

    assert parameters.setup_parametric_sweep(model, "", [1])["success"] is False
    assert parameters.setup_parametric_sweep(model, "wl", [])["success"] is False


def test_parametric_sweep_uses_exact_type_and_rejects_ambiguous_candidates(monkeypatch):
    unrelated = FakeSweep(label="Parametric note", kind="Annotation")
    exact = FakeSweep(label="Custom", kind="Parametric")
    study = FakeStudy({"param_note": unrelated, "custom": exact})
    model = FakeModel({"std1": study})
    monkeypatch.setattr(parameters, "_java_string_array", list)

    result = parameters.setup_parametric_sweep(model, "wl", [1, 2])
    assert result["success"] is True
    assert result["sweep_tag"] == "custom"
    assert unrelated.properties["pname"] == ["old"]

    study.features["second"] = FakeSweep()
    ambiguous = parameters.setup_parametric_sweep(model, "wl", [1, 2])
    assert ambiguous["success"] is False
    assert "ambiguous" in ambiguous["error"]


def test_parametric_sweep_failure_restores_reused_or_removes_created(monkeypatch):
    monkeypatch.setattr(parameters, "_java_string_array", list)
    existing = FakeSweep()
    existing.fail_on = "plistarr"
    study = FakeStudy({"custom": existing})
    before = dict(existing.properties)

    reused = parameters.setup_parametric_sweep(FakeModel({"std1": study}), "wl", [3, 4])
    assert reused["success"] is False
    assert reused["rolled_back"] is False
    assert existing.properties["pname"] == before["pname"]

    class FailingCreatedStudy(FakeStudy):
        def create(self, tag, feature_type):
            feature = super().create(tag, feature_type)
            feature.fail_on = "plistarr"
            return feature

    created_study = FailingCreatedStudy()
    created = parameters.setup_parametric_sweep(FakeModel({"std1": created_study}), "wl", [3, 4])
    assert created["success"] is False
    assert created["rolled_back"] is True
    assert created_study.features == {}


class FakeParameterJava:
    def __init__(self, model):
        self.model = model

    def param(self):
        return self

    def remove(self, name):
        self.model.values.pop(name, None)
        self.model.descriptions.pop(name, None)

    def descr(self, name):
        return self.model.descriptions.get(name)

    def set(self, name, value, description):
        self.model.values[name] = value
        if description is None:
            self.model.descriptions[name] = None
        else:
            self.model.descriptions[name] = description


class FakeParameterModel:
    def __init__(self, *, fail_description=False):
        self.values = {"wl": "1[m]"}
        self.descriptions = {"wl": "old"}
        self.fail_description = fail_description
        self.java = FakeParameterJava(self)

    def parameters(self, evaluate=False):
        assert evaluate is False
        return dict(self.values)

    def parameter(self, name, value=None, evaluate=False):
        if value is not None:
            self.values[name] = value
            return None
        assert evaluate is False
        return self.values[name]

    def description(self, name, value=None):
        if value is not None:
            if self.fail_description:
                self.fail_description = False
                raise RuntimeError("description failure")
            self.descriptions[name] = value
            return None
        return self.descriptions.get(name, "")


def test_parameter_description_failure_restores_value_and_empty_description_clears():
    model = FakeParameterModel(fail_description=True)
    failed = parameters.set_parameter(model, "wl", "2[m]", description="new")
    assert failed["success"] is False
    assert failed["rolled_back"] is True
    assert model.values == {"wl": "1[m]"}
    assert model.descriptions == {"wl": "old"}

    cleared = parameters.set_parameter(model, "wl", "2[m]", description="")
    assert cleared == {
        "success": True,
        "parameter": "wl",
        "value": "2[m]",
        "description": "",
    }


def test_new_parameter_is_removed_when_description_fails():
    model = FakeParameterModel(fail_description=True)

    result = parameters.set_parameter(model, "theta", "10[deg]", description="angle")

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert "theta" not in model.values
    assert "theta" not in model.descriptions


def test_new_parameter_without_description_accepts_clientapi_empty_readback():
    model = FakeParameterModel()

    result = parameters.set_parameter(model, "theta", "10[deg]", description=None)

    assert result == {
        "success": True,
        "parameter": "theta",
        "value": "10[deg]",
        "description": "",
    }


def test_parameter_rollback_preserves_unset_clientapi_description():
    model = FakeParameterModel(fail_description=True)
    model.descriptions["wl"] = None

    result = parameters.set_parameter(model, "wl", "2[m]", description="new")

    assert result["success"] is False
    assert result["rolled_back"] is True
    assert model.values["wl"] == "1[m]"
    assert model.descriptions["wl"] is None
