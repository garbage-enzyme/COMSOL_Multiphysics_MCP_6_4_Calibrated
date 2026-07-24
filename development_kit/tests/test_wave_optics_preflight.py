"""Mock gates for threshold-free, read-only Wave Optics preflight evidence."""

from __future__ import annotations

import hashlib

import pytest
from src.tools.wave_optics_preflight import (
    EvidenceLedger,
    collect_preflight_foundation,
    collect_wave_optics_preflight,
)


class MetadataOnlyModel:
    def __init__(self, path):
        self._path = path

    def file(self):
        return str(self._path)

    def name(self):
        return "LoadedModel"

    def version(self):
        return "6.4.0.293"

    @property
    def java(self):
        raise AssertionError("foundation collector must not touch clientapi")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_evidence_ledger_has_stable_status_precedence():
    ledger = EvidenceLedger()
    assert ledger.inspection_status == "complete"
    ledger.add("warning", "warning_code", "warning")
    assert ledger.inspection_status == "complete"
    ledger.add("unknown", "unknown_code", "unknown")
    assert ledger.inspection_status == "partial"
    ledger.add("integrity_error", "integrity_code", "blocked")
    assert ledger.inspection_status == "integrity_blocked"


def test_foundation_reports_evidence_only_and_preserves_source(tmp_path, monkeypatch):
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable model bytes")
    source_hash = _hash(source)
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {
            "session": {"connected": True},
            "lease": {"state": "absent"},
            "external_solver_processes": [],
            "collision": False,
        },
    )

    result = collect_preflight_foundation(
        MetadataOnlyModel(source),
        model_name="ExactModel",
        session_state={"connected": True},
        active_profile="wave_optics",
        expected_source_path=str(source),
        expected_source_sha256=source_hash,
    )

    assert result["inspection_status"] == "partial"
    assert result["assessment"] == {
        "mode": "evidence_only",
        "project_verdict": None,
        "long_sweep_recommendation": None,
    }
    assert result["provenance"]["source_sha256"] == source_hash
    assert result["ownership"]["solve_permitted"] is True
    assert result["incidence"]["physical_polarization_evidence"] == "label_only"
    assert result["next_call"]["available"] is False
    assert result["next_call"]["missing_evidence"] == [
        "topology",
        "periodicity",
        "ports",
        "incidence",
        "wavelength",
        "mesh_study_results",
    ]
    assert _hash(source) == source_hash


@pytest.mark.parametrize("mismatch", ["path", "hash"])
def test_foundation_blocks_only_declared_integrity_mismatch(tmp_path, monkeypatch, mismatch):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False},
    )
    kwargs = {
        "expected_source_path": str(source),
        "expected_source_sha256": _hash(source),
    }
    if mismatch == "path":
        kwargs["expected_source_path"] = str(tmp_path / "other.mph")
    else:
        kwargs["expected_source_sha256"] = "0" * 64

    result = collect_preflight_foundation(
        MetadataOnlyModel(source),
        model_name="ExactModel",
        session_state={},
        active_profile="full",
        **kwargs,
    )

    assert result["inspection_status"] == "integrity_blocked"
    assert result["next_call"]["available"] is False
    codes = {item["code"] for item in result["evidence"]["integrity_errors"]}
    assert f"source_{mismatch}_mismatch" in codes


def test_foundation_treats_solver_collision_as_integrity_blocker(tmp_path, monkeypatch):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": True, "external_solver_processes": [{"pid": 1}]},
    )

    result = collect_preflight_foundation(
        MetadataOnlyModel(source),
        model_name="ExactModel",
        session_state={},
        active_profile="core",
    )

    assert result["inspection_status"] == "integrity_blocked"
    assert result["ownership"]["solve_permitted"] is False


def test_foundation_requires_exact_nonempty_model_name(tmp_path):
    with pytest.raises(ValueError, match="exact and non-empty"):
        collect_preflight_foundation(
            MetadataOnlyModel(tmp_path / "missing.mph"),
            model_name="",
            session_state={},
            active_profile="full",
        )


class FakeSelection:
    def __init__(self, entities=None, error=None):
        self._entities = entities or []
        self._error = error

    def entities(self):
        if self._error:
            raise RuntimeError(self._error)
        return self._entities


class FakeContainer:
    def __init__(self, items=None):
        self.items = items or {}

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[str(tag)]


