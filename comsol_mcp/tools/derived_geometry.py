"""Typed geometry edits restricted to provenance-tracked derived models."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .ownership import ownership_manager
from .properties import _read_property
from .session import session_manager

_NUMBER_WITH_UNIT = re.compile(
    r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*\[([^\]]+)\]\s*$"
)


@dataclass
class DerivedGeometryRecord:
    derived_model_id: str
    model_name: str
    source_path: str
    source_sha256: str
    backing_path: str
    backing_sha256: str
    dirty: bool = False
    dirty_reason: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


_DERIVED: dict[str, DerivedGeometryRecord] = {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tags(container: Any) -> list[str]:
    return [str(value) for value in list(container.tags())]


def _get(container: Any, tag: str) -> Any:
    try:
        return container.get(tag)
    except Exception:
        return container(tag)


def _feature_type(feature: Any) -> str:
    for getter in ("getType", "type"):
        try:
            return str(getattr(feature, getter)())
        except Exception:
            continue
    try:
        return str(feature.label())
    except Exception:
        return "unknown"


def _feature_snapshot(feature: Any, tag: str) -> dict[str, Any]:
    try:
        property_names = sorted({str(name) for name in feature.properties()})
    except Exception as exc:
        raise ValueError(f"cannot inventory geometry feature {tag!r}: {exc}") from exc
    properties = {}
    for name in property_names:
        try:
            value, value_type = _read_property(feature, name)
        except Exception as exc:
            raise ValueError(
                f"cannot snapshot geometry feature {tag!r} property {name!r}: {exc}"
            ) from exc
        properties[name] = {"value_type": value_type, "value": value}
    try:
        label = str(feature.label())
    except Exception as exc:
        raise ValueError(f"cannot snapshot geometry feature {tag!r} label: {exc}") from exc
    return {
        "tag": tag,
        "type": _feature_type(feature),
        "label": label,
        "properties": properties,
    }


def _set_vector(feature: Any, name: str, values: list[str]) -> None:
    """Set a string vector through clientapi, with a mock-friendly fallback."""
    try:
        import jpype
        if not jpype.isJVMStarted():
            raise ImportError("JVM not started")
        feature.set(name, jpype.JArray(jpype.JString)(values))
    except (ImportError, TypeError):
        feature.set(name, values)


def _geometry(model: Any, component_tag: str, geometry_tag: str) -> Any:
    components = model.java.component()
    if component_tag not in _tags(components):
        raise ValueError(f"component tag does not exist: {component_tag}")
    component = _get(components, component_tag)
    geometries = component.geom()
    if geometry_tag not in _tags(geometries):
        raise ValueError(f"geometry tag does not exist: {geometry_tag}")
    return _get(geometries, geometry_tag)


def _snapshot(model: Any, component_tag: str, geometry_tag: str) -> dict[str, Any]:
    geom = _geometry(model, component_tag, geometry_tag)
    features = geom.feature()
    tags = _tags(features)
    feature_snapshots = {
        tag: _feature_snapshot(_get(features, tag), tag) for tag in tags
    }
    fin = None
    if "fin" in feature_snapshots:
        properties = feature_snapshots["fin"]["properties"]
        fin = {
            name: properties.get(name, {}).get("value")
            for name in ("action", "imprint", "createpairs")
        }
    blocks = {}
    for tag, feature_snapshot in feature_snapshots.items():
        kind = feature_snapshot["type"]
        if kind.casefold() == "block":
            properties = feature_snapshot["properties"]
            if "size" not in properties or "pos" not in properties:
                raise ValueError(f"Block feature {tag!r} lacks complete size/pos properties")
            blocks[tag] = {
                "type": kind,
                "size": list(properties["size"]["value"]),
                "pos": list(properties["pos"]["value"]),
            }
    return {
        "component_tag": component_tag,
        "geometry_tag": geometry_tag,
        "fin": fin,
        "blocks": blocks,
        "features": feature_snapshots,
    }


def _state_hash(record: DerivedGeometryRecord, snapshot: dict[str, Any]) -> str:
    payload = {
        "derived_model_id": record.derived_model_id,
        "source_sha256": record.source_sha256,
        "geometry": snapshot,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _record(derived_model_id: str, model_name: str) -> DerivedGeometryRecord:
    record = _DERIVED.get(derived_model_id)
    if record is None or record.model_name != model_name:
        raise ValueError("unknown or mismatched provenance-tracked derived_model_id")
    if record.dirty:
        raise ValueError(f"derived model is dirty and unusable for validation: {record.dirty_reason}")
    return record


def _validate_vector(values: object, *, positive: bool) -> list[str]:
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError("size and pos must each be complete 3-element string vectors")
    result = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("geometry vector values must be strings with explicit units")
        match = _NUMBER_WITH_UNIT.fullmatch(value)
        if not match:
            raise ValueError(f"geometry value must be a finite literal with explicit unit: {value!r}")
        number = float(match.group(1))
        if not math.isfinite(number) or (positive and number <= 0):
            raise ValueError("block sizes must be finite and positive")
        result.append(value.strip())
    return result


def _validate_edits(edits: object) -> list[dict[str, Any]]:
    if not isinstance(edits, list) or not edits:
        raise ValueError("block_edits must be a non-empty list")
    normalized = []
    seen = set()
    for item in edits:
        if not isinstance(item, dict) or set(item) != {"block_tag", "size", "pos"}:
            raise ValueError("each block edit requires exactly block_tag, size, and pos")
        tag = item["block_tag"]
        if not isinstance(tag, str) or not tag or tag in seen:
            raise ValueError("block_tag must be a unique non-empty string")
        seen.add(tag)
        normalized.append({
            "block_tag": tag,
            "size": _validate_vector(item["size"], positive=True),
            "pos": _validate_vector(item["pos"], positive=False),
        })
    return normalized


def create_derived_geometry_clone(
    source_model: Any,
    client: Any,
    *,
    new_name: str,
    runtime_dir: Path | None = None,
) -> tuple[Any, DerivedGeometryRecord]:
    source_path = Path(str(source_model.file())).resolve()
    if not source_path.is_file():
        raise ValueError("source model has no readable immutable file")
    root = Path(runtime_dir) if runtime_dir else ownership_manager.runtime_dir
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="comsol_mcp_clone_geometry_", dir=root))
    clone_path = directory / "clone.mph"
    clone = None
    try:
        shutil.copyfile(source_path, clone_path)
        source_hash = _sha256(clone_path)
        clone = client.load(str(clone_path))
        clone.java.label(new_name)
        backing_hash = _sha256(clone_path)
        if backing_hash != source_hash:
            raise RuntimeError("derived clone backing bytes changed during load")
    except Exception as exc:
        cleanup_errors = []
        if clone is not None:
            try:
                client.remove(clone)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"clone_remove: {cleanup_exc}")
        try:
            clone_path.unlink(missing_ok=True)
            directory.rmdir()
        except Exception as cleanup_exc:
            cleanup_errors.append(f"backing_remove: {cleanup_exc}")
        if cleanup_errors:
            raise RuntimeError(
                f"{exc}; clone cleanup failed: {'; '.join(cleanup_errors)}"
            ) from exc
        raise
    record = DerivedGeometryRecord(
        derived_model_id=f"derived-{uuid.uuid4().hex}",
        model_name=str(clone.name()),
        source_path=str(source_path),
        source_sha256=source_hash,
        backing_path=str(clone_path),
        backing_sha256=backing_hash,
    )
    return clone, record


def _discard_derived_model(model_name: str) -> None:
    for derived_model_id, record in list(_DERIVED.items()):
        if record.model_name == model_name:
            _DERIVED.pop(derived_model_id, None)


session_manager.add_model_removal_listener(_discard_derived_model)


def _cleanup_clone_artifact(path_value: str) -> None:
    path = Path(path_value)
    path.unlink(missing_ok=True)
    path.parent.rmdir()


def _create_registered_derived_geometry_clone(
    source_model: Any,
    client: Any,
    *,
    new_name: str,
) -> tuple[Any, DerivedGeometryRecord, dict[str, Any]]:
    clone, record = create_derived_geometry_clone(
        source_model, client, new_name=new_name
    )
    registered_name = None
    try:
        registered_name = session_manager.add_model(
            clone, cleanup_path=record.backing_path
        )
        record.model_name = registered_name
        _DERIVED[record.derived_model_id] = record
        snapshot = _snapshot(clone, "comp1", "geom1")
        return clone, record, snapshot
    except Exception as exc:
        _DERIVED.pop(record.derived_model_id, None)
        cleanup_errors = []
        removed = False
        if registered_name is not None:
            try:
                removed = session_manager.remove_model(registered_name)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"session_remove: {cleanup_exc}")
        if not removed:
            try:
                client.remove(clone)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"clone_remove: {cleanup_exc}")
            try:
                _cleanup_clone_artifact(record.backing_path)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"backing_remove: {cleanup_exc}")
        if cleanup_errors:
            raise RuntimeError(
                f"{exc}; clone transaction cleanup failed: {'; '.join(cleanup_errors)}"
            ) from exc
        raise


def derived_model_validation_status(model_name: str) -> dict[str, Any]:
    """Return tracked dirty-state evidence for validation entry points."""
    matches = [record for record in _DERIVED.values() if record.model_name == model_name]
    if not matches:
        return {"tracked": False, "validation_allowed": True}
    record = matches[-1]
    return {
        "tracked": True,
        "derived_model_id": record.derived_model_id,
        "validation_allowed": not record.dirty,
        "dirty": record.dirty,
        "dirty_reason": record.dirty_reason,
    }


def preview_fin(
    model: Any,
    record: DerivedGeometryRecord,
    *,
    expected_state_sha256: str,
    component_tag: str,
    geometry_tag: str,
    action: str,
    imprint: bool,
    create_pairs: bool,
) -> dict[str, Any]:
    if action not in {"union", "assembly"}:
        raise ValueError("action must be union or assembly")
    snapshot = _snapshot(model, component_tag, geometry_tag)
    state_hash = _state_hash(record, snapshot)
    if state_hash != expected_state_sha256:
        raise ValueError("stale expected_state_sha256")
    if snapshot["fin"] is None:
        raise ValueError("reserved fin feature does not exist")
    return {
        "operation": "fin",
        "derived_model_id": record.derived_model_id,
        "pre_state_sha256": state_hash,
        "before": snapshot["fin"],
        "planned": {
            "action": action,
            "imprint": "on" if imprint else "off",
            "createpairs": "on" if create_pairs else "off",
        },
        "likely_topology_consequences": [
            "assembly may preserve interior boundaries and optionally create pairs",
            "union may merge compatible interior boundaries",
            "geometry selections can renumber after a build",
        ],
        "mutated": False,
    }


def preview_blocks(
    model: Any,
    record: DerivedGeometryRecord,
    *,
    expected_state_sha256: str,
    component_tag: str,
    geometry_tag: str,
    block_edits: object,
) -> dict[str, Any]:
    edits = _validate_edits(block_edits)
    snapshot = _snapshot(model, component_tag, geometry_tag)
    state_hash = _state_hash(record, snapshot)
    if state_hash != expected_state_sha256:
        raise ValueError("stale expected_state_sha256")
    for edit in edits:
        if edit["block_tag"] not in snapshot["blocks"]:
            raise ValueError(f"missing or non-Block feature: {edit['block_tag']}")
    return {
        "operation": "blocks",
        "derived_model_id": record.derived_model_id,
        "pre_state_sha256": state_hash,
        "before": {edit["block_tag"]: snapshot["blocks"][edit["block_tag"]] for edit in edits},
        "planned": edits,
        "geometry_run": False,
        "mesh_run": False,
        "mutated": False,
    }


def _topology(geom: Any) -> dict[str, Any]:
    try:
        return {
            "domains": int(geom.getNDomains()),
            "boundaries": int(geom.getNBoundaries()),
            "pairs": None,
        }
    except Exception as exc:
        return {"error": str(exc)[:300]}


def apply_fin(model: Any, record: DerivedGeometryRecord, preview: dict[str, Any], component_tag: str, geometry_tag: str) -> dict[str, Any]:
    current = _snapshot(model, component_tag, geometry_tag)
    if _state_hash(record, current) != preview["pre_state_sha256"]:
        raise ValueError("stale pre-state; preview must be regenerated")
    geom = _geometry(model, component_tag, geometry_tag)
    fin = _get(geom.feature(), "fin")
    before_topology = _topology(geom)
    rollback_errors = []
    try:
        for key, value in preview["planned"].items():
            fin.set(key, value == "on" if key in {"imprint", "createpairs"} else value)
        geom.run()
        after = _snapshot(model, component_tag, geometry_tag)
    except Exception as exc:
        for key, value in current["fin"].items():
            if value is None:
                continue
            try:
                fin.set(key, value)
            except Exception as rollback_exc:
                rollback_errors.append(f"{key}: {rollback_exc}")
        try:
            geom.run()
        except Exception as rollback_exc:
            rollback_errors.append(f"geometry: {rollback_exc}")
        try:
            rollback_snapshot = _snapshot(model, component_tag, geometry_tag)
            if rollback_snapshot != current:
                rollback_errors.append("restored geometry does not match the complete pre-state")
            if _topology(geom) != before_topology:
                rollback_errors.append("restored topology does not match the pre-state")
        except Exception as rollback_exc:
            rollback_errors.append(f"rollback_snapshot: {rollback_exc}")
        if rollback_errors:
            record.dirty = True
            record.dirty_reason = "; ".join(rollback_errors)[:500]
        return {
            "success": False,
            "error": str(exc)[:300],
            "rollback_proved": not rollback_errors,
            "rollback_errors": rollback_errors,
            "derived_model_dirty": record.dirty,
        }
    return {
        "success": True,
        "derived_model_id": record.derived_model_id,
        "pre_state_sha256": preview["pre_state_sha256"],
        "post_state_sha256": _state_hash(record, after),
        "before": current["fin"],
        "after": after["fin"],
        "before_topology": before_topology,
        "after_topology": _topology(geom),
        "geometry_run": True,
        "mesh_run": False,
        "derived_model_dirty": False,
    }


def apply_blocks(model: Any, record: DerivedGeometryRecord, preview: dict[str, Any], component_tag: str, geometry_tag: str) -> dict[str, Any]:
    current = _snapshot(model, component_tag, geometry_tag)
    if _state_hash(record, current) != preview["pre_state_sha256"]:
        raise ValueError("stale pre-state; preview must be regenerated")
    geom = _geometry(model, component_tag, geometry_tag)
    captured = {tag: current["blocks"][tag] for tag in (item["block_tag"] for item in preview["planned"])}
    rollback_errors = []
    try:
        for edit in preview["planned"]:
            node = _get(geom.feature(), edit["block_tag"])
            _set_vector(node, "size", edit["size"])
            _set_vector(node, "pos", edit["pos"])
        after = _snapshot(model, component_tag, geometry_tag)
    except Exception as exc:
        for tag, values in captured.items():
            node = _get(geom.feature(), tag)
            for key in ("size", "pos"):
                try:
                    _set_vector(node, key, values[key])
                except Exception as rollback_exc:
                    rollback_errors.append(f"{tag}.{key}: {rollback_exc}")
        try:
            rollback_snapshot = _snapshot(model, component_tag, geometry_tag)
            if rollback_snapshot != current:
                rollback_errors.append("restored geometry does not match the complete pre-state")
        except Exception as rollback_exc:
            rollback_errors.append(f"rollback_snapshot: {rollback_exc}")
        if rollback_errors:
            record.dirty = True
            record.dirty_reason = "; ".join(rollback_errors)[:500]
        return {
            "success": False,
            "error": str(exc)[:300],
            "rollback_proved": not rollback_errors,
            "rollback_errors": rollback_errors,
            "derived_model_dirty": record.dirty,
        }
    return {
        "success": True,
        "derived_model_id": record.derived_model_id,
        "pre_state_sha256": preview["pre_state_sha256"],
        "post_state_sha256": _state_hash(record, after),
        "before": captured,
        "after": {tag: after["blocks"][tag] for tag in captured},
        "geometry_run": False,
        "mesh_run": False,
        "derived_model_dirty": False,
    }


def register_derived_geometry_tools(mcp: FastMCP) -> None:
    """Register provenance clone and typed preview/apply operations."""

    @mcp.tool()
    def geometry_derived_clone(source_model_name: str, new_name: str) -> dict[str, Any]:
        source = session_manager.get_model(source_model_name)
        client = session_manager.client
        if source is None or client is None:
            return {"success": False, "error": "source model or COMSOL client unavailable"}
        try:
            clone, record, snapshot = _create_registered_derived_geometry_clone(
                source, client, new_name=new_name
            )
            model_name = record.model_name
            return {
                "success": True,
                "derived_model_id": record.derived_model_id,
                "model_name": model_name,
                "source_sha256": record.source_sha256,
                "derived_backing_sha256": record.backing_sha256,
                "state_sha256": _state_hash(record, snapshot),
                "dirty": False,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def geometry_fin_preview(derived_model_id: str, model_name: str, expected_state_sha256: str, action: Literal["union", "assembly"], imprint: bool, create_pairs: bool, component_tag: str = "comp1", geometry_tag: str = "geom1") -> dict[str, Any]:
        try:
            record = _record(derived_model_id, model_name)
            return {"success": True, **preview_fin(session_manager.get_model(model_name), record, expected_state_sha256=expected_state_sha256, component_tag=component_tag, geometry_tag=geometry_tag, action=action, imprint=imprint, create_pairs=create_pairs)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def geometry_fin_apply(derived_model_id: str, model_name: str, expected_state_sha256: str, action: Literal["union", "assembly"], imprint: bool, create_pairs: bool, component_tag: str = "comp1", geometry_tag: str = "geom1") -> dict[str, Any]:
        try:
            record = _record(derived_model_id, model_name)
            preview = preview_fin(session_manager.get_model(model_name), record, expected_state_sha256=expected_state_sha256, component_tag=component_tag, geometry_tag=geometry_tag, action=action, imprint=imprint, create_pairs=create_pairs)
            return apply_fin(session_manager.get_model(model_name), record, preview, component_tag, geometry_tag)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def geometry_blocks_preview(derived_model_id: str, model_name: str, expected_state_sha256: str, block_edits: list[dict[str, Any]], component_tag: str = "comp1", geometry_tag: str = "geom1") -> dict[str, Any]:
        try:
            record = _record(derived_model_id, model_name)
            return {"success": True, **preview_blocks(session_manager.get_model(model_name), record, expected_state_sha256=expected_state_sha256, component_tag=component_tag, geometry_tag=geometry_tag, block_edits=block_edits)}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def geometry_blocks_apply(derived_model_id: str, model_name: str, expected_state_sha256: str, block_edits: list[dict[str, Any]], component_tag: str = "comp1", geometry_tag: str = "geom1") -> dict[str, Any]:
        try:
            record = _record(derived_model_id, model_name)
            preview = preview_blocks(session_manager.get_model(model_name), record, expected_state_sha256=expected_state_sha256, component_tag=component_tag, geometry_tag=geometry_tag, block_edits=block_edits)
            return apply_blocks(session_manager.get_model(model_name), record, preview, component_tag, geometry_tag)
        except Exception as exc:
            return {"success": False, "error": str(exc)}


__all__ = [
    "DerivedGeometryRecord", "apply_blocks", "apply_fin",
    "create_derived_geometry_clone", "preview_blocks", "preview_fin",
    "derived_model_validation_status", "register_derived_geometry_tools",
]
