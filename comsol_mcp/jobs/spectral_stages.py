"""Immutable stage plans for durable adaptive spectral characterization."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping

from .spectral_characterization import MAX_INITIAL_GRID_POINTS
from .spectral_rows import (
    SPECTRAL_STAGE_KINDS,
    normalize_spectral_wavelength_m,
    spectral_point_identity,
)
from .store import JobLock, atomic_write_json, read_json

SPECTRAL_STAGE_SCHEMA_NAME = "comsol_mcp.spectral_stage_plan"
SPECTRAL_STAGE_SCHEMA_VERSION = "1.0.0"
MAX_SPECTRAL_STAGE_PLANS = 17
MAX_SPECTRAL_STAGE_PLAN_BYTES = 256 * 1024


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    return dict(value)


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{name} must be {qualifier}")
    return number


def _hex_or_none(value: object, name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value.lower())
    ):
        raise ValueError(f"{name} must be null or exactly 64 hexadecimal characters")
    return value.lower()


def inclusive_wavelength_grid(lower_m: object, upper_m: object, point_count: object) -> list[float]:
    """Return one deterministic finite inclusive wavelength grid."""
    lower = _finite(lower_m, "lower_m", positive=True)
    upper = _finite(upper_m, "upper_m", positive=True)
    if upper <= lower:
        raise ValueError("upper_m must exceed lower_m")
    if isinstance(point_count, bool) or not isinstance(point_count, int) or point_count < 2:
        raise ValueError("point_count must be an integer of at least 2")
    with localcontext() as context:
        context.prec = 40
        decimal_lower = Decimal(str(lower))
        decimal_upper = Decimal(str(upper))
        decimal_span = decimal_upper - decimal_lower
        values = [
            normalize_spectral_wavelength_m(
                float(decimal_lower + decimal_span * index / (point_count - 1))
            )
            for index in range(point_count)
        ]
    values[0] = normalize_spectral_wavelength_m(lower)
    values[-1] = normalize_spectral_wavelength_m(upper)
    if any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise ValueError("wavelength grid contains a nonpositive or nonfinite value")
    if any(right <= left for left, right in zip(values, values[1:])):
        raise ValueError("wavelength grid is not strictly increasing at float precision")
    return values


def build_spectral_stage_plan(
    spec: Mapping[str, Any],
    *,
    stage_index: int,
    stage_kind: str,
    planning_reason: str,
    window_lower_m: float,
    window_upper_m: float,
    requested_wavelengths_m: list[float],
    previous_stage_sha256: str | None,
    evidence_row_sha256: str | None,
) -> dict[str, Any]:
    """Build one canonical stage request whose targets can be frozen before solving."""
    if spec.get("job_type") != "spectral_characterization":
        raise ValueError("stage plans require a spectral_characterization job")
    if isinstance(stage_index, bool) or not isinstance(stage_index, int) or stage_index < 0:
        raise ValueError("stage_index must be a nonnegative integer")
    if stage_kind not in SPECTRAL_STAGE_KINDS:
        raise ValueError("stage_kind is unsupported")
    if not isinstance(planning_reason, str) or not planning_reason or len(planning_reason) > 128:
        raise ValueError("planning_reason must be a bounded nonempty string")
    lower = normalize_spectral_wavelength_m(
        _finite(window_lower_m, "window_lower_m", positive=True)
    )
    upper = normalize_spectral_wavelength_m(
        _finite(window_upper_m, "window_upper_m", positive=True)
    )
    if upper <= lower:
        raise ValueError("stage window upper bound must exceed its lower bound")
    if not isinstance(requested_wavelengths_m, list) or not requested_wavelengths_m:
        raise ValueError("requested_wavelengths_m must be a nonempty list")
    wavelengths = [
        normalize_spectral_wavelength_m(
            _finite(value, f"requested_wavelengths_m[{index}]", positive=True)
        )
        for index, value in enumerate(requested_wavelengths_m)
    ]
    if any(right <= left for left, right in zip(wavelengths, wavelengths[1:])):
        raise ValueError("requested wavelengths must be sorted and unique")
    if wavelengths[0] < lower or wavelengths[-1] > upper:
        raise ValueError("requested wavelengths must remain inside the stage window")
    if len(wavelengths) > int(spec["maximum_points"]):
        raise ValueError("stage request exceeds the declared total point cap")
    points = [spectral_point_identity(spec, wavelength) for wavelength in wavelengths]
    body = {
        "schema_name": SPECTRAL_STAGE_SCHEMA_NAME,
        "schema_version": SPECTRAL_STAGE_SCHEMA_VERSION,
        "spec_fingerprint": spec["spec_fingerprint"],
        "stage_index": stage_index,
        "stage_kind": stage_kind,
        "planning_reason": planning_reason,
        "window": {"lower_m": lower, "upper_m": upper},
        "requested_wavelengths_m": wavelengths,
        "requested_points": [
            {
                "point_id": point["point_id"],
                "point_fingerprint": point["point_fingerprint"],
            }
            for point in points
        ],
        "previous_stage_sha256": _hex_or_none(
            previous_stage_sha256, "previous_stage_sha256"
        ),
        "evidence_row_sha256": _hex_or_none(
            evidence_row_sha256, "evidence_row_sha256"
        ),
    }
    encoded = _canonical_bytes(body)
    if len(encoded) > MAX_SPECTRAL_STAGE_PLAN_BYTES:
        raise ValueError("spectral stage plan exceeds its byte limit")
    return {**body, "stage_sha256": _fingerprint(body)}


def build_initial_spectral_stage(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Build the exact initial locator request from the immutable job spec."""
    grid = _mapping(spec.get("initial_grid"), "initial_grid")
    point_count = grid.get("point_count")
    maximum_points = spec.get("maximum_points")
    if (
        isinstance(point_count, bool)
        or not isinstance(point_count, int)
        or point_count < 2
        or point_count > MAX_INITIAL_GRID_POINTS
    ):
        raise ValueError(
            f"initial_grid.point_count must be an integer from 2 to {MAX_INITIAL_GRID_POINTS}"
        )
    if (
        isinstance(maximum_points, bool)
        or not isinstance(maximum_points, int)
        or maximum_points < point_count
    ):
        raise ValueError("maximum_points must cover the initial grid")
    wavelengths = inclusive_wavelength_grid(
        grid.get("lower_m"), grid.get("upper_m"), point_count
    )
    return build_spectral_stage_plan(
        spec,
        stage_index=0,
        stage_kind="initial_locator",
        planning_reason="caller_declared_initial_locator",
        window_lower_m=grid["lower_m"],
        window_upper_m=grid["upper_m"],
        requested_wavelengths_m=wavelengths,
        previous_stage_sha256=None,
        evidence_row_sha256=None,
    )


