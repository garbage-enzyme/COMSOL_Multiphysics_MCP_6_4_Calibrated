"""No-solve COMSOL 6.4 round-trip gate for constrained property access."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import mph

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.properties import get_existing_property, set_existing_property


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _verify_runtime_release(client) -> dict[str, object]:
    mph_version = str(client.version)
    java_version = str(client.java.getComsolVersion())
    numbers = tuple(int(value) for value in re.findall(r"\d+", java_version)[:4])
    _require(
        mph_version.startswith("6.4"), f"MPh selected unexpected COMSOL release: {mph_version}"
    )
    _require(
        numbers == (6, 4, 0, 293),
        f"connected COMSOL runtime is not 6.4.0.293: {java_version}",
    )
    return {
        "mph_client_version": mph_version,
        "java_reported_version": java_version,
        "expected_build": "6.4.0.293",
        "verified": True,
    }


def _solution_tags(java_model) -> list[str]:
    return sorted(str(tag) for tag in java_model.sol().tags())


def _round_trip_case(
    model,
    container: str,
    feature_tag: str,
    property_name: str,
    temporary_value: str,
) -> dict[str, object]:
    before = get_existing_property(model, "comp1", container, feature_tag, property_name)
    _require(bool(before.get("success")), f"property read failed: {before}")
    changed = set_existing_property(
        model,
        "comp1",
        container,
        feature_tag,
        property_name,
        temporary_value,
    )
    _require(bool(changed.get("success")), f"property update failed: {changed}")
    temporary = None
    try:
        temporary = get_existing_property(model, "comp1", container, feature_tag, property_name)
        _require(
            bool(temporary.get("success")),
            f"temporary property readback failed: {temporary}",
        )
        _require(
            temporary.get("value") == changed.get("new_value"),
            f"temporary property readback mismatch: changed={changed}, readback={temporary}",
        )
    finally:
        restored = set_existing_property(
            model,
            "comp1",
            container,
            feature_tag,
            property_name,
            before["value"],
        )
        _require(bool(restored.get("success")), f"property restore failed: {restored}")
        final = get_existing_property(model, "comp1", container, feature_tag, property_name)
        _require(bool(final.get("success")), f"property readback failed: {final}")
        _require(
            final.get("value") == before.get("value"),
            f"property restoration mismatch: before={before}, final={final}",
        )
    return {
        "container": container,
        "feature_tag": feature_tag,
        "property": property_name,
        "before": before["value"],
        "temporary": temporary["value"],
        "restored": final["value"],
    }


def main() -> None:
    artifact_dir = (
        Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime")) / "clientapi property"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_path = artifact_dir / "property_gate_source.mph"
    manifest_path = artifact_dir / "property_gate_result.json"

    client = mph.Client(cores=1)
    runtime_release = _verify_runtime_release(client)
    model = client.create("ClientapiPropertyGate")
    jm = model.java
    component = jm.component().create("comp1", True)

    geometry = component.geom().create("geom1", 3)
    block = geometry.feature().create("blk1", "Block")
    block.set("base", "corner")
    block.set("size", ["1", "1", "1"])
    geometry.run()

    physics = component.physics().create("es", "Electrostatics", "3")
    potential = physics.feature().create("ep1", "ElectricPotential", 2)
    potential.set("V0", "1[V]")

    mesh = component.mesh().create("mesh1")
    size = mesh.feature().create("size1", "Size")
    size.set("custom", "off")

    study = jm.study().create("std1")
    wavelength = study.create("step1", "Wavelength")
    wavelength.set("plist", "1[um]")
    solution_tags_before = _solution_tags(jm)

    jm.save(str(source_path))
    source_hash_before = _sha256(source_path)

    cases = (
        ("geometry_feature", "geom1/blk1", "base", "center"),
        ("physics_feature", "es/ep1", "V0", "2[V]"),
        ("mesh_feature", "mesh1/size1", "custom", "on"),
        ("study_step", "std1/step1", "plist", "2[um]"),
    )
    results = []
    for container, feature_tag, property_name, temporary_value in cases:
        results.append(
            _round_trip_case(
                model,
                container,
                feature_tag,
                property_name,
                temporary_value,
            )
        )

    solution_tags_after = _solution_tags(jm)
    _require(
        solution_tags_after == solution_tags_before == [],
        "property acceptance unexpectedly created or changed solution state",
    )

    source_hash_after = _sha256(source_path)
    _require(
        source_hash_after == source_hash_before,
        "source model changed during property acceptance",
    )
    result = {
        "success": True,
        "solve_ran": bool(solution_tags_after or solution_tags_after != solution_tags_before),
        "solve_state_evidence": {
            "solution_tags_before": solution_tags_before,
            "solution_tags_after": solution_tags_after,
            "unchanged": solution_tags_after == solution_tags_before,
        },
        "runtime_release": runtime_release,
        "client": {"standalone": client.port is None, "cores": 1},
        "source_path": str(source_path),
        "source_sha256_before": source_hash_before,
        "source_sha256_after": source_hash_after,
        "round_trips": results,
    }
    manifest_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    client.clear()
    print(json.dumps(result, ensure_ascii=False), flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
