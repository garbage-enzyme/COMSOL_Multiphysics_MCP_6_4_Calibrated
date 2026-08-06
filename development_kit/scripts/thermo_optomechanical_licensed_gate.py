"""Generate and validate the minimal licensed COMSOL 6.4 thermo-optical fixture."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
ROOT_TEXT = str(ROOT)
sys.path[:] = [ROOT_TEXT, *(item for item in sys.path if item != ROOT_TEXT)]

_replay = importlib.import_module("comsol_mcp.jobs.thermo_optomechanical_replay")
_execution = importlib.import_module("comsol_mcp.jobs.thermo_optomechanical_replay_execution")
_runner = importlib.import_module("comsol_mcp.jobs.thermo_optomechanical_replay_runner")
_ownership = importlib.import_module("comsol_mcp.tools.ownership")
_thermal_material = importlib.import_module("comsol_mcp.evidence.thermal_material")
THERMO_OPTOMECHANICAL_CONTROLS = _replay.THERMO_OPTOMECHANICAL_CONTROLS
normalize_thermo_optomechanical_replay_spec = _replay.normalize_thermo_optomechanical_replay_spec
ThermoOptomechanicalComsolExecutor = _execution.ThermoOptomechanicalComsolExecutor
run_thermo_optomechanical_replay = _runner.run_thermo_optomechanical_replay
SolverOwnership = _ownership.SolverOwnership
normalize_thermal_material_ledger = _thermal_material.normalize_thermal_material_ledger

SCHEMA_NAME = "comsol_mcp.thermo_optomechanical_licensed_acceptance"
SCHEMA_VERSION = "1.0.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _jdouble(values: list[float]):
    import jpype

    return jpype.JArray(jpype.JDouble)(values)


def _jint(values: list[int]):
    import jpype

    return jpype.JArray(jpype.JInt)(values)


def _jstring(values: list[str]):
    import jpype

    return jpype.JArray(jpype.JString)(values)


def _faces(geometry: Any) -> tuple[list[dict[str, Any]], list[float]]:
    import jpype

    bounds = [float(value) for value in list(geometry.getBoundingBox())]
    point = jpype.JArray(jpype.JArray(jpype.JDouble))(1)
    faces = []
    for boundary in range(1, int(geometry.getNBoundaries()) + 1):
        ranges = [float(value) for value in list(geometry.faceParamRange(boundary))]
        point[0] = _jdouble([(ranges[0] + ranges[1]) / 2.0, (ranges[2] + ranges[3]) / 2.0])
        center = [float(value) for value in list(geometry.faceX(boundary, point)[0])]
        normal = [float(value) for value in list(geometry.faceNormal(boundary, point)[0])]
        faces.append({"boundary": boundary, "center": center, "normal": normal})
    return faces, bounds


def _face_at(
    faces: list[dict[str, Any]], axis: int, coordinate: float, sign: int, tolerance: float
) -> int:
    candidates = [
        face
        for face in faces
        if abs(face["center"][axis] - coordinate) <= tolerance and sign * face["normal"][axis] > 0.9
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"fixture face is not unique: axis={axis}, candidates={candidates}")
    return int(candidates[0]["boundary"])


def _selection(component: Any, tag: str, dimension: int, entities: list[int]) -> None:
    selection = component.selection().create(tag, "Explicit")
    selection.geom("geom1", dimension)
    selection.set(_jint(entities))
    observed = [int(value) for value in list(selection.entities())]
    if sorted(observed) != sorted(entities):
        raise RuntimeError(f"selection {tag} did not read back exactly")


def _set_material(property_group: Any, name: str, value: str | list[str]) -> None:
    property_group.set(name, _jstring(value) if isinstance(value, list) else value)


def _physics_field_readback(physics: Any) -> list[dict[str, Any]]:
    """Read the native dependent-field names without assuming COMSOL defaults."""
    fields = physics.field()
    readback = []
    for tag_value in list(fields.tags()):
        tag = str(tag_value)
        field = fields.get(tag)
        readback.append(
            {
                "tag": tag,
                "field": str(field.field()),
                "components": [str(value) for value in list(field.component())],
            }
        )
    return readback


def _property_inventory(feature: Any) -> list[dict[str, Any]]:
    inventory = []
    for property_value in list(feature.properties()):
        name = str(property_value)
        row = {"name": name, "value_type": str(feature.getValueType(name))}
        try:
            allowed = feature.getAllowedPropertyValues(name)
            row["allowed_values"] = (
                [str(value) for value in list(allowed)] if allowed is not None else []
            )
        except Exception:
            row["allowed_values"] = []
        inventory.append(row)
    return inventory


def _configure_study_step(step: Any, entities: dict[str, tuple[Any, bool]]) -> dict[str, Any]:
    """Set and prove the exact model entities solved by one study step."""
    readback = {}
    for name, (entity, requested) in entities.items():
        path = str(entity.resolveModelPath())
        step.setSolveFor(path, requested)
        observed = bool(step.solveFor(path))
        if observed != requested:
            raise RuntimeError(f"study solve-for state did not read back for {name}")
        readback[name] = {"model_path": path, "solve_for": observed}
    return readback


def _executor_diagnostic(executor: Any) -> dict[str, Any]:
    """Capture bounded live solution metadata after a licensed gate failure."""
    model = executor.model
    if model is None:
        return {"model_available": False}
    java = model.java
    errors = []
    try:
        component = java.component("comp1")
    except Exception as exc:
        return {
            "model_available": True,
            "component_available": False,
            "errors": [{"tag": "comp1", "error_type": type(exc).__name__}],
            "studies": {},
            "datasets": [],
        }
    entities = {}
    for name, tag in (
        ("heat_transfer", "ht"),
        ("solid_mechanics", "solid"),
        ("wave_optics", "ewfd"),
    ):
        try:
            entities[name] = component.physics(tag)
        except Exception as exc:
            errors.append({"tag": tag, "error_type": type(exc).__name__})
    studies = {}
    for study_tag in ("std_ts", "std_ale", "std_opt"):
        try:
            study = java.study(study_tag)
        except Exception as exc:
            errors.append({"tag": study_tag, "error_type": type(exc).__name__})
            continue
        step_tags = [str(value) for value in list(study.feature().tags())]
        step_rows = []
        for step_tag in step_tags:
            step = study.feature(step_tag)
            step_rows.append(
                {
                    "tag": step_tag,
                    "solve_for": {
                        name: bool(step.solveFor(str(entity.resolveModelPath())))
                        for name, entity in entities.items()
                    },
                }
            )
        studies[study_tag] = {
            "solver_sequences": [str(value) for value in list(study.getSolverSequences("All"))],
            "steps": step_rows,
        }
    datasets = []
    probes = ("T", "usolid", "u", "u2", "solid.disp", "solid.mises")
    for dataset in model / "datasets":
        properties = {str(value) for value in dataset.properties()}
        row = {
            "name": str(dataset.name()),
            "tag": str(dataset.tag()),
            "type": str(dataset.type()),
            "solution": str(dataset.property("solution")) if "solution" in properties else None,
            "probes": {},
        }
        for expression in probes:
            try:
                value = model.evaluate(expression, dataset=dataset)
                row["probes"][expression] = {
                    "success": True,
                    "shape": list(getattr(value, "shape", ())),
                }
            except Exception as exc:
                row["probes"][expression] = {
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
        datasets.append(row)
    return {
        "model_available": True,
        "component_available": True,
        "solid_field_readback": (
            _physics_field_readback(entities["solid_mechanics"])
            if "solid_mechanics" in entities
            else []
        ),
        "studies": studies,
        "datasets": datasets,
        "errors": errors,
    }


def probe_heat_transfer_constructors(client: Any) -> list[dict[str, Any]]:
    """Record the exact constructor accepted by the installed COMSOL build."""
    variants = (
        ("component_geometry", "HeatTransfer", "geom1"),
        ("component_dimension", "HeatTransfer", "3"),
        ("root_geometry", "HeatTransfer", "geom1"),
        ("root_dimension", "HeatTransfer", "3"),
        ("component_geometry", "HeatTransferInSolids", "geom1"),
        ("component_dimension", "HeatTransferInSolids", "3"),
    )
    results = []
    for index, (scope, interface_type, target) in enumerate(variants):
        client.clear()
        model = client.create(f"H1HeatConstructor{index}")
        java = model.java
        component = java.component().create("comp1", True)
        geometry = component.geom().create("geom1", 3)
        geometry.feature().create("blk1", "Block")
        geometry.run()
        try:
            physics = component.physics() if scope.startswith("component") else java.physics()
            node = physics.create("ht", interface_type, target)
            results.append(
                {
                    "scope": scope,
                    "interface_type": interface_type,
                    "target": target,
                    "success": True,
                    "tag": str(node.tag()),
                }
            )
        except Exception as exc:
            results.append(
                {
                    "scope": scope,
                    "interface_type": interface_type,
                    "target": target,
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                }
            )
    client.clear()
    return results


def build_fixture(client: Any, source_path: Path) -> dict[str, Any]:
    """Build one generated single-cell thermal/structural/optical model."""
    model = client.create("H1SyntheticFixture")
    java = model.java
    parameters = {
        "Lx": "1e-6[m]",
        "Ly": "1e-6[m]",
        "Lz": "1e-6[m]",
        "Tinit": "300[K]",
        "Tamb": "300[K]",
        "Tapp": "400[K]",
        "Tref": "300[K]",
        "alpha": "1e-5[1/K]",
        "lambda0": "1.5e-6[m]",
        "pol": "0",
        "dscale": "0",
        "Qvol": "0[W/m^3]",
        "hconv": "10[W/(m^2*K)]",
    }
    for name, expression in parameters.items():
        java.param().set(name, expression)

    component = java.component().create("comp1", True)
    geometry = component.geom().create("geom1", 3)
    block = geometry.feature().create("blk1", "Block")
    block.set("size", _jstring(["Lx", "Ly", "Lz"]))
    block.set("selresult", True)
    geometry.run()
    faces, bounds = _faces(geometry)
    tolerance = max(max(abs(value) for value in bounds) * 1.0e-8, 1.0e-14)
    x_min = _face_at(faces, 0, bounds[0], -1, tolerance)
    x_max = _face_at(faces, 0, bounds[1], 1, tolerance)
    y_min = _face_at(faces, 1, bounds[2], -1, tolerance)
    z_min = _face_at(faces, 2, bounds[4], -1, tolerance)
    all_boundaries = [int(face["boundary"]) for face in faces]

    _selection(component, "sel_heat", 3, [1])
    _selection(component, "sel_solid", 3, [1])
    _selection(component, "sel_fixed", 2, [x_min, y_min, z_min])
    _selection(component, "sel_temp", 2, all_boundaries)
    _selection(component, "sel_opt", 3, [1])

    heat = component.physics().create("ht", "HeatTransfer", str(geometry.getSDim()))
    heat.selection().set(_jint([1]))
    temperature = heat.feature().create("temp1", "TemperatureBoundary", 2)
    temperature.selection().set(_jint(all_boundaries))
    temperature.set("T0", "Tapp")
    source = heat.feature().create("hs1", "HeatSource", 3)
    source.selection().set(_jint([1]))
    source.set("Q0", "Qvol")

    solid = component.physics().create("solid", "SolidMechanics", str(geometry.getSDim()))
    solid.selection().set(_jint([1]))
    displacement = solid.field().get("displacement")
    displacement.field("usolid")
    displacement.component(_jstring(["usolid", "vsolid", "wsolid"]))
    solid_field_readback = _physics_field_readback(solid)
    if solid_field_readback[0]["components"] != ["usolid", "vsolid", "wsolid"]:
        raise RuntimeError("Solid Mechanics displacement components did not read back exactly")
    solid_features = solid.feature()
    if "lemm1" not in {str(value) for value in list(solid_features.tags())}:
        solid_features.create("lemm1", "LinearElasticMaterial", 3)
    fixed = solid.feature().create("fix1", "Roller", 2)
    fixed.selection().set(_jint([x_min, y_min, z_min]))
    thermal_expansion = component.multiphysics().create("te1", "ThermalExpansion", "geom1", 3)
    thermal_expansion.selection().set(_jint([1]))
    thermal_expansion.set("Solid_physics", "solid")
    thermal_expansion.set("Heat_physics", "ht")
    thermal_expansion.set("InputType", "SecantCoefficient")
    thermal_expansion.set("alpha_mat", "from_mat")
    thermal_expansion.set("minput_strainreferencetemperature_src", "userdef")
    thermal_expansion.set("minput_strainreferencetemperature", "Tref")

    moving_mesh = component.common().create("ale", "PrescribedDeformation")
    moving_mesh.selection().set(_jint([1]))
    moving_mesh.set(
        "prescribedDeformation",
        _jstring(
            [
                "dscale*alpha*(Tapp-Tref)*X",
                "dscale*alpha*(Tapp-Tref)*Y",
                "dscale*alpha*(Tapp-Tref)*Z",
            ]
        ),
    )

    wave = component.physics().create(
        "ewfd", "ElectromagneticWavesFrequencyDomain", str(geometry.getSDim())
    )
    wave.selection().set(_jint([1]))
    periodic = wave.feature().create("ps1", "PeriodicStructure", 3)
    port_one = [int(value) for value in list(periodic.feature("pport1").selection().entities())]
    port_two = [int(value) for value in list(periodic.feature("pport2").selection().entities())]
    if len(port_one) != 1 or len(port_two) != 1 or port_one == port_two:
        raise RuntimeError(f"periodic ports are invalid: {port_one}, {port_two}")
    periodic.selection("excitedPortSelection").set(_jint(port_one))

    material = component.material().create("mat1", "Common")
    material.selection().set(_jint([1]))
    properties = material.propertyGroup("def")
    _set_material(properties, "density", "2200[kg/m^3]")
    _set_material(properties, "heatcapacity", "700[J/(kg*K)]")
    _set_material(
        properties,
        "thermalconductivity",
        ["1.4[W/(m*K)]", "0", "0", "0", "1.4[W/(m*K)]", "0", "0", "0", "1.4[W/(m*K)]"],
    )
    _set_material(properties, "youngsmodulus", "70e9[Pa]")
    _set_material(properties, "poissonsratio", "0.17")
    _set_material(
        properties,
        "thermalexpansioncoefficient",
        ["alpha", "0", "0", "0", "alpha", "0", "0", "0", "alpha"],
    )
    _set_material(properties, "relpermittivity", "2.25")
    _set_material(properties, "electricconductivity", "0[S/m]")
    _set_material(properties, "relpermeability", "1")

    minimum = component.cpl().create("minDom", "Minimum", "geom1")
    minimum.selection().set(_jint([1]))
    maximum = component.cpl().create("maxDom", "Maximum", "geom1")
    maximum.selection().set(_jint([1]))
    integral = component.cpl().create("intDom", "Integration", "geom1")
    integral.selection().set(_jint([1]))
    average_left = component.cpl().create("aveLeft", "Average", "geom1")
    average_left.selection().geom(2)
    average_left.selection().set(_jint([x_min]))
    average_right = component.cpl().create("aveRight", "Average", "geom1")
    average_right.selection().geom(2)
    average_right.selection().set(_jint([x_max]))
    variables = component.variable().create("var1")
    expressions = {
        "TminH1": "minDom(T)",
        "TmaxH1": "maxDom(T)",
        "umaxH1": "maxDom(sqrt(usolid^2+vsolid^2+wsolid^2))",
        "smaxH1": "maxDom(solid.mises)",
        "QinH1": "intDom(Qvol)",
        "QoutH1": "0[W]",
        "dLH1": "aveRight(usolid)-aveLeft(usolid)",
        "qminH1": "minDom(qual)",
        "RH1": "ewfd.Rtotal",
        "TrH1": "ewfd.Ttotal",
        "AH1": "ewfd.Atotal",
    }
    for name, expression in expressions.items():
        variables.set(name, expression)

    mesh = component.mesh().create("mesh1")
    size = mesh.feature().create("size1", "Size")
    size.set("hmax", 2.5e-7)
    size.set("hmaxactive", True)
    free_triangle = mesh.feature().create("ftri1", "FreeTri")
    free_triangle.selection().set(_jint(port_two))
    sweep = mesh.feature().create("sw1", "Sweep")
    sweep.selection().set(_jint([1]))
    mesh.run()
    element_count = int(mesh.getNumElem())
    vertex_count = int(mesh.getNumVertex())
    if element_count <= 0 or vertex_count <= 0:
        raise RuntimeError("generated thermo-optomechanical fixture mesh is empty")
    java.param().set("nelemH1", str(element_count))
    java.param().set("nvertH1", str(vertex_count))

    thermal_study = java.study().create("std_ts")
    thermal_step = thermal_study.create("stat", "Stationary")
    thermal_solve_for = _configure_study_step(
        thermal_step,
        {
            "heat_transfer": (heat, True),
            "solid_mechanics": (solid, True),
            "wave_optics": (wave, False),
        },
    )
    transfer_study = java.study().create("std_ale")
    transfer_step = transfer_study.create("stat", "Stationary")
    transfer_solve_for = _configure_study_step(
        transfer_step,
        {
            "heat_transfer": (heat, False),
            "solid_mechanics": (solid, False),
            "wave_optics": (wave, False),
        },
    )
    optical_study = java.study().create("std_opt")
    wavelength = optical_study.create("wave", "Wavelength")
    optical_solve_for = _configure_study_step(
        wavelength,
        {
            "heat_transfer": (heat, False),
            "solid_mechanics": (solid, False),
            "wave_optics": (wave, True),
        },
    )
    wavelength.set("punit", "m")
    wavelength.set("plist", "lambda0")

    source_path.parent.mkdir(parents=True, exist_ok=True)
    model.java.save(str(source_path), True)
    if not source_path.is_file():
        raise RuntimeError("generated thermo-optomechanical source model was not saved")
    return {
        "source_model_sha256": _sha256(source_path),
        "mesh": {"element_count": element_count, "vertex_count": vertex_count},
        "faces": {
            "x_min": x_min,
            "x_max": x_max,
            "y_min": y_min,
            "z_min": z_min,
            "all": all_boundaries,
        },
        "periodic_ports": {"one": port_one, "two": port_two},
        "solid_field_readback": solid_field_readback,
        "solid_feature_tags": [str(value) for value in list(solid.feature().tags())],
        "linear_elastic_subfeature_tags": [
            str(value) for value in list(solid.feature("lemm1").feature().tags())
        ],
        "thermal_expansion_properties": _property_inventory(thermal_expansion),
        "thermal_expansion_readback": {
            "input_type": str(thermal_expansion.getString("InputType")),
            "coefficient_source": str(thermal_expansion.getString("alpha_mat")),
            "reference_temperature_source": str(
                thermal_expansion.getString("minput_strainreferencetemperature_src")
            ),
            "reference_temperature": str(
                thermal_expansion.getString("minput_strainreferencetemperature")
            ),
        },
        "study_solve_for": {
            "thermal_structural": thermal_solve_for,
            "deformation_transfer": transfer_solve_for,
            "optical_replay": optical_solve_for,
        },
        "physics_tags": [str(value) for value in list(component.physics().tags())],
        "common_tags": [str(value) for value in list(component.common().tags())],
        "study_tags": [str(value) for value in list(java.study().tags())],
    }


def _ledger() -> dict[str, Any]:
    return {
        "material_identity_sha256": "a" * 64,
        "sample_identity_sha256": "b" * 64,
        "states": [
            {
                "state_id": "synthetic_glass",
                "phase_id": "solid",
                "fabrication_state": "generated analytic fixture",
                "classification": "assumed",
                "source": {
                    "source_kind": "citation",
                    "citation": "Generated analytic thermo-optomechanical material fixture",
                    "source_state_description": "lossless isotropic synthetic solid",
                },
                "validity": {
                    "wavelength_min_m": 1.0e-6,
                    "wavelength_max_m": 2.5e-6,
                    "temperature_min_K": 250.0,
                    "temperature_max_K": 500.0,
                },
                "uncertainty": {"kind": "relative", "relative_fraction": 0.0},
                "measurement_conditions": {"method": "analytic", "ambient": "vacuum"},
                "optical_model": {
                    "model_kind": "thermo_optic",
                    "reference_temperature_K": 300.0,
                    "refractive_index_at_reference": 1.5,
                    "extinction_coefficient_at_reference": 0.0,
                    "dn_dT_per_K": 0.0,
                    "dk_dT_per_K": 0.0,
                },
            }
        ],
        "comsol_target": {
            "component_tag": "comp1",
            "material_tag": "mat1",
            "property_group_tag": "def",
            "relative_permittivity_property_key": "relpermittivity",
            "function_tag_prefix": "tm",
        },
    }


def _material_state_reference(source_path: Path) -> dict[str, Any]:
    ledger = normalize_thermal_material_ledger(_ledger())
    state = ledger["states"][0]
    target = ledger["comsol_target"]
    source_sha256 = _sha256(source_path)
    application_receipt = {
        "ledger_sha256": ledger["ledger_sha256"],
        "state_id": state["state_id"],
        "source_model_sha256": source_sha256,
        "target": target,
        "property_values": ["2.25"],
        "function_tags": [],
        "readback_exact": True,
    }
    return {
        "schema_name": "comsol_mcp.thermal_material_state_reference",
        "schema_version": "1.0.0",
        "ledger_sha256": ledger["ledger_sha256"],
        "material_identity_sha256": ledger["material_identity_sha256"],
        "sample_identity_sha256": ledger["sample_identity_sha256"],
        "state_id": state["state_id"],
        "classification": state["classification"],
        "validity": state["validity"],
        "target": {
            "component_tag": target["component_tag"],
            "material_tag": target["material_tag"],
            "property_group_tag": target["property_group_tag"],
            "property_key": target["relative_permittivity_property_key"],
        },
        "source_model_sha256": source_sha256,
        "expected_property_values": ["2.25"],
        "expected_function_tags": [],
        "application_receipt_sha256": _canonical_hash(application_receipt),
    }


def _raw_spec(source_path: Path, specification_path: Path, *, cores: int = 2) -> dict[str, Any]:
    manifest = {
        "job_type": "thermo_optomechanical_replay",
        "source_model_path": str(source_path),
        "source_model_relative_identity": ("generated/thermo_optomechanical_synthetic_source.mph"),
        "optical_configuration_sha256": "c" * 64,
        "material_state": _material_state_reference(source_path),
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
                "temperature_min": "TminH1",
                "temperature_max": "TmaxH1",
                "displacement_max": "umaxH1",
                "stress_max": "smaxH1",
                "heat_source_integral": "QinH1",
                "boundary_loss_integral": "QoutH1",
                "delta_length": "dLH1",
                "minimum_mesh_quality": "qminH1",
                "mesh_element_count": "nelemH1",
                "mesh_vertex_count": "nvertH1",
                "reflectance": "RH1",
                "transmittance": "TrH1",
                "absorptance": "AH1",
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
            "reference_length_m": 1.0e-6,
            "measurement_axis": "x",
        },
        "deformation_transfer": {
            "method": "moving_mesh_spatial_frame",
            "displacement_frame": "spatial",
            "topology_change_allowed": False,
            "deformation_scale": 1.0,
        },
        "optical_replay": {
            "wavelengths_m": [1.5e-6],
            "branches": ["TE"],
            "wavelength_coordinate": "vacuum_wavelength_m",
        },
        "validation_controls": list(THERMO_OPTOMECHANICAL_CONTROLS),
        "acceptance_policy": {
            "expansion_relative_tolerance": 0.05,
            "zero_control_absolute_tolerance_m": 1.0e-11,
            "energy_relative_tolerance": 1.0e-8,
            "rta_closure_absolute_tolerance": 1.0e-6,
            "minimum_mesh_quality": 0.01,
            "maximum_displacement_to_length": 0.01,
        },
        "resource_policy": {
            "wall_time_budget_seconds": 900.0,
            "minimum_next_point_seconds": 1.0,
        },
        "cores": cores,
        "wall_time_budget_seconds": 900,
        "version": "6.4",
        "max_retries": 1,
        "continue_on_error": False,
    }
    _atomic_json(specification_path, manifest)
    return {
        "job_type": "thermo_optomechanical_replay",
        "specification_path": str(specification_path),
        "specification_sha256": _sha256(specification_path),
    }


def _cleanup_inventory(owner: SolverOwnership) -> dict[str, Any]:
    status = owner.status(require_fresh_inventory=True)
    return {
        "lease_state": status["lease"]["state"],
        "collision": status["collision"],
        "external_solver_processes": status["external_solver_processes"],
        "inventory_complete": status["process_inventory"]["complete"],
    }


def _finalize_gate_result(
    output: Path,
    result: dict[str, Any],
    owner: SolverOwnership,
) -> dict[str, Any]:
    try:
        result["cleanup"] = _cleanup_inventory(owner)
    except Exception as exc:
        result["cleanup"] = {
            "lease_state": "unknown",
            "collision": True,
            "external_solver_processes": [],
            "inventory_complete": False,
            "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
        }
        result["success"] = False
    if (
        result["cleanup"]["lease_state"] != "absent"
        or result["cleanup"]["collision"]
        or not result["cleanup"]["inventory_complete"]
    ):
        result["success"] = False
    result["finished_at_epoch"] = time.time()
    try:
        body = dict(result)
        result["receipt_sha256"] = _canonical_hash(body)
    except Exception as exc:
        result["success"] = False
        result["receipt_hash_error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:1000],
        }
    try:
        _atomic_json(output / "licensed_acceptance.json", result)
    except Exception as exc:
        result["success"] = False
        result["receipt_write_error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:1000],
        }
        fallback = {
            "schema_name": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "success": False,
            "error": result.get("error"),
            "cleanup": result.get("cleanup"),
            "receipt_write_error": result["receipt_write_error"],
            "finished_at_epoch": result["finished_at_epoch"],
        }
        try:
            _atomic_json(output / "licensed_acceptance_failure.json", fallback)
        except Exception as fallback_exc:
            result["receipt_fallback_write_error"] = {
                "type": type(fallback_exc).__name__,
                "message": str(fallback_exc)[:1000],
            }
    return result


def run_gate(output: Path, *, cores: int) -> dict[str, Any]:
    if output.exists():
        raise ValueError("licensed thermo-optomechanical gate output must not already exist")
    output.mkdir(parents=True)
    source_path = output / "source" / "thermo_optomechanical_synthetic_source.mph"
    owner = SolverOwnership("D:/comsol_runtime", owner="v4.1-thermo-optomechanical-licensed-gate")
    result: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "started_at_epoch": time.time(),
        "source_tree_sha256": _canonical_hash(
            {
                str(path.relative_to(ROOT)).replace("\\", "/"): _sha256(path)
                for path in sorted((ROOT / "comsol_mcp").rglob("*.py"))
            }
        ),
    }
    client = None
    executor = None
    release: dict[str, Any] | None = None
    try:
        preflight = owner.preflight(
            model_path=None,
            output_path=str(output),
            requested_version="6.4",
        )
        result["preflight"] = preflight
        if not preflight.get("ready"):
            raise RuntimeError(
                f"licensed thermo-optomechanical preflight failed: {preflight.get('blockers')}"
            )
        claim = owner.acquire(mode="thermo-optomechanical-licensed-gate")
        result["lease_claim"] = claim
        if not claim.get("success"):
            raise RuntimeError(
                "licensed thermo-optomechanical gate could not acquire the solver lease"
            )

        import mph

        client = mph.Client(cores=cores, version="6.4")
        result["heat_transfer_constructor_probe"] = probe_heat_transfer_constructors(client)
        if not any(item["success"] for item in result["heat_transfer_constructor_probe"]):
            raise RuntimeError("no Heat Transfer constructor is available on the licensed host")
        result["fixture"] = build_fixture(client, source_path)
        source_before = _sha256(source_path)
        spec = normalize_thermo_optomechanical_replay_spec(
            _raw_spec(source_path, output / "submission_specification.json", cores=cores)
        )
        _atomic_json(output / "normalized_spec.json", spec)
        client.clear()
        executor = ThermoOptomechanicalComsolExecutor(client, spec, output / "job")
        replay = run_thermo_optomechanical_replay(
            spec,
            output / "job",
            attempt=1,
            stage_executor=executor,
        )
        source_after = _sha256(source_path)
        if source_before != source_after:
            raise RuntimeError("licensed thermo-optomechanical source changed during replay")
        result["replay"] = replay
        result["source_immutability"] = {
            "before_sha256": source_before,
            "after_sha256": source_after,
            "unchanged": True,
        }
        result["success"] = replay["summary"]["scientific_disposition"] == "accepted"
        if not result["success"]:
            raise RuntimeError(
                f"licensed thermo-optomechanical scientific gate failed: {replay['summary']}"
            )
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:3000]}
        result["traceback"] = traceback.format_exc(limit=30)
        if executor is not None:
            if executor.last_stage_payload is not None:
                result["failed_stage_payload"] = executor.last_stage_payload
            try:
                result["solution_diagnostic"] = _executor_diagnostic(executor)
            except Exception as diagnostic_exc:
                result["solution_diagnostic_error"] = {
                    "type": type(diagnostic_exc).__name__,
                    "message": str(diagnostic_exc)[:1000],
                }
    finally:
        if client is not None:
            try:
                client.clear()
                result["client_clear"] = True
            except Exception as exc:
                result["client_clear"] = False
                result["client_clear_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                result["success"] = False
            if getattr(client, "port", None):
                try:
                    client.disconnect()
                    result["client_disconnect"] = True
                except Exception as exc:
                    result["client_disconnect"] = False
                    result["client_disconnect_error"] = f"{type(exc).__name__}: {exc}"[:1000]
                    result["success"] = False
        try:
            release = owner.release()
            result["lease_release"] = release
            if not release.get("success"):
                result["success"] = False
        except Exception as exc:
            result["lease_release"] = {"success": False, "error": str(exc)[:1000]}
            result["success"] = False
        _finalize_gate_result(output, result, owner)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "RUN_REAL_COMSOL":
        raise SystemExit("licensed thermo-optomechanical gate requires --confirm RUN_REAL_COMSOL")
    output = args.output.resolve()
    if not str(output).isascii():
        raise SystemExit("licensed thermo-optomechanical gate output must use an ASCII path")
    result = run_gate(output, cores=args.cores)
    print(
        json.dumps(
            {
                "success": result["success"],
                "output": str(output),
                "error": result.get("error"),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
