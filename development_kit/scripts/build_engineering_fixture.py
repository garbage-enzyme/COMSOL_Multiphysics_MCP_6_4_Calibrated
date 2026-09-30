"""Build the controlled engineering fixture model and its hash-bound spec.

This is a **fixture builder**, not a scientific recipe. It constructs the
periodic wave-optics engineering model that the licensed integration probes
require, reads the top-air domain identity and coordinate extent back out of the
model it actually built, saves the model, and writes the SHA-256-bound fixture
spec those probes consume.

Scope, stated plainly: the model uses a single lossless dielectric
(``relative_permittivity = 2.1``) as the layer material. It exists to exercise
interfaces, ownership, artifact handling, and gate behaviour against real
licensed COMSOL. It does **not** reproduce any published structure and does
**not** validate a real metal or metasurface. No physical claim is made here.

Safety properties enforced here, all of which the diagnostic recipes do not have:

* single core, always, declared through ``--cores`` and refused if not exactly 1;
* a private per-run output directory, created under an ASCII root outside the
  checkout, never a shared one;
* the solver lease is acquired before COMSOL starts and released in ``finally``;
* cleanup runs on every path, including failures, and reports each step;
* the source model is never overwritten -- the target must not already exist and
  saving goes through a unique staging file that is published with ``os.link``
  only after the COMSOL client has released it;
* the air domain identity and coordinate extent are read back from the built
  model, never copied from the Python literals used to create it;
* the spec's SHA-256 is computed after the model is saved.

Usage::

    python development_kit/scripts/build_engineering_fixture.py \
      --cores 1 \
      --output-root D:\\mcp_tests\\a76fix1 \
      --confirm BUILD_REAL_COMSOL
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from importlib import import_module
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Imported after the repository root is on the path, matching the sibling
# repository-only gates, so this script runs from any working directory.
SolverOwnership = import_module("comsol_mcp.tools.ownership").SolverOwnership

#: Geometry of the controlled fixture, in metres. These are the *requested*
#: values only; every value written into the spec is re-read from the built
#: model so the spec describes the artifact rather than this intent.
PERIOD_X_M = 0.6e-6
PERIOD_Y_M = 0.6e-6
LAYER_THICKNESS_M = 30e-9
AIR_HEIGHT_M = 0.83e-6

TOP_AIR_SELECTION_TAG = "geom1_b_air_dom"
COMPONENT_TAG = "comp1"
GEOMETRY_TAG = "geom1"
AIR_BLOCK_TAG = "b_air"
LAYER_BLOCK_TAG = "b_al2"

#: The interface probes require the layer to be a plain lossless dielectric.
#: This is deliberately not a Drude or measured-gold model.
FIXTURE_RELATIVE_PERMITTIVITY = "2.1"

#: Fixture wavelength in micrometres, matching the spec schema's declared unit.
FIXTURE_WAVELENGTH_UM = 5.0

MODEL_FILENAME = "controlled_wo_fixture.mph"
SPEC_FILENAME = "controlled_wo_fixture_spec.json"
RECEIPT_FILENAME = "fixture_build_receipt.json"

#: A coordinate match this tight is safe because COMSOL returns the block's own
#: corners; the earlier API probe confirmed exact agreement to 1e-18 relative.
COORDINATE_RELATIVE_TOLERANCE = 1e-9

#: Bounded retry for the Windows handle race between a COMSOL save and publishing
#: the staged file, matching the canonical standalone recipes.
WINDOWS_FILE_RETRY_SECONDS = 3.0
WINDOWS_FILE_RETRY_INTERVAL_SECONDS = 0.05


def _jarray(values: list[float]):
    import jpype

    return jpype.JArray(jpype.JDouble)(values)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_ascii_absolute(path: Path, label: str) -> Path:
    text = str(path)
    if not text.isascii():
        raise ValueError(f"{label} must be an ASCII path")
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    return path.resolve()


def _require_single_core(cores: int) -> int:
    """The fixture is a single-cell engineering model and must not claim more."""
    if isinstance(cores, bool) or cores != 1:
        raise ValueError("the engineering fixture must be built with exactly 1 core")
    return cores


def _face_corners(geometry: Any, boundary: int) -> list[list[float]]:
    """Return the parameter-space corners of one boundary in 3-D.

    ``faceX`` takes and returns ``double[][]``; passing ``double[]`` raises a
    JPype overload error. This shape is confirmed on real COMSOL 6.4.
    """
    import jpype

    ranges = [float(value) for value in list(geometry.faceParamRange(boundary))]
    corners = []
    for first in (ranges[0], ranges[1]):
        for second in (ranges[2], ranges[3]):
            point = jpype.JArray(jpype.JArray(jpype.JDouble))(1)
            point[0] = _jarray([first, second])
            corners.append([float(value) for value in list(geometry.faceX(boundary, point)[0])])
    return corners


def _extent_of(geometry: Any, boundaries: list[int]) -> dict[str, list[float]]:
    """Aggregate sampled face corners into a per-axis [min, max] extent."""
    if not boundaries:
        raise ValueError("cannot measure an extent from an empty boundary set")
    collected: dict[str, list[float]] = {"x": [], "y": [], "z": []}
    for boundary in boundaries:
        for corner in _face_corners(geometry, boundary):
            for index, axis in enumerate(("x", "y", "z")):
                collected[axis].append(corner[index])
    return {axis: [min(values), max(values)] for axis, values in collected.items()}


def _boundaries_adjacent_to(geometry: Any, domains: set[int]) -> list[int]:
    up_down = geometry.getUpDown()
    ups = [int(value) for value in list(up_down[0])]
    downs = [int(value) for value in list(up_down[1])]
    if len(ups) != len(downs) or len(ups) != int(geometry.getNBoundaries()):
        raise ValueError("geometry adjacency arrays are incomplete")
    return [
        index
        for index, (up, down) in enumerate(zip(ups, downs, strict=True), start=1)
        if {up, down} & domains
    ]


def _close(actual: list[float], expected: list[float]) -> bool:
    for observed, wanted in zip(actual, expected, strict=True):
        scale = max(abs(wanted), 1e-12)
        if abs(observed - wanted) / scale > COORDINATE_RELATIVE_TOLERANCE:
            return False
    return True


def _build_model(client: Any) -> Any:
    """Create the two-block periodic cell and its wave-optics interface."""
    model = client.create("ControlledWaveOpticsFixture")
    java = model.java
    component = java.component().create(COMPONENT_TAG, True)
    geometry = component.geom().create(GEOMETRY_TAG, 3)

    layer = geometry.feature().create(LAYER_BLOCK_TAG, "Block")
    layer.set("size", _jarray([PERIOD_X_M, PERIOD_Y_M, LAYER_THICKNESS_M]))
    layer.set("pos", _jarray([0.0, 0.0, 0.0]))
    layer.set("selresult", True)

    air = geometry.feature().create(AIR_BLOCK_TAG, "Block")
    air.set("size", _jarray([PERIOD_X_M, PERIOD_Y_M, AIR_HEIGHT_M]))
    air.set("pos", _jarray([0.0, 0.0, LAYER_THICKNESS_M]))
    air.set("selresult", True)

    geometry.run()

    air_domains = sorted(
        int(value) for value in list(component.selection(TOP_AIR_SELECTION_TAG).entities(3))
    )
    if len(air_domains) != 1:
        raise ValueError(f"the air block must select exactly one domain, got {air_domains}")
    layer_selection_tag = f"geom1_{LAYER_BLOCK_TAG}_dom"
    layer_domains = sorted(
        int(value) for value in list(component.selection(layer_selection_tag).entities(3))
    )
    if len(layer_domains) != 1:
        raise ValueError(f"the layer block must select exactly one domain, got {layer_domains}")

    for material_tag, domains, label in (
        ("mat_layer", layer_domains, "fixture layer (lossless dielectric placeholder)"),
        ("mat_air", air_domains, "fixture air"),
    ):
        material = component.material().create(material_tag, "Common")
        material.propertyGroup("def").set(
            "relpermittivity",
            FIXTURE_RELATIVE_PERMITTIVITY if material_tag == "mat_layer" else "1",
        )
        material.selection().set(domains)

    physics = component.physics().create(
        "ewfd", "ElectromagneticWavesFrequencyDomain", str(int(geometry.getSDim()))
    )
    # The incidence probe requires exactly one PeriodicStructure.
    physics.feature().create("ps1", "PeriodicStructure", 3)

    mesh = component.mesh().create("mesh1")
    size = mesh.feature().create("size1", "Size")
    size.set("hmax", float(AIR_HEIGHT_M / 10.0))
    size.set("hmaxactive", True)
    mesh.feature().create("ftet1", "FreeTet")
    mesh.run()

    study = java.study().create("std1")
    study.create("step1", "Wavelength")
    java.param().set("wl", f"{FIXTURE_WAVELENGTH_UM}[um]")

    return model


def _retry_permission_error(
    operation, *, retry_seconds: float = WINDOWS_FILE_RETRY_SECONDS
) -> None:
    """Retry a Windows file operation while COMSOL still holds the handle.

    Saving then publishing raises ``WinError 32`` because the client has not yet
    released the staging file. This is the same race the canonical standalone
    recipes handle with a bounded retry, and it was observed on real hardware
    while building this fixture.
    """
    deadline = time.monotonic() + retry_seconds
    while True:
        try:
            operation()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(WINDOWS_FILE_RETRY_INTERVAL_SECONDS)


def _save_staged(java_model: Any, destination: Path) -> Path:
    """Save to a unique staging path beside the destination.

    Publication is deliberately *not* done here. A loaded COMSOL model keeps a
    handle open on the file it just wrote, so publishing or deleting that file
    while the client still owns the model raises ``WinError 32``. Measured on
    real hardware: the staging file survived a 3-second retry window until the
    client was cleared first.
    """
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite an existing fixture model: {destination}")
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.staging")
    java_model.save(str(staging))
    if not staging.is_file() or staging.stat().st_size <= 0:
        raise RuntimeError("COMSOL did not produce a nonempty staging model")
    return staging


def _publish_staged(staging: Path, destination: Path) -> Path:
    """Publish a released staging file without ever replacing an existing model.

    ``os.link`` is used instead of ``os.replace`` so publication is atomic *and*
    cannot silently clobber an existing fixture model: linking fails outright
    when the destination already exists.
    """
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite an existing fixture model: {destination}")
    _retry_permission_error(lambda: os.link(staging, destination))
    _retry_permission_error(staging.unlink)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("published fixture model is missing or empty")
    return destination


def _release_client(client: Any, cleanup_errors: list[str]) -> None:
    """Clear and disconnect one client, recording every cleanup failure.

    A failure here is never swallowed: it lands in ``cleanup_errors``, which
    fails the run, because a client left holding a model also leaves file
    handles behind.
    """
    try:
        client.clear()
    except Exception as exc:
        cleanup_errors.append(f"client.clear: {type(exc).__name__}")
    if getattr(client, "port", None):
        try:
            client.disconnect()
        except Exception as exc:
            cleanup_errors.append(f"client.disconnect: {type(exc).__name__}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--confirm", required=True, choices=["BUILD_REAL_COMSOL"])
    parser.add_argument("--version", default="6.4")
    arguments = parser.parse_args()

    _require_single_core(arguments.cores)

    run_root = _require_ascii_absolute(arguments.output_root, "output root") / (
        f"fixture-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    model_path = run_root / MODEL_FILENAME
    spec_path = run_root / SPEC_FILENAME
    receipt_path = run_root / RECEIPT_FILENAME

    ownership = SolverOwnership(owner="engineering-fixture-builder")
    preflight = ownership.preflight(output_path=str(run_root), requested_version=arguments.version)
    if not preflight.get("ready"):
        raise RuntimeError(f"COMSOL preflight failed: {preflight.get('blockers')}")
    claim = ownership.acquire(mode="build-engineering-fixture")
    if not claim.get("success"):
        raise RuntimeError(claim.get("error", "solver ownership claim failed"))

    import mph

    client = None
    receipt: dict[str, Any] = {
        "schema_name": "comsol_mcp.engineering_fixture_build",
        "schema_version": "1.0.0",
        "success": False,
        "cores": arguments.cores,
        "run_root": str(run_root),
        "scope": (
            "controlled engineering fixture for interface and gate behaviour only; "
            "no published-structure and no real-metal validation is claimed"
        ),
        "material_model": {
            "layer": "lossless dielectric placeholder",
            "relative_permittivity": FIXTURE_RELATIVE_PERMITTIVITY,
            "is_real_metal": False,
        },
        "source_model_overwritten": False,
    }
    cleanup_errors: list[str] = []
    staging: Path | None = None
    try:
        client = mph.Client(cores=arguments.cores, version=arguments.version)
        ownership.heartbeat(refresh_server_processes=True)
        model = _build_model(client)

        component = model.java.component(COMPONENT_TAG)
        geometry = component.geom(GEOMETRY_TAG)
        air_domains = sorted(
            int(value) for value in list(component.selection(TOP_AIR_SELECTION_TAG).entities(3))
        )
        air_coordinate_range = _extent_of(
            geometry, _boundaries_adjacent_to(geometry, set(air_domains))
        )

        # Every value below is read from the model that was actually built.
        expected_air = {
            "x": [0.0, PERIOD_X_M],
            "y": [0.0, PERIOD_Y_M],
            "z": [LAYER_THICKNESS_M, LAYER_THICKNESS_M + AIR_HEIGHT_M],
        }
        for axis in ("x", "y", "z"):
            if not _close(air_coordinate_range[axis], expected_air[axis]):
                raise ValueError(
                    f"measured air extent on {axis} is {air_coordinate_range[axis]}, "
                    f"which contradicts the built geometry {expected_air[axis]}"
                )

        tags = {
            "components": [str(value) for value in list(model.java.component().tags())],
            "physics": [str(value) for value in list(component.physics().tags())],
            "studies": [str(value) for value in list(model.java.study().tags())],
            "meshes": [str(value) for value in list(component.mesh().tags())],
            "periodic_structures": [
                str(value)
                for value in list(component.physics("ewfd").feature().tags())
                if "periodic" in str(component.physics("ewfd").feature(value).getType()).casefold()
            ],
        }
        if tags["periodic_structures"] != ["ps1"]:
            raise ValueError(
                f"exactly one PeriodicStructure is required, found {tags['periodic_structures']}"
            )
        mesh_elements = int(component.mesh("mesh1").getNumElem())

        staging = _save_staged(model.java, model_path)

        # Release COMSOL before touching the file it just wrote: a loaded model
        # keeps a handle open, and the staging file cannot be published or
        # removed until that handle is gone.
        _release_client(client, cleanup_errors)
        client = None
        if cleanup_errors:
            raise RuntimeError(f"client release failed before publish: {cleanup_errors}")

        _publish_staged(staging, model_path)
        staging = None
        model_sha256 = _sha256_file(model_path)

        spec = {
            "source_model_path": str(model_path),
            "expected_source_sha256": model_sha256,
            "wavelength": {"value": FIXTURE_WAVELENGTH_UM, "unit": "um"},
            "reference_air": {
                "top_air_domain_ids": air_domains,
                "top_air_coordinate_range": air_coordinate_range,
            },
        }
        spec_path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")

        receipt.update(
            success=True,
            model_path=str(model_path),
            model_sha256=model_sha256,
            model_bytes=model_path.stat().st_size,
            spec_path=str(spec_path),
            spec_sha256=_sha256_file(spec_path),
            air_domains=air_domains,
            air_coordinate_range=air_coordinate_range,
            air_extent_verified_against_geometry=True,
            mesh_elements=mesh_elements,
            tags=tags,
        )
    except BaseException as exc:  # noqa: BLE001 - record, clean up, then re-raise
        receipt["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if client is not None:
            _release_client(client, cleanup_errors)
        if staging is not None and staging.exists():
            try:
                _retry_permission_error(staging.unlink)
            except OSError as exc:
                cleanup_errors.append(f"staging.unlink: {type(exc).__name__}")
        release = ownership.release()
        if not release.get("success"):
            cleanup_errors.append(f"lease.release: {release.get('error', 'unknown error')}")
        receipt["cleanup"] = {"passed": not cleanup_errors, "errors": cleanup_errors}
        receipt_path.write_text(json.dumps(receipt, indent=2, default=str) + "\n", encoding="utf-8")
        if cleanup_errors:
            raise RuntimeError(f"fixture build cleanup was incomplete: {cleanup_errors}")

    print(json.dumps(receipt, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
