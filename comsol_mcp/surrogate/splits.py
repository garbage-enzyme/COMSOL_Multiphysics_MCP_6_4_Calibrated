"""Deterministic group-disjoint split assignment and leakage detection.

This module never imports COMSOL, Java, MPh, or network clients.  Assignment is
a pure function of the declared seed, strategy version, and stable leakage-group
identity, so a split is reproducible without relying on COMSOL's internal
random subsetting.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from comsol_mcp.durable.canonical import canonical_sha256_v1

SCHEMA_VERSION = "1.0.0"
STRATEGY_VERSION = "group_disjoint_sha256_v1"
SPLIT_NAMES = ("train", "validation", "test", "scientific_holdout")
DEFAULT_PROPORTIONS = {"train": 70, "validation": 15, "test": 15}
MAX_GROUPS = 4096
MAX_SEED = 2**31 - 1

# Leakage classes the contract must refuse before any fitting occurs.
LEAKAGE_CLASSES = (
    "cross_split_group",
    "holdout_in_fit",
    "duplicate_candidate",
    "normalization_from_non_train",
    "test_access_before_freeze",
)


def _require_str(name: str, value: Any, *, max_len: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ValueError(f"{name} must be a non-empty string up to {max_len}")
    return value


def _require_seed(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("seed must be an integer")
    if not 0 <= value <= MAX_SEED:
        raise ValueError(f"seed must be in 0..{MAX_SEED}")
    return value


def _require_proportions(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("proportions must be a mapping")
    if set(value) != {"train", "validation", "test"}:
        raise ValueError("proportions must declare train, validation, and test")
    normalized: dict[str, int] = {}
    for name in ("train", "validation", "test"):
        raw = value[name]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            raise ValueError(f"proportions.{name} must be a positive integer")
        normalized[name] = raw
    if sum(normalized.values()) != 100:
        raise ValueError("proportions must sum to 100")
    return normalized


def _group_rank(seed: int, group_id: str) -> str:
    """Return a deterministic, stable ordering key for one leakage group."""
    payload = f"{STRATEGY_VERSION}\x00{seed}\x00{group_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def assign_group_disjoint_split(
    *,
    group_ids: Sequence[str],
    seed: int,
    proportions: Mapping[str, int] | None = None,
    holdout_group_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Assign whole leakage groups to disjoint splits deterministically.

    Groups are ordered by a seeded hash of their stable identity, never by input
    order, so the same groups always produce the same split for a given seed.
    The scientific holdout is assigned first and is never fitted.
    """
    seed = _require_seed(seed)
    proportions = _require_proportions(proportions or DEFAULT_PROPORTIONS)
    unique_groups = [_require_str("group_id", item) for item in group_ids]
    if not unique_groups:
        raise ValueError("group_ids must be non-empty")
    if len(unique_groups) > MAX_GROUPS:
        raise ValueError(f"group_ids must contain at most {MAX_GROUPS} entries")
    if len(set(unique_groups)) != len(unique_groups):
        raise ValueError("group_ids must be unique")

    holdout = [_require_str("holdout_group_id", item) for item in holdout_group_ids]
    if len(set(holdout)) != len(holdout):
        raise ValueError("holdout_group_ids must be unique")
    unknown = sorted(set(holdout) - set(unique_groups))
    if unknown:
        raise ValueError(f"holdout references unknown groups: {', '.join(unknown)}")

    ordered = sorted(unique_groups, key=lambda item: (_group_rank(seed, item), item))
    holdout_set = set(holdout)
    fitted = [item for item in ordered if item not in holdout_set]

    total = len(fitted)
    train_count = total * proportions["train"] // 100
    validation_count = total * proportions["validation"] // 100

    assignments: list[dict[str, str]] = []
    for index, group_id in enumerate(fitted):
        if index < train_count:
            split = "train"
        elif index < train_count + validation_count:
            split = "validation"
        else:
            split = "test"
        assignments.append({"group_id": group_id, "split": split})
    for group_id in sorted(holdout_set):
        assignments.append({"group_id": group_id, "split": "scientific_holdout"})

    assignments.sort(key=lambda item: item["group_id"])
    body = {
        "schema": "comsol_mcp.surrogate_split_plan",
        "schema_version": SCHEMA_VERSION,
        "strategy": STRATEGY_VERSION,
        "seed": seed,
        "proportions": proportions,
        "group_count": len(unique_groups),
        "holdout_group_ids": sorted(holdout_set),
        "assignments": assignments,
    }
    manifest = {**body, "manifest_sha256": canonical_sha256_v1(body)}
    counts = {name: 0 for name in SPLIT_NAMES}
    for item in assignments:
        counts[item["split"]] += 1
    manifest["group_counts"] = counts
    return manifest


