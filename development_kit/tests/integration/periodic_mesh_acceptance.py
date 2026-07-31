"""Controlled real-COMSOL periodic-mesh audit and clone-smoke gate."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.durable.io import atomic_write_json_exclusive
from src.evidence.real_fixture import controlled_fixture_from_environment
from src.tools.ownership import SolverOwnership
from src.tools.periodic_mesh_audit import (
    collect_periodic_mesh_audit,
    run_clone_mesh_smoke,
)


def _owned_model_path(artifact_dir: Path, *, lease_acquired: bool) -> Path:
    if lease_acquired is not True:
        raise RuntimeError("periodic-mesh artifact allocation requires solver lease ownership")
    return artifact_dir / f"derived_missing_copyface-{uuid.uuid4().hex}.mph"


def _result_path(artifact_dir: Path) -> Path:
    return artifact_dir / f"periodic_mesh_gate_result-{uuid.uuid4().hex}.json"


def _cleanup_periodic_session(
    client,
    models: dict[str, object | None],
    broken_path: Path | None,
) -> dict[str, object]:
    cleanup: dict[str, object] = {
        "model_removals": {},
        "client_clear": client is None,
        "errors": [],
    }
    removals = cleanup["model_removals"]
    errors = cleanup["errors"]
    if client is not None:
        for label, model in models.items():
            if model is not None:
                try:
                    client.remove(model)
                    removals[label] = True
                except Exception as exc:
                    removals[label] = False
                    errors.append({"stage": f"remove_{label}", "type": type(exc).__name__})
        try:
            client.clear()
            cleanup["client_clear"] = True
        except Exception as exc:
            cleanup["client_clear"] = False
            errors.append({"stage": "client_clear", "type": type(exc).__name__})
    if broken_path is not None:
        try:
            broken_path.unlink(missing_ok=True)
        except OSError as exc:
            errors.append({"stage": "derived_unlink", "type": type(exc).__name__})
    cleanup["derived_file_removed"] = broken_path is None or not broken_path.exists()
    cleanup["passed"] = bool(
        cleanup["client_clear"]
        and all(removals.values())
        and cleanup["derived_file_removed"]
        and not errors
    )
    return cleanup


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


def _negative_copyface_recipe(audit: dict) -> tuple[Any, str]:
    recipes = audit.get("group_recipes")
    if not isinstance(recipes, list) or not recipes:
        raise AssertionError("compatible audit exposes no periodic mesh recipe")
    candidates = []
    for recipe in recipes:
        if not isinstance(recipe, dict) or recipe.get("mesh_recipe_present") is not True:
            raise AssertionError("compatible audit contains an incomplete periodic mesh recipe")
        tag = recipe.get("copy_face_tag")
        matching = recipe.get("matching_copyface_tags")
        if not isinstance(tag, str) or not tag or matching != [tag]:
            raise AssertionError("periodic group does not identify exactly one CopyFace feature")
        candidates.append((recipe.get("group_id"), tag))
    tags = [tag for _group_id, tag in candidates]
    if len(set(tags)) != len(tags):
        raise AssertionError("CopyFace feature identity is shared across periodic groups")
    return sorted(
        candidates,
        key=lambda item: json.dumps(item[0], sort_keys=True, separators=(",", ":")),
    )[0]


def main() -> None:
    runtime = Path(os.environ.get("COMSOL_MCP_RUNTIME_DIR", "D:/comsol_runtime"))
    artifact_dir = runtime / "periodic_mesh"
    result_path = None
    broken_path = None
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
        artifact_dir.mkdir(parents=True, exist_ok=True)
        result_path = _result_path(artifact_dir)
        broken_path = _owned_model_path(artifact_dir, lease_acquired=True)
        import mph

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
            raise AssertionError(
                f"compatible source geometry gate failed: {audit['actionable_mismatches']}"
            )
        if not audit["summary"]["mesh_recipe_present"]:
            raise AssertionError(
                f"compatible source recipe gate failed: {audit['actionable_mismatches']}"
            )
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
        target_group_id, copy_face_tag = _negative_copyface_recipe(audit)
        broken_mesh.feature().remove(copy_face_tag)
        broken_audit = collect_periodic_mesh_audit(
            broken_model,
            model_name=broken_model.name(),
            expected_source_path=str(broken_path),
            expected_source_sha256=broken_hash,
            **common,
        )
        if broken_audit["summary"]["mesh_recipe_present"]:
            raise AssertionError("derived missing-CopyFace model was not rejected")
        target_mismatches = next(
            (
                item["mismatches"]
                for item in broken_audit["actionable_mismatches"]
                if item.get("group_id") == target_group_id
            ),
            [],
        )
        if "add_matching_copyface_source_destination" not in target_mismatches:
            raise AssertionError(
                f"selected periodic group did not report the removed CopyFace: {target_mismatches}"
            )

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
                "target_group_id": target_group_id,
                "removed_copyface_tag": copy_face_tag,
                "summary": broken_audit["summary"],
                "actionable_mismatches": broken_audit["actionable_mismatches"],
            },
        )
        exit_code = 0
    except Exception as exc:
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc(limit=10)
    finally:
        cleanup = _cleanup_periodic_session(
            client,
            {"broken": broken_model, "source": source_model},
            broken_path,
        )
        result["derived_cleanup"] = cleanup["derived_file_removed"]
        result["lease_release"] = owner.release()
        cleanup["passed"] = bool(cleanup["passed"] and result["lease_release"].get("success"))
        result["cleanup"] = cleanup
        result["success"] = bool(result.get("success") and cleanup["passed"])
        exit_code = 0 if result["success"] else 1
        artifact_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_path or _result_path(artifact_dir)
        result["receipt_name"] = result_path.name
        atomic_write_json_exclusive(result_path, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        os._exit(exit_code)


if __name__ == "__main__":
    main()
