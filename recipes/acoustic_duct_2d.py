"""Build a minimal physical 2D pressure-acoustics duct model.

The model is a lossless rectangular air duct with rigid top and bottom walls,
a prescribed harmonic pressure at the left boundary, and zero pressure at the
right boundary. The default frequency is below the first transverse cutoff, so
the numerical midline pressure can be compared with the one-dimensional
Helmholtz solution.

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
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import jpype
import mph
import numpy as np

from comsol_mcp.tools.acoustics_pde import (
    add_pressure_acoustics_interface,
    configure_boundaries,
)
from comsol_mcp.tools.geometry_selections import create_side_selections
from comsol_mcp.tools.mesh import create_mesh_sequence
from comsol_mcp.tools.ownership import SolverOwnership

LENGTH_M = 1.0
HEIGHT_M = 0.1
DENSITY_KG_PER_CUBIC_METER = 1.204
SOUND_SPEED_M_PER_S = 343.0
DEFAULT_FREQUENCY_HZ = 100.0
DEFAULT_MAX_RELATIVE_ERROR = 0.02
WINDOWS_FILE_RETRY_SECONDS = 3.0
WINDOWS_FILE_RETRY_INTERVAL_SECONDS = 0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-model", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--frequency-hz", type=float, default=DEFAULT_FREQUENCY_HZ)
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


def _validate_inputs(frequency_hz: float, maximum_relative_error: float) -> None:
    if not math.isfinite(frequency_hz) or frequency_hz <= 0.0:
        raise ValueError("--frequency-hz must be finite and positive")
    cutoff = SOUND_SPEED_M_PER_S / (2.0 * HEIGHT_M)
    if frequency_hz >= cutoff:
        raise ValueError("--frequency-hz must remain below the first transverse cutoff")
    denominator = math.sin(2.0 * math.pi * frequency_hz * LENGTH_M / SOUND_SPEED_M_PER_S)
    if abs(denominator) < 0.1:
        raise ValueError("--frequency-hz is too close to a longitudinal resonance")
    if (
        not math.isfinite(maximum_relative_error)
        or maximum_relative_error <= 0.0
        or maximum_relative_error > 0.2
    ):
        raise ValueError("--maximum-relative-error must be in (0, 0.2]")


def _require_success(result: dict[str, Any], operation: str) -> dict[str, Any]:
    if not result.get("success"):
        raise RuntimeError(f"{operation} failed: {result}")
    return result


def build_acoustic_duct(model: mph.Model, frequency_hz: float) -> dict[str, Any]:
    """Build the geometry, selections, acoustics, mesh, and one-point study."""
    java_model = model.java
    java_model.param().set("duct_L", f"{LENGTH_M:.17g}[m]")
    java_model.param().set("duct_H", f"{HEIGHT_M:.17g}[m]")
    java_model.param().set("rho_air", f"{DENSITY_KG_PER_CUBIC_METER:.17g}[kg/m^3]")
    java_model.param().set("c_air", f"{SOUND_SPEED_M_PER_S:.17g}[m/s]")
    java_model.param().set("freq0", f"{frequency_hz:.17g}[Hz]")

    component = java_model.component().create("comp1", True)
    geometry = component.geom().create("geom1", 2)
    rectangle = geometry.feature().create("duct", "Rectangle")
    rectangle.set("size", jpype.JArray(jpype.JString)(["duct_L", "duct_H"]))
    rectangle.set("pos", jpype.JArray(jpype.JString)(["0", "0"]))
    geometry.run()

    selections = _require_success(
        create_side_selections(
            model,
            x_min="0[m]",
            x_max="duct_L",
            y_min="0[m]",
            y_max="duct_H",
            prefix="duct",
            tolerance="1e-7[m]",
        ),
        "side selection creation",
    )
    entity_sets = {side: tuple(info["entities"]) for side, info in selections["selections"].items()}
    if any(len(entities) != 1 for entities in entity_sets.values()):
        raise RuntimeError(f"each duct side must resolve to one boundary: {entity_sets}")
    if len({entities[0] for entities in entity_sets.values()}) != 4:
        raise RuntimeError(f"duct side selections must be distinct: {entity_sets}")

    physics_result = _require_success(
        add_pressure_acoustics_interface(model, physics_tag="acpr"),
        "Pressure Acoustics creation",
    )
    acoustics = component.physics().get("acpr")
    fluid = acoustics.feature().get("fpam1")
    fluid.set("rho_mat", "userdef")
    fluid.set("rho", "rho_air")
    fluid.set("c_mat", "userdef")
    fluid.set("c", "c_air")

    boundaries = _require_success(
        configure_boundaries(
            model,
            "acpr",
            [
                {
                    "type": "Pressure",
                    "selection_name": "duct_left",
                    "properties": {"p0": "1[Pa]"},
                    "tag": "pressure_in",
                    "label": "One pascal inlet",
                },
                {
                    "type": "SoundSoft",
                    "selection_name": "duct_right",
                    "tag": "pressure_zero",
                    "label": "Zero pressure outlet",
                },
            ],
            family="acoustic",
        ),
        "acoustic boundary setup",
    )

    mesh = _require_success(
        create_mesh_sequence(
            model,
            mesh_name="mesh1",
            element_type="FreeTri",
            build=True,
        ),
        "mesh creation",
    )
    study = java_model.study().create("std1")
    frequency = study.create("freq", "Frequency")
    frequency.set("plist", "freq0")
    return {
        "geometry": {
            "domains": int(geometry.getNDomains()),
            "boundaries": int(geometry.getNBoundaries()),
        },
        "side_entities": {side: list(values) for side, values in entity_sets.items()},
        "physics": physics_result["physics"],
        "configured_boundaries": boundaries["configured_boundaries"],
        "mesh": mesh,
        "study": {"tag": "std1", "step": "freq", "frequency_hz": frequency_hz},
    }


def _field_columns(model: mph.Model) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    raw = np.asarray(model.evaluate(["x", "y", "acpr.p_t"]), dtype=np.complex128)
    if raw.ndim != 2:
        raise RuntimeError(f"unexpected field-evaluation rank: {raw.shape}")
    if raw.shape[0] == 3:
        x, y, pressure = raw
    elif raw.shape[1] == 3:
        x, y, pressure = raw.T
    else:
        raise RuntimeError(f"unexpected field-evaluation shape: {raw.shape}")
    return np.real(x), np.real(y), pressure


def validate_solution(
    model: mph.Model,
    frequency_hz: float,
    maximum_relative_error: float,
) -> dict[str, Any]:
    """Solve and compare the nearest center sample with the analytical mode."""
    model.java.study("std1").run()
    x, y, pressure = _field_columns(model)
    distances = np.hypot(x - LENGTH_M / 2.0, y - HEIGHT_M / 2.0)
    index = int(np.argmin(distances))
    sample_x = float(x[index])
    sample_y = float(y[index])
    measured = complex(pressure[index])
    wavenumber = 2.0 * math.pi * frequency_hz / SOUND_SPEED_M_PER_S
    analytical = math.sin(wavenumber * (LENGTH_M - sample_x)) / math.sin(wavenumber * LENGTH_M)
    relative_error = abs(measured - analytical) / max(abs(analytical), 1e-12)
    if relative_error > maximum_relative_error:
        raise AssertionError(
            "Acoustic pressure mismatch: "
            f"measured={measured}, analytical={analytical}, relative_error={relative_error}"
        )
    return {
        "status": "verified",
        "sample": {
            "x_m": sample_x,
            "y_m": sample_y,
            "distance_to_center_m": float(distances[index]),
        },
        "measured_pressure_pa": {"real": measured.real, "imag": measured.imag},
        "analytical_pressure_pa": analytical,
        "relative_error": float(relative_error),
        "maximum_relative_error": maximum_relative_error,
        "analytical_model": "lossless_one_dimensional_Helmholtz_rigid_sidewalls",
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
    _validate_inputs(args.frequency_hz, args.maximum_relative_error)
    if output.suffix.casefold() != ".mph":
        raise ValueError("--output-model must use the .mph suffix")
    if output == receipt:
        raise ValueError("--receipt must differ from --output-model")
    if output.exists() and not args.overwrite_output:
        raise FileExistsError(
            "--output-model already exists; pass --overwrite-output to replace it"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)

    ownership = SolverOwnership(owner="acoustic-duct-recipe")
    preflight = ownership.preflight(output_path=str(output), requested_version="6.4")
    if not preflight.get("ready"):
        raise RuntimeError(f"COMSOL preflight failed: {preflight.get('blockers')}")
    claim = ownership.acquire(mode="standalone-acoustic-recipe")
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
        model = client.create("MinimalPressureAcousticsDuct")
        build = build_acoustic_duct(model, args.frequency_hz)
        validation = (
            validate_solution(model, args.frequency_hz, args.maximum_relative_error)
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
            message = f"Acoustic recipe cleanup was incomplete: {cleanup_errors}"
            active_error = sys.exception()
            if active_error is None:
                raise RuntimeError(message)
            active_error.add_note(message)

    publish_staged_model(staging, output, overwrite=args.overwrite_output)
    payload = {
        "schema_name": "comsol_mcp.acoustic_duct_recipe_receipt",
        "schema_version": "1.0.0",
        "status": "verified" if args.solve else "built_not_solved",
        "model_sha256": _sha256_file(output),
        "configuration": {
            "length_m": LENGTH_M,
            "height_m": HEIGHT_M,
            "density_kg_per_cubic_meter": DENSITY_KG_PER_CUBIC_METER,
            "sound_speed_m_per_s": SOUND_SPEED_M_PER_S,
            "frequency_hz": args.frequency_hz,
            "first_transverse_cutoff_hz": SOUND_SPEED_M_PER_S / (2.0 * HEIGHT_M),
        },
        "build": build,
        "validation": validation,
        "source_model": "created_from_scratch",
    }
    _atomic_json(receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
