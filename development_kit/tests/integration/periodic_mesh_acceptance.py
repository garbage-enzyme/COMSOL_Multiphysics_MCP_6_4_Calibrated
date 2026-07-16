"""Controlled real-COMSOL periodic-mesh audit and clone-smoke gate."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

import mph

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.tools.ownership import SolverOwnership
from src.tools.periodic_mesh_audit import (
    collect_periodic_mesh_audit,
    run_clone_mesh_smoke,
)
from src.evidence.real_fixture import controlled_fixture_from_environment


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _first_tag(tags, preferred):
    values = [str(value) for value in list(tags)]
    if not values:
        return None
    return preferred if preferred in values else values[0]


def _copyface_tags(mesh) -> list[str]:
    result = []
    for tag in [str(value) for value in list(mesh.feature().tags())]:
        feature = mesh.feature().get(tag)
        kind = None
        for name in ("getType", "type"):
            try:
                kind = str(getattr(feature, name)())
                break
            except Exception:
                continue
        label = str(feature.label()) if hasattr(feature, "label") else ""
        if "copyface" in f"{tag} {kind} {label}".replace(" ", "").lower():
            result.append(tag)
    return result


def main() -> None:
    runtime = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
    artifact_dir = runtime / "periodic_mesh"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result_path = artifact_dir / "periodic_mesh_gate_result.json"
    broken_path = artifact_dir / "derived_missing_copyface.mph"
    owner = SolverOwnership(owner="periodic-mesh-gate")
    client = None
    source_model = None
    broken_model = None
    result = {"success": False, "solve_ran": False}
    exit_code = 1
    try:
        source_path = controlled_fixture_from_environment()["source"]
        source_hash = _sha256(source_path)
        source_stat = source_path.stat()
        claim = owner.acquire(mode="periodic_mesh_audit", model_path=str(source_path))
        if not claim.get("acquired"):
            raise RuntimeError(f"solver lease unavailable: {claim}")
        client = mph.Client(cores=1)
        source_model = client.load(str(source_path))
        component_tag = _first_tag(source_model.java.component().tags(), "comp1")
        if component_tag is None:
            raise AssertionError("real model exposes no component")
        component = source_model.java.component().get(component_tag)
        physics_tag = _first_tag(component.physics().tags(), "ewfd")
        if physics_tag is None:
            raise AssertionError("real model exposes no physics")
        study_tag = _first_tag(source_model.java.study().tags(), "std1")
        mesh_tag = _first_tag(component.mesh().tags(), "mesh1")
        if mesh_tag is None:
            raise AssertionError("real model exposes no mesh")
        common = {
            "session_state": {"connected": True},
            "active_profile": "wave_optics",
            "expected_component_tag": component_tag,
            "expected_physics_tag": physics_tag,
            "expected_study_tag": study_tag,
            "expected_mesh_tag": mesh_tag,
        }
        audit = collect_periodic_mesh_audit(
            source_model,
            model_name=source_model.name(),
            expected_source_path=str(source_path),
            expected_source_sha256=source_hash,
            **common,
        )
        result["compatible_audit_probe"] = {
            "summary": audit["summary"],
            "groups": audit["periodic_groups"],
            "mesh_sequence": audit["mesh_sequence"],
            "recipes": audit["group_recipes"],
            "actionable_mismatches": audit["actionable_mismatches"],
        }
        if not audit["summary"]["geometry_consistent"]:
            raise AssertionError(f"compatible source geometry gate failed: {audit['actionable_mismatches']}")
        if not audit["summary"]["mesh_recipe_present"]:
            raise AssertionError(f"compatible source recipe gate failed: {audit['actionable_mismatches']}")
        if audit["summary"]["compatibility_assessment"] != "compatibility_unproven":
            raise AssertionError("read-only audit overclaimed compatibility")

        smoke = run_clone_mesh_smoke(
            source_model,
            client,
            expected_source_sha256=source_hash,
            expected_component_tag=component_tag,
            expected_mesh_tag=mesh_tag,
            runtime_dir=artifact_dir,
        )
        if not smoke["success"]:
            raise AssertionError(f"clone mesh smoke failed: {smoke}")

        source_model.java.save(str(broken_path), True)
        broken_hash = _sha256(broken_path)
        broken_model = client.load(str(broken_path))
        broken_component = broken_model.java.component().get(component_tag)
        broken_mesh = broken_component.mesh().get(mesh_tag)
        copy_tags = _copyface_tags(broken_mesh)
        if not copy_tags:
            raise AssertionError("real model exposes no CopyFace feature")
        broken_mesh.feature().remove(copy_tags[-1])
        broken_audit = collect_periodic_mesh_audit(
            broken_model,
            model_name=broken_model.name(),
            expected_source_path=str(broken_path),
            expected_source_sha256=broken_hash,
            **common,
        )
        if broken_audit["summary"]["mesh_recipe_present"]:
            raise AssertionError("derived missing-CopyFace model was not rejected")
        mismatches = [
            mismatch
            for item in broken_audit["actionable_mismatches"]
            for mismatch in item["mismatches"]
        ]
        if "add_matching_copyface_source_destination" not in mismatches:
            raise AssertionError(f"smallest mismatch was not reported: {mismatches}")

        final_stat = source_path.stat()
        source_unchanged = (
            _sha256(source_path) == source_hash
            and final_stat.st_mtime_ns == source_stat.st_mtime_ns
            and final_stat.st_size == source_stat.st_size
        )
        if not source_unchanged:
            raise AssertionError("immutable source changed")
        result.update(
            success=True,
            source_sha256=source_hash,
            source_unchanged=True,
            compatible_audit={
                "summary": audit["summary"],
                "groups": audit["periodic_groups"],
                "recipes": audit["group_recipes"],
            },
            clone_smoke=smoke,
            incompatible_probe={
                "removed_copyface_tag": copy_tags[-1],
                "summary": broken_audit["summary"],
                "actionable_mismatches": broken_audit["actionable_mismatches"],
            },
        )
        exit_code = 0
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc(limit=10)
    finally:
        if client is not None:
            for model in (broken_model, source_model):
                if model is not None:
                    try:
                        client.remove(model)
                    except Exception:
                        pass
            try:
                client.clear()
            except Exception:
                pass
        broken_path.unlink(missing_ok=True)
        result["derived_cleanup"] = not broken_path.exists()
        result["lease_release"] = owner.release()
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(exit_code)


if __name__ == "__main__":
    main()