def validate_spectral_stage_plan(
    value: object,
    spec: Mapping[str, Any],
    *,
    expected_index: int,
    previous_stage_sha256: str | None,
) -> dict[str, Any]:
    """Validate one canonical stage plan and its chain position."""
    raw = _mapping(value, f"spectral stage {expected_index}")
    fields = {
        "schema_name",
        "schema_version",
        "spec_fingerprint",
        "stage_index",
        "stage_kind",
        "planning_reason",
        "window",
        "requested_wavelengths_m",
        "requested_points",
        "previous_stage_sha256",
        "evidence_row_sha256",
        "stage_sha256",
    }
    if set(raw) != fields:
        raise ValueError("spectral stage plan has missing or unsupported fields")
    if raw["schema_name"] != SPECTRAL_STAGE_SCHEMA_NAME or raw["schema_version"] != SPECTRAL_STAGE_SCHEMA_VERSION:
        raise ValueError("spectral stage plan schema is unsupported")
    if raw["stage_index"] != expected_index:
        raise ValueError("spectral stage indices are not contiguous")
    if raw["previous_stage_sha256"] != previous_stage_sha256:
        raise ValueError("spectral stage hash chain is discontinuous")
    window = _mapping(raw["window"], "stage window")
    if set(window) != {"lower_m", "upper_m"}:
        raise ValueError("spectral stage window fields are invalid")
    rebuilt = build_spectral_stage_plan(
        spec,
        stage_index=raw["stage_index"],
        stage_kind=raw["stage_kind"],
        planning_reason=raw["planning_reason"],
        window_lower_m=window["lower_m"],
        window_upper_m=window["upper_m"],
        requested_wavelengths_m=raw["requested_wavelengths_m"],
        previous_stage_sha256=raw["previous_stage_sha256"],
        evidence_row_sha256=raw["evidence_row_sha256"],
    )
    if raw != rebuilt:
        raise ValueError("spectral stage plan is noncanonical or its hash does not match")
    return deepcopy(rebuilt)