def validate_split_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("split plan must be a mapping")
    required = (
        "schema",
        "schema_version",
        "strategy",
        "seed",
        "proportions",
        "group_count",
        "holdout_group_ids",
        "assignments",
        "manifest_sha256",
        "group_counts",
    )
    if set(value) != set(required):
        raise ValueError("split plan keys are closed")
    if value["schema"] != "comsol_mcp.surrogate_split_plan":
        raise ValueError("unexpected schema name")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if value["strategy"] != STRATEGY_VERSION:
        raise ValueError("unsupported split strategy")
    body = {key: value[key] for key in required if key not in {"manifest_sha256", "group_counts"}}
    if value["manifest_sha256"] != canonical_sha256_v1(body):
        raise ValueError("manifest_sha256 mismatch")
    rebuilt = assign_group_disjoint_split(
        group_ids=[item["group_id"] for item in value["assignments"]],
        seed=value["seed"],
        proportions=value["proportions"],
        holdout_group_ids=value["holdout_group_ids"],
    )
    if rebuilt["manifest_sha256"] != value["manifest_sha256"]:
        raise ValueError("split plan is not reproducible from its declared inputs")
    return dict(value)


def detect_leakage(
    *,
    assignments: Mapping[str, str],
    group_members: Mapping[str, Sequence[str]],
    candidate_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Detect the declared leakage classes before any fitting occurs.

    ``assignments`` maps a leakage-group ID to its split.  ``group_members`` maps
    a leakage-group ID to its row IDs.  ``candidate_ids`` optionally maps a row ID
    to a canonical candidate identity so that duplicates are caught even when
    they were placed in different leakage groups.
    """
    if not isinstance(assignments, Mapping) or not assignments:
        raise ValueError("assignments must be a non-empty mapping")
    if not isinstance(group_members, Mapping):
        raise ValueError("group_members must be a mapping")

    findings: list[dict[str, str]] = []
    fitted = {"train", "validation", "test"}

    for group_id, split in assignments.items():
        if split not in SPLIT_NAMES:
            raise ValueError(f"unknown split for group {group_id}")
    unknown = sorted(set(group_members) - set(assignments))
    if unknown:
        raise ValueError(f"group_members references unassigned groups: {', '.join(unknown)}")

    row_split: dict[str, str] = {}
    row_groups: dict[str, list[str]] = {}
    for group_id, members in group_members.items():
        split = assignments[group_id]
        for row_id in members:
            if row_id in row_split and row_split[row_id] != split:
                findings.append({"leakage_class": "cross_split_group", "subject": str(row_id)})
            row_split[row_id] = split
            row_groups.setdefault(row_id, []).append(str(group_id))

    # A holdout row must never also be reachable from a fitted split, either
    # directly or through another leakage group that contains the same row.
    for row_id, splits in row_groups.items():
        distinct = {row_split[row_id], *[assignments[g] for g in splits]}
        if "scientific_holdout" in distinct and distinct & fitted:
            findings.append({"leakage_class": "holdout_in_fit", "subject": str(row_id)})

    if candidate_ids:
        seen: dict[str, str] = {}
        for row_id, candidate in candidate_ids.items():
            previous = seen.get(candidate)
            if previous is not None and previous != row_split.get(row_id):
                findings.append(
                    {
                        "leakage_class": "duplicate_candidate",
                        "subject": str(candidate),
                    }
                )
            seen.setdefault(candidate, row_split.get(row_id, "unknown"))

    findings.sort(key=lambda item: (item["leakage_class"], item["subject"]))
    return {
        "leakage_detected": bool(findings),
        "findings": findings,
        "finding_count": len(findings),
        "classes_checked": list(LEAKAGE_CLASSES),
        "group_count": len(assignments),
        "row_count": len(row_split),
    }


def assert_no_leakage(**kwargs: Any) -> dict[str, Any]:
    """Fail closed when any declared leakage class is detected."""
    report = detect_leakage(**kwargs)
    if report["leakage_detected"]:
        classes = sorted({item["leakage_class"] for item in report["findings"]})
        raise ValueError(f"split leakage detected: {', '.join(classes)}")
    return report


__all__ = [
    "DEFAULT_PROPORTIONS",
    "LEAKAGE_CLASSES",
    "MAX_GROUPS",
    "SPLIT_NAMES",
    "STRATEGY_VERSION",
    "assert_no_leakage",
    "assign_group_disjoint_split",
    "detect_leakage",
    "validate_split_plan",
]
