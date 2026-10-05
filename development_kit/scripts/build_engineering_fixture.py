"""Build the controlled engineering fixture model and its hash-bound spec.

This is a **fixture builder**, not a scientific recipe. It constructs the
periodic wave-optics engineering model that the licensed integration probes
require, reads the top-air domain identity and coordinate extent back out of the
model it actually built, solves the declared wavelength, saves the model, and
writes the SHA-256-bound fixture spec those probes consume.

Scope, stated plainly: the model uses a single lossless dielectric
(``relative_permittivity = 2.1``) as the layer material. It exists to exercise
interfaces, ownership, artifact handling, periodic evidence, a real solve, and
gate behaviour against real licensed COMSOL. It does **not** reproduce any
published structure and does **not** validate a real metal or metasurface. The
R/T/A values it records are a numerical self-consistency check on a lossless
placeholder, not the reflectance of any physical device. No physical claim is
made here.

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
* the periodic evidence each licensed audit consumes is verified, not assumed;
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
MESH_TAG = "mesh1"
STUDY_TAG = "std1"
STUDY_STEP_TAG = "wl_step"
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


def _boundary_normals(geometry: Any) -> dict[int, dict[str, Any]]:
    """Read each boundary's center and outward normal, as the preflight does.

    ``_periodic_groups`` splits a Floquet selection by opposing normals, so the
    fixture must be able to prove which boundaries form a periodic side pair.
    """
    import jpype

    ups: list[int] = []
    downs: list[int] = []
    adjacency = geometry.getUpDown()
    ups = [int(value) for value in list(adjacency[0])]
    downs = [int(value) for value in list(adjacency[1])]

    out: dict[int, dict[str, Any]] = {}
    for number in range(1, int(geometry.getNBoundaries()) + 1):
        item: dict[str, Any] = {}
        if number <= len(ups) and number <= len(downs):
            item["up_domain"] = ups[number - 1]
            item["down_domain"] = downs[number - 1]
            item["interior"] = ups[number - 1] != 0 and downs[number - 1] != 0
        ranges = [float(value) for value in list(geometry.faceParamRange(number))]
        point = jpype.JArray(jpype.JArray(jpype.JDouble))(1)
        point[0] = _jarray([(ranges[0] + ranges[1]) / 2, (ranges[2] + ranges[3]) / 2])
        item["center"] = [float(value) for value in list(geometry.faceX(number, point)[0])]
        item["normal"] = [float(value) for value in list(geometry.faceNormal(number, point)[0])]
        out[number] = item
    return out


def _periodic_side_pairs(
    boundaries: dict[int, dict[str, Any]], bounding_box: list[float]
) -> dict[str, list[int]]:
    """Classify exterior boundaries into the periodic cell sides.

    Mirrors ``comsol_mcp/tools/mim_patch.py::_identify_side_pairs``, including its
    coordinate filter: a face is only a cell side if its normal points out of the
    cell *and* its center lies on the matching bounding-box plane. Without the
    coordinate filter an interior interface sharing the same normal would be
    copied as a periodic source and break Floquet mesh compatibility.
    """
    xmin, xmax, ymin, ymax, _zmin, _zmax = bounding_box
    tolerance = COORDINATE_RELATIVE_TOLERANCE * max(abs(xmax - xmin), abs(ymax - ymin), 1.0)
    sides: dict[str, list[int]] = {key: [] for key in ("x_src", "x_dst", "y_src", "y_dst")}
    for number, item in boundaries.items():
        if item.get("interior"):
            continue
        normal = item.get("normal")
        center = item.get("center")
        if not normal or not center:
            continue
        nx, ny, _nz = normal
        cx, cy, _cz = center
        if nx < -0.5 and abs(cx - xmin) <= tolerance:
            sides["x_src"].append(number)
        elif nx > 0.5 and abs(cx - xmax) <= tolerance:
            sides["x_dst"].append(number)
        elif ny < -0.5 and abs(cy - ymin) <= tolerance:
            sides["y_src"].append(number)
        elif ny > 0.5 and abs(cy - ymax) <= tolerance:
            sides["y_dst"].append(number)
    return {key: sorted(value) for key, value in sides.items()}


def _set_copy_face(feature: Any, source: list[int], destination: list[int]) -> str:
    """Set a directed CopyFace pair, matching mim_patch's contract handling."""
    failures = []
    for source_name, destination_name in (("source", "destination"), ("src", "dst")):
        try:
            feature.selection(source_name).set(source)
            feature.selection(destination_name).set(destination)
            return f"{source_name}/{destination_name}"
        except Exception as exc:
            failures.append(type(exc).__name__)
    raise RuntimeError(
        "CopyFace does not expose a supported directed source/destination selection "
        f"contract ({', '.join(failures)})"
    )


