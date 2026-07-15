"""Read-only locale-safe discovery of MPh dataset names, tags, and solutions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


FIELD_DATASET_DISCOVERY_SCHEMA = "comsol_mcp.field_dataset_discovery"
FIELD_DATASET_DISCOVERY_VERSION = "1.0.0"
MAX_DISCOVERED_DATASETS = 64
MAX_DISCOVERED_COMPONENTS = 32
MAX_DISCOVERY_TEXT = 4096

_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _bounded_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_DISCOVERY_TEXT:
        raise ValueError(f"{label} must be bounded nonempty text")
    return value.strip()


def _tag(value: object, label: str) -> str:
    text = _bounded_text(value, label)
    if not _TAG.fullmatch(text):
        raise ValueError(f"{label} must be one exact clientapi tag")
    return text


def _positive_limit(value: object, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value


def _children(model: Any, group: str) -> list[Any]:
    try:
        return list(model / group)
    except Exception as exc:
        raise ValueError(f"MPh {group} collection is unavailable") from exc


def discover_field_datasets(
    model: Any,
    *,
    max_datasets: int = MAX_DISCOVERED_DATASETS,
    max_components: int = MAX_DISCOVERED_COMPONENTS,
) -> dict[str, Any]:
    """Return bounded live name/tag pairs without evaluating or solving data."""
    dataset_limit = _positive_limit(
        max_datasets, "max_datasets", MAX_DISCOVERED_DATASETS
    )
    component_limit = _positive_limit(
        max_components, "max_components", MAX_DISCOVERED_COMPONENTS
    )
    components = _children(model, "components")
    datasets = _children(model, "datasets")
    solutions = _children(model, "solutions")
    if len(components) > component_limit:
        raise ValueError("component count exceeds the caller-declared discovery limit")
    if len(datasets) > dataset_limit:
        raise ValueError("dataset count exceeds the caller-declared discovery limit")

    component_rows = [
        {
            "component_name": _bounded_text(node.name(), f"components[{index}].name"),
            "component_tag": _tag(node.tag(), f"components[{index}].tag"),
        }
        for index, node in enumerate(components)
    ]
    if len({row["component_name"] for row in component_rows}) != len(component_rows):
        raise ValueError("component names must be unique")
    if len({row["component_tag"] for row in component_rows}) != len(component_rows):
        raise ValueError("component tags must be unique")

    solution_by_tag: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(solutions):
        tag = _tag(node.tag(), f"solutions[{index}].tag")
        if tag in solution_by_tag:
            raise ValueError("solution tags must be unique")
        try:
            empty = bool(node.java.isEmpty())
            computed_state = "verified_empty" if empty else "verified_computed"
        except Exception:
            computed_state = "unknown"
        solution_by_tag[tag] = {
            "solution_tag": tag,
            "solution_name": _bounded_text(node.name(), f"solutions[{index}].name"),
            "computed_state": computed_state,
        }

    dataset_rows: list[dict[str, Any]] = []
    for index, node in enumerate(datasets):
        name = _bounded_text(node.name(), f"datasets[{index}].name")
        tag = _tag(node.tag(), f"datasets[{index}].tag")
        dataset_type = _bounded_text(node.type(), f"datasets[{index}].type")
        try:
            properties = {str(item) for item in node.properties()}
        except Exception as exc:
            raise ValueError(f"datasets[{index}] properties are unavailable") from exc
        reference_kind = (
            "solution" if "solution" in properties else ("data" if "data" in properties else None)
        )
        solution_tag = None
        if reference_kind is not None:
            try:
                solution_tag = str(node.property(reference_kind))
            except Exception as exc:
                raise ValueError(
                    f"datasets[{index}] {reference_kind} property is unavailable"
                ) from exc
        solution = solution_by_tag.get(solution_tag) if solution_tag else None
        row = {
            "dataset_name": name,
            "dataset_tag": tag,
            "dataset_type": dataset_type,
            "solution_reference_kind": reference_kind,
            "solution_tag": solution_tag,
            "solution_name": solution["solution_name"] if solution else None,
            "computed_state": solution["computed_state"] if solution else "not_solution",
            "field_evaluation_eligible": bool(
                solution and solution["computed_state"] == "verified_computed"
            ),
        }
        row["dataset_identity_sha256"] = _canonical_hash(row)
        dataset_rows.append(row)
    if len({row["dataset_name"] for row in dataset_rows}) != len(dataset_rows):
        raise ValueError("dataset names must be unique")
    if len({row["dataset_tag"] for row in dataset_rows}) != len(dataset_rows):
        raise ValueError("dataset tags must be unique")

    result = {
        "schema_name": FIELD_DATASET_DISCOVERY_SCHEMA,
        "schema_version": FIELD_DATASET_DISCOVERY_VERSION,
        "components": component_rows,
        "datasets": dataset_rows,
        "component_count": len(component_rows),
        "dataset_count": len(dataset_rows),
        "eligible_dataset_count": sum(
            int(row["field_evaluation_eligible"]) for row in dataset_rows
        ),
        "model_mutated": False,
        "study_run": False,
        "locale_guidance": (
            "Use the discovered dataset_name for MPh evaluation and dataset_tag "
            "for clientapi identity; never infer one from the other."
        ),
    }
    result["discovery_sha256"] = _canonical_hash(result)
    return result


__all__ = [
    "FIELD_DATASET_DISCOVERY_SCHEMA",
    "FIELD_DATASET_DISCOVERY_VERSION",
    "MAX_DISCOVERED_COMPONENTS",
    "MAX_DISCOVERED_DATASETS",
    "discover_field_datasets",
]