class FakeFeature:
    def __init__(self, tag, kind, *, props=None, selections=None, children=None, label=None):
        self.tag = tag
        self.kind = kind
        self.props = props or {}
        self.selections = selections or {}
        self.children = FakeContainer(children)
        self._label = label or tag

    def getType(self):
        return self.kind

    def label(self):
        return self._label

    def getString(self, name):
        if name not in self.props:
            raise RuntimeError("property unavailable")
        return self.props[name]

    def selection(self, name=None):
        key = name or "default"
        value = self.selections.get(key)
        if isinstance(value, Exception):
            raise value
        return FakeSelection(value)

    def feature(self):
        return self.children


class FakeMaterial(FakeFeature):
    def propertyGroup(self, name):
        assert name == "def"
        return FakeFeature("def", "Basic", props={"relpermittivity": "1"})


class FakeGeometry(FakeFeature):
    centers = {
        1: [0, 0.5, 0.5], 2: [1, 0.5, 0.5],
        3: [0.5, 0, 0.5], 4: [0.5, 1, 0.5],
        5: [0.5, 0.5, 0], 6: [0.5, 0.5, 1],
    }
    normals = {
        1: [-1, 0, 0], 2: [1, 0, 0],
        3: [0, -1, 0], 4: [0, 1, 0],
        5: [0, 0, -1], 6: [0, 0, 1],
    }

    def getNBoundaries(self):
        return 6

    def getNDomains(self):
        return 1

    def getSDim(self):
        return 3

    def getUpDown(self):
        return [[1, 0, 1, 0, 1, 0], [0, 1, 0, 1, 0, 1]]

    def getBoundingBox(self):
        return [0, 1, 0, 1, 0, 1]

    def faceParamRange(self, _number):
        return [0, 1, 0, 1]

    def faceX(self, number, _point):
        return [self.centers[number]]

    def faceNormal(self, number, _point):
        return [self.normals[number]]


class FakeMesh(FakeFeature):
    def __init__(self, elements, features=None):
        super().__init__("mesh1", "MeshSequence", children=features)
        self.elements = elements

    def getNumElem(self):
        return self.elements

    def getNumVertex(self):
        return self.elements // 2


class FakeComponent:
    def __init__(self, *, missing_rdir=False, mismatched_floquet=False, absent_excited=False, empty_mesh=False, inaccessible_incidence=False, mesh_case="valid"):
        fpc_x = [1, 3, 2] if mismatched_floquet else [1, 2]
        children = {
            "fpc1": FakeFeature("fpc1", "PeriodicCondition", selections={"default": fpc_x}, props={"PeriodicType": "Floquet"}),
            "fpc2": FakeFeature("fpc2", "PeriodicCondition", selections={"default": [3, 4]}, props={"PeriodicType": "Floquet"}),
            "pport1": FakeFeature("pport1", "PeriodicPort", selections={"default": [6]}),
            "pport2": FakeFeature("pport2", "PeriodicPort", selections={"default": [5]}),
        }
        if not missing_rdir:
            children["rdir1"] = FakeFeature("rdir1", "ReferenceDirection", selections={"default": [10]})
        props = {} if inaccessible_incidence else {
            "Polarization": "LinearPol", "LinearPol": "S",
            "alpha1_inc": "theta", "alpha2_inc": "phi",
        }
        ps = FakeFeature(
            "ps1", "PeriodicStructure", props=props, children=children,
            selections={"allBoundaries": [1, 2, 3, 4, 5, 6], "excitedPortSelection": [] if absent_excited else [6]},
        )
        ewfd = FakeFeature("ewfd", "ElectromagneticWavesFrequencyDomain", children={"ps1": ps})
        fin = FakeFeature("fin", "FormUnion", props={"action": "union", "createpairs": "off"})
        geom = FakeGeometry("geom1", "Geometry", children={"fin": fin})
        mesh_features = {
            "ft_x": FakeFeature("ft_x", "FreeTri", selections={"default": [1]}),
            "cp_x": FakeFeature("cp_x", "CopyFace", selections={"source": [1], "destination": [2]}),
            "ft_y": FakeFeature("ft_y", "FreeTri", selections={"default": [3]}),
            "cp_y": FakeFeature("cp_y", "CopyFace", selections={"source": [3], "destination": [4]}),
            "ftet1": FakeFeature("ftet1", "FreeTet", selections={"default": [1]}),
        }
        if mesh_case == "missing_copy_y":
            del mesh_features["cp_y"]
        elif mesh_case == "wrong_order":
            mesh_features = {
                "cp_x": mesh_features["cp_x"],
                "ft_x": mesh_features["ft_x"],
                "ft_y": mesh_features["ft_y"],
                "cp_y": mesh_features["cp_y"],
                "ftet1": mesh_features["ftet1"],
            }
        self._physics = FakeContainer({"ewfd": ewfd})
        self._geom = FakeContainer({"geom1": geom})
        self._mesh = FakeContainer({"mesh1": FakeMesh(0 if empty_mesh else 1200, mesh_features)})
        self._materials = FakeContainer({"mat1": FakeMaterial("mat1", "Common", selections={"default": [1]})})

    def physics(self):
        return self._physics

    def geom(self):
        return self._geom

    def mesh(self):
        return self._mesh

    def material(self):
        return self._materials

    def pair(self):
        return FakeContainer()


