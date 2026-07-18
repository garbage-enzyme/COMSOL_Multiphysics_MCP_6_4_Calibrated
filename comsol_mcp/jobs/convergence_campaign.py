"""Pure normalization for bounded durable convergence campaigns."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

from comsol_mcp.build_identity import get_build_identity
from comsol_mcp.compatibility import module_identity_matches
from comsol_mcp.evidence.convergence_evaluation import MAX_CONVERGENCE_LEVELS

from .spectral_characterization import normalize_spectral_characterization_job_spec
from .store import JOB_SCHEMA_VERSION


MAX_CONVERGENCE_CAMPAIGN_LEVELS = min(MAX_CONVERGENCE_LEVELS, 8)
MAX_CONVERGENCE_CAMPAIGN_POINTS = 512
MAX_CONVERGENCE_CAMPAIGN_SPEC_BYTES = 2 * 1024 * 1024
MAX_CONVERGENCE_CAMPAIGN_WALL_SECONDS = 30 * 24 * 60 * 60
CONVERGENCE_CAMPAIGN_DRIVER_VERSION = "1.0.0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_BUILTIN_METRIC_UNITS = {
    "peak_wavelength_m": "m",
    "peak_response_value": "1",
    "fwhm_m": "m",
    "quality_factor": "1",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return dict(value)


def _exact_mapping(value: object, fields: set[str], name: str) -> dict[str, Any]:
    item = _mapping(value, name)
    if set(item) != fields:
        raise ValueError(f"{name} requires exactly: {', '.join(sorted(fields))}")
    return item


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be one bounded identifier")
    return value


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be exactly 64 hexadecimal characters")
    return value.lower()


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _nonnegative_optional(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be nonnegative and finite when provided")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name} must be nonnegative and finite when provided")
    return number


def _normalize_campaign_policy(value: object) -> dict[str, Any]:
    raw = _exact_mapping(
        value,
        {
            "policy_id", "metrics", "minimum_level_count", "governing_pairs",
            "relative_denominator", "declared_cap_reached",
        },
        "convergence_policy",
    )
    metrics = raw["metrics"]
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= 32:
        raise ValueError("convergence_policy.metrics must be a bounded nonempty list")
    normalized_metrics = []
    for index, value in enumerate(metrics):
        name = f"convergence_policy.metrics[{index}]"
        rule = _exact_mapping(
            value,
            {"metric", "unit", "absolute_tolerance", "relative_tolerance"},
            name,
        )
        metric = _identifier(rule["metric"], f"{name}.metric")
        if metric not in _BUILTIN_METRIC_UNITS:
            raise ValueError(f"{name}.metric is unsupported for a durable campaign")
        if rule["unit"] != _BUILTIN_METRIC_UNITS[metric]:
            raise ValueError(f"{name}.unit does not match the selected metric")
        absolute = _nonnegative_optional(rule["absolute_tolerance"], f"{name}.absolute_tolerance")
        relative = _nonnegative_optional(rule["relative_tolerance"], f"{name}.relative_tolerance")
        if absolute is None and relative is None:
            raise ValueError(f"{name} must declare an absolute and/or relative tolerance")
        normalized_metrics.append(
            {
                "metric": metric,
                "unit": rule["unit"],
                "absolute_tolerance": absolute,
                "relative_tolerance": relative,
            }
        )
    names = [item["metric"] for item in normalized_metrics]
    if len(names) != len(set(names)):
        raise ValueError("convergence_policy metrics must be unique")
    if raw["governing_pairs"] not in {"all_adjacent", "final_pair"}:
        raise ValueError("convergence_policy.governing_pairs is unsupported")
    if raw["relative_denominator"] not in {"previous_abs", "maximum_abs"}:
        raise ValueError("convergence_policy.relative_denominator is unsupported")
    if not isinstance(raw["declared_cap_reached"], bool):
        raise ValueError("convergence_policy.declared_cap_reached must be boolean")
    return {
        "policy_id": _identifier(raw["policy_id"], "convergence_policy.policy_id"),
        "metrics": normalized_metrics,
        "minimum_level_count": _integer(
            raw["minimum_level_count"],
            "convergence_policy.minimum_level_count",
            minimum=2,
            maximum=MAX_CONVERGENCE_CAMPAIGN_LEVELS,
        ),
        "governing_pairs": raw["governing_pairs"],
        "relative_denominator": raw["relative_denominator"],
        "declared_cap_reached": raw["declared_cap_reached"],
    }


def current_convergence_campaign_driver_identity() -> dict[str, str]:
    """Bind campaign resume to the exact package bytes and driver contract."""
    build = get_build_identity()
    return {
        "implementation": "comsol_mcp.jobs.convergence_campaign_worker",
        "driver_version": CONVERGENCE_CAMPAIGN_DRIVER_VERSION,
        "package_content_sha256": build["package_content_sha256"],
        "build_identity_sha256": build["build_identity_sha256"],
    }


def validate_convergence_campaign_driver_identity(spec: Mapping[str, Any]) -> dict[str, str]:
    observed = spec.get("driver_identity")
    expected = current_convergence_campaign_driver_identity()
    if (
        not isinstance(observed, Mapping)
        or set(observed) != set(expected)
        or any(
            key != "implementation" and observed[key] != expected[key]
            for key in expected
        )
        or not module_identity_matches(
            expected.get("implementation"), observed.get("implementation")
        )
    ):
        raise ValueError("convergence campaign driver identity differs from the running package")
    return expected


def normalize_convergence_campaign_spec(value: object) -> dict[str, Any]:
    """Normalize one immutable exact-model configuration ladder."""
    raw = _exact_mapping(
        value,
        {
            "job_type", "campaign_id", "levels", "convergence_policy",
            "stop_policy", "maximum_total_points", "wall_time_budget_seconds",
        },
        "convergence campaign specification",
    )
    if raw["job_type"] != "convergence_campaign":
        raise ValueError("job_type must be convergence_campaign")
    levels = raw["levels"]
    if not isinstance(levels, list) or not 2 <= len(levels) <= MAX_CONVERGENCE_CAMPAIGN_LEVELS:
        raise ValueError(f"levels must contain 2 to {MAX_CONVERGENCE_CAMPAIGN_LEVELS} entries")
    normalized_levels = []
    for index, value in enumerate(levels):
        name = f"levels[{index}]"
        level = _exact_mapping(
            value,
            {
                "level_id", "ordinal", "declared_predecessor_level_id",
                "model_preparation", "material_identity_sha256",
                "incidence_identity_sha256", "spectral_job",
            },
            name,
        )
        level_id = _identifier(level["level_id"], f"{name}.level_id")
        if level["ordinal"] != index:
            raise ValueError(f"{name}.ordinal must equal its declared ladder position")
        predecessor = None if index == 0 else normalized_levels[-1]["level_id"]
        if level["declared_predecessor_level_id"] != predecessor:
            raise ValueError(f"{name} adjacency does not match the preceding level")
        preparation = _exact_mapping(
            level["model_preparation"], {"mode"}, f"{name}.model_preparation"
        )
        if preparation["mode"] != "exact_model":
            raise ValueError(f"{name}.model_preparation.mode must be exact_model")
        spectral = normalize_spectral_characterization_job_spec(level["spectral_job"])
        normalized_levels.append(
            {
                "level_id": level_id,
                "ordinal": index,
                "declared_predecessor_level_id": predecessor,
                "model_preparation": {"mode": "exact_model"},
                "material_identity_sha256": _sha256(
                    level["material_identity_sha256"], f"{name}.material_identity_sha256"
                ),
                "incidence_identity_sha256": _sha256(
                    level["incidence_identity_sha256"], f"{name}.incidence_identity_sha256"
                ),
                "spectral_job": spectral,
            }
        )
    level_ids = [item["level_id"] for item in normalized_levels]
    source_hashes = [item["spectral_job"]["source_model_sha256"] for item in normalized_levels]
    configurations = [item["spectral_job"]["configuration_sha256"] for item in normalized_levels]
    if len(level_ids) != len(set(level_ids)):
        raise ValueError("convergence campaign level identifiers must be unique")
    if len(source_hashes) != len(set(source_hashes)):
        raise ValueError("exact-model convergence levels require distinct source model bytes")
    if len(configurations) != len(set(configurations)):
        raise ValueError("convergence campaign configuration identities must be unique")
    if len({item["material_identity_sha256"] for item in normalized_levels}) != 1:
        raise ValueError("convergence campaign material identity must be constant")
    if len({item["incidence_identity_sha256"] for item in normalized_levels}) != 1:
        raise ValueError("convergence campaign incidence identity must be constant")
    if len({item["spectral_job"]["cores"] for item in normalized_levels}) != 1:
        raise ValueError("convergence campaign levels must use one core allocation")
    if len({item["spectral_job"]["version"] for item in normalized_levels}) != 1:
        raise ValueError("convergence campaign levels must use one COMSOL version request")

    policy = _normalize_campaign_policy(raw["convergence_policy"])
    if policy["minimum_level_count"] > len(normalized_levels):
        raise ValueError("convergence policy minimum exceeds the declared ladder")
    stop = _exact_mapping(
        raw["stop_policy"],
        {"allow_early_acceptance", "minimum_completed_levels"},
        "stop_policy",
    )
    if not isinstance(stop["allow_early_acceptance"], bool):
        raise ValueError("stop_policy.allow_early_acceptance must be boolean")
    minimum_completed = _integer(
        stop["minimum_completed_levels"],
        "stop_policy.minimum_completed_levels",
        minimum=2,
        maximum=len(normalized_levels),
    )
    if minimum_completed < policy["minimum_level_count"]:
        raise ValueError("stop policy minimum cannot precede the convergence policy minimum")
    total_points = sum(item["spectral_job"]["maximum_points"] for item in normalized_levels)
    maximum_total_points = _integer(
        raw["maximum_total_points"],
        "maximum_total_points",
        minimum=total_points,
        maximum=MAX_CONVERGENCE_CAMPAIGN_POINTS,
    )
    wall_time_budget = _integer(
        raw["wall_time_budget_seconds"],
        "wall_time_budget_seconds",
        minimum=1,
        maximum=MAX_CONVERGENCE_CAMPAIGN_WALL_SECONDS,
    )
    child_wall_budget = sum(
        item["spectral_job"]["resource_policy"]["rules"]["wall_time_budget_seconds"]
        for item in normalized_levels
    )
    if wall_time_budget < child_wall_budget:
        raise ValueError("campaign wall-time budget is smaller than its declared level budgets")

    spec = {
        "job_type": "convergence_campaign",
        "schema_version": JOB_SCHEMA_VERSION,
        "campaign_id": _identifier(raw["campaign_id"], "campaign_id"),
        "levels": normalized_levels,
        "convergence_policy": policy,
        "stop_policy": {
            "allow_early_acceptance": stop["allow_early_acceptance"],
            "minimum_completed_levels": minimum_completed,
        },
        "maximum_total_points": maximum_total_points,
        "wall_time_budget_seconds": wall_time_budget,
        "declared_level_count": len(normalized_levels),
        "declared_point_count": total_points,
        "driver_identity": current_convergence_campaign_driver_identity(),
    }
    if len(_canonical_bytes(spec)) > MAX_CONVERGENCE_CAMPAIGN_SPEC_BYTES:
        raise ValueError(f"convergence campaign exceeds {MAX_CONVERGENCE_CAMPAIGN_SPEC_BYTES} bytes")
    spec["spec_fingerprint"] = _fingerprint(spec)
    return spec


__all__ = [
    "CONVERGENCE_CAMPAIGN_DRIVER_VERSION",
    "MAX_CONVERGENCE_CAMPAIGN_LEVELS",
    "MAX_CONVERGENCE_CAMPAIGN_POINTS",
    "current_convergence_campaign_driver_identity",
    "normalize_convergence_campaign_spec",
    "validate_convergence_campaign_driver_identity",
]
