"""Portable environment contract for controlled licensed COMSOL fixtures."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Mapping


MODEL_ENV = "COMSOL_REAL_TEST_MODEL"
WAVELENGTH_ENV = "COMSOL_REAL_TEST_WAVELENGTH_UM"
DOMAINS_ENV = "COMSOL_REAL_TEST_TOP_AIR_DOMAIN_IDS"
RANGE_ENV = "COMSOL_REAL_TEST_TOP_AIR_COORDINATE_RANGE"


def _positive_wavelength(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{WAVELENGTH_ENV} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{WAVELENGTH_ENV} must be finite and positive")
    return result


def _domains(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{DOMAINS_ENV} must be a non-empty JSON integer list")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in value):
        raise ValueError(f"{DOMAINS_ENV} must contain positive integers")
    if len(value) != len(set(value)):
        raise ValueError(f"{DOMAINS_ENV} must not contain duplicates")
    return sorted(value)


def _coordinate_range(value: Any) -> dict[str, list[float]]:
    if not isinstance(value, dict) or set(value) != {"x", "y", "z"}:
        raise ValueError(f"{RANGE_ENV} must contain exactly x, y, and z")
    result: dict[str, list[float]] = {}
    for axis in ("x", "y", "z"):
        bounds = value[axis]
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError(f"{RANGE_ENV}.{axis} must contain two numbers")
        low, high = (float(bounds[0]), float(bounds[1]))
        if not math.isfinite(low) or not math.isfinite(high) or low > high:
            raise ValueError(f"{RANGE_ENV}.{axis} is invalid")
        result[axis] = [low, high]
    return result


def controlled_fixture_from_environment(
    environment: Mapping[str, str] | None = None,
    *,
    verify_file: bool = True,
) -> dict[str, Any]:
    """Read one explicit local real-test fixture; never infer a private path."""
    values = environment if environment is not None else os.environ
    missing = [name for name in (MODEL_ENV, WAVELENGTH_ENV, DOMAINS_ENV, RANGE_ENV) if not values.get(name)]
    if missing:
        raise ValueError(f"controlled licensed fixture environment is incomplete: {missing}")
    source = Path(values[MODEL_ENV]).expanduser().resolve()
    if verify_file and not source.is_file():
        raise FileNotFoundError(source)
    try:
        domains_raw = json.loads(values[DOMAINS_ENV])
        range_raw = json.loads(values[RANGE_ENV])
    except json.JSONDecodeError as exc:
        raise ValueError("controlled licensed fixture metadata must be valid JSON") from exc
    return {
        "name": "current_controlled_fixture",
        "source": source,
        "wavelength_um": _positive_wavelength(values[WAVELENGTH_ENV]),
        "top_air_domain_ids": _domains(domains_raw),
        "top_air_coordinate_range": _coordinate_range(range_raw),
    }


def controlled_fixture_environment_from_h1_spec(
    spec_path: Path,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Translate a validated local H1 spec into subprocess-only fixture inputs."""
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    wavelength = raw.get("wavelength")
    reference = raw.get("reference_air")
    if not isinstance(wavelength, dict) or wavelength.get("unit") != "um":
        raise ValueError("licensed regression requires an H1 wavelength declared in um")
    if not isinstance(reference, dict):
        raise ValueError("licensed regression requires H1 reference_air metadata")
    environment = dict(base_environment if base_environment is not None else os.environ)
    environment.update(
        {
            MODEL_ENV: str(Path(str(raw.get("source_model_path", ""))).expanduser().resolve()),
            WAVELENGTH_ENV: format(_positive_wavelength(wavelength.get("value")), ".17g"),
            DOMAINS_ENV: json.dumps(_domains(reference.get("top_air_domain_ids")), separators=(",", ":")),
            RANGE_ENV: json.dumps(
                _coordinate_range(reference.get("top_air_coordinate_range")),
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )
    controlled_fixture_from_environment(environment, verify_file=True)
    return environment


__all__ = [
    "DOMAINS_ENV",
    "MODEL_ENV",
    "RANGE_ENV",
    "WAVELENGTH_ENV",
    "controlled_fixture_environment_from_h1_spec",
    "controlled_fixture_from_environment",
]