def _read_spectral_stage_plans_unlocked(
    job_dir: str | Path, spec: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Read one contiguous immutable stage chain from its durable directory."""
    root = Path(job_dir) / "stage_plans"
    if not root.exists():
        return []
    paths = sorted(root.glob("*.json"))
    if len(paths) > MAX_SPECTRAL_STAGE_PLANS:
        raise ValueError("spectral stage plan count exceeds its server cap")
    expected_names = [f"{index:03d}.json" for index in range(len(paths))]
    if [path.name for path in paths] != expected_names:
        raise ValueError("spectral stage plan filenames are not contiguous")
    plans: list[dict[str, Any]] = []
    previous: str | None = None
    seen_points: set[str] = set()
    for index, path in enumerate(paths):
        if path.stat().st_size > MAX_SPECTRAL_STAGE_PLAN_BYTES:
            raise ValueError("spectral stage plan exceeds its byte limit")
        plan = validate_spectral_stage_plan(
            read_json(path),
            spec,
            expected_index=index,
            previous_stage_sha256=previous,
        )
        if index == 0 and plan != build_initial_spectral_stage(spec):
            raise ValueError("first spectral stage differs from the immutable initial grid")
        fingerprints = {
            point["point_fingerprint"] for point in plan["requested_points"]
        }
        if fingerprints & seen_points:
            raise ValueError("spectral stage plans request a duplicate exact point")
        seen_points.update(fingerprints)
        if len(seen_points) > int(spec["maximum_points"]):
            raise ValueError("spectral stage chain exceeds the declared point cap")
        plans.append(plan)
        previous = plan["stage_sha256"]
    return plans


def read_spectral_stage_plans(job_dir: str | Path, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read one contiguous immutable stage chain under its publication lock."""
    root = Path(job_dir)
    root.mkdir(parents=True, exist_ok=True)
    with JobLock(root / ".stage_plans.lock"):
        return _read_spectral_stage_plans_unlocked(root, spec)


def write_spectral_stage_plan(
    job_dir: str | Path,
    spec: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically freeze one next stage; exact replay observes the existing bytes."""
    root = Path(job_dir)
    root.mkdir(parents=True, exist_ok=True)
    with JobLock(root / ".stage_plans.lock"):
        existing = _read_spectral_stage_plans_unlocked(root, spec)
        requested_index = plan.get("stage_index")
        if isinstance(requested_index, bool) or not isinstance(requested_index, int):
            raise ValueError("spectral stage index is invalid")
        if requested_index > len(existing):
            raise ValueError("spectral stage index is not contiguous")
        previous = (
            existing[requested_index - 1]["stage_sha256"]
            if requested_index > 0
            else None
        )
        normalized = validate_spectral_stage_plan(
            plan,
            spec,
            expected_index=requested_index,
            previous_stage_sha256=previous,
        )
        if requested_index < len(existing):
            observed = existing[requested_index]
            if observed != normalized:
                raise ValueError("existing spectral stage bytes differ from the requested plan")
            return normalized
        target = root / "stage_plans" / f"{requested_index:03d}.json"
        atomic_write_json(target, normalized)
        replayed = _read_spectral_stage_plans_unlocked(root, spec)
        if replayed[-1] != normalized:
            raise RuntimeError("spectral stage did not replay after its atomic write")
        return normalized


__all__ = [
    "MAX_SPECTRAL_STAGE_PLANS",
    "SPECTRAL_STAGE_SCHEMA_NAME",
    "SPECTRAL_STAGE_SCHEMA_VERSION",
    "build_initial_spectral_stage",
    "build_spectral_stage_plan",
    "inclusive_wavelength_grid",
    "read_spectral_stage_plans",
    "validate_spectral_stage_plan",
    "write_spectral_stage_plan",
]
