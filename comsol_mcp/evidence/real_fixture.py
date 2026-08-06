"""Portable environment contract for controlled licensed COMSOL fixtures."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.utils.validation import strict_json_number


MODEL_ENV = "COMSOL_REAL_TEST_MODEL"
WAVELENGTH_ENV = "COMSOL_REAL_TEST_WAVELENGTH_UM"
DOMAINS_ENV = "COMSOL_REAL_TEST_TOP_AIR_DOMAIN_IDS"
RANGE_ENV = "COMSOL_REAL_TEST_TOP_AIR_COORDINATE_RANGE"
SOURCE_SHA256_ENV = "COMSOL_REAL_TEST_SOURCE_SHA256"


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_sha256(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{SOURCE_SHA256_ENV} must be a lowercase SHA-256 digest")
    return value


def _positive_wavelength(value: Any) -> float:
    return strict_json_number(value, WAVELENGTH_ENV, positive=True)


def _environment_wavelength(value: Any) -> float:
    if not isinstance(value, str):
        raise ValueError(f"{WAVELENGTH_ENV} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{WAVELENGTH_ENV} must be numeric") from exc
    return _positive_wavelength(parsed)


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
        low = strict_json_number(bounds[0], f"{RANGE_ENV}.{axis}[0]")
        high = strict_json_number(bounds[1], f"{RANGE_ENV}.{axis}[1]")
        if low > high:
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
    missing = [
        name
        for name in (MODEL_ENV, SOURCE_SHA256_ENV, WAVELENGTH_ENV, DOMAINS_ENV, RANGE_ENV)
        if not values.get(name)
    ]
    if missing:
        raise ValueError(f"controlled licensed fixture environment is incomplete: {missing}")
    source_value = values[MODEL_ENV]
    if not isinstance(source_value, str) or not source_value.strip():
        raise ValueError(f"{MODEL_ENV} must be a non-empty absolute path")
    source_candidate = Path(source_value).expanduser()
    if not source_candidate.is_absolute():
        raise ValueError(f"{MODEL_ENV} must be an absolute path")
    source = source_candidate.resolve()
    if verify_file and not source.is_file():
        raise FileNotFoundError(source)
    expected_source_sha256 = _source_sha256(values[SOURCE_SHA256_ENV])
    if verify_file and _sha256_file(source) != expected_source_sha256:
        raise ValueError("controlled licensed fixture source SHA-256 mismatch")
    try:
        domains_raw = json.loads(values[DOMAINS_ENV])
        range_raw = json.loads(values[RANGE_ENV])
    except json.JSONDecodeError as exc:
        raise ValueError("controlled licensed fixture metadata must be valid JSON") from exc
    return {
        "name": "current_controlled_fixture",
        "source": source,
        "expected_source_sha256": expected_source_sha256,
        "wavelength_um": _environment_wavelength(values[WAVELENGTH_ENV]),
        "top_air_domain_ids": _domains(domains_raw),
        "top_air_coordinate_range": _coordinate_range(range_raw),
    }


def controlled_fixture_environment_from_reference_power_spec(
    spec_path: Path,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Translate a validated local reference-power spec into subprocess-only fixture inputs."""
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("reference-power spec must be a JSON object")
    wavelength = raw.get("wavelength")
    reference = raw.get("reference_air")
    if not isinstance(wavelength, dict) or wavelength.get("unit") != "um":
        raise ValueError("licensed regression requires a reference-power wavelength declared in um")
    if not isinstance(reference, dict):
        raise ValueError("licensed regression requires reference-power reference_air metadata")
    environment = dict(base_environment if base_environment is not None else os.environ)
    environment.update(
        {
            MODEL_ENV: str(Path(str(raw.get("source_model_path", ""))).expanduser().resolve()),
            SOURCE_SHA256_ENV: _source_sha256(raw.get("expected_source_sha256")),
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
    "SOURCE_SHA256_ENV",
    "WAVELENGTH_ENV",
    "controlled_fixture_environment_from_reference_power_spec",
    "controlled_fixture_from_environment",
]
