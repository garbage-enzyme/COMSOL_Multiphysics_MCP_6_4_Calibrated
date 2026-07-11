"""Unit tests for parameter tools without a COMSOL client."""

from src.tools import parameters


class FakeSweep:
    def __init__(self, label="Parametric Sweep"):
        self._label = label
        self.properties = {}
        self.enabled = False

    def label(self):
        return self._label

    def set(self, name, value):
        self.properties[name] = value

    def active(self, enabled):
        self.enabled = enabled


class FakeFeatureList:
    def __init__(self, features):
        self.features = features

    def tags(self):
        return list(self.features)

    def get(self, tag):
        return self.features[tag]


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