class FakeStudy:
    def __init__(self, linked=True):
        props = {"plist": "wl" if linked else "5e-6", "punit": "m"}
        self._features = FakeContainer({"wl_step": FakeFeature("wl_step", "Wavelength", props=props)})

    def feature(self):
        return self._features


class FakeJavaModel:
    def __init__(self, component, linked=True):
        self._components = FakeContainer({"comp1": component})
        self._studies = FakeContainer({"std1": FakeStudy(linked)})

    def component(self):
        return self._components

    def study(self):
        return self._studies


class FullFakeModel(MetadataOnlyModel):
    def __init__(self, path, **fixture):
        super().__init__(path)
        linked = fixture.pop("linked", True)
        self._java = FakeJavaModel(FakeComponent(**fixture), linked=linked)
        self._linked = linked

    @property
    def java(self):
        return self._java

    def parameters(self, evaluate=False):
        assert evaluate is False
        return {"wl": "5[um]", "theta": "0[deg]"}

    def solutions(self):
        return ["Solution 1"]

    def datasets(self):
        return ["Study 1//Solution 1"]


def _full_result(tmp_path, monkeypatch, *, active_profile="wave_optics", **fixture):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"immutable fixture")
    monkeypatch.setattr(
        "src.tools.wave_optics_preflight.ownership_manager.status",
        lambda **_kwargs: {"collision": False, "session": {"connected": True}},
    )
    result = collect_wave_optics_preflight(
        FullFakeModel(source, **fixture),
        model_name="ExactModel",
        session_state={"connected": True},
        active_profile=active_profile,
        expected_component_tag="comp1",
        expected_physics_tag="ewfd",
        expected_study_tag="std1",
        expected_source_path=str(source),
        expected_source_sha256=_hash(source),
        target_wavelength_parameter="wl",
    )
    assert _hash(source) == result["provenance"]["source_sha256"]
    return result


def test_full_preflight_collects_read_only_wave_optics_evidence(tmp_path, monkeypatch):
    result = _full_result(tmp_path, monkeypatch)

    assert result["topology"]["domain_count"] == 1
    assert result["topology"]["form_finalization"]["properties"]["action"] == "union"
    assert len(result["periodicity"]["floquet_features"]) == 2
    assert result["ports"]["excited_port_selection"] == [6]
    assert result["incidence"]["raw_properties"]["LinearPol"] == "S"
    assert result["incidence"]["physical_polarization_evidence"] == "label_only"
    assert result["wavelength"]["structurally_linked"] is True
    assert result["mesh_study_results"]["meshes"][0]["element_count"] == 1200
    assert result["assessment"]["project_verdict"] is None
    assert result["next_call"]["available"] is True
    assert result["next_call"]["implementation_status"] == "experimental"
    assert result["next_call"]["missing_evidence"] == []


def test_complete_preflight_does_not_recommend_tool_outside_profile(tmp_path, monkeypatch):
    result = _full_result(tmp_path, monkeypatch, active_profile="core")

    assert result["inspection_status"] != "integrity_blocked"
    assert result["next_call"]["available"] is False
    assert result["next_call"]["implementation_status"] == "experimental"
    assert result["next_call"]["missing_evidence"] == []


@pytest.mark.parametrize(
    ("fixture", "code", "level"),
    [
        ({"missing_rdir": True}, "reference_direction_missing", "unknowns"),
        ({"mismatched_floquet": True}, "floquet_face_count_mismatch", "warnings"),
        ({"absent_excited": True}, "excited_port_selection_empty", "unknowns"),
        ({"linked": False}, "wavelength_link_missing", "unknowns"),
        ({"empty_mesh": True}, "mesh_empty", "warnings"),
        ({"inaccessible_incidence": True}, "incidence_properties_unreadable", "unknowns"),
    ],
)
def test_preflight_fixtures_preserve_failures_as_evidence(tmp_path, monkeypatch, fixture, code, level):
    result = _full_result(tmp_path, monkeypatch, **fixture)
    codes = {item["code"] for item in result["evidence"][level]}
    assert code in codes
