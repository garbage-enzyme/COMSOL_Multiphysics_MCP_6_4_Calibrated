"""Unit gates for policy-separated one-point Wave Optics evidence."""

from __future__ import annotations

import hashlib
import json

import numpy as np

from src.tools.wave_optics_audit import (
    _load_air_reference,
    evaluate_validation_policy,
    run_wave_optics_point_audit,
)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _measurement(*, absorption=1.2, closure=0.5, evidence_level="label_only"):
    return {
        "power": {
            "complete": True,
            "R": 0.2,
            "T": 0.1,
            "A": absorption,
            "closure_abs": closure,
        },
        "wavelength": {
            "complete": True,
            "absolute_difference_m": 2e-12,
            "relative_difference": 4e-7,
        },
        "polarization": {"evidence_level": evidence_level},
        "losses": {"items": []},
        "mesh": {"element_count": 1000, "unchanged_during_audit": True},
        "integrity": {"source_unchanged": True},
    }


def test_same_evidence_can_fail_or_pass_two_declared_policies():
    evidence = _measurement(absorption=0.7, closure=0.05)
    strict = evaluate_validation_policy(
        evidence, {"tolerances": {"closure_abs": 0.01}}
    )
    permissive = evaluate_validation_policy(
        evidence, {"tolerances": {"closure_abs": 0.1}}
    )

    assert strict["overall"] == "fail"
    assert permissive["overall"] == "pass"
    assert evidence["power"]["closure_abs"] == 0.05


def test_a_above_one_is_classified_only_under_passive_normalized_assumptions():
    evidence = _measurement()
    passive = evaluate_validation_policy(
        evidence,
        {
            "assumptions": {"passive": True, "port_power_normalized": True},
            "tolerances": {"quantity_bounds_margin": 0.0},
        },
    )
    gain_or_unknown = evaluate_validation_policy(
        evidence,
        {
            "assumptions": {"passive": False, "port_power_normalized": True},
            "tolerances": {"quantity_bounds_margin": 0.0},
        },
    )

    assert passive["overall"] == "fail"
    assert gain_or_unknown["overall"] == "pass"
    assert gain_or_unknown["rules"][0]["outcome"] == "not_applicable"


def test_label_only_polarization_is_missing_only_when_policy_requires_incident_evidence():
    result = evaluate_validation_policy(
        _measurement(), {"required_evidence": ["incident_polarization"]}
    )

    assert result["overall"] == "missing"
    assert result["rules"][0]["measured"] is False


def test_wavelength_differences_remain_raw_until_policy_supplies_tolerances():
    evidence = _measurement(absorption=0.3, closure=0.0)
    strict = evaluate_validation_policy(
        evidence,
        {"tolerances": {"wavelength_abs_m": 1e-12, "wavelength_rel": 1e-7}},
    )
    loose = evaluate_validation_policy(
        evidence,
        {"tolerances": {"wavelength_abs_m": 3e-12, "wavelength_rel": 5e-7}},
    )

    assert strict["overall"] == "fail"
    assert loose["overall"] == "pass"
    assert evidence["wavelength"]["absolute_difference_m"] == 2e-12


def test_loss_without_normalization_remains_raw_evidence():
    evidence = _measurement(absorption=0.3, closure=0.0)
    evidence["losses"]["items"] = [
        {"label": "Au", "value": 0.2, "expression": "intAu(ewfd.Qh)"}
    ]
    result = evaluate_validation_policy(
        evidence, {"required_evidence": ["volume_loss"]}
    )

    assert result["overall"] == "pass"
    assert "normalized_value" not in evidence["losses"]["items"][0]


