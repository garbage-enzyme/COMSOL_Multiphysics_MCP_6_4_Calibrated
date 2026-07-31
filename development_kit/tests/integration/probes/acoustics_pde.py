"""Licensed analytical Acoustics and PDE acceptance for COMSOL 6.4."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import jpype
import mph
import numpy as np

ROOT = Path(__file__).parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from comsol_mcp.tools.acoustics_pde import (  # noqa
    add_pde_interface,
    configure_boundaries,
)
from comsol_mcp.tools.geometry_selections import create_side_selections  # noqa
from comsol_mcp.tools.mesh import create_mesh_sequence  # noqa
from comsol_mcp.tools.ownership import SolverOwnership  # noqa
from recipes.acoustic_duct_2d import (  # noqa
    DEFAULT_FREQUENCY_HZ,
    DEFAULT_MAX_RELATIVE_ERROR,
    build_acoustic_duct,
    validate_solution,
)


def _require_success(result: dict, operation: str) -> dict:
    if not result.get("success"):
        raise RuntimeError(f"{operation} failed: {result}")
    return result


def _field_columns(model: mph.Model) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    raw = np.asarray(model.evaluate(["x", "y", "u"]), dtype=np.complex128)
    if raw.ndim != 2:
        raise RuntimeError(f"unexpected PDE field rank: {raw.shape}")
    if raw.shape[0] == 3:
        x, y, value = raw
    elif raw.shape[1] == 3:
        x, y, value = raw.T
    else:
        raise RuntimeError(f"unexpected PDE field shape: {raw.shape}")
    return np.real(x), np.real(y), value


def _build_and_validate_poisson(client: mph.Client) -> dict:
    model = client.create("AnalyticalPoissonAcceptance")
    jm = model.java
    component = jm.component().create("comp1", True)
    geometry = component.geom().create("geom1", 2)
    rectangle = geometry.feature().create("square", "Rectangle")
    rectangle.set("size", jpype.JArray(jpype.JString)(["1[m]", "1[m]"]))
    rectangle.set("pos", jpype.JArray(jpype.JString)(["0", "0"]))
    geometry.run()

    selections = _require_success(
        create_side_selections(
            model,
            x_min="0[m]",
            x_max="1[m]",
            y_min="0[m]",
            y_max="1[m]",
            prefix="square",
            tolerance="1e-7[m]",
        ),
        "PDE side selections",
    )
    side_entities = {
        side: tuple(info["entities"]) for side, info in selections["selections"].items()
    }
    if any(len(values) != 1 for values in side_entities.values()):
        raise RuntimeError(f"each square side must select one boundary: {side_entities}")
    if len({values[0] for values in side_entities.values()}) != 4:
        raise RuntimeError(f"square sides must be distinct: {side_entities}")

    coefficient = _require_success(
        add_pde_interface(
            model,
            "coefficient",
            dependent_variables=["u"],
            equation_properties={
                "c": "1",
                "a": "0",
                "f": "2*pi^2*sin(pi*x/1[m])*sin(pi*y/1[m])/1[m^2]",
            },
            physics_tag="c",
        ),
        "Coefficient Form PDE",
    )
    boundaries = _require_success(
        configure_boundaries(
            model,
            "c",
            [
                {
                    "type": "DirichletBoundary",
                    "selection_name": f"square_{side}",
                    "properties": {"r": "0"},
                    "tag": f"zero_{side}",
                }
                for side in ("left", "right", "bottom", "top")
            ],
            family="pde",
        ),
        "PDE boundary batch",
    )
    mesh = _require_success(
        create_mesh_sequence(model, mesh_name="mesh1", element_type="FreeTri", build=True),
        "PDE mesh",
    )
    study = jm.study().create("std1")
    study.create("stat", "Stationary")
    study.run()

    x, y, values = _field_columns(model)
    index = int(np.argmin(np.hypot(x - 0.5, y - 0.5)))
    measured = complex(values[index])
    analytical = math.sin(math.pi * float(x[index])) * math.sin(math.pi * float(y[index]))
    relative_error = abs(measured - analytical) / max(abs(analytical), 1e-12)
    if relative_error > 0.02:
        raise AssertionError(
            f"Poisson mismatch: measured={measured}, analytical={analytical}, "
            f"relative_error={relative_error}"
        )

    general = _require_success(
        add_pde_interface(
            model,
            "general",
            dependent_variables=["ugen"],
            physics_tag="g",
        ),
        "General Form PDE",
    )
    weak = _require_success(
        add_pde_interface(
            model,
            "weak",
            dependent_variables=["uweak"],
            physics_tag="w",
        ),
        "Weak Form PDE",
    )
    return {
        "status": "verified",
        "side_entities": {side: list(values) for side, values in side_entities.items()},
        "coefficient": coefficient["physics"],
        "general": general["physics"],
        "weak": weak["physics"],
        "boundary_count": boundaries["configured_count"],
        "mesh": mesh,
        "sample": {
            "x_m": float(x[index]),
            "y_m": float(y[index]),
            "measured": {"real": measured.real, "imag": measured.imag},
            "analytical": analytical,
            "relative_error": float(relative_error),
        },
    }


def main() -> None:
    ownership = SolverOwnership(owner="acoustics-pde-integration-probe")
    preflight = ownership.preflight(requested_version="6.4")
    if not preflight.get("ready"):
        raise RuntimeError(preflight.get("blockers"))
    claim = ownership.acquire(mode="licensed-acoustics-pde-probe")
    if not claim.get("success"):
        raise RuntimeError(claim)

    client = None
    output = None
    cleanup_errors = []
    try:
        client = mph.Client(version="6.4")
        ownership.heartbeat(refresh_server_processes=True)
        acoustic_model = client.create("AnalyticalAcousticDuctAcceptance")
        acoustic_build = build_acoustic_duct(acoustic_model, DEFAULT_FREQUENCY_HZ)
        acoustic_validation = validate_solution(
            acoustic_model,
            DEFAULT_FREQUENCY_HZ,
            DEFAULT_MAX_RELATIVE_ERROR,
        )
        pde = _build_and_validate_poisson(client)
        output = {
            "success": True,
            "runtime": {"comsol": "6.4.0.293", "mph": mph.__version__},
            "acoustic": {"build": acoustic_build, "validation": acoustic_validation},
            "pde": pde,
        }
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
            cleanup_errors.append(f"lease.release: {release.get('error', 'unknown')}")
        if cleanup_errors:
            raise RuntimeError(f"Acoustics/PDE probe cleanup failed: {cleanup_errors}")

    if output is None:
        raise RuntimeError("Acoustics/PDE probe produced no output")
    print(json.dumps(output, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
