"""Build a physical three-dimensional parallel-plate capacitor model.

The dielectric fills the space between two square electrodes. The lower face
is grounded and the upper face is held at a prescribed voltage; the remaining
faces retain the Electrostatics insulation default. The capacitance obtained
from stored electric energy can therefore be compared with
``epsilon_0 * epsilon_r * area / separation``.

By default this recipe builds and saves without solving. Pass ``--solve`` only
on an admitted licensed host; a solved run must meet the declared analytical
error limit before it is reported as verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

import jpype
import mph
import numpy as np

from comsol_mcp.tools.ownership import SolverOwnership

PLATE_SIDE_M = 0.01
PLATE_SEPARATION_M = 0.001
RELATIVE_PERMITTIVITY = 2.1
POTENTIAL_V = 1.0
VACUUM_PERMITTIVITY_F_PER_M = 8.8541878128e-12
DEFAULT_MAX_RELATIVE_ERROR = 1e-6
WINDOWS_FILE_RETRY_SECONDS = 3.0
WINDOWS_FILE_RETRY_INTERVAL_SECONDS = 0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-model", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--plate-side-m", type=float, default=PLATE_SIDE_M)
    parser.add_argument("--plate-separation-m", type=float, default=PLATE_SEPARATION_M)
    parser.add_argument("--relative-permittivity", type=float, default=RELATIVE_PERMITTIVITY)
    parser.add_argument("--potential-v", type=float, default=POTENTIAL_V)
    parser.add_argument(
        "--maximum-relative-error",
        type=float,
        default=DEFAULT_MAX_RELATIVE_ERROR,
    )
    parser.add_argument("--solve", action="store_true")
    parser.add_argument("--overwrite-output", action="store_true")
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _geometry_size_expressions(
    plate_side_parameter: str = "plate_side",
    separation_parameter: str = "plate_gap",
) -> tuple[str, str, str]:
    return (plate_side_parameter, plate_side_parameter, separation_parameter)


def _theoretical_capacitance_pf(
    plate_side_m: float = PLATE_SIDE_M,
    plate_separation_m: float = PLATE_SEPARATION_M,
    relative_permittivity: float = RELATIVE_PERMITTIVITY,
) -> float:
    return (
        VACUUM_PERMITTIVITY_F_PER_M
        * relative_permittivity
        * math.pow(plate_side_m, 2)
        / plate_separation_m
        * 1e12
    )


def _validate_inputs(
    plate_side_m: float,
    plate_separation_m: float,
    relative_permittivity: float,
    potential_v: float,
    maximum_relative_error: float,
) -> None:
    named = {
        "--plate-side-m": plate_side_m,
        "--plate-separation-m": plate_separation_m,
        "--relative-permittivity": relative_permittivity,
        "--potential-v": potential_v,
        "--maximum-relative-error": maximum_relative_error,
    }
    for name, value in named.items():
        if not math.isfinite(value) or value <= 0.0:
            raise ValueError(f"{name} must be finite and positive")
    if plate_side_m / plate_separation_m > 1000.0:
        raise ValueError("plate-side to separation ratio must not exceed 1000")
    if relative_permittivity > 1000.0:
        raise ValueError("--relative-permittivity must not exceed 1000")
    if maximum_relative_error > 0.1:
        raise ValueError("--maximum-relative-error must not exceed 0.1")


def _probe_faces(geometry: Any) -> tuple[list[dict[str, Any]], list[float]]:
    bounding_box = [float(value) for value in list(geometry.getBoundingBox())]
    if len(bounding_box) != 6:
        raise RuntimeError(f"unexpected geometry bounding box: {bounding_box}")
    faces = []
    point = jpype.JArray(jpype.JArray(jpype.JDouble))(1)
    for boundary in range(1, int(geometry.getNBoundaries()) + 1):
        ranges = [float(value) for value in list(geometry.faceParamRange(boundary))]
        if len(ranges) != 4:
            raise RuntimeError(f"unexpected face parameter range for boundary {boundary}")
        point[0] = jpype.JArray(jpype.JDouble)(
            [(ranges[0] + ranges[1]) / 2.0, (ranges[2] + ranges[3]) / 2.0]
        )
        center = [float(value) for value in list(geometry.faceX(boundary, point)[0])]
        normal = [float(value) for value in list(geometry.faceNormal(boundary, point)[0])]
        if len(center) != 3 or len(normal) != 3:
            raise RuntimeError(f"unexpected face probe shape for boundary {boundary}")
        faces.append({"boundary": boundary, "center_m": center, "normal": normal})
    return faces, bounding_box


def _identify_electrode_faces(
    faces: list[dict[str, Any]], bounding_box: list[float]
) -> dict[str, dict[str, Any]]:
    if len(bounding_box) != 6:
        raise ValueError("bounding_box must contain six coordinates")
    z_min, z_max = bounding_box[4], bounding_box[5]
    scale = max(abs(value) for value in bounding_box) if bounding_box else 1.0
    tolerance = max(scale * 1e-9, 1e-12)

    def candidates(z_coordinate: float, normal_sign: int) -> list[dict[str, Any]]:
        return [
            face
            for face in faces
            if len(face.get("center_m", [])) == 3
            and len(face.get("normal", [])) == 3
            and abs(float(face["center_m"][2]) - z_coordinate) <= tolerance
            and normal_sign * float(face["normal"][2]) > 0.9
            and abs(float(face["normal"][0])) < 0.1
            and abs(float(face["normal"][1])) < 0.1
        ]

    bottom = candidates(z_min, -1)
    top = candidates(z_max, 1)
    if len(bottom) != 1 or len(top) != 1:
        raise RuntimeError(
            "electrode faces are not uniquely identified by bounding plane and normal: "
            f"bottom={bottom}, top={top}"
        )
    if int(bottom[0]["boundary"]) == int(top[0]["boundary"]):
        raise RuntimeError("top and bottom electrode selections overlap")
    return {"ground": bottom[0], "potential": top[0]}


def build_parallel_plate_capacitor(
    model: mph.Model,
    *,
    plate_side_m: float = PLATE_SIDE_M,
    plate_separation_m: float = PLATE_SEPARATION_M,
    relative_permittivity: float = RELATIVE_PERMITTIVITY,
    potential_v: float = POTENTIAL_V,
) -> dict[str, Any]:
    """Build the geometry, verified electrode selections, physics, mesh, and study."""
    java_model = model.java
    for name, value in (
        ("plate_side", f"{plate_side_m:.17g}[m]"),
        ("plate_gap", f"{plate_separation_m:.17g}[m]"),
        ("epsr", f"{relative_permittivity:.17g}"),
        ("V0", f"{potential_v:.17g}[V]"),
    ):
        java_model.param().set(name, value)

    component = java_model.component().create("comp1", True)
    geometry = component.geom().create("geom1", 3)
    dielectric = geometry.feature().create("dielectric", "Block")
    dielectric.set("size", jpype.JArray(jpype.JString)(_geometry_size_expressions()))
    dielectric.set("pos", jpype.JArray(jpype.JDouble)([0.0, 0.0, 0.0]))
    geometry.run()
    topology = {
        "domains": int(geometry.getNDomains()),
        "boundaries": int(geometry.getNBoundaries()),
    }
    if topology != {"domains": 1, "boundaries": 6}:
        raise RuntimeError(f"unexpected capacitor topology: {topology}")

    faces, bounding_box = _probe_faces(geometry)
    electrodes = _identify_electrode_faces(faces, bounding_box)
    ground_boundary = int(electrodes["ground"]["boundary"])
    potential_boundary = int(electrodes["potential"]["boundary"])

    dimension = str(geometry.getSDim())
    electrostatics = component.physics().create("es", "Electrostatics", dimension)
    conservation = electrostatics.feature().create("ccn1", "ChargeConservation", int(dimension))
    conservation.selection().set([1])
    conservation.set("materialType", "from_mat")

    material = component.material().create("mat1", "Common")
    material.label("Linear dielectric")
    material.propertyGroup("def").set("relpermittivity", "epsr")
    material.selection().set([1])

    ground = electrostatics.feature().create("gnd1", "Ground", 2)
    ground.selection().set([ground_boundary])
    potential = electrostatics.feature().create("ep1", "ElectricPotential", 2)
    potential.selection().set([potential_boundary])
    potential.set("V0", "V0")

    mesh = component.mesh().create("mesh1")
    mesh.feature().create("ftr1", "FreeTet")
    mesh.run()
    study = java_model.study().create("std1")
    study.create("stat", "Stationary")
    return {
        "geometry": {**topology, "bounding_box_m": bounding_box},
        "electrodes": electrodes,
        "mesh": {
            "tag": "mesh1",
            "element_type": "FreeTet",
            "elements": int(mesh.getNumElem()),
            "vertices": int(mesh.getNumVertex()),
        },
        "physics": {"tag": "es", "domain_feature": "ccn1", "material": "mat1"},
        "study": {"tag": "std1", "step": "stat", "type": "Stationary"},
    }


def validate_solution(
    model: mph.Model,
    *,
    plate_side_m: float = PLATE_SIDE_M,
    plate_separation_m: float = PLATE_SEPARATION_M,
    relative_permittivity: float = RELATIVE_PERMITTIVITY,
    maximum_relative_error: float = DEFAULT_MAX_RELATIVE_ERROR,
) -> dict[str, Any]:
    """Solve and compare energy-derived capacitance with the analytical value."""
    model.java.study("std1").run()
    expression = "2*es.intWe/(V0)^2"
    values = np.asarray(model.evaluate(expression, "pF")).reshape(-1)
    if values.size != 1:
        raise RuntimeError(f"unexpected capacitance result shape: {values.shape}")
    measured_complex = complex(values[0])
    if not math.isfinite(measured_complex.real) or not math.isfinite(measured_complex.imag):
        raise RuntimeError("capacitance result is non-finite")
    if abs(measured_complex.imag) > max(abs(measured_complex.real), 1.0) * 1e-12:
        raise RuntimeError(f"capacitance has an unexpected imaginary part: {measured_complex}")
    measured = measured_complex.real
    analytical = _theoretical_capacitance_pf(
        plate_side_m,
        plate_separation_m,
        relative_permittivity,
    )
    relative_error = abs(measured - analytical) / analytical
    if relative_error > maximum_relative_error:
        raise AssertionError(
            "Capacitance mismatch: "
            f"measured={measured}, analytical={analytical}, relative_error={relative_error}"
        )
    return {
        "status": "verified",
        "expression": expression,
        "unit": "pF",
        "measured_capacitance_pf": measured,
        "analytical_capacitance_pf": analytical,
        "relative_error": relative_error,
        "maximum_relative_error": maximum_relative_error,
        "analytical_model": "epsilon_0_times_epsilon_r_times_area_over_separation",
    }


def save_staged_model(java_model: Any, output: Path) -> Path:
    """Save a complete model while COMSOL may still hold the staging file."""
    staging = output.with_name(f".{output.stem}.{uuid.uuid4().hex}.mph")
    try:
        java_model.save(str(staging))
        if not staging.is_file() or staging.stat().st_size <= 0:
            raise RuntimeError("COMSOL did not create a complete staging model")
        return staging
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def _retry_permission_error(
    operation, *, retry_seconds: float = WINDOWS_FILE_RETRY_SECONDS
) -> None:
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            operation()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(WINDOWS_FILE_RETRY_INTERVAL_SECONDS)


def publish_staged_model(staging: Path, output: Path, *, overwrite: bool) -> None:
    """Publish only after COMSOL releases the staged model file."""
    try:
        if overwrite:
            _retry_permission_error(lambda: os.replace(staging, output))
            return
        _retry_permission_error(lambda: os.link(staging, output))
        _retry_permission_error(staging.unlink)
    except BaseException:
        if staging.exists():
            _retry_permission_error(staging.unlink)
        raise


def main() -> None:
    args = parse_args()
    output = args.output_model.expanduser().resolve()
    receipt = (
        args.receipt.expanduser().resolve()
        if args.receipt is not None
        else output.with_suffix(".receipt.json")
    )
    _validate_inputs(
        args.plate_side_m,
        args.plate_separation_m,
        args.relative_permittivity,
        args.potential_v,
        args.maximum_relative_error,
    )
    if output.suffix.casefold() != ".mph":
        raise ValueError("--output-model must use the .mph suffix")
    if not str(output).isascii() or not str(receipt).isascii():
        raise ValueError("model and receipt paths must contain ASCII characters only")
    if output == receipt:
        raise ValueError("--receipt must differ from --output-model")
    if output.exists() and not args.overwrite_output:
        raise FileExistsError(
            "--output-model already exists; pass --overwrite-output to replace it"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)

    ownership = SolverOwnership(owner="parallel-plate-capacitor-recipe")
    preflight = ownership.preflight(output_path=str(output), requested_version="6.4")
    if not preflight.get("ready"):
        raise RuntimeError(f"COMSOL preflight failed: {preflight.get('blockers')}")
    claim = ownership.acquire(mode="standalone-capacitor-recipe")
    if not claim.get("success"):
        raise RuntimeError(claim.get("error", "solver ownership claim failed"))

    client = None
    staging = None
    build = None
    validation = None
    cleanup_errors: list[str] = []
    try:
        client = mph.Client(version="6.4")
        ownership.heartbeat(refresh_server_processes=True)
        model = client.create("ParallelPlateCapacitorRecipe")
        build = build_parallel_plate_capacitor(
            model,
            plate_side_m=args.plate_side_m,
            plate_separation_m=args.plate_separation_m,
            relative_permittivity=args.relative_permittivity,
            potential_v=args.potential_v,
        )
        validation = (
            validate_solution(
                model,
                plate_side_m=args.plate_side_m,
                plate_separation_m=args.plate_separation_m,
                relative_permittivity=args.relative_permittivity,
                maximum_relative_error=args.maximum_relative_error,
            )
            if args.solve
            else {"status": "not_requested", "reason": "--solve was not supplied"}
        )
        staging = save_staged_model(model.java, output)
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception as exc:
                cleanup_errors.append(f"client.clear: {type(exc).__name__}")
            if getattr(client, "port", None):
                try:
                    client.disconnect()
                except Exception as exc:
                    cleanup_errors.append(f"client.disconnect: {type(exc).__name__}")
        release = ownership.release()
        if not release.get("success"):
            cleanup_errors.append(f"lease.release: {release.get('error', 'unknown error')}")
        if cleanup_errors:
            if staging is not None and staging.exists():
                _retry_permission_error(staging.unlink)
            raise RuntimeError(f"Capacitor recipe cleanup was incomplete: {cleanup_errors}")

    if staging is None or build is None or validation is None:
        raise RuntimeError("capacitor model staging did not complete")
    publish_staged_model(staging, output, overwrite=args.overwrite_output)
    payload = {
        "schema_name": "comsol_mcp.parallel_plate_capacitor_recipe_receipt",
        "schema_version": "1.0.0",
        "status": "verified" if args.solve else "built_not_solved",
        "model_sha256": _sha256_file(output),
        "configuration": {
            "plate_side_m": args.plate_side_m,
            "plate_separation_m": args.plate_separation_m,
            "relative_permittivity": args.relative_permittivity,
            "potential_v": args.potential_v,
        },
        "build": build,
        "validation": validation,
        "source_model": "created_from_scratch",
    }
    _atomic_json(receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