def test_air_reference_requires_matching_config_and_field_statistics(tmp_path):
    artifact = tmp_path / "air.json"
    payload = {
        "config_id": "air-v4",
        "component_statistics": {
            axis: {"complex_mean": {"real": value, "imag": 0.0}}
            for axis, value in (("x", 1.0), ("y", 0.0), ("z", 0.0))
        },
        "stokes_xy": {"S0": 1.0, "S3": 0.0},
    }
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    valid, warnings = _load_air_reference(str(artifact), "air-v4")
    invalid, mismatch = _load_air_reference(str(artifact), "other")

    assert valid["config_id"] == "air-v4"
    assert not warnings
    assert invalid is None
    assert mismatch[0]["code"] == "air_reference_config_mismatch"


def test_valid_incident_reference_supports_declared_vector_policy_and_config_gate():
    evidence = _measurement(
        absorption=0.3, closure=0.0, evidence_level="incident_reference"
    )
    evidence["polarization"]["incident_reference"] = {
        "config_id": "air-v4",
        "component_statistics": {
            "x": {"complex_mean": {"real": 1.0, "imag": 0.0}},
            "y": {"complex_mean": {"real": 0.0, "imag": 0.0}},
            "z": {"complex_mean": {"real": 0.0, "imag": 0.0}},
        },
        "stokes_xy": {"S0": 1.0, "S3": 0.0},
    }
    valid = evaluate_validation_policy(
        evidence,
        {
            "required_evidence": ["incident_polarization"],
            "polarization": {
                "reference_config_id": "air-v4",
                "target_vector": [1, 0, 0],
                "max_cross_power_fraction": 1e-6,
                "max_ellipticity": 1e-6,
            },
        },
    )
    mismatch = evaluate_validation_policy(
        evidence,
        {"polarization": {"reference_config_id": "different"}},
    )

    assert valid["overall"] == "pass"
    assert mismatch["overall"] == "fail"


class Container:
    def __init__(self, items):
        self.items = items

    def tags(self):
        return list(self.items)

    def get(self, tag):
        return self.items[str(tag)]


class Selection:
    def __init__(self, entities):
        self._entities = entities

    def entities(self, *_args):
        return self._entities


class Mesh:
    def getNumElem(self):
        return 1200

    def getNumVertex(self):
        return 600


class Component:
    def __init__(self):
        self._physics = Container({"ewfd": object()})
        self._mesh = Container({"mesh1": Mesh()})

    def physics(self):
        return self._physics

    def mesh(self):
        return self._mesh

    def selection(self, tag):
        assert tag == "topair"
        return Selection([3])


class Step:
    def __init__(self):
        self.values = {}

    def set(self, name, value):
        self.values[name] = value


class Study:
    def __init__(self, model):
        self.model = model
        self.step = Step()

    def feature(self):
        return Container({"wl_step": self.step})

    def run(self):
        self.model.solved = True
        if self.model.drift_source:
            self.model.path.write_bytes(self.model.path.read_bytes() + b"drift")


class Parameters:
    def __init__(self):
        self.values = {"wl": "4.37[um]"}

    def set(self, name, value):
        self.values[name] = value


class JavaModel:
    def __init__(self, model):
        self._component = Component()
        self._study = Study(model)
        self._param = Parameters()

    def component(self, tag=None):
        return Container({"comp1": self._component}) if tag is None else self._component

    def study(self, tag=None):
        return Container({"std1": self._study}) if tag is None else self._study

    def param(self):
        return self._param


class AuditModel:
    def __init__(self, path, *, absorption=1.2, nonfinite=False, drift_source=False):
        self.path = path
        self.absorption = absorption
        self.nonfinite = nonfinite
        self.drift_source = drift_source
        self.solved = False
        self.java = JavaModel(self)

    def file(self):
        return str(self.path)

    def name(self):
        return "AuditModel"

    def evaluate(self, expression):
        if isinstance(expression, list):
            if expression == ["wl", "c_const/ewfd.freq"]:
                return [4.37e-6, 4.37e-6]
            if expression[-1] == "dom":
                return [
                    np.array([1 + 0j, 1 + 0j, 1 + 0j]),
                    np.array([0 + 0j, 0 + 0j, 0 + 0j]),
                    np.array([0 + 0j, 0 + 0j, 0 + 0j]),
                    np.array([0.1, 0.2, 0.3]),
                    np.array([0.1, 0.2, 0.3]),
                    np.array([0.8, 0.85, 0.9]),
                    np.array([3, 3, 3]),
                ]
        values = {
            "ewfd.Rtotal": 0.2,
            "ewfd.Ttotal": 0.1,
            "ewfd.Atotal": float("nan") if self.nonfinite else self.absorption,
            "4.37[um]": 4.37e-6,
            "intAu(ewfd.Qh)": 0.2,
        }
        return values[expression]


