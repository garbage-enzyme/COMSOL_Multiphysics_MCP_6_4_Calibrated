"""No-solve COMSOL 6.4 round-trip gate for constrained property access."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import mph

ROOT = Path(__file__).parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.properties import get_existing_property, set_existing_property


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    artifact_dir = Path(
        os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime")
    ) / "H3c"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_path = artifact_dir / "property_gate_source.mph"
    manifest_path = artifact_dir / "property_gate_result.json"

    client = mph.Client(cores=1)
    model = client.create("H3cPropertyGate")
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
        before = get_existing_property(
            model, "comp1", container, feature_tag, property_name
        )
        assert before["success"], before
        changed = set_existing_property(
            model,
            "comp1",
            container,
            feature_tag,
            property_name,
            temporary_value,
        )
        assert changed["success"], changed
        restored = set_existing_property(
            model,
            "comp1",
            container,
            feature_tag,
            property_name,
            before["value"],
        )
        assert restored["success"], restored
        final = get_existing_property(
            model, "comp1", container, feature_tag, property_name
        )
        assert final["success"], final
        assert final["value"] == before["value"], (before, final)
        results.append({
            "container": container,
            "feature_tag": feature_tag,
            "property": property_name,
            "before": before["value"],
            "temporary": changed["new_value"],
            "restored": final["value"],
        })

    source_hash_after = _sha256(source_path)
    assert source_hash_after == source_hash_before
    result = {
        "success": True,
        "solve_ran": False,
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