def _build_periodic_mesh(component: Any, sides: dict[str, list[int]]) -> tuple[Any, int]:
    """Build a Floquet-compatible mesh: FreeTri on the sources, CopyFace, FreeTet.

    A bare FreeTet mesh makes the periodic conditions fail at solve time with
    "source and destination meshes are not compatible", measured on real COMSOL
    6.4. Copying the source-face mesh onto the destination face is what makes the
    Floquet pair node-for-node identical, and it is the same recipe the
    repository's own ``mim_patch`` tool uses.
    """
    for key in ("x_src", "x_dst", "y_src", "y_dst"):
        if not sides[key]:
            raise ValueError(f"periodic side pair {key} is empty; cannot build a periodic mesh")

    mesh = component.mesh().create(MESH_TAG)
    size = mesh.feature().create("size1", "Size")
    size.set("hmax", float(AIR_HEIGHT_M / 10.0))
    size.set("hmaxactive", True)

    triangle_x = mesh.feature().create("ftri_x", "FreeTri")
    triangle_x.selection().set(sides["x_src"])
    triangle_y = mesh.feature().create("ftri_y", "FreeTri")
    triangle_y.selection().set(sides["y_src"])

    copy_x = mesh.feature().create("cp_x", "CopyFace")
    _set_copy_face(copy_x, sides["x_src"], sides["x_dst"])
    copy_y = mesh.feature().create("cp_y", "CopyFace")
    _set_copy_face(copy_y, sides["y_src"], sides["y_dst"])

    mesh.feature().create("ftet1", "FreeTet")
    mesh.run()
    return mesh, int(mesh.getNumElem())


def _build_model(client: Any) -> tuple[Any, dict[str, list[int]]]:
    """Create the two-block periodic cell and its wave-optics interface.

    Returns the model and the measured periodic side pairs, so the caller can
    verify the periodic evidence against the same classification the mesh was
    built from instead of re-deriving (and possibly re-guessing) it.
    """
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
    # The incidence probe requires exactly one PeriodicStructure. COMSOL creates
    # the two PeriodicPort children, the two Floquet periodic conditions and the
    # reference direction automatically; measured on COMSOL 6.4, so the builder
    # verifies them rather than fabricating them.
    physics.feature().create("ps1", "PeriodicStructure", 3)

    sides = _periodic_side_pairs(
        _boundary_normals(geometry), [float(value) for value in geometry.getBoundingBox()]
    )
    _build_periodic_mesh(component, sides)

    study = java.study().create(STUDY_TAG)
    # `wl_step` is the repository-wide convention for a Wavelength study step
    # (see the documented `study_step_tag` example in tools/workflow.py). The
    # live-profile gate addresses the step by that tag, so the fixture must use
    # it too rather than a private name.
    study.create(STUDY_STEP_TAG, "Wavelength")
    java.param().set("wl", f"{FIXTURE_WAVELENGTH_UM}[um]")

    return model, sides


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


