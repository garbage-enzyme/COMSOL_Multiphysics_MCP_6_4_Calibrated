"""Fail-closed helpers shared by the standalone MIM recipes."""

from __future__ import annotations

import math
import os
import uuid
from pathlib import Path
from typing import Iterable, Mapping, Sequence

PASSIVE_REFLECTION_TOLERANCE = 1.0e-6


def require_entities(values: Iterable[object], label: str) -> list[int]:
    """Return a unique nonempty positive entity selection."""
    entities = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} must contain positive integer entity IDs")
        entities.append(value)
    normalized = sorted(set(entities))
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def require_port_pair(
    top: Iterable[object],
    bottom: Iterable[object],
    *,
    geometry: object | None = None,
    top_domains: Sequence[int] = (),
    bottom_domains: Sequence[int] = (),
) -> tuple[list[int], list[int]]:
    """Require distinct nonempty top and bottom port selections."""
    top_entities = require_entities(top, "top port selection")
    bottom_entities = require_entities(bottom, "bottom port selection")
    if set(top_entities) & set(bottom_entities):
        raise ValueError("top and bottom port selections must not overlap")
    if geometry is not None:
        expected_top = set(require_entities(top_domains, "top port domains"))
        expected_bottom = set(require_entities(bottom_domains, "bottom port domains"))
        if expected_top & expected_bottom:
            raise ValueError("top and bottom port domains must not overlap")
        up_down = geometry.getUpDown()
        up = list(up_down[0])
        down = list(up_down[1])
        if len(up) != len(down) or len(up) != int(geometry.getNBoundaries()):
            raise ValueError("geometry adjacency arrays are incomplete")

        def require_exterior(boundaries: Sequence[int], domains: set[int], label: str) -> None:
            for boundary in boundaries:
                if boundary > len(up):
                    raise ValueError(f"{label} contains a boundary outside the built topology")
                adjacent = {int(up[boundary - 1]), int(down[boundary - 1])}
                if 0 not in adjacent or not (adjacent & domains):
                    raise ValueError(f"{label} does not match its intended exterior domain")

        require_exterior(top_entities, expected_top, "top port selection")
        require_exterior(bottom_entities, expected_bottom, "bottom port selection")
    return top_entities, bottom_entities


def require_named_domains(component: object, selection_tag: str) -> list[int]:
    """Read one generated geometry domain selection without raw entity assumptions."""
    selection = component.selection(selection_tag)
    failures = []
    for arguments in ((3,), tuple()):
        try:
            return require_entities(selection.entities(*arguments), selection_tag)
        except Exception as exc:
            failures.append(type(exc).__name__)
    raise ValueError(
        f"generated domain selection {selection_tag!r} is unavailable ({', '.join(failures)})"
    )


def require_interface_boundaries(
    geometry: object,
    first_domains: Sequence[int],
    second_domains: Sequence[int],
) -> list[int]:
    """Find boundaries adjacent to both exact domain groups, independent of orientation."""
    first = set(require_entities(first_domains, "first domain group"))
    second = set(require_entities(second_domains, "second domain group"))
    if first & second:
        raise ValueError("interface domain groups must not overlap")
    up_down = geometry.getUpDown()
    up = list(up_down[0])
    down = list(up_down[1])
    if len(up) != len(down) or len(up) != int(geometry.getNBoundaries()):
        raise ValueError("geometry adjacency arrays are incomplete")
    boundaries = [
        index
        for index, (up_domain, down_domain) in enumerate(zip(up, down), start=1)
        if ({int(up_domain), int(down_domain)} & first)
        and ({int(up_domain), int(down_domain)} & second)
    ]
    if len(boundaries) != 1:
        raise ValueError("material interface boundary is missing or ambiguous")
    return boundaries


def require_passive_reflection(values: Sequence[float], label: str) -> list[float]:
    """Require passive reflection within a small numerical solve tolerance."""
    normalized = [float(value) for value in values]
    if any(
        value < -PASSIVE_REFLECTION_TOLERANCE
        or value > 1.0 + PASSIVE_REFLECTION_TOLERANCE
        for value in normalized
    ):
        raise ValueError(f"{label} lies outside the passive reflection range")
    return normalized


def require_required_properties(node: object, values: Mapping[str, object]) -> None:
    """Apply every required property and verify string readback when supported."""
    for name, value in values.items():
        node.set(name, value)
        getter = getattr(node, "getString", None)
        if callable(getter):
            observed = str(getter(name))
            expected = str(value)
            if isinstance(value, bool):
                accepted = {"true", "on", "1"} if value else {"false", "off", "0"}
                matches = observed.casefold() in accepted
            elif isinstance(value, (int, float)):
                try:
                    matches = math.isclose(
                        float(observed), float(value), rel_tol=1.0e-9, abs_tol=1.0e-12
                    )
                except ValueError:
                    matches = False
            else:
                matches = observed == expected
            if not matches:
                raise ValueError(f"required property {name!r} readback mismatch")


def bind_wavelength_step(step: object, parameter_name: str) -> None:
    """Bind the electromagnetic Wavelength step to the swept material parameter."""
    if not isinstance(parameter_name, str) or not parameter_name.strip():
        raise ValueError("wavelength parameter name must be non-empty")
    require_required_properties(step, {"punit": "m", "plist": parameter_name})


def require_spectrum(values: object, wavelengths: Sequence[float], label: str) -> list[float]:
    """Require one finite real result per requested wavelength."""
    try:
        raw = list(values)
    except TypeError:
        raw = [values]
    if len(raw) != len(wavelengths):
        raise ValueError(f"{label} result count does not match the wavelength sweep")
    normalized = []
    for value in raw:
        if isinstance(value, bool) or isinstance(value, complex):
            raise ValueError(f"{label} results must be finite real scalars")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{label} results must be finite real scalars")
        normalized.append(number)
    return normalized


def require_partition_result(
    before_boundary_count: int,
    after_boundary_count: int,
    patch_candidates: Iterable[object],
) -> int:
    """Require an observed interface split and one exact patch boundary."""
    if after_boundary_count <= before_boundary_count:
        raise ValueError("partition did not increase the geometry boundary count")
    candidates = require_entities(patch_candidates, "patch boundary candidates")
    if len(candidates) != 1:
        raise ValueError("patch boundary identity is ambiguous")
    return candidates[0]


def save_required(java_model: object, destination: Path) -> Path:
    """Save to a unique staging path and publish only a nonempty completed model."""
    target = destination.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.save")
    try:
        java_model.save(str(staging))
        if not staging.is_file() or staging.stat().st_size <= 0:
            raise RuntimeError("COMSOL save did not produce a nonempty staging model")
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("published COMSOL model is missing or empty")
    return target