def _run(tmp_path, monkeypatch, **model_options):
    source = tmp_path / "source.mph"
    source.write_bytes(b"immutable")
    model = AuditModel(source, **model_options)
    monkeypatch.setattr(
        "src.tools.wave_optics_audit.collect_wave_optics_preflight",
        lambda *_args, **_kwargs: {
            "inspection_status": "complete",
            "evidence": {"observations": [], "warnings": [], "unknowns": [], "integrity_errors": []},
        },
    )
    monkeypatch.setattr(
        "src.tools.wave_optics_audit.ownership_manager.status",
        lambda **_kwargs: {"collision": False, "lease": {"state": "active"}},
    )
    monkeypatch.setattr(
        "src.tools.wave_optics_audit._validate_ascii_dir",
        lambda _value: tmp_path / "artifacts",
    )
    result = run_wave_optics_point_audit(
        model,
        model_name="AuditModel",
        component_tag="comp1",
        physics_tag="ewfd",
        study_tag="std1",
        wavelength_value=4.37,
        wavelength_unit="um",
        wavelength_parameter="wl",
        study_step_tag="wl_step",
        study_step_property="plist",
        expected_source_sha256=_hash(source),
        config_id="unit-audit",
        artifact_dir=str(tmp_path / "ascii_artifacts"),
        top_air_selection="topair",
        top_air_coordinate_range={"x": [0, 1], "y": [0, 1], "z": [0.7, 1]},
        loss_map=[{"label": "Au", "domains": [2], "expression": "intAu(ewfd.Qh)"}],
        session_state={"connected": True},
        active_profile="wave_optics",
        ownership_preflight={"ready": True},
    )
    return result, source


def test_evidence_only_a_above_one_is_preserved_without_project_verdict(tmp_path, monkeypatch):
    result, _source = _run(tmp_path, monkeypatch)

    assert result["audit_status"] == "measurement_complete"
    assert result["measurement"]["power"]["A"] == 1.2
    assert result["assessment"]["project_verdict"] is None
    assert result["measurement"]["polarization"]["evidence_level"] == "structure_total_field"
    assert result["measurement"]["polarization"]["structure_total_field"]["diagnostic_only"] is True
    manifest = json.loads(open(result["artifacts"]["manifest"], encoding="utf-8").read())
    assert manifest["audit_status"] == "measurement_complete"
    assert manifest["config_sha256"] == result["measurement"]["provenance"]["config_sha256"]
    assert manifest["preflight"]["inspection_status"] == "complete"


def test_nonfinite_power_is_an_integrity_blocker_and_is_journaled(tmp_path, monkeypatch):
    result, _source = _run(tmp_path, monkeypatch, nonfinite=True)

    assert result["audit_status"] == "integrity_blocked"
    assert result["measurement"]["integrity_errors"][0]["code"] == "nonfinite_power"
    assert open(result["artifacts"]["csv"], encoding="utf-8").read()


def test_source_hash_drift_after_solve_is_an_integrity_blocker(tmp_path, monkeypatch):
    result, source = _run(tmp_path, monkeypatch, drift_source=True)

    assert result["audit_status"] == "integrity_blocked"
    assert result["measurement"]["integrity"]["source_unchanged"] is False
    assert _hash(source) == result["measurement"]["provenance"]["source_sha256_after"]
