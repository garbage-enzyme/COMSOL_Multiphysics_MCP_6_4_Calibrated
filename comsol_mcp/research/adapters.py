"""Trusted structure-family adapter manifests and exact tree audits."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _text, _timestamp

STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME = "comsol_mcp.research_structure_adapter_manifest"
STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION = "1.0.0"
STRUCTURE_TREE_AUDIT_SCHEMA_NAME = "comsol_mcp.research_structure_tree_audit"
STRUCTURE_TREE_AUDIT_SCHEMA_VERSION = "1.0.0"
STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME = "comsol_mcp.research_structure_adapter_application"
STRUCTURE_ADAPTER_APPLICATION_SCHEMA_VERSION = "1.0.0"
PERIODIC_MIM_PATCH_ADAPTER_ID = "periodic_mim_patch_v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VARIABLES = ("patch_length_x", "patch_length_y")
_FIXED_KEYS = {
    "evidence",
    "incidence",
    "layer_stack",
    "materials",
    "mesh",
    "period",
    "study",
    "topology",
}
_SCOPES = {"geometry", "material", "mesh", "physics", "result", "study"}


class PeriodicMimPatchBackend(Protocol):
    """Minimal derived-model backend required by the trusted adapter."""

    def source_sha256(self) -> str: ...

    def snapshot(self) -> dict[str, Any]: ...

    def apply_patch(self, size: list[float], position: list[float]) -> None: ...

    def rebuild_geometry(self) -> None: ...

    def reprobe_selections(self) -> dict[str, int]: ...

    def rebuild_mesh(self) -> str: ...

    def restore(self, snapshot: dict[str, Any]) -> None: ...

    def mark_dirty(self, reason_code: str) -> None: ...


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _clientapi_tags(container: Any) -> list[str]:
    return [str(value) for value in list(container.tags())]


def _clientapi_get(container: Any, tag: str) -> Any:
    try:
        return container.get(tag)
    except Exception:
        return container(tag)


def _clientapi_feature_type(feature: Any) -> str:
    errors = []
    for getter in ("getType", "type"):
        try:
            return str(getattr(feature, getter)())
        except Exception as exc:
            errors.append(type(exc).__name__)
    raise ValueError(f"trusted feature type is unreadable ({','.join(errors)})")


def _clientapi_set_vector(feature: Any, name: str, values: list[str]) -> None:
    from comsol_mcp.tools.derived_geometry import _set_vector

    _set_vector(feature, name, values)


def _clientapi_patch_feature(model: Any, manifest: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    component_tag = manifest["component_tag"]
    geometry_tag = manifest["geometry_tag"]
    components = model.java.component()
    if component_tag not in _clientapi_tags(components):
        raise ValueError("trusted adapter component is absent")
    component = _clientapi_get(components, component_tag)
    geometries = component.geom()
    if geometry_tag not in _clientapi_tags(geometries):
        raise ValueError("trusted adapter geometry is absent")
    geometry = _clientapi_get(geometries, geometry_tag)
    path = manifest["patch_feature"]["tag_path"]
    if len(path) != 1:
        raise ValueError("first concrete periodic MIM backend requires one top-level feature")
    features = geometry.feature()
    if path[0] not in _clientapi_tags(features):
        raise ValueError("trusted patch feature is absent")
    feature = _clientapi_get(features, path[0])
    observed_type = _clientapi_feature_type(feature)
    if observed_type != manifest["patch_feature"]["feature_types"][0]:
        raise ValueError("trusted patch feature type changed")
    if observed_type != "Block":
        raise ValueError("first concrete periodic MIM backend supports only an existing Block")
    return component, geometry, feature


def _clientapi_vector(feature: Any, property_name: str) -> list[float]:
    try:
        value_type = str(feature.getValueType(property_name))
        value = [float(item) for item in feature.getDoubleArray(property_name)]
    except Exception as exc:
        raise ValueError("trusted patch property is unreadable") from exc
    if value_type.casefold().replace("[]", "array") not in {
        "doublearray",
        "floatarray",
    }:
        raise ValueError("trusted patch property must remain a floating-point array")
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("trusted Block size and position must contain exactly three values")
    result = [_finite(item, f"clientapi vector[{index}]") for index, item in enumerate(value)]
    return result


def _default_topology_observer(
    model: Any,
    manifest: Mapping[str, Any],
    patch_size: list[float],
    patch_position: list[float],
) -> tuple[dict[str, int], str]:
    from comsol_mcp.tools.mim_patch import (
        _identify_patch_topology,
        _identify_side_pairs,
        _list_pair_metadata,
        _probe_boundaries,
    )

    component, geometry, _feature = _clientapi_patch_feature(model, manifest)
    boundaries, domains, boundary_count, _sdim = _probe_boundaries(geometry)
    bounding_box = [float(value) for value in list(geometry.getBoundingBox())]
    if len(bounding_box) != 6:
        raise ValueError("trusted adapter geometry bounding box is incomplete")
    side_pairs = _identify_side_pairs(boundaries, bbox=tuple(bounding_box))
    if len(side_pairs["x_src"]) != len(side_pairs["x_dst"]):
        raise ValueError("x periodic side partitions are not cardinality matched")
    if len(side_pairs["y_src"]) != len(side_pairs["y_dst"]):
        raise ValueError("y periodic side partitions are not cardinality matched")
    patch_domain, patch_footprint = _identify_patch_topology(
        boundaries,
        patch_size,
        patch_position,
        preferred_footprint=_trusted_patch_footprint(component, manifest),
    )
    topology = {
        "domain_count": int(domains),
        "boundary_count": int(boundary_count),
        "x_pair_count": len(side_pairs["x_src"]),
        "y_pair_count": len(side_pairs["y_src"]),
        "top_port_count": len(side_pairs["top"]),
        "bottom_port_count": len(side_pairs["bottom"]),
    }
    if any(value < 1 for value in topology.values()):
        raise ValueError("trusted topology observation is incomplete")
    identity = domain_sha256_v2(
        "comsol_mcp.research_periodic_mim_selection_state",
        {
            "bounding_box": bounding_box,
            "boundaries": boundaries,
            "side_pairs": side_pairs,
            "component_pairs": _list_pair_metadata(component),
            "patch_domain": patch_domain,
            "patch_footprint": patch_footprint,
        },
    )
    return topology, identity


def _trusted_patch_footprint(component: Any, manifest: Mapping[str, Any]) -> list[int]:
    paths = [
        item["tag_path"]
        for item in manifest["required_features"]
        if item["scope"] == "physics"
        and item["feature_type"] == "LayeredTransitionBoundaryCondition"
        and len(item["tag_path"]) == 2
    ]
    if len(paths) != 1:
        raise ValueError("trusted manifest must identify one layered transition boundary")
    physics_tag, feature_tag = paths[0]
    physics = component.physics()
    if physics_tag not in _clientapi_tags(physics):
        raise ValueError("trusted Wave Optics interface is absent")
    interface = _clientapi_get(physics, physics_tag)
    feature = _clientapi_get(interface.feature(), feature_tag)
    if feature is None:
        raise ValueError("trusted layered transition feature is absent")
    entities = [int(value) for value in list(feature.selection().entities())]
    if len(entities) != 1:
        raise ValueError("trusted layered transition selection must contain one footprint")
    return entities


def _manifest_mesh_tag(manifest: Mapping[str, Any]) -> str:
    paths = [
        item["tag_path"]
        for item in manifest["required_features"]
        if item["scope"] == "mesh" and len(item["tag_path"]) == 1
    ]
    if len(paths) != 1:
        raise ValueError("trusted adapter manifest must identify exactly one mesh sequence")
    return _identifier(paths[0][0], "trusted mesh tag")


def _clientapi_mesh(model: Any, manifest: Mapping[str, Any]) -> Any:
    component_tag = manifest["component_tag"]
    component = _clientapi_get(model.java.component(), component_tag)
    meshes = component.mesh()
    mesh_tag = _manifest_mesh_tag(manifest)
    if mesh_tag not in _clientapi_tags(meshes):
        raise ValueError("trusted adapter mesh is absent")
    return _clientapi_get(meshes, mesh_tag)


def _default_mesh_observer(model: Any, manifest: Mapping[str, Any]) -> str:
    mesh = _clientapi_mesh(model, manifest)
    features = mesh.feature()
    feature_inventory = [
        {"tag": tag, "type": _clientapi_feature_type(_clientapi_get(features, tag))}
        for tag in _clientapi_tags(features)
    ]
    return str(
        domain_sha256_v2(
            "comsol_mcp.research_periodic_mim_mesh_state",
            {
                "mesh_tag": _manifest_mesh_tag(manifest),
                "features": feature_inventory,
                "num_elements": int(mesh.getNumElem()),
                "num_vertices": int(mesh.getNumVertex()),
            },
        )
    )


class ClientapiPeriodicMimPatchBackend:
    """Concrete atomic backend for a tracked derived model's existing Block patch."""

    def __init__(
        self,
        model: Any,
        derived_record: Any,
        manifest: object,
        *,
        topology_observer: Any = _default_topology_observer,
        mesh_observer: Any = _default_mesh_observer,
    ) -> None:
        self.model = model
        self.record = derived_record
        self.manifest = normalize_structure_adapter_manifest(manifest)
        self._topology_observer = topology_observer
        self._mesh_observer = mesh_observer
        self._topology: dict[str, int] | None = None
        self._selection_identity: str | None = None
        source = Path(str(derived_record.source_path)).resolve(strict=True)
        backing = Path(str(derived_record.backing_path)).resolve(strict=True)
        if source == backing or Path(str(model.file())).resolve(strict=True) != backing:
            raise ValueError("adapter requires one distinct provenance-tracked derived model")
        if getattr(derived_record, "dirty", False):
            raise ValueError("derived model is dirty and unusable for trusted adapter work")
        if _file_sha256(source) != self.manifest["source_identity"]["source_sha256"]:
            raise ValueError("derived record source bytes do not match the trusted manifest")
        _component, _geometry, feature = _clientapi_patch_feature(model, self.manifest)
        self._fixed_size = _clientapi_vector(
            feature, self.manifest["patch_feature"]["size_property"]
        )
        self._fixed_position = _clientapi_vector(
            feature, self.manifest["patch_feature"]["position_property"]
        )

    def source_sha256(self) -> str:
        return _file_sha256(Path(str(self.record.source_path)).resolve(strict=True))

    def _vectors(self) -> tuple[list[float], list[float]]:
        _component, _geometry, feature = _clientapi_patch_feature(self.model, self.manifest)
        size = _clientapi_vector(feature, self.manifest["patch_feature"]["size_property"])
        position = _clientapi_vector(feature, self.manifest["patch_feature"]["position_property"])
        if size[2] != self._fixed_size[2] or position[2] != self._fixed_position[2]:
            raise ValueError("adapter fixed z geometry changed")
        return size, position

    def _observe_topology(self, size: list[float], position: list[float]) -> dict[str, int]:
        topology, identity = self._topology_observer(self.model, self.manifest, size, position)
        self._topology = _topology(topology, "clientapi topology")
        self._selection_identity = _sha(identity, "clientapi selection identity")
        return dict(self._topology)

    def snapshot(self) -> dict[str, Any]:
        size, position = self._vectors()
        topology = self._observe_topology(size, position)
        mesh_identity = _sha(
            self._mesh_observer(self.model, self.manifest), "clientapi mesh identity"
        )
        return {
            "patch_size": size[:2],
            "patch_position": position[:2],
            "topology": topology,
            "selection_identity": self._selection_identity,
            "mesh_identity": mesh_identity,
        }

    def apply_patch(self, size: list[float], position: list[float]) -> None:
        _component, _geometry, feature = _clientapi_patch_feature(self.model, self.manifest)
        full_size = [*size, self._fixed_size[2]]
        full_position = [*position, self._fixed_position[2]]
        _clientapi_set_vector(
            feature,
            self.manifest["patch_feature"]["size_property"],
            [format(value, ".17g") for value in full_size],
        )
        _clientapi_set_vector(
            feature,
            self.manifest["patch_feature"]["position_property"],
            [format(value, ".17g") for value in full_position],
        )

    def rebuild_geometry(self) -> None:
        _component, geometry, _feature = _clientapi_patch_feature(self.model, self.manifest)
        geometry.run()

    def reprobe_selections(self) -> dict[str, int]:
        size, position = self._vectors()
        return self._observe_topology(size, position)

    def rebuild_mesh(self) -> str:
        mesh = _clientapi_mesh(self.model, self.manifest)
        mesh.run()
        return _sha(self._mesh_observer(self.model, self.manifest), "clientapi mesh identity")

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.apply_patch(list(snapshot["patch_size"]), list(snapshot["patch_position"]))

    def mark_dirty(self, reason_code: str) -> None:
        self.record.dirty = True
        self.record.dirty_reason = _identifier(reason_code, "adapter dirty reason")


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _tag_path(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError(f"{name} must be a bounded nonempty tag path")
    return [_identifier(item, f"{name}[{index}]") for index, item in enumerate(value)]


def _features(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise ValueError(f"{name} must be a bounded nonempty list")
    result = []
    for index, item in enumerate(value):
        item_name = f"{name}[{index}]"
        raw = _object(item, {"scope", "tag_path", "feature_type"}, item_name)
        if raw["scope"] not in _SCOPES:
            raise ValueError(f"{item_name}.scope is unsupported")
        result.append(
            {
                "scope": raw["scope"],
                "tag_path": _tag_path(raw["tag_path"], f"{item_name}.tag_path"),
                "feature_type": _text(
                    raw["feature_type"], f"{item_name}.feature_type", maximum=128
                ),
            }
        )
    keys = [(item["scope"], tuple(item["tag_path"])) for item in result]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{name} paths must be unique")
    return sorted(result, key=lambda item: (item["scope"], item["tag_path"]))


def _dimensions(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("mutable_dimensions must contain exactly two entries")
    by_id = {}
    for index, item in enumerate(value):
        name = f"mutable_dimensions[{index}]"
        raw = _object(
            item,
            {"variable_id", "property_index", "unit", "baseline", "lower", "upper"},
            name,
        )
        variable_id = _identifier(raw["variable_id"], f"{name}.variable_id")
        property_index = raw["property_index"]
        if isinstance(property_index, bool) or not isinstance(property_index, int):
            raise ValueError(f"{name}.property_index must be integer")
        baseline = _finite(raw["baseline"], f"{name}.baseline")
        lower = _finite(raw["lower"], f"{name}.lower")
        upper = _finite(raw["upper"], f"{name}.upper")
        if baseline <= 0 or lower != 0.75 * baseline or upper != 1.25 * baseline:
            raise ValueError(f"{name} must freeze exact positive +/-25 percent bounds")
        if variable_id in by_id:
            raise ValueError("mutable variable IDs must be unique")
        by_id[variable_id] = {
            "variable_id": variable_id,
            "property_index": property_index,
            "unit": _text(raw["unit"], f"{name}.unit", maximum=32),
            "baseline": baseline,
            "lower": lower,
            "upper": upper,
        }
    if set(by_id) != set(_VARIABLES) or {
        by_id["patch_length_x"]["property_index"],
        by_id["patch_length_y"]["property_index"],
    } != {0, 1}:
        raise ValueError("mutable dimensions are outside the trusted x/y size contract")
    return [by_id[item] for item in _VARIABLES]


def _fixed(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _FIXED_KEYS:
        raise ValueError(f"{name} must cover every immutable adapter surface")
    return {key: _sha(value[key], f"{name}.{key}") for key in sorted(value)}


def _topology(value: object, name: str) -> dict[str, int]:
    fields = {
        "boundary_count",
        "bottom_port_count",
        "domain_count",
        "top_port_count",
        "x_pair_count",
        "y_pair_count",
    }
    raw = _object(value, fields, name)
    for key, item in raw.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"{name}.{key} must be a positive integer")
    return {key: raw[key] for key in sorted(raw)}


def normalize_structure_adapter_manifest(value: object) -> dict[str, Any]:
    """Normalize one caller-reviewed exact periodic MIM template contract."""
    bounded = _bounded_json(value, "structure adapter manifest", 512 * 1024)
    supplied = bounded.pop("manifest_fingerprint", None) if isinstance(bounded, dict) else None
    fields = {
        "adapter_id",
        "component_tag",
        "evidence_collectors",
        "fixed_contracts",
        "geometry_tag",
        "mutable_dimensions",
        "patch_center",
        "patch_feature",
        "required_features",
        "review",
        "schema_name",
        "schema_version",
        "source_identity",
        "structure_family",
        "topology_invariants",
    }
    raw = _object(bounded, fields, "structure adapter manifest")
    if (
        raw["schema_name"] != STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME
        or raw["schema_version"] != STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION
        or raw["adapter_id"] != PERIODIC_MIM_PATCH_ADAPTER_ID
        or raw["structure_family"] != PERIODIC_MIM_PATCH_ADAPTER_ID
    ):
        raise ValueError("structure adapter schema or family identity is unsupported")
    source = _object(
        raw["source_identity"], {"comsol_build", "source_sha256", "tree_sha256"}, "source_identity"
    )
    patch = _object(
        raw["patch_feature"],
        {"feature_types", "position_property", "size_property", "tag_path"},
        "patch_feature",
    )
    path = _tag_path(patch["tag_path"], "patch_feature.tag_path")
    types = patch["feature_types"]
    if not isinstance(types, list) or len(types) != len(path):
        raise ValueError("patch feature types must align with its tag path")
    center = raw["patch_center"]
    if not isinstance(center, list) or len(center) != 2:
        raise ValueError("patch_center must contain x and y")
    collectors = raw["evidence_collectors"]
    if not isinstance(collectors, list) or not 1 <= len(collectors) <= 16:
        raise ValueError("evidence_collectors must be a bounded nonempty list")
    normalized_collectors = sorted(
        {
            _identifier(item, f"evidence_collectors[{index}]")
            for index, item in enumerate(collectors)
        }
    )
    if len(normalized_collectors) != len(collectors):
        raise ValueError("evidence_collectors must be unique")
    review = _object(
        raw["review"],
        {"baseline_receipt_sha256", "reviewed_at", "reviewer", "status"},
        "review",
    )
    if review["status"] != "accepted":
        raise ValueError("structure adapter manifest must be caller-reviewed and accepted")
    body = {
        "schema_name": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME,
        "schema_version": STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION,
        "adapter_id": PERIODIC_MIM_PATCH_ADAPTER_ID,
        "structure_family": PERIODIC_MIM_PATCH_ADAPTER_ID,
        "source_identity": {
            "comsol_build": _text(
                source["comsol_build"], "source_identity.comsol_build", maximum=64
            ),
            "source_sha256": _sha(source["source_sha256"], "source_identity.source_sha256"),
            "tree_sha256": _sha(source["tree_sha256"], "source_identity.tree_sha256"),
        },
        "component_tag": _identifier(raw["component_tag"], "component_tag"),
        "geometry_tag": _identifier(raw["geometry_tag"], "geometry_tag"),
        "patch_feature": {
            "tag_path": path,
            "feature_types": [
                _text(item, f"patch_feature.feature_types[{index}]", maximum=128)
                for index, item in enumerate(types)
            ],
            "size_property": _identifier(patch["size_property"], "patch_feature.size_property"),
            "position_property": _identifier(
                patch["position_property"], "patch_feature.position_property"
            ),
        },
        "mutable_dimensions": _dimensions(raw["mutable_dimensions"]),
        "patch_center": [
            _finite(center[0], "patch_center[0]"),
            _finite(center[1], "patch_center[1]"),
        ],
        "required_features": _features(raw["required_features"], "required_features"),
        "fixed_contracts": _fixed(raw["fixed_contracts"], "fixed_contracts"),
        "topology_invariants": _topology(raw["topology_invariants"], "topology_invariants"),
        "evidence_collectors": normalized_collectors,
        "review": {
            "status": "accepted",
            "reviewer": _text(review["reviewer"], "review.reviewer", maximum=256),
            "reviewed_at": _timestamp(review["reviewed_at"], "review.reviewed_at"),
            "baseline_receipt_sha256": _sha(
                review["baseline_receipt_sha256"], "review.baseline_receipt_sha256"
            ),
        },
    }
    fingerprint = domain_sha256_v2(STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME, body)
    if supplied is not None and supplied != fingerprint:
        raise ValueError("structure adapter manifest fingerprint is invalid")
    return {**body, "manifest_fingerprint": fingerprint}


def normalize_structure_tree_audit(value: object, manifest: object) -> dict[str, Any]:
    """Bind a read-only live tree observation to one exact accepted manifest."""
    expected = normalize_structure_adapter_manifest(manifest)
    bounded = _bounded_json(value, "structure tree audit", 512 * 1024)
    supplied = bounded.pop("audit_fingerprint", None) if isinstance(bounded, dict) else None
    supplied_accepted = bounded.pop("accepted", None) if isinstance(bounded, dict) else None
    if supplied_accepted is not None and supplied_accepted is not True:
        raise ValueError("structure tree audit accepted field must be true")
    raw = _object(
        bounded,
        {
            "features",
            "fixed_contracts",
            "manifest_fingerprint",
            "schema_name",
            "schema_version",
            "source_identity",
            "topology",
        },
        "structure tree audit",
    )
    if (
        raw["schema_name"] != STRUCTURE_TREE_AUDIT_SCHEMA_NAME
        or raw["schema_version"] != STRUCTURE_TREE_AUDIT_SCHEMA_VERSION
        or raw["manifest_fingerprint"] != expected["manifest_fingerprint"]
    ):
        raise ValueError("structure tree audit identity is unsupported or foreign")
    source = _object(
        raw["source_identity"], {"comsol_build", "source_sha256", "tree_sha256"}, "source_identity"
    )
    observed_source = {
        "comsol_build": _text(source["comsol_build"], "source_identity.comsol_build", maximum=64),
        "source_sha256": _sha(source["source_sha256"], "source_identity.source_sha256"),
        "tree_sha256": _sha(source["tree_sha256"], "source_identity.tree_sha256"),
    }
    features = _features(raw["features"], "features")
    fixed = _fixed(raw["fixed_contracts"], "fixed_contracts")
    topology = _topology(raw["topology"], "topology")
    if (
        observed_source != expected["source_identity"]
        or features != expected["required_features"]
        or fixed != expected["fixed_contracts"]
        or topology != expected["topology_invariants"]
    ):
        raise ValueError("live model tree does not match the accepted structure adapter manifest")
    body = {
        "schema_name": STRUCTURE_TREE_AUDIT_SCHEMA_NAME,
        "schema_version": STRUCTURE_TREE_AUDIT_SCHEMA_VERSION,
        "manifest_fingerprint": expected["manifest_fingerprint"],
        "source_identity": observed_source,
        "features": features,
        "fixed_contracts": fixed,
        "topology": topology,
        "accepted": True,
    }
    fingerprint = domain_sha256_v2(STRUCTURE_TREE_AUDIT_SCHEMA_NAME, body)
    if supplied is not None and supplied != fingerprint:
        raise ValueError("structure tree audit fingerprint is invalid")
    return {**body, "audit_fingerprint": fingerprint}


def _snapshot(value: object, manifest: Mapping[str, Any]) -> dict[str, Any]:
    raw = _object(
        _bounded_json(value, "adapter snapshot", 256 * 1024),
        {
            "mesh_identity",
            "patch_position",
            "patch_size",
            "selection_identity",
            "topology",
        },
        "adapter snapshot",
    )
    size = raw["patch_size"]
    position = raw["patch_position"]
    if not isinstance(size, list) or len(size) != 2:
        raise ValueError("adapter snapshot patch_size must contain x and y")
    if not isinstance(position, list) or len(position) != 2:
        raise ValueError("adapter snapshot patch_position must contain x and y")
    normalized_size = [_finite(item, f"patch_size[{index}]") for index, item in enumerate(size)]
    normalized_position = [
        _finite(item, f"patch_position[{index}]") for index, item in enumerate(position)
    ]
    center = manifest["patch_center"]
    if any(
        not math.isclose(
            normalized_position[index] + normalized_size[index] / 2.0,
            center[index],
            rel_tol=0.0,
            abs_tol=1e-15,
        )
        for index in range(2)
    ):
        raise ValueError("adapter snapshot patch is not centered at the trusted fixed center")
    return {
        "patch_size": normalized_size,
        "patch_position": normalized_position,
        "topology": _topology(raw["topology"], "snapshot.topology"),
        "selection_identity": _sha(raw["selection_identity"], "selection_identity"),
        "mesh_identity": _sha(raw["mesh_identity"], "mesh_identity"),
    }


def adapter_state_sha256(manifest: object, snapshot: object) -> str:
    """Hash one complete mutable/readback state under its exact manifest."""
    normalized_manifest = normalize_structure_adapter_manifest(manifest)
    normalized_snapshot = _snapshot(snapshot, normalized_manifest)
    return str(
        domain_sha256_v2(
            "comsol_mcp.research_structure_adapter_state",
            {
                "manifest_fingerprint": normalized_manifest["manifest_fingerprint"],
                "snapshot": normalized_snapshot,
            },
        )
    )


def _candidate(value: object, manifest: Mapping[str, Any]) -> dict[str, float]:
    raw = _object(value, set(_VARIABLES), "adapter candidate")
    by_id = {item["variable_id"]: item for item in manifest["mutable_dimensions"]}
    result = {}
    for variable_id in _VARIABLES:
        number = _finite(raw[variable_id], variable_id)
        bounds = by_id[variable_id]
        if not bounds["lower"] <= number <= bounds["upper"]:
            raise ValueError(f"{variable_id} is outside the trusted adapter bounds")
        result[variable_id] = number
    return result


def apply_periodic_mim_patch_candidate(
    backend: PeriodicMimPatchBackend,
    manifest: object,
    tree_audit: object,
    candidate: object,
    *,
    expected_state_sha256: str,
) -> dict[str, Any]:
    """Apply one centered x/y candidate atomically to a derived model backend."""
    normalized_manifest = normalize_structure_adapter_manifest(manifest)
    normalized_audit = normalize_structure_tree_audit(tree_audit, normalized_manifest)
    expected_state = _sha(expected_state_sha256, "expected_state_sha256")
    values = _candidate(candidate, normalized_manifest)
    source_before = _sha(backend.source_sha256(), "backend.source_sha256")
    if source_before != normalized_manifest["source_identity"]["source_sha256"]:
        raise ValueError("backend source identity does not match the accepted manifest")
    before = _snapshot(backend.snapshot(), normalized_manifest)
    pre_state = adapter_state_sha256(normalized_manifest, before)
    if pre_state != expected_state:
        raise ValueError("stale expected_state_sha256")
    size = [values["patch_length_x"], values["patch_length_y"]]
    center = normalized_manifest["patch_center"]
    position = [center[index] - size[index] / 2.0 for index in range(2)]
    failure_code = None
    rollback_errors = []
    try:
        backend.apply_patch(size, position)
        backend.rebuild_geometry()
        topology = _topology(backend.reprobe_selections(), "observed topology")
        if topology != normalized_manifest["topology_invariants"]:
            raise ValueError("candidate changed the trusted topology invariants")
        mesh_identity = _sha(backend.rebuild_mesh(), "rebuilt mesh identity")
        after = _snapshot(backend.snapshot(), normalized_manifest)
        if after["patch_size"] != size or after["patch_position"] != position:
            raise ValueError("candidate readback does not match the requested patch geometry")
        if after["topology"] != topology or after["mesh_identity"] != mesh_identity:
            raise ValueError("candidate readback does not match rebuilt topology or mesh")
        source_after = _sha(backend.source_sha256(), "backend.source_sha256")
        if source_after != source_before:
            raise ValueError("immutable source changed during adapter application")
    except Exception as exc:
        failure_code = type(exc).__name__
        try:
            backend.restore(before)
            backend.rebuild_geometry()
            backend.reprobe_selections()
            backend.rebuild_mesh()
            restored = _snapshot(backend.snapshot(), normalized_manifest)
            if restored != before or backend.source_sha256() != source_before:
                rollback_errors.append("restored_state_mismatch")
        except Exception as rollback_exc:
            rollback_errors.append(type(rollback_exc).__name__)
        if rollback_errors:
            backend.mark_dirty("adapter_rollback_unproved")
        body = {
            "schema_name": STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME,
            "schema_version": STRUCTURE_ADAPTER_APPLICATION_SCHEMA_VERSION,
            "manifest_fingerprint": normalized_manifest["manifest_fingerprint"],
            "audit_fingerprint": normalized_audit["audit_fingerprint"],
            "candidate": values,
            "pre_state_sha256": pre_state,
            "post_state_sha256": None,
            "source_sha256": source_before,
            "success": False,
            "failure_code": failure_code,
            "rollback_proved": not rollback_errors,
            "rollback_errors": rollback_errors,
            "derived_model_dirty": bool(rollback_errors),
        }
        return {
            **body,
            "application_fingerprint": domain_sha256_v2(
                STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME, body
            ),
        }
    post_state = adapter_state_sha256(normalized_manifest, after)
    body = {
        "schema_name": STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME,
        "schema_version": STRUCTURE_ADAPTER_APPLICATION_SCHEMA_VERSION,
        "manifest_fingerprint": normalized_manifest["manifest_fingerprint"],
        "audit_fingerprint": normalized_audit["audit_fingerprint"],
        "candidate": values,
        "pre_state_sha256": pre_state,
        "post_state_sha256": post_state,
        "source_sha256": source_before,
        "success": True,
        "failure_code": None,
        "rollback_proved": None,
        "rollback_errors": [],
        "derived_model_dirty": False,
    }
    return {
        **body,
        "application_fingerprint": domain_sha256_v2(
            STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME, body
        ),
    }


__all__ = [
    "ClientapiPeriodicMimPatchBackend",
    "PERIODIC_MIM_PATCH_ADAPTER_ID",
    "STRUCTURE_ADAPTER_APPLICATION_SCHEMA_NAME",
    "STRUCTURE_ADAPTER_APPLICATION_SCHEMA_VERSION",
    "STRUCTURE_ADAPTER_MANIFEST_SCHEMA_NAME",
    "STRUCTURE_ADAPTER_MANIFEST_SCHEMA_VERSION",
    "STRUCTURE_TREE_AUDIT_SCHEMA_NAME",
    "STRUCTURE_TREE_AUDIT_SCHEMA_VERSION",
    "PeriodicMimPatchBackend",
    "adapter_state_sha256",
    "apply_periodic_mim_patch_candidate",
    "normalize_structure_adapter_manifest",
    "normalize_structure_tree_audit",
]
