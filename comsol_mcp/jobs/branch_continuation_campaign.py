"""Pure normalization for bounded durable branch-continuation campaigns."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping

from comsol_mcp.build_identity import get_build_identity
from comsol_mcp.compatibility import module_identity_matches

from .spectral_characterization import normalize_spectral_characterization_job_spec
from .store import JOB_SCHEMA_VERSION


MIN_BRANCH_CONTINUATION_STATES = 2
MAX_BRANCH_CONTINUATION_STATES = 16
MAX_BRANCH_CONTINUATION_POINTS = 512
MAX_BRANCH_CONTINUATION_SPEC_BYTES = 4 * 1024 * 1024
MAX_BRANCH_CONTINUATION_WALL_SECONDS = 30 * 24 * 60 * 60
BRANCH_CONTINUATION_CAMPAIGN_DRIVER_VERSION = "1.0.0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9A-Fa-f]{64}$")
_POLARIZATIONS = frozenset({"TE", "TM", "S", "P", "rhcp", "lhcp", "unpolarized"})
_STOP_POLICIES = frozenset({"stop_at_first_unresolved", "continue_all_declared"})


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


def _finite(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0.0):
        qualifier = "positive and finite" if positive else "finite"
        raise ValueError(f"{name} must be {qualifier}")
    return number


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _normalize_angles(value: object, name: str) -> dict[str, float]:
    raw = _exact_mapping(value, {"alpha1_deg", "alpha2_deg"}, name)
    return {
        "alpha1_deg": _finite(raw["alpha1_deg"], f"{name}.alpha1_deg"),
        "alpha2_deg": _finite(raw["alpha2_deg"], f"{name}.alpha2_deg"),
    }


def _normalize_incidence_readback(
    value: object,
    name: str,
    *,
    source_model_sha256: str,
    configuration_sha256: str,
) -> dict[str, Any]:
    raw = _exact_mapping(
        value,
        {
            "measurement_state", "source_model_sha256", "configuration_sha256",
            "requested", "parent", "ports", "evidence_sha256",
        },
        name,
    )
    if raw["measurement_state"] != "measured":
        raise ValueError(f"{name}.measurement_state must be measured")
    requested = _normalize_angles(raw["requested"], f"{name}.requested")
    parent = _normalize_angles(raw["parent"], f"{name}.parent")
    ports = raw["ports"]
    if not isinstance(ports, list) or len(ports) != 2:
        raise ValueError(f"{name}.ports must contain exactly two periodic-port readbacks")
    normalized_ports = []
    for index, value in enumerate(ports):
        port_name = f"{name}.ports[{index}]"
        port = _exact_mapping(value, {"port_tag", "alpha1_deg", "alpha2_deg"}, port_name)
        normalized_ports.append(
            {
                "port_tag": _identifier(port["port_tag"], f"{port_name}.port_tag"),
                "alpha1_deg": _finite(port["alpha1_deg"], f"{port_name}.alpha1_deg"),
                "alpha2_deg": _finite(port["alpha2_deg"], f"{port_name}.alpha2_deg"),
            }
        )
    port_tags = [item["port_tag"] for item in normalized_ports]
    if len(set(port_tags)) != 2:
        raise ValueError(f"{name}.ports must identify two distinct periodic ports")
    if parent != requested or any(
        {"alpha1_deg": port["alpha1_deg"], "alpha2_deg": port["alpha2_deg"]}
        != requested
        for port in normalized_ports
    ):
        raise ValueError(f"{name} must exactly match requested, parent, and port angles")
    body = {
        "measurement_state": "measured",
        "source_model_sha256": _sha256(
            raw["source_model_sha256"], f"{name}.source_model_sha256"
        ),
        "configuration_sha256": _sha256(
            raw["configuration_sha256"], f"{name}.configuration_sha256"
        ),
        "requested": requested,
        "parent": parent,
        "ports": normalized_ports,
    }
    if body["source_model_sha256"] != source_model_sha256:
        raise ValueError(f"{name}.source_model_sha256 does not match the spectral job")
    if body["configuration_sha256"] != configuration_sha256:
        raise ValueError(f"{name}.configuration_sha256 does not match the spectral job")
    supplied = _sha256(raw["evidence_sha256"], f"{name}.evidence_sha256")
    if _fingerprint(body) != supplied:
        raise ValueError(f"{name}.evidence_sha256 does not match the incidence readback")
    return {**body, "evidence_sha256": supplied}


def build_branch_continuation_coordinate_identity(
    *,
    coordinate_name: str,
    coordinate_value: float,
    coordinate_unit: str,
    polarization: str,
    material_identity_sha256: str,
    source_model_sha256: str,
    configuration_sha256: str,
    incidence_readback_sha256: str,
) -> str:
    """Build the exact identity that binds one coordinate to its model evidence."""
    if polarization not in _POLARIZATIONS:
        raise ValueError("polarization is unsupported")
    body = {
        "coordinate_name": _identifier(coordinate_name, "coordinate_name"),
        "coordinate_value": _finite(coordinate_value, "coordinate_value"),
        "coordinate_unit": _identifier(coordinate_unit, "coordinate_unit"),
        "polarization": polarization,
        "material_identity_sha256": _sha256(
            material_identity_sha256, "material_identity_sha256"
        ),
        "source_model_sha256": _sha256(source_model_sha256, "source_model_sha256"),
        "configuration_sha256": _sha256(configuration_sha256, "configuration_sha256"),
        "incidence_readback_sha256": _sha256(
            incidence_readback_sha256, "incidence_readback_sha256"
        ),
    }
    return _fingerprint(body)


def _normalize_policy(value: object) -> dict[str, Any]:
    raw = _exact_mapping(
        value,
        {
            "policy_id", "guard_window_m", "absolute_bounds_m", "max_expansions",
            "max_total_window_m", "request_grid", "stop_policy",
        },
        "continuation_policy",
    )
    bounds = _exact_mapping(
        raw["absolute_bounds_m"], {"lower_m", "upper_m"},
        "continuation_policy.absolute_bounds_m",
    )
    lower = _finite(
        bounds["lower_m"], "continuation_policy.absolute_bounds_m.lower_m", positive=True
    )
    upper = _finite(
        bounds["upper_m"], "continuation_policy.absolute_bounds_m.upper_m", positive=True
    )
    if upper <= lower:
        raise ValueError("continuation_policy absolute upper bound must exceed its lower bound")
    request_grid = _exact_mapping(
        raw["request_grid"], {"point_count", "spacing_rule"},
        "continuation_policy.request_grid",
    )
    if request_grid["spacing_rule"] != "uniform_inclusive":
        raise ValueError("continuation_policy.request_grid.spacing_rule is unsupported")
    if raw["stop_policy"] not in _STOP_POLICIES:
        raise ValueError("continuation_policy.stop_policy is unsupported")
    return {
        "policy_id": _identifier(raw["policy_id"], "continuation_policy.policy_id"),
        "guard_window_m": _finite(
            raw["guard_window_m"], "continuation_policy.guard_window_m", positive=True
        ),
        "absolute_bounds_m": {"lower_m": lower, "upper_m": upper},
        "max_expansions": _integer(
            raw["max_expansions"], "continuation_policy.max_expansions", minimum=0, maximum=8
        ),
        "max_total_window_m": _finite(
            raw["max_total_window_m"],
            "continuation_policy.max_total_window_m",
            positive=True,
        ),
        "request_grid": {
            "point_count": _integer(
                request_grid["point_count"],
                "continuation_policy.request_grid.point_count",
                minimum=2,
                maximum=257,
            ),
            "spacing_rule": "uniform_inclusive",
        },
        "stop_policy": raw["stop_policy"],
    }


def current_branch_continuation_campaign_driver_identity() -> dict[str, str]:
    """Bind campaign resume to the exact package bytes and driver contract."""
    build = get_build_identity()
    return {
        "implementation": "comsol_mcp.jobs.branch_continuation_campaign_worker",
        "driver_version": BRANCH_CONTINUATION_CAMPAIGN_DRIVER_VERSION,
        "package_content_sha256": build["package_content_sha256"],
        "build_identity_sha256": build["build_identity_sha256"],
    }


def validate_branch_continuation_campaign_driver_identity(
    spec: Mapping[str, Any],
) -> dict[str, str]:
    observed = spec.get("driver_identity")
    expected = current_branch_continuation_campaign_driver_identity()
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
        raise ValueError("branch-continuation campaign driver identity differs from the running package")
    return expected


def normalize_branch_continuation_campaign_spec(value: object) -> dict[str, Any]:
    """Normalize one immutable exact-model continuation sequence."""
    raw = _exact_mapping(
        value,
        {
            "job_type", "campaign_id", "states", "continuation_policy",
            "maximum_total_points", "wall_time_budget_seconds",
        },
        "branch-continuation campaign specification",
    )
    if raw["job_type"] != "branch_continuation_campaign":
        raise ValueError("job_type must be branch_continuation_campaign")
    states = raw["states"]
    if not isinstance(states, list) or not MIN_BRANCH_CONTINUATION_STATES <= len(states) <= MAX_BRANCH_CONTINUATION_STATES:
        raise ValueError(
            f"states must contain {MIN_BRANCH_CONTINUATION_STATES} to "
            f"{MAX_BRANCH_CONTINUATION_STATES} entries"
        )
    normalized_states = []
    for index, value in enumerate(states):
        name = f"states[{index}]"
        state = _exact_mapping(
            value,
            {
                "state_id", "ordinal", "declared_predecessor_state_id",
                "model_preparation", "coordinate", "polarization",
                "material_identity_sha256", "incidence_readback", "spectral_job",
            },
            name,
        )
        state_id = _identifier(state["state_id"], f"{name}.state_id")
        if state["ordinal"] != index:
            raise ValueError(f"{name}.ordinal must equal its declared sequence position")
        predecessor = None if index == 0 else normalized_states[-1]["state_id"]
        if state["declared_predecessor_state_id"] != predecessor:
            raise ValueError(f"{name} adjacency does not match the preceding state")
        preparation = _exact_mapping(
            state["model_preparation"], {"mode"}, f"{name}.model_preparation"
        )
        if preparation["mode"] != "exact_model":
            raise ValueError(f"{name}.model_preparation.mode must be exact_model")
        coordinate = _exact_mapping(
            state["coordinate"], {"name", "value", "unit", "identity_sha256"},
            f"{name}.coordinate",
        )
        polarization = state["polarization"]
        if polarization not in _POLARIZATIONS:
            raise ValueError(f"{name}.polarization is unsupported")
        spectral = normalize_spectral_characterization_job_spec(state["spectral_job"])
        material_identity = _sha256(
            state["material_identity_sha256"], f"{name}.material_identity_sha256"
        )
        readback = _normalize_incidence_readback(
            state["incidence_readback"],
            f"{name}.incidence_readback",
            source_model_sha256=spectral["source_model_sha256"],
            configuration_sha256=spectral["configuration_sha256"],
        )
        coordinate_identity = _sha256(
            coordinate["identity_sha256"], f"{name}.coordinate.identity_sha256"
        )
        expected_coordinate_identity = build_branch_continuation_coordinate_identity(
            coordinate_name=coordinate["name"],
            coordinate_value=coordinate["value"],
            coordinate_unit=coordinate["unit"],
            polarization=polarization,
            material_identity_sha256=material_identity,
            source_model_sha256=spectral["source_model_sha256"],
            configuration_sha256=spectral["configuration_sha256"],
            incidence_readback_sha256=readback["evidence_sha256"],
        )
        if coordinate_identity != expected_coordinate_identity:
            raise ValueError(f"{name}.coordinate.identity_sha256 does not match its state evidence")
        normalized_states.append(
            {
                "state_id": state_id,
                "ordinal": index,
                "declared_predecessor_state_id": predecessor,
                "model_preparation": {"mode": "exact_model"},
                "coordinate": {
                    "name": _identifier(coordinate["name"], f"{name}.coordinate.name"),
                    "value": _finite(coordinate["value"], f"{name}.coordinate.value"),
                    "unit": _identifier(coordinate["unit"], f"{name}.coordinate.unit"),
                    "identity_sha256": coordinate_identity,
                },
                "polarization": polarization,
                "material_identity_sha256": material_identity,
                "incidence_readback": readback,
                "spectral_job": spectral,
            }
        )

    state_ids = [item["state_id"] for item in normalized_states]
    source_hashes = [item["spectral_job"]["source_model_sha256"] for item in normalized_states]
    configurations = [item["spectral_job"]["configuration_sha256"] for item in normalized_states]
    coordinate_identities = [item["coordinate"]["identity_sha256"] for item in normalized_states]
    if len(state_ids) != len(set(state_ids)):
        raise ValueError("branch-continuation state identifiers must be unique")
    if len(source_hashes) != len(set(source_hashes)):
        raise ValueError("exact-model continuation states require distinct source model bytes")
    if len(configurations) != len(set(configurations)):
        raise ValueError("branch-continuation configuration identities must be unique")
    if len(coordinate_identities) != len(set(coordinate_identities)):
        raise ValueError("branch-continuation coordinate identities must be unique")
    for field, label in (
        ("material_identity_sha256", "material identity"),
        ("polarization", "polarization"),
    ):
        if len({item[field] for item in normalized_states}) != 1:
            raise ValueError(f"branch-continuation {label} must be constant")
    coordinate_names = {item["coordinate"]["name"] for item in normalized_states}
    coordinate_units = {item["coordinate"]["unit"] for item in normalized_states}
    if len(coordinate_names) != 1 or len(coordinate_units) != 1:
        raise ValueError("branch-continuation coordinate name and unit must be constant")
    if len({item["spectral_job"]["cores"] for item in normalized_states}) != 1:
        raise ValueError("branch-continuation states must use one core allocation")
    if len({item["spectral_job"]["version"] for item in normalized_states}) != 1:
        raise ValueError("branch-continuation states must use one COMSOL version request")

    policy = _normalize_policy(raw["continuation_policy"])
    bounds = policy["absolute_bounds_m"]
    for index, state in enumerate(normalized_states):
        spectral = state["spectral_job"]
        expansion = spectral["expansion_policy"]
        if (
            expansion["absolute_lower_m"] < bounds["lower_m"]
            or expansion["absolute_upper_m"] > bounds["upper_m"]
        ):
            raise ValueError(
                f"states[{index}] spectral absolute bounds exceed the continuation policy"
            )
    declared_child_expansions = sum(
        item["spectral_job"]["expansion_policy"]["maximum_expansions"]
        for item in normalized_states
    )
    if declared_child_expansions > policy["max_expansions"]:
        raise ValueError("declared spectral expansions exceed the continuation policy")

    declared_points = sum(item["spectral_job"]["maximum_points"] for item in normalized_states)
    maximum_total_points = _integer(
        raw["maximum_total_points"],
        "maximum_total_points",
        minimum=declared_points,
        maximum=MAX_BRANCH_CONTINUATION_POINTS,
    )
    wall_time_budget = _integer(
        raw["wall_time_budget_seconds"],
        "wall_time_budget_seconds",
        minimum=1,
        maximum=MAX_BRANCH_CONTINUATION_WALL_SECONDS,
    )
    child_wall_budget = sum(
        item["spectral_job"]["resource_policy"]["rules"]["wall_time_budget_seconds"]
        for item in normalized_states
    )
    if wall_time_budget < child_wall_budget:
        raise ValueError("campaign wall-time budget is smaller than its declared state budgets")

    spec = {
        "job_type": "branch_continuation_campaign",
        "schema_version": JOB_SCHEMA_VERSION,
        "campaign_id": _identifier(raw["campaign_id"], "campaign_id"),
        "states": normalized_states,
        "continuation_policy": policy,
        "maximum_total_points": maximum_total_points,
        "wall_time_budget_seconds": wall_time_budget,
        "declared_state_count": len(normalized_states),
        "declared_point_count": declared_points,
        "driver_identity": current_branch_continuation_campaign_driver_identity(),
    }
    if len(_canonical_bytes(spec)) > MAX_BRANCH_CONTINUATION_SPEC_BYTES:
        raise ValueError(
            f"branch-continuation campaign exceeds {MAX_BRANCH_CONTINUATION_SPEC_BYTES} bytes"
        )
    spec["spec_fingerprint"] = _fingerprint(spec)
    return spec


__all__ = [
    "BRANCH_CONTINUATION_CAMPAIGN_DRIVER_VERSION",
    "MAX_BRANCH_CONTINUATION_POINTS",
    "MAX_BRANCH_CONTINUATION_STATES",
    "build_branch_continuation_coordinate_identity",
    "current_branch_continuation_campaign_driver_identity",
    "normalize_branch_continuation_campaign_spec",
    "validate_branch_continuation_campaign_driver_identity",
]
