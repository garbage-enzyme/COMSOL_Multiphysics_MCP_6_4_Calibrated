"""Solver-free thermo-optomechanical contract, durability, and cleanup tests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

import pytest
import src.jobs.thermo_optomechanical_replay as replay_module
import src.jobs.thermo_optomechanical_replay_execution as replay_execution_module
from pydantic import ValidationError
from src.jobs.manager import JobManager, _worker_module
from src.jobs.store import JobStore, process_identity
from src.jobs.thermo_optomechanical_replay import (
    THERMO_OPTOMECHANICAL_CONTROLS,
    THERMO_OPTOMECHANICAL_STAGES,
    normalize_thermo_optomechanical_replay_spec,
)
from src.jobs.thermo_optomechanical_replay_execution import (
    ThermoOptomechanicalComsolExecutor,
)
from src.jobs.thermo_optomechanical_replay_rows import (
    append_thermo_optomechanical_stage_row,
    build_stage_evidence,
    read_thermo_optomechanical_stage_rows,
)
from src.jobs.thermo_optomechanical_replay_runner import (
    run_thermo_optomechanical_replay,
)
from src.jobs.thermo_optomechanical_replay_worker import _run as run_worker
from src.tools.jobs import _preview_job_spec

from comsol_mcp.contracts.thermo_optomechanical import (
    ThermoOpticalMaterialValidity,
    ThermoOptomechanicalReplayManifest,
)
from development_kit.scripts import thermo_optomechanical_licensed_gate as licensed_gate


def _material_state(source: Path) -> dict:
    return {
        "schema_name": "comsol_mcp.thermal_material_state_reference",
        "schema_version": "1.0.0",
        "ledger_sha256": "a" * 64,
        "material_identity_sha256": "a" * 64,
        "sample_identity_sha256": "b" * 64,
        "state_id": "state_a",
        "classification": "fitted",
        "validity": {
            "wavelength_min_m": 1.0e-6,
            "wavelength_max_m": 3.0e-6,
            "temperature_min_K": 250.0,
            "temperature_max_K": 500.0,
        },
        "target": {
            "component_tag": "comp1",
            "material_tag": "mat1",
            "property_group_tag": "def",
            "property_key": "relpermittivity",
        },
        "source_model_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "expected_property_values": ["2.25"],
        "expected_function_tags": [],
        "application_receipt_sha256": "d" * 64,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"wavelength_min_m": 3.0e-6, "wavelength_max_m": 1.0e-6},
        {"temperature_min_K": 500.0, "temperature_max_K": 250.0},
    ],
)
def test_material_validity_rejects_inverted_ranges(overrides):
    value = {
        "wavelength_min_m": 1.0e-6,
        "wavelength_max_m": 3.0e-6,
        "temperature_min_K": 250.0,
        "temperature_max_K": 500.0,
        **overrides,
    }

    with pytest.raises(ValidationError):
        ThermoOpticalMaterialValidity.model_validate(value)


def test_licensed_gate_cleanup_failure_preserves_verdict_and_writes_fallback(tmp_path, monkeypatch):
    class BrokenOwner:
        def status(self, *, require_fresh_inventory):
            assert require_fresh_inventory is True
            raise RuntimeError("injected inventory failure")

    original_atomic_json = licensed_gate._atomic_json

    def fail_primary_receipt(path, value):
        if path.name == "licensed_acceptance.json":
            raise OSError("injected primary receipt failure")
        original_atomic_json(path, value)

    monkeypatch.setattr(licensed_gate, "_atomic_json", fail_primary_receipt)
    result = {
        "schema_name": licensed_gate.SCHEMA_NAME,
        "schema_version": licensed_gate.SCHEMA_VERSION,
        "success": False,
        "error": {"type": "RuntimeError", "message": "original gate failure"},
    }

    finalized = licensed_gate._finalize_gate_result(tmp_path, result, BrokenOwner())

    assert finalized["error"]["message"] == "original gate failure"
    assert finalized["cleanup"]["inventory_complete"] is False
    assert finalized["cleanup"]["error"]["message"] == "injected inventory failure"
    assert finalized["receipt_write_error"]["type"] == "OSError"
    fallback = json.loads((tmp_path / "licensed_acceptance_failure.json").read_text("utf-8"))
    assert fallback["success"] is False
    assert fallback["error"]["message"] == "original gate failure"


def _raw_spec(root: Path) -> dict:
    source = root / "source.mph"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"synthetic-thermo-optomechanical-source")
    manifest = {
        "job_type": "thermo_optomechanical_replay",
        "source_model_path": str(source),
        "source_model_relative_identity": "fixtures/thermo_optomechanical/source.mph",
        "optical_configuration_sha256": "c" * 64,
        "material_state": _material_state(source),
        "model_contract": {
            "component_tag": "comp1",
            "heat_transfer_tag": "ht",
            "solid_mechanics_tag": "solid",
            "moving_mesh_tag": "ale",
            "wave_optics_tag": "ewfd",
            "thermal_structure_study_tag": "std_ts",
            "transfer_study_tag": "std_ale",
            "optical_study_tag": "std_opt",
            "initial_temperature_parameter": "Tinit",
            "ambient_temperature_parameter": "Tamb",
            "applied_temperature_parameter": "Tapp",
            "cte_parameter": "alpha",
            "reference_temperature_parameter": "Tref",
            "wavelength_parameter": "lambda0",
            "polarization_parameter": "pol",
            "deformation_scale_parameter": "dscale",
            "heated_domain_selection": "sel_heat",
            "structural_domain_selection": "sel_solid",
            "fixed_boundary_selection": "sel_fixed",
            "thermal_boundary_selection": "sel_temp",
            "optical_domain_selection": "sel_opt",
            "mesh_tag": "mesh1",
            "expressions": {
                "temperature_min": "Tmin",
                "temperature_max": "Tmax",
                "displacement_max": "umax",
                "stress_max": "smax",
                "heat_source_integral": "Qin",
                "boundary_loss_integral": "Qout",
                "delta_length": "dL",
                "minimum_mesh_quality": "qmin",
                "mesh_element_count": "nelem",
                "mesh_vertex_count": "nvert",
                "reflectance": "R",
                "transmittance": "Tr",
                "absorptance": "A",
            },
        },
        "thermal_load": {
            "temperature_unit": "K",
            "heat_source_unit": "W/m^3",
            "convection_coefficient_unit": "W/(m^2*K)",
            "initial_temperature_K": 300.0,
            "ambient_temperature_K": 300.0,
            "applied_temperature_K": 400.0,
            "volumetric_heat_source_W_per_m3": 0.0,
            "convection_coefficient_W_per_m2_K": 10.0,
        },
        "thermal_expansion": {
            "coefficient_input_type": "secant_coefficient",
            "coefficient_per_K": 1.0e-5,
            "reference_temperature_K": 300.0,
            "reference_length_m": 0.01,
            "measurement_axis": "x",
        },
        "deformation_transfer": {
            "method": "moving_mesh_spatial_frame",
            "displacement_frame": "spatial",
            "topology_change_allowed": False,
            "deformation_scale": 1.0,
        },
        "optical_replay": {
            "wavelengths_m": [1.5e-6, 2.0e-6],
            "branches": ["TE"],
            "wavelength_coordinate": "vacuum_wavelength_m",
        },
        "validation_controls": list(THERMO_OPTOMECHANICAL_CONTROLS),
        "acceptance_policy": {
            "expansion_relative_tolerance": 0.02,
            "zero_control_absolute_tolerance_m": 1.0e-10,
            "energy_relative_tolerance": 1.0e-6,
            "rta_closure_absolute_tolerance": 1.0e-8,
            "minimum_mesh_quality": 0.1,
            "maximum_displacement_to_length": 0.01,
        },
        "resource_policy": {
            "wall_time_budget_seconds": 300.0,
            "minimum_next_point_seconds": 1.0,
        },
        "cores": 1,
        "wall_time_budget_seconds": 300,
        "version": "6.4",
        "max_retries": 1,
        "continue_on_error": False,
    }
    specification = root / "thermo_optomechanical_specification.json"
    specification.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "job_type": "thermo_optomechanical_replay",
        "specification_path": str(specification),
        "specification_sha256": hashlib.sha256(specification.read_bytes()).hexdigest(),
    }


def _payload(stage_id: str, spec: dict) -> dict:
    contract = spec["model_contract"]
    if stage_id == "preflight":
        return {
            "required_products": ["COMSOL", "Heat", "Structural", "Wave Optics"],
            "available_products": ["COMSOL", "Heat", "Structural", "Wave Optics"],
            "interface_tags": {
                "heat_transfer": contract["heat_transfer_tag"],
                "solid_mechanics": contract["solid_mechanics_tag"],
                "moving_mesh": contract["moving_mesh_tag"],
                "wave_optics": contract["wave_optics_tag"],
            },
            "selection_readback": {
                "heated_domain": {"tag": "sel_heat", "entity_count": 1},
                "structural_domain": {"tag": "sel_solid", "entity_count": 1},
                "fixed_boundary": {"tag": "sel_fixed", "entity_count": 1},
                "thermal_boundary": {"tag": "sel_temp", "entity_count": 1},
                "optical_domain": {"tag": "sel_opt", "entity_count": 1},
            },
            "temperature_unit_readback": "K",
            "material_state_id": "state_a",
            "material_state_readback": {
                "ledger_sha256": spec["material_state"]["ledger_sha256"],
                "state_id": "state_a",
                "classification": "fitted",
                "target": spec["material_state"]["target"],
                "property_value_type": "String",
                "property_values": ["2.25"],
                "function_tags": [],
                "application_receipt_sha256": spec["material_state"]["application_receipt_sha256"],
            },
            "source_unchanged": True,
            "rollback_available": False,
        }
    if stage_id == "thermal_structural_solve":
        return {
            "temperature": {"minimum_K": 400.0, "maximum_K": 400.0},
            "displacement": {"maximum_m": 1.0e-5, "delta_length_m": 1.0e-5, "frame": "spatial"},
            "stress": {"maximum_abs_Pa": 0.0},
            "energy_balance": {
                "source_W": 0.0,
                "loss_W": 0.0,
                "residual_W": 0.0,
                "relative_residual": 0.0,
            },
            "expansion": {
                "coefficient_input_type": "secant_coefficient",
                "coefficient_per_K": 1.0e-5,
                "reference_temperature_K": 300.0,
                "expected_delta_length_m": 1.0e-5,
                "observed_delta_length_m": 1.0e-5,
                "relative_error": 0.0,
            },
        }
    if stage_id == "state_evidence":
        return {
            "mesh": {
                "identity_sha256": "d" * 64,
                "element_count": 100,
                "vertex_count": 80,
                "minimum_quality": 0.8,
                "inverted_element_count": 0,
            },
            "frame": {
                "identity_sha256": "e" * 64,
                "displacement_frame": "spatial",
                "topology_unchanged": True,
            },
            "deformation_scale": 1.0,
            "displacement_to_length": 0.001,
        }
    if stage_id == "deformation_transfer":
        return {
            "method": "moving_mesh_spatial_frame",
            "material_frame_semantics": "spatial_deformation_preserves_material",
            "source_geometry_sha256": "f" * 64,
            "deformed_geometry_sha256": "1" * 64,
            "readback_exact": True,
            "rollback_verified": True,
        }
    rows = []
    for wavelength in spec["optical_replay"]["wavelengths_m"]:
        for branch in spec["optical_replay"]["branches"]:
            rta = {"R": 0.2, "T": 0.7, "A": 0.1, "closure_residual": 0.0, "passive": True}
            rows.append(
                {
                    "requested_wavelength_m": wavelength,
                    "solved_wavelength_m": wavelength,
                    "branch": branch,
                    "baseline_rta": dict(rta),
                    "deformed_rta": dict(rta),
                }
            )
    return {
        "rows": rows,
        "control_results": [
            {"control_id": item, "passed": True, "reason_code": "synthetic_control_passed"}
            for item in spec["validation_controls"]
        ],
        "source_unchanged": True,
        "derived_model_sha256": "2" * 64,
    }


def test_thermo_optomechanical_spec_is_closed_bounded_hash_bound_and_previewable(
    ascii_tmp_path,
):
    raw = _raw_spec(ascii_tmp_path / "spec")
    spec = normalize_thermo_optomechanical_replay_spec(raw)
    assert spec["declared_stages"] == list(THERMO_OPTOMECHANICAL_STAGES)
    assert spec["declared_optical_point_count"] == 2
    assert len(spec["source_model_sha256"]) == 64
    assert len(spec["material_ledger_sha256"]) == 64
    assert _worker_module(spec["job_type"]).endswith("thermo_optomechanical_replay_worker")
    preview = _preview_job_spec(raw)
    assert preview["inventory"]["declared_optical_points"] == 2
    assert preview["inventory"]["control_count"] == len(THERMO_OPTOMECHANICAL_CONTROLS)
    assert preview["submission_manifest"]["hash_verified"] is True

    manager = JobManager(
        ascii_tmp_path / "manager" / "jobs",
        preflight=lambda **_kwargs: {"ready": True},
        reconcile_on_start=False,
    )
    manager._launch_worker = lambda _job_id, module: {
        "pid": 123,
        "process_create_time": 456.0,
        "command_signature": module,
    }
    submitted = manager.submit(raw)
    stored = manager.store.read_spec(submitted["job_id"])
    assert stored["job_type"] == "thermo_optomechanical_replay"
    assert manager.store.read_state(submitted["job_id"])["progress"]["total"] == 5


def test_submission_manifest_hash_must_match_before_normalization(ascii_tmp_path):
    raw = _raw_spec(ascii_tmp_path / "manifest-hash")
    raw["specification_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="specification_sha256"):
        normalize_thermo_optomechanical_replay_spec(raw)


def test_manifest_contract_requires_each_validation_control_exactly_once(ascii_tmp_path):
    raw = _raw_spec(ascii_tmp_path / "typed-controls")
    manifest = json.loads(Path(raw["specification_path"]).read_text(encoding="utf-8"))
    manifest["validation_controls"][0] = manifest["validation_controls"][1]

    with pytest.raises(ValidationError, match="exactly once"):
        ThermoOptomechanicalReplayManifest.model_validate(manifest)


def test_submission_manifest_hash_and_parse_share_one_snapshot(ascii_tmp_path, monkeypatch):
    raw = _raw_spec(ascii_tmp_path / "manifest-snapshot")
    manifest_path = Path(raw["specification_path"])
    original_read = replay_module.read_file_bytes_bounded
    reads = 0

    def read_once(path, *, max_bytes):
        nonlocal reads
        payload = original_read(path, max_bytes=max_bytes)
        if Path(path).samefile(manifest_path):
            reads += 1
            manifest_path.write_text("{}\n", encoding="utf-8")
        return payload

    monkeypatch.setattr(replay_module, "read_file_bytes_bounded", read_once)
    spec = normalize_thermo_optomechanical_replay_spec(raw)

    assert reads == 1
    assert spec["submission_manifest_sha256"] == raw["specification_sha256"]
    assert spec["declared_optical_point_count"] == 2


@pytest.mark.parametrize(
    "mutation,match",
    [
        (
            lambda value: value["thermal_load"].__setitem__("temperature_unit", "degC"),
            "literal_error",
        ),
        (lambda value: value.pop("material_state"), "Field required"),
        (lambda value: value["optical_replay"].__setitem__("wavelengths_m", [4.0e-6]), "outside"),
        (
            lambda value: value.__setitem__("source_model_relative_identity", "../source.mph"),
            "contained",
        ),
        (lambda value: value["validation_controls"].__setitem__(0, "zero_cte"), "exactly once"),
    ],
)
def test_thermo_optomechanical_units_state_domain_path_and_controls_fail_closed(
    ascii_tmp_path, mutation, match
):
    raw = _raw_spec(ascii_tmp_path / "negative")
    specification = Path(raw["specification_path"])
    manifest = json.loads(specification.read_text(encoding="utf-8"))
    mutation(manifest)
    specification.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    raw["specification_sha256"] = hashlib.sha256(specification.read_bytes()).hexdigest()
    with pytest.raises((ValueError, ValidationError), match=match):
        normalize_thermo_optomechanical_replay_spec(raw)


def test_stage_evidence_resume_never_executes_a_completed_or_orphaned_stage_twice(
    ascii_tmp_path,
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "resume"))
    root = ascii_tmp_path / "resume-artifacts"
    calls = []

    def executor(stage_id, _stage_dir, current_spec):
        calls.append(stage_id)
        return _payload(stage_id, current_spec)

    def fail_after_thermal_evidence(point, evidence):
        if point == "after_stage_evidence" and evidence["stage_id"] == "thermal_structural_solve":
            raise RuntimeError("injected crash after durable evidence")

    with pytest.raises(RuntimeError, match="injected crash"):
        run_thermo_optomechanical_replay(
            spec,
            root,
            attempt=1,
            stage_executor=executor,
            fault_hook=fail_after_thermal_evidence,
        )
    result = run_thermo_optomechanical_replay(spec, root, attempt=2, stage_executor=executor)
    assert result["completed"] is True
    assert result["skipped_complete"] == 1
    assert calls.count("preflight") == 1
    assert calls.count("thermal_structural_solve") == 1
    assert calls == list(THERMO_OPTOMECHANICAL_STAGES)
    rows = read_thermo_optomechanical_stage_rows(
        root / "thermo_optomechanical_stages.jsonl", spec, artifact_root=root
    )
    assert [row["attempt"] for row in rows] == [1, 2, 2, 2, 2]
    assert result["summary"]["scientific_disposition"] == "accepted"


def test_stage_artifact_tampering_is_rejected_before_resume(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "tamper"))
    root = ascii_tmp_path / "tamper-artifacts"
    run_thermo_optomechanical_replay(
        spec,
        root,
        attempt=1,
        stage_executor=lambda stage, _directory, current: _payload(stage, current),
    )
    evidence = root / "state_evidence" / "evidence.json"
    original = evidence.read_bytes()
    changed = original.replace(b"0.8", b"0.7", 1)
    assert changed != original
    evidence.write_bytes(changed)
    with pytest.raises(ValueError, match="artifact|hash|evidence"):
        read_thermo_optomechanical_stage_rows(
            root / "thermo_optomechanical_stages.jsonl", spec, artifact_root=root
        )


def test_invalid_stage_attempt_is_rejected_before_journal_append(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "row"))
    root = ascii_tmp_path / "row-artifacts"
    evidence = build_stage_evidence(spec, "preflight", _payload("preflight", spec))
    evidence_path = root / "preflight" / "evidence.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    journal = root / "thermo_optomechanical_stages.jsonl"

    with pytest.raises(ValueError, match="attempt"):
        append_thermo_optomechanical_stage_row(
            journal,
            spec,
            attempt=0,
            artifact_root=root,
        )

    assert not journal.exists() or journal.read_bytes() == b""


def test_optical_evidence_preserves_tiny_negative_absorption_but_rejects_active_values(
    ascii_tmp_path,
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "rta"))
    payload = _payload("optical_replay", spec)
    payload["rows"][0]["deformed_rta"]["A"] = -1.0e-18
    evidence = build_stage_evidence(spec, "optical_replay", payload)
    assert evidence["payload"]["rows"][0]["deformed_rta"]["A"] == -1.0e-18

    payload["rows"][0]["deformed_rta"]["A"] = -1.0e-8
    with pytest.raises(ValueError, match="non-passive"):
        build_stage_evidence(spec, "optical_replay", payload)


class _Dataset:
    def __init__(self, tag="dset1", solution="sol1"):
        self._tag = tag
        self._solution = solution

    def properties(self):
        return ["solution"]

    def property(self, name):
        assert name == "solution"
        return self._solution

    def tag(self):
        return self._tag


class _Study:
    def getSolverSequences(self, kind):
        assert kind == "All"
        return ["sol1"]


class _Solution:
    def isEmpty(self):
        return False


class _JavaModel:
    def __init__(self, current_path=None):
        self.current_path = "" if current_path is None else str(current_path)
        self.saved = []

    def study(self, tag):
        assert tag == "std_ts"
        return _Study()

    def sol(self, tag):
        assert tag == "sol1"
        return _Solution()

    def getFilePath(self):
        return self.current_path

    def save(self, path, save_copy):
        self.saved.append((path, save_copy))
        if save_copy:
            Path(path).write_bytes(b"checkpoint")


class _ExecutorModel:
    def __init__(self, java, datasets=None):
        self.java = java
        self.datasets = [_Dataset()] if datasets is None else datasets
        self.evaluations = []

    def __truediv__(self, group):
        assert group == "datasets"
        return self.datasets

    def evaluate(self, expression, *, dataset):
        self.evaluations.append((expression, dataset))
        return 2.5


def test_executor_binds_the_study_solution_dataset_before_evaluation(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "dataset"))
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    model = _ExecutorModel(_JavaModel())
    executor.model = model
    executor._bind_study_dataset("std_ts")
    assert executor._evaluate("temperature_min") == 2.5
    assert model.evaluations == [("Tmin", model.datasets[0])]


def test_thermal_structure_readbacks_bind_before_mesh_and_expansion(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "readbacks"))
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    calls = []
    executor._bind_study_dataset = lambda tag: calls.append(("bind", tag))
    executor._evaluate = lambda key: calls.append(("evaluate", key)) or 0.5

    assert executor._thermal_structure_readbacks() == (0.5, 0.5)
    assert calls == [
        ("bind", "std_ts"),
        ("evaluate", "minimum_mesh_quality"),
        ("evaluate", "delta_length"),
    ]


def test_zero_temperature_control_resets_every_temperature_driver(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "zero-temperature")
    )
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    calls = []
    executor._load_derived = lambda: None
    executor._apply_positive_parameters = lambda: None
    executor._parameter = lambda key, value, unit=None: calls.append((key, value, unit))
    executor._set_moving_mesh_active = lambda _enabled: None
    executor._study = lambda _key: None
    executor._bind_study_dataset = lambda _tag: None
    executor._evaluate = lambda _key: 0.0

    assert executor._zero_control(zero_cte=False) == 0.0
    reference = spec["thermal_expansion"]["reference_temperature_K"]
    assert calls == [
        ("initial_temperature_parameter", reference, "K"),
        ("ambient_temperature_parameter", reference, "K"),
        ("applied_temperature_parameter", reference, "K"),
    ]


def test_wavelength_readback_accepts_last_ulp_but_rejects_material_drift():
    requested = 1.5e-6

    assert replay_execution_module._wavelength_readback_matches(
        math.nextafter(requested, float("inf")), requested
    )
    assert not replay_execution_module._wavelength_readback_matches(1.6e-6, requested)


def test_rollback_availability_requires_a_persisted_checkpoint(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "rollback"))
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")

    assert executor._rollback_available() is False
    executor.checkpoint_path.parent.mkdir(parents=True)
    executor.checkpoint_path.write_bytes(b"checkpoint")
    assert executor._rollback_available() is True


def test_executor_uses_normal_save_for_current_model_and_save_copy_for_checkpoint(
    ascii_tmp_path,
):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "save"))
    current = ascii_tmp_path / "current.mph"
    current.write_bytes(b"current")
    java = _JavaModel(current)
    executor = ThermoOptomechanicalComsolExecutor(None, spec, ascii_tmp_path / "job")
    executor.model = _ExecutorModel(java)
    executor._save(current)
    assert java.saved == [(str(current), False)]

    checkpoint = ascii_tmp_path / "checkpoint.mph"
    executor._save(checkpoint)
    assert java.saved[-1][1] is True
    assert checkpoint.read_bytes() == b"checkpoint"


class _Ownership:
    def __init__(self):
        self.acquired = False
        self.released = False

    def preflight(self, **_kwargs):
        return {"ready": True}

    def acquire(self, **_kwargs):
        self.acquired = True
        return {"success": True}

    def release(self):
        self.released = True
        return {"success": True}


class _Client:
    port = None

    def __init__(self):
        self.clear_count = 0

    def clear(self):
        self.clear_count += 1


def test_worker_publishes_completion_only_after_client_and_lease_cleanup(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(_raw_spec(ascii_tmp_path / "worker"))
    store = JobStore(ascii_tmp_path / "worker-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    owner = _Ownership()
    client = _Client()

    def factory(_client, current_spec, _root):
        return lambda stage, _directory, _spec: _payload(stage, current_spec)

    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: owner,
        client_factory=lambda _spec: client,
        stage_executor_factory=factory,
        native_cancel_enabled=False,
    )
    state = store.read_state(job_id)
    assert code == 0
    assert state["status"] == "completed"
    assert state["thermo_optomechanical_summary"]["scientific_disposition"] == "accepted"
    assert state["cleanup"] == {
        "client_cleared": True,
        "lease_released": True,
        "errors": [],
    }
    assert client.clear_count == 1
    assert owner.acquired is True
    assert owner.released is True


def test_native_cancel_monitor_failure_becomes_durable_worker_error(ascii_tmp_path, monkeypatch):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "native-monitor-error")
    )
    store = JobStore(ascii_tmp_path / "native-monitor-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    monkeypatch.setattr(
        "src.jobs.native_cancel_probe.request_native_cancel_once",
        lambda: (_ for _ in ()).throw(RuntimeError("injected native monitor failure")),
    )
    requested = False

    def factory(_client, current_spec, _root):
        def execute(stage, _directory, _spec):
            nonlocal requested
            if not requested:
                requested = True
                store.request_cancel(job_id, requester_identity=process_identity(os.getpid()))
                time.sleep(0.1)
            return _payload(stage, current_spec)

        return execute

    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: _Ownership(),
        client_factory=lambda _spec: _Client(),
        stage_executor_factory=factory,
        native_cancel_enabled=True,
    )
    state = store.read_state(job_id)

    assert code == 1
    assert state["status"] == "cancel_requested"
    assert "native cancel monitor failed" in state["cancel"]["worker_error"]["message"]


def test_worker_rejects_changed_submission_manifest_before_client_start(ascii_tmp_path):
    spec = normalize_thermo_optomechanical_replay_spec(
        _raw_spec(ascii_tmp_path / "changed-manifest")
    )
    store = JobStore(ascii_tmp_path / "changed-manifest-runtime" / "jobs")
    job_id = store.create(
        spec,
        {
            "schema_version": "2",
            "status": "submitted",
            "attempt": 1,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": 5},
            "last_error": None,
        },
    )
    Path(spec["submission_manifest_path"]).write_text("{}\n", encoding="utf-8")
    owner = _Ownership()

    code = run_worker(
        str(store.root),
        job_id,
        ownership_factory=lambda *_args: owner,
        client_factory=lambda _spec: pytest.fail("client must not start"),
        native_cancel_enabled=False,
    )
    state = store.read_state(job_id)
    assert code == 1
    assert state["status"] == "failed"
    assert "manifest changed before client startup" in state["last_error"]["message"]
    assert owner.acquired is False
