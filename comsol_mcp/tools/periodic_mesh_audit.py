"""Bounded periodic geometry/mesh evidence and clone-only native mesh smoke."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import tempfile
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from .ownership import ownership_manager
from .session import session_manager
from .wave_optics_preflight import (
    EvidenceLedger,
    _error,
    _feature_inventory,
    _feature_type,
    _get,
    _label,
    _selection_entities,
    _tags,
    collect_wave_optics_preflight,
)


MAX_MESH_FEATURES = 256
NORMAL_DOT_TOLERANCE = 1e-8


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mean(points: list[list[float]]) -> list[float] | None:
    if not points:
        return None
    return [sum(point[axis] for point in points) / len(points) for axis in range(len(points[0]))]


def _periodic_groups(preflight: dict[str, Any]) -> list[dict[str, Any]]:
    boundary_map = {
        int(item["boundary"]): item
        for item in preflight.get("topology", {}).get("boundaries", [])
        if "boundary" in item
    }
    groups: list[dict[str, Any]] = []
    for feature in preflight.get("periodicity", {}).get("floquet_features", []):
        selection = [int(value) for value in feature.get("selection") or []]
        first_normal = next(
            (
                boundary_map[number].get("normal")
                for number in selection
                if boundary_map.get(number, {}).get("normal")
            ),
            None,
        )
        side_a: list[int] = []
        side_b: list[int] = []
        ambiguous: list[int] = []
        if first_normal is None:
            ambiguous = selection[:]
        else:
            for number in selection:
                normal = boundary_map.get(number, {}).get("normal")
                if not normal or len(normal) != len(first_normal):
                    ambiguous.append(number)
                    continue
                dot = sum(float(a) * float(b) for a, b in zip(first_normal, normal))
                if dot > NORMAL_DOT_TOLERANCE:
                    side_a.append(number)
                elif dot < -NORMAL_DOT_TOLERANCE:
                    side_b.append(number)
                else:
                    ambiguous.append(number)
        internal = [
            number for number in selection if boundary_map.get(number, {}).get("interior") is True
        ]
        centers_a = [boundary_map[number]["center"] for number in side_a if boundary_map.get(number, {}).get("center")]
        centers_b = [boundary_map[number]["center"] for number in side_b if boundary_map.get(number, {}).get("center")]
        mean_a = _mean(centers_a)
        mean_b = _mean(centers_b)
        translation = (
            [float(b) - float(a) for a, b in zip(mean_a, mean_b)]
            if mean_a is not None and mean_b is not None
            else None
        )
        finite_translation = translation is not None and all(math.isfinite(value) for value in translation)
        opposing = feature.get("opposing_face_groups") or {}
        geometry_consistent = bool(
            side_a
            and len(side_a) == len(side_b)
            and not ambiguous
            and not internal
            and finite_translation
        )
        groups.append(
            {
                "group_id": str(feature.get("tag")),
                "physics_feature_type": feature.get("type"),
                "selection": selection,
                "source_candidate": side_a,
                "destination_candidate": side_b,
                "source_destination_orientation": "not_inferred_from_floquet_selection",
                "cardinality": {
                    "source_candidate": len(side_a),
                    "destination_candidate": len(side_b),
                    "balanced": len(side_a) == len(side_b) and bool(side_a),
                },
                "inferred_translation": translation,
                "ambiguous_faces": ambiguous,
                "internal_faces": internal,
                "adjacent_domain_id_signatures_match": opposing.get(
                    "adjacent_domain_signatures_match"
                ),
                "geometry_consistent": geometry_consistent,
                "limitation": (
                    "Normals, centers, cardinality, and adjacency are topology evidence only; "
                    "they do not prove a node-to-node mesh mapping. Numeric domain-ID "
                    "signatures are diagnostic and are not required to match across a "
                    "partitioned or oblique translation."
                ),
            }
        )
    return groups


def _mesh_sequence(component: Any, expected_mesh_tag: str | None) -> tuple[dict[str, Any], Any | None]:
    container = component.mesh()
    tags = _tags(container)
    if expected_mesh_tag is not None:
        if expected_mesh_tag not in tags:
            raise ValueError(
                f"expected mesh tag {expected_mesh_tag!r} does not exist; available={tags}"
            )
        mesh_tag = expected_mesh_tag
    elif "mesh1" in tags:
        mesh_tag = "mesh1"
    elif len(tags) == 1:
        mesh_tag = tags[0]
    else:
        return {"mesh_tags": tags, "selection_status": "ambiguous"}, None
    mesh = _get(container, mesh_tag)
    features: list[dict[str, Any]] = []
    feature_container = mesh.feature()
    for index, (tag, feature, kind) in enumerate(_feature_inventory(feature_container)):
        if index >= MAX_MESH_FEATURES:
            break
        default, default_error = _selection_entities(feature)
        source, source_error = _selection_entities(feature, "source")
        destination, destination_error = _selection_entities(feature, "destination")
        item = {
            "index": index,
            "tag": tag,
            "type": kind,
            "label": _label(feature),
            "selection": default,
            "source": source,
            "destination": destination,
        }
        errors = {
            name: value
            for name, value in (
                ("selection", default_error),
                ("source", source_error),
                ("destination", destination_error),
            )
            if value
        }
        if errors:
            item["selection_errors"] = errors
        features.append(item)
    try:
        element_count = int(mesh.getNumElem())
        vertex_count = int(mesh.getNumVertex())
    except Exception as exc:
        element_count = None
        vertex_count = None
        count_error = _error(exc)
    else:
        count_error = None
    return (
        {
            "mesh_tag": mesh_tag,
            "mesh_tags": tags,
            "features_in_execution_order": features,
            "feature_count_truncated": len(features) >= MAX_MESH_FEATURES,
            "element_count": element_count,
            "vertex_count": vertex_count,
            "count_error": count_error,
            "built_mesh_observed": bool(element_count and vertex_count),
        },
        mesh,
    )


def _kind(item: dict[str, Any], *needles: str) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("tag", "type", "label")).casefold()
    return any(needle.casefold() in text for needle in needles)


def _recipe_for_group(group: dict[str, Any], features: list[dict[str, Any]]) -> dict[str, Any]:
    side_a = set(group["source_candidate"])
    side_b = set(group["destination_candidate"])
    copies = []
    for item in features:
        if not _kind(item, "copyface", "copy face"):
            continue
        source = set(item.get("source") or [])
        destination = set(item.get("destination") or [])
        if (source == side_a and destination == side_b) or (
            source == side_b and destination == side_a
        ):
            copies.append(item)
    copy = copies[0] if len(copies) == 1 else None
    free_tri = None
    free_tet = None
    if copy is not None:
        copy_source = set(copy.get("source") or [])
        prior_triangles = [
            item
            for item in features
            if item["index"] < copy["index"]
            and _kind(item, "freetri", "free tri")
            and copy_source <= set(item.get("selection") or [])
        ]
        later_tetrahedra = [
            item
            for item in features
            if item["index"] > copy["index"] and _kind(item, "freetet", "free tet")
        ]
        free_tri = prior_triangles[-1] if prior_triangles else None
        free_tet = later_tetrahedra[0] if later_tetrahedra else None
    recipe_present = bool(copy and free_tri and free_tet)
    actionable = []
    if not group["geometry_consistent"]:
        actionable.append("repair_periodic_geometry_group_before_meshing")
    if not copies:
        actionable.append("add_matching_copyface_source_destination")
    elif len(copies) > 1:
        actionable.append("resolve_multiple_matching_copyface_features")
    elif free_tri is None:
        actionable.append("place_freetri_for_copyface_source_before_copyface")
    elif free_tet is None:
        actionable.append("place_freetet_after_copyface")
    return {
        "group_id": group["group_id"],
        "matching_copyface_tags": [item["tag"] for item in copies],
        "free_tri_tag": free_tri["tag"] if free_tri else None,
        "copy_face_tag": copy["tag"] if copy else None,
        "free_tet_tag": free_tet["tag"] if free_tet else None,
        "mesh_recipe_present": recipe_present,
        "order_verified": recipe_present,
        "order_ambiguous": bool(copies and not recipe_present),
        "actionable_mismatches": actionable,
    }


def collect_periodic_mesh_audit(
    model: Any,
    *,
    model_name: str,
    session_state: dict[str, Any],
    active_profile: str,
    expected_source_path: str | None = None,
    expected_source_sha256: str | None = None,
    expected_component_tag: str | None = None,
    expected_physics_tag: str | None = None,
    expected_study_tag: str | None = None,
    expected_mesh_tag: str | None = None,
) -> dict[str, Any]:
    """Inspect periodic topology and the mesh recipe without running the mesh."""
    preflight = collect_wave_optics_preflight(
        model,
        model_name=model_name,
        session_state=session_state,
        active_profile=active_profile,
        expected_source_path=expected_source_path,
        expected_source_sha256=expected_source_sha256,
        expected_component_tag=expected_component_tag,
        expected_physics_tag=expected_physics_tag,
        expected_study_tag=expected_study_tag,
    )
    groups = _periodic_groups(preflight)
    mesh_sequence: dict[str, Any] = {}
    recipes: list[dict[str, Any]] = []
    if preflight["inspection_status"] != "integrity_blocked":
        component_tag = preflight.get("topology", {}).get("component_tag")
        if component_tag:
            component = _get(model.java.component(), component_tag)
            mesh_sequence, _ = _mesh_sequence(component, expected_mesh_tag)
            features = mesh_sequence.get("features_in_execution_order", [])
            recipes = [_recipe_for_group(group, features) for group in groups]
    all_geometry = bool(groups) and all(group["geometry_consistent"] for group in groups)
    all_recipes = bool(recipes) and all(item["mesh_recipe_present"] for item in recipes)
    actionable = [
        {"group_id": item["group_id"], "mismatches": item["actionable_mismatches"]}
        for item in recipes
        if item["actionable_mismatches"]
    ]
    if not groups:
        actionable.append({"group_id": None, "mismatches": ["declare_unambiguous_periodic_groups"]})
    return {
        "schema_name": "comsol_mcp.periodic_mesh_audit",
        "schema_version": "1.0.0",
        "assessment_mode": "evidence_only",
        "source": preflight["provenance"],
        "inspection_status": preflight["inspection_status"],
        "periodic_port_faces": [
            {"tag": item.get("tag"), "selection": item.get("selection")}
            for item in preflight.get("ports", {}).get("periodic_port_features", [])
        ],
        "periodic_groups": groups,
        "mesh_sequence": mesh_sequence,
        "group_recipes": recipes,
        "summary": {
            "geometry_consistent": all_geometry,
            "mesh_recipe_present": all_recipes,
            "built_mesh_observed": bool(mesh_sequence.get("built_mesh_observed")),
            "compatibility_assessment": "compatibility_unproven",
            "node_by_node_mesh_equality": "not_evaluated",
        },
        "actionable_mismatches": actionable,
        "limitations": [
            "Read-only inspection does not run geometry or mesh features.",
            "A present recipe or previously built mesh does not prove current node-to-node equality.",
            "Use the explicit clone-only native mesh smoke for native build evidence.",
        ],
        "preflight_evidence": preflight["evidence"],
    }


def run_clone_mesh_smoke(
    model: Any,
    client: Any,
    *,
    expected_source_sha256: str,
    expected_component_tag: str | None = None,
    expected_mesh_tag: str | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any]:
    """Save-copy, load, and build one cloned mesh, then clean it deterministically."""
    source = Path(str(model.file())).resolve()
    if not source.is_file():
        raise ValueError("loaded model source file is unavailable")
    before_hash = _sha256(source)
    if before_hash != expected_source_sha256.strip().lower():
        raise ValueError("source SHA-256 does not match expected_source_sha256")
    before_stat = source.stat()
    root = Path(runtime_dir) if runtime_dir is not None else ownership_manager.runtime_dir
    root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="periodic_mesh_smoke_", dir=root))
    clone_path = temporary / "clone.mph"
    clone = None
    cleanup = {"client_model_removed": False, "clone_file_removed": False, "clone_dir_removed": False}
    native_error = None
    counts = None
    try:
        model.java.save(str(clone_path), True)
        clone = client.load(str(clone_path))
        component_tags = _tags(clone.java.component())
        component_tag = expected_component_tag or ("comp1" if "comp1" in component_tags else None)
        if component_tag is None and len(component_tags) == 1:
            component_tag = component_tags[0]
        if component_tag is None or component_tag not in component_tags:
            raise ValueError(f"component tag is ambiguous; available={component_tags}")
        component = _get(clone.java.component(), component_tag)
        mesh_info, mesh = _mesh_sequence(component, expected_mesh_tag)
        if mesh is None:
            raise ValueError(f"mesh tag is ambiguous; available={mesh_info.get('mesh_tags')}")
        mesh.run()
        counts = {
            "component_tag": component_tag,
            "mesh_tag": mesh_info["mesh_tag"],
            "element_count": int(mesh.getNumElem()),
            "vertex_count": int(mesh.getNumVertex()),
        }
    except Exception as exc:
        native_error = _error(exc)
    finally:
        if clone is not None:
            try:
                client.remove(clone)
                cleanup["client_model_removed"] = True
            except Exception as exc:
                cleanup["client_remove_error"] = _error(exc)
        try:
            clone_path.unlink(missing_ok=True)
            cleanup["clone_file_removed"] = not clone_path.exists()
        except OSError as exc:
            cleanup["clone_file_error"] = _error(exc)
        try:
            temporary.rmdir()
            cleanup["clone_dir_removed"] = not temporary.exists()
        except OSError as exc:
            cleanup["clone_dir_error"] = _error(exc)
    after_stat = source.stat()
    after_hash = _sha256(source)
    source_unchanged = (
        before_hash == after_hash
        and before_stat.st_mtime_ns == after_stat.st_mtime_ns
        and before_stat.st_size == after_stat.st_size
    )
    success = bool(
        native_error is None
        and counts
        and counts["element_count"] > 0
        and source_unchanged
        and all(cleanup.get(key) for key in ("client_model_removed", "clone_file_removed", "clone_dir_removed"))
    )
    return {
        "schema_name": "comsol_mcp.periodic_mesh_smoke",
        "schema_version": "1.0.0",
        "success": success,
        "native_mesh_build": "passed" if native_error is None and counts else "failed",
        "native_error": native_error,
        "counts": counts,
        "source_integrity": {
            "sha256_before": before_hash,
            "sha256_after": after_hash,
            "unchanged": source_unchanged,
        },
        "cleanup": cleanup,
        "derived_artifact": {
            "artifact_id": "ephemeral_clone",
            "retained": not cleanup.get("clone_dir_removed", False),
        },
        "compatibility_assessment": (
            "native_mesh_smoke_passed" if success else "native_mesh_smoke_failed"
        ),
        "limitation": "A native mesh build is stronger than recipe inspection but is not an explicit node-by-node equality export.",
    }


def register_periodic_mesh_audit_tools(mcp: FastMCP) -> None:
    """Register read-only audit and explicit clone-only mesh smoke tools."""

    @mcp.tool()
    def wave_optics_periodic_mesh_audit(
        model_name: str,
        expected_source_path: Optional[str] = None,
        expected_source_sha256: Optional[str] = None,
        expected_component_tag: Optional[str] = None,
        expected_physics_tag: Optional[str] = None,
        expected_study_tag: Optional[str] = None,
        expected_mesh_tag: Optional[str] = None,
    ) -> dict[str, Any]:
        """Inspect periodic geometry and mesh ordering without running either."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        profile = getattr(getattr(mcp, "profile_selection", None), "name", "unknown")
        try:
            return {
                "success": True,
                **collect_periodic_mesh_audit(
                    model,
                    model_name=model_name,
                    session_state=session_manager.get_status(),
                    active_profile=profile,
                    expected_source_path=expected_source_path,
                    expected_source_sha256=expected_source_sha256,
                    expected_component_tag=expected_component_tag,
                    expected_physics_tag=expected_physics_tag,
                    expected_study_tag=expected_study_tag,
                    expected_mesh_tag=expected_mesh_tag,
                ),
            }
        except (ValueError, OSError) as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"Periodic mesh audit failed safely: {_error(exc)}"}

    @mcp.tool()
    def wave_optics_periodic_mesh_smoke(
        model_name: str,
        expected_source_sha256: str,
        expected_component_tag: Optional[str] = None,
        expected_mesh_tag: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build only a save-copy clone's mesh and delete the clone afterward."""
        model = session_manager.get_model(model_name)
        client = session_manager.client
        if model is None:
            return {"success": False, "error": f"Model not found: {model_name}"}
        if client is None:
            return {"success": False, "error": "No connected COMSOL client"}
        ownership = ownership_manager.status(session_state=session_manager.get_status())
        if ownership.get("collision"):
            return {"success": False, "error": "Solver ownership collision blocks clone mesh smoke"}
        try:
            return run_clone_mesh_smoke(
                model,
                client,
                expected_source_sha256=expected_source_sha256,
                expected_component_tag=expected_component_tag,
                expected_mesh_tag=expected_mesh_tag,
            )
        except (ValueError, OSError) as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            return {"success": False, "error": f"Clone mesh smoke failed safely: {_error(exc)}"}


__all__ = [
    "collect_periodic_mesh_audit",
    "register_periodic_mesh_audit_tools",
    "run_clone_mesh_smoke",
]