def _verify_periodic_evidence(component: Any, sides: dict[str, list[int]]) -> dict[str, Any]:
    """Confirm the periodic features each licensed audit consumes actually exist.

    ``incidence_configuration_acceptance`` requires exactly two PeriodicPort
    children and one non-empty reference-direction edge selection;
    ``periodic_mesh_acceptance`` requires Floquet selections whose opposing
    normal groups are balanced. COMSOL creates all of these automatically when
    the PeriodicStructure is created -- measured on 6.4 -- so this verifies them
    instead of creating duplicates, and fails loudly if a future COMSOL release
    stops providing them.
    """
    periodic_structure = component.physics("ewfd").feature("ps1")
    children = {
        str(tag): str(periodic_structure.feature(tag).getType())
        for tag in list(periodic_structure.feature().tags())
    }
    ports = sorted(tag for tag, kind in children.items() if "periodicport" in kind.casefold())
    floquet = sorted(tag for tag, kind in children.items() if "floquetperiodic" in kind.casefold())
    references = sorted(
        tag for tag, kind in children.items() if "referencedirection" in kind.casefold()
    )
    if len(ports) != 2:
        raise ValueError(f"exactly two PeriodicPort children are required, found {ports}")
    if len(references) != 1:
        raise ValueError(f"exactly one ReferenceDirection child is required, found {references}")
    if not floquet:
        raise ValueError("no Floquet periodic condition was created")

    reference_edges = sorted(
        int(value) for value in periodic_structure.feature(references[0]).selection().entities()
    )
    if not reference_edges:
        raise ValueError("the reference-direction edge selection is empty")

    groups = {}
    for tag in floquet:
        selection = sorted(
            int(value) for value in periodic_structure.feature(tag).selection().entities()
        )
        if not selection:
            raise ValueError(f"Floquet feature {tag} has an empty boundary selection")
        groups[tag] = selection

    copied = {key: sides[key] for key in ("x_src", "x_dst", "y_src", "y_dst")}
    for key, value in copied.items():
        if not value:
            raise ValueError(f"periodic side pair {key} is empty")
    return {
        "periodic_ports": ports,
        "floquet_groups": groups,
        "reference_direction": {"tag": references[0], "edges": reference_edges},
        "copy_face_side_pairs": copied,
    }


def _verify_solve(model: Any) -> dict[str, Any]:
    """Confirm the built model solved and exposes the R/T/A expressions.

    A fixture that claims to be solvable must prove it here. The values are not
    a physical result -- the layer is a lossless dielectric placeholder -- so
    they are recorded only as an internal consistency check.
    """
    solutions = [str(tag) for tag in list(model.java.sol().tags())]
    datasets = [str(tag) for tag in list(model.java.result().dataset().tags())]
    if not solutions:
        raise ValueError("the wavelength study produced no solution")
    if not datasets:
        raise ValueError("the wavelength study produced no dataset")

    power: dict[str, float] = {}
    for expression in ("ewfd.Rtotal", "ewfd.Ttotal", "ewfd.Atotal"):
        try:
            power[expression] = float(model.evaluate(expression))
        except Exception as exc:
            raise ValueError(f"flux expression {expression} is not evaluable: {exc}") from exc
    return {
        "solutions": solutions,
        "datasets": datasets,
        "flux_self_consistency": power,
        "note": (
            "flux values are a numerical self-consistency check on a lossless "
            "dielectric placeholder, not a physical reflectance of any real structure"
        ),
    }


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
        model, sides = _build_model(client)

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
        mesh_elements = int(component.mesh(MESH_TAG).getNumElem())

        # Verify the periodic evidence the licensed audits read, rather than
        # assuming COMSOL created it. Each check names the exact consumer.
        periodic = _verify_periodic_evidence(component, sides)

        # The point-audit gate requires a model that actually solves at one
        # wavelength and exposes R/T/A, so the fixture is solved here. This is
        # still not a physical claim: the layer is a lossless placeholder.
        model.java.study(STUDY_TAG).run()
        solve_evidence = _verify_solve(model)

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
            periodic=periodic,
            solve=solve_evidence,
            solved=True,
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
