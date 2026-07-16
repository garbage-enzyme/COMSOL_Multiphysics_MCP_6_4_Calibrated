"""Controlled COMSOL 6.4 gate for typed derived-geometry edits."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import jpype
import mph

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.derived_geometry import (
    _snapshot,
    _state_hash,
    apply_blocks,
    apply_fin,
    create_derived_geometry_clone,
    preview_blocks,
    preview_fin,
)
from src.tools.ownership import SolverOwnership


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strings(values):
    return jpype.JArray(jpype.JString)(values)


def main() -> None:
    runtime = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
    artifact_dir = runtime / "derived_geometry"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_path = artifact_dir / "derived_geometry_source.mph"
    result_path = artifact_dir / "derived_geometry_gate_result.json"
    owner = SolverOwnership(owner="derived-geometry-gate")
    client = None
    source = None
    clone = None
    record = None
    result = {"success": False, "solve_ran": False}
    exit_code = 1
    try:
        claim = owner.acquire(mode="derived_geometry", model_path=str(source_path))
        if not claim.get("acquired"):
            raise RuntimeError(f"solver lease unavailable: {claim}")
        client = mph.Client(cores=1)
        source = client.create("DerivedGeometrySource")
        jm = source.java
        component = jm.component().create("comp1", True)
        geom = component.geom().create("geom1", 3)
        blk1 = geom.feature().create("blk1", "Block")
        blk1.set("size", _strings(["1[mm]", "1[mm]", "1[mm]"]))
        blk1.set("pos", _strings(["0[mm]", "0[mm]", "0[mm]"]))
        blk2 = geom.feature().create("blk2", "Block")
        blk2.set("size", _strings(["1[mm]", "1[mm]", "1[mm]"]))
        blk2.set("pos", _strings(["0[mm]", "0[mm]", "1[mm]"]))
        geom.run()
        mesh = component.mesh().create("mesh1")
        mesh.feature().create("ftet1", "FreeTet")
        mesh.run()
        jm.save(str(source_path))
        source_hash = _sha256(source_path)
        source_stat = source_path.stat()

        clone, record = create_derived_geometry_clone(
            source, client, new_name="DerivedGeometryClone", runtime_dir=artifact_dir
        )
        initial = _snapshot(clone, "comp1", "geom1")
        initial_hash = _state_hash(record, initial)
        fin_preview = preview_fin(
            clone, record,
            expected_state_sha256=initial_hash,
            component_tag="comp1", geometry_tag="geom1",
            action="assembly", imprint=True, create_pairs=False,
        )
        fin_result = apply_fin(clone, record, fin_preview, "comp1", "geom1")
        if not fin_result.get("success"):
            raise AssertionError(f"fin apply failed: {fin_result}")

        block_edits = [{
            "block_tag": "blk1",
            "size": ["1.2[mm]", "1[mm]", "1[mm]"],
            "pos": ["-0.1[mm]", "0[mm]", "0[mm]"],
        }]
        block_preview = preview_blocks(
            clone, record,
            expected_state_sha256=fin_result["post_state_sha256"],
            component_tag="comp1", geometry_tag="geom1",
            block_edits=block_edits,
        )
        block_result = apply_blocks(clone, record, block_preview, "comp1", "geom1")
        if not block_result.get("success"):
            raise AssertionError(f"block apply failed: {block_result}")
        if block_result["geometry_run"] or block_result["mesh_run"]:
            raise AssertionError("block apply ran geometry or mesh implicitly")

        clone_geom = clone.java.component().get("comp1").geom().get("geom1")
        clone_mesh = clone.java.component().get("comp1").mesh().get("mesh1")
        clone_geom.run()
        clone_mesh.run()
        counts = {
            "domains": int(clone_geom.getNDomains()),
            "boundaries": int(clone_geom.getNBoundaries()),
            "elements": int(clone_mesh.getNumElem()),
            "vertices": int(clone_mesh.getNumVertex()),
        }
        final_stat = source_path.stat()
        source_unchanged = (
            _sha256(source_path) == source_hash
            and final_stat.st_mtime_ns == source_stat.st_mtime_ns
            and final_stat.st_size == source_stat.st_size
        )
        if not source_unchanged or counts["elements"] <= 0:
            raise AssertionError("source integrity or explicit mesh rebuild gate failed")
        result.update(
            success=True,
            source_sha256=source_hash,
            source_unchanged=True,
            derived_model_id=record.derived_model_id,
            derived_backing_sha256=record.backing_sha256,
            fin=fin_result,
            blocks=block_result,
            explicit_post_edit_build=counts,
        )
        exit_code = 0
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc(limit=10)
    finally:
        if client is not None:
            for model in (clone, source):
                if model is not None:
                    try:
                        client.remove(model)
                    except Exception:
                        pass
            try:
                client.clear()
            except Exception:
                pass
        if record is not None:
            backing = Path(record.backing_path)
            backing.unlink(missing_ok=True)
            try:
                backing.parent.rmdir()
            except OSError:
                pass
            result["derived_cleanup"] = not backing.exists() and not backing.parent.exists()
        result["lease_release"] = owner.release()
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(exit_code)


if __name__ == "__main__":
    main()
