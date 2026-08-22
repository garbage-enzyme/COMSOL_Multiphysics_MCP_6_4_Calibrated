"""Durable outer-loop GCMMA state for COMSOL-native condition evaluations."""

from __future__ import annotations

import hashlib
import importlib.metadata
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .derivative_support import _finite, _sha256

GCMMA_STATE_SCHEMA_NAME = "comsol_mcp.robust_outer_gcmma_state"
GCMMA_STATE_SCHEMA_VERSION = "1.0.0"
GCMMA_PROPOSAL_SCHEMA_NAME = "comsol_mcp.robust_outer_gcmma_proposal"
GCMMA_PROPOSAL_SCHEMA_VERSION = "1.0.0"


def _finite_vector(value: object, name: str, *, length: int | None = None) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{name} must be a finite numeric list")
    result = [_finite(item, f"{name}[{index}]") for index, item in enumerate(value)]
    if length is not None and len(result) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _backend_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "package_name",
        "package_version",
        "distribution_license",
        "distribution_sha256",
        "distribution_path",
    }:
        raise ValueError("GCMMA backend identity fields are invalid")
    if value["package_name"] != "mmapy" or value["package_version"] != "0.3.1":
        raise ValueError("GCMMA backend must be mmapy 0.3.1")
    if value["distribution_license"] != "GPL-3.0-or-later":
        raise ValueError("GCMMA backend license identity differs")
    path = value["distribution_path"]
    if not isinstance(path, str) or not path.isascii():
        raise ValueError("GCMMA backend distribution path must be ASCII")
    from pathlib import Path

    wheel = Path(path)
    if not wheel.is_absolute() or wheel.suffix.casefold() != ".whl":
        raise ValueError("GCMMA backend distribution path must be an absolute wheel")
    return {
        "package_name": "mmapy",
        "package_version": "0.3.1",
        "distribution_license": "GPL-3.0-or-later",
        "distribution_sha256": _sha256(
            value["distribution_sha256"], "backend_identity.distribution_sha256"
        ),
        "distribution_path": str(wheel),
    }


def verify_mmapy_backend(expected: Mapping[str, Any]) -> dict[str, Any]:
    """Lazy-load and verify the separately installed reviewed optimizer backend."""
    normalized = _backend_identity(expected)
    from pathlib import Path

    wheel = Path(normalized["distribution_path"])
    if not wheel.is_file() or wheel.is_symlink():
        raise RuntimeError("reviewed mmapy wheel is missing or is not a regular file")
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != normalized["distribution_sha256"]:
        raise RuntimeError("reviewed mmapy wheel SHA-256 differs from the run contract")
    wheel_text = str(wheel)
    if wheel_text not in sys.path:
        sys.path.insert(0, wheel_text)
    try:
        version = importlib.metadata.version("mmapy")
        from mmapy import asymp, concheck, gcmmasub, raaupdate
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        raise RuntimeError(
            "mmapy 0.3.1 is required only for the explicit robust GCMMA execution path"
        ) from exc
    if version != normalized["package_version"]:
        raise RuntimeError("installed mmapy version differs from the run contract")
    return {
        "identity": normalized,
        "asymp": asymp,
        "concheck": concheck,
        "gcmmasub": gcmmasub,
        "raaupdate": raaupdate,
    }


def create_gcmma_state(
    *,
    variable_ids: Sequence[str],
    lower_bounds: object,
    upper_bounds: object,
    initial_values: object,
    move_limit: object,
    max_iterations: object,
    max_inner_iterations: object,
    max_condition_solves: object,
    backend_identity: object,
) -> dict[str, Any]:
    """Create a normalized, serializable GCMMA state before licensed work."""
    if not isinstance(variable_ids, Sequence) or isinstance(variable_ids, (str, bytes)):
        raise ValueError("variable_ids must be a nonempty ordered list")
    ids = list(variable_ids)
    if (
        not ids
        or any(not isinstance(item, str) or not item for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("variable_ids must contain unique nonempty strings")
    count = len(ids)
    lower = _finite_vector(lower_bounds, "lower_bounds", length=count)
    upper = _finite_vector(upper_bounds, "upper_bounds", length=count)
    initial = _finite_vector(initial_values, "initial_values", length=count)
    if any(not lo < value < hi for lo, value, hi in zip(lower, initial, upper, strict=True)):
        raise ValueError("initial_values must lie strictly inside every variable bound")
    move = _finite(move_limit, "move_limit", positive=True)
    if move > 1.0:
        raise ValueError("move_limit must not exceed one normalized design range")
    integer_values = {
        "max_iterations": max_iterations,
        "max_inner_iterations": max_inner_iterations,
        "max_condition_solves": max_condition_solves,
    }
    for name, item in integer_values.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"{name} must be a positive integer")
    normalized = [
        (value - lo) / (hi - lo) for value, lo, hi in zip(initial, lower, upper, strict=True)
    ]
    body: dict[str, Any] = {
        "schema_name": GCMMA_STATE_SCHEMA_NAME,
        "schema_version": GCMMA_STATE_SCHEMA_VERSION,
        "variable_ids": ids,
        "lower_bounds": lower,
        "upper_bounds": upper,
        "current_values": initial,
        "current_normalized": normalized,
        "previous_normalized": normalized,
        "second_previous_normalized": normalized,
        "lower_asymptotes": [0.0] * count,
        "upper_asymptotes": [1.0] * count,
        "raa0": 1e-5,
        "raa": [],
        "move_limit": move,
        "max_iterations": max_iterations,
        "max_inner_iterations": max_inner_iterations,
        "max_condition_solves": max_condition_solves,
        "outer_iteration": 0,
        "inner_iteration": 0,
        "condition_solves_used": 0,
        "accepted_objective": None,
        "accepted_gradient": None,
        "pending_proposal": None,
        "backend_identity": _backend_identity(backend_identity),
        "status": "ready",
    }
    body["state_fingerprint"] = domain_sha256_v2(GCMMA_STATE_SCHEMA_NAME, body)
    return body


def normalize_gcmma_state(value: object) -> dict[str, Any]:
    """Validate a durable GCMMA state and its complete fingerprint."""
    if not isinstance(value, Mapping):
        raise ValueError("GCMMA state must be an object")
    raw = dict(value)
    supplied = raw.pop("state_fingerprint", None)
    required = {
        "schema_name",
        "schema_version",
        "variable_ids",
        "lower_bounds",
        "upper_bounds",
        "current_values",
        "current_normalized",
        "previous_normalized",
        "second_previous_normalized",
        "lower_asymptotes",
        "upper_asymptotes",
        "raa0",
        "raa",
        "move_limit",
        "max_iterations",
        "max_inner_iterations",
        "max_condition_solves",
        "outer_iteration",
        "inner_iteration",
        "condition_solves_used",
        "accepted_objective",
        "accepted_gradient",
        "pending_proposal",
        "backend_identity",
        "status",
    }
    if set(raw) != required:
        raise ValueError("GCMMA state fields are invalid")
    if (
        raw["schema_name"] != GCMMA_STATE_SCHEMA_NAME
        or raw["schema_version"] != GCMMA_STATE_SCHEMA_VERSION
    ):
        raise ValueError("GCMMA state schema identity is unsupported")
    ids = raw["variable_ids"]
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, str) or not item for item in ids)
    ):
        raise ValueError("GCMMA variable_ids are invalid")
    count = len(ids)
    for name in (
        "lower_bounds",
        "upper_bounds",
        "current_values",
        "current_normalized",
        "previous_normalized",
        "second_previous_normalized",
        "lower_asymptotes",
        "upper_asymptotes",
    ):
        raw[name] = _finite_vector(raw[name], name, length=count)
    raw["raa0"] = _finite(raw["raa0"], "raa0", positive=True)
    if raw["raa"] != []:
        raise ValueError("unconstrained robust GCMMA state must have an empty raa vector")
    raw["move_limit"] = _finite(raw["move_limit"], "move_limit", positive=True)
    if raw["move_limit"] > 1.0:
        raise ValueError("GCMMA move_limit exceeds one")
    for name in ("max_iterations", "max_inner_iterations", "max_condition_solves"):
        if isinstance(raw[name], bool) or not isinstance(raw[name], int) or raw[name] < 1:
            raise ValueError(f"GCMMA {name} is invalid")
    for name in ("outer_iteration", "inner_iteration", "condition_solves_used"):
        if isinstance(raw[name], bool) or not isinstance(raw[name], int) or raw[name] < 0:
            raise ValueError(f"GCMMA {name} is invalid")
    if raw["accepted_objective"] is not None:
        raw["accepted_objective"] = _finite(raw["accepted_objective"], "accepted_objective")
    if raw["accepted_gradient"] is not None:
        raw["accepted_gradient"] = _finite_vector(
            raw["accepted_gradient"], "accepted_gradient", length=count
        )
    if raw["pending_proposal"] is not None:
        proposal = raw["pending_proposal"]
        if (
            not isinstance(proposal, Mapping)
            or proposal.get("schema_name") != GCMMA_PROPOSAL_SCHEMA_NAME
        ):
            raise ValueError("GCMMA pending proposal is invalid")
    if raw["status"] not in {
        "ready",
        "proposal_pending",
        "accepted",
        "complete",
        "budget_exhausted",
    }:
        raise ValueError("GCMMA state status is invalid")
    raw["backend_identity"] = _backend_identity(raw["backend_identity"])
    if supplied is None:
        raise ValueError("GCMMA state fingerprint is required")
    expected = domain_sha256_v2(GCMMA_STATE_SCHEMA_NAME, raw)
    if supplied != expected:
        raise ValueError("GCMMA state fingerprint is invalid")
    raw["state_fingerprint"] = expected
    return raw


def _column(np: Any, values: Sequence[float]) -> Any:
    return np.asarray(values, dtype=float).reshape((-1, 1))


def propose_gcmma_candidate(
    state: object,
    *,
    objective: object,
    gradient: object,
    condition_solves: object,
    backend: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose one bounded GCMMA candidate from accepted COMSOL evidence."""
    current = normalize_gcmma_state(state)
    if current["pending_proposal"] is not None:
        raise ValueError("GCMMA proposal is already pending")
    if current["status"] in {"complete", "budget_exhausted"}:
        raise ValueError("GCMMA state is already terminal")
    if current["outer_iteration"] >= current["max_iterations"]:
        raise ValueError("GCMMA outer iteration budget is exhausted")
    if (
        isinstance(condition_solves, bool)
        or not isinstance(condition_solves, int)
        or condition_solves < 1
    ):
        raise ValueError("condition_solves must be a positive integer")
    if current["condition_solves_used"] + condition_solves > current["max_condition_solves"]:
        raise ValueError("GCMMA condition solve budget is exhausted")
    value = _finite(objective, "objective")
    physical_gradient = _finite_vector(gradient, "gradient", length=len(current["variable_ids"]))
    runtime = (
        verify_mmapy_backend(current["backend_identity"]) if backend is None else dict(backend)
    )
    import numpy as np

    xval = _column(np, current["current_normalized"])
    xold1 = _column(np, current["previous_normalized"])
    xold2 = _column(np, current["second_previous_normalized"])
    xmin = np.zeros_like(xval)
    xmax = np.ones_like(xval)
    low = _column(np, current["lower_asymptotes"])
    upp = _column(np, current["upper_asymptotes"])
    ranges = [
        hi - lo for lo, hi in zip(current["lower_bounds"], current["upper_bounds"], strict=True)
    ]
    # mmapy minimizes. The public robust objective is maximized, and physical
    # gradients are per declared variable unit, so transform to normalized y.
    df0dx = _column(
        np, [-item * scale for item, scale in zip(physical_gradient, ranges, strict=True)]
    )
    m = 0
    n = len(current["variable_ids"])
    empty_vector: np.ndarray = np.empty((m, 1), dtype=float)
    empty_matrix: np.ndarray = np.empty((m, n), dtype=float)
    outer = current["outer_iteration"] + 1
    low, upp, raa0, raa = runtime["asymp"](
        outer,
        n,
        xval,
        xold1,
        xold2,
        xmin,
        xmax,
        low,
        upp,
        current["raa0"],
        empty_vector,
        1e-5,
        empty_vector,
        df0dx,
        empty_matrix,
    )
    local_min = np.maximum(xmin, xval - current["move_limit"])
    local_max = np.minimum(xmax, xval + current["move_limit"])
    result = runtime["gcmmasub"](
        m,
        n,
        outer,
        1e-7,
        xval,
        local_min,
        local_max,
        low,
        upp,
        raa0,
        raa,
        np.asarray([[-value]], dtype=float),
        df0dx,
        empty_vector,
        empty_matrix,
        1.0,
        empty_vector,
        empty_vector,
        empty_vector,
    )
    normalized = [float(item) for item in result[0].reshape(-1)]
    values = [
        lo + item * (hi - lo)
        for item, lo, hi in zip(
            normalized, current["lower_bounds"], current["upper_bounds"], strict=True
        )
    ]
    proposal_body = {
        "schema_name": GCMMA_PROPOSAL_SCHEMA_NAME,
        "schema_version": GCMMA_PROPOSAL_SCHEMA_VERSION,
        "outer_iteration": outer,
        "inner_iteration": 0,
        "base_state_fingerprint": current["state_fingerprint"],
        "normalized_values": normalized,
        "physical_values": values,
        "minimization_objective_approximation": float(result[-2].reshape(-1)[0]),
        "condition_solves_before_proposal": current["condition_solves_used"] + condition_solves,
        "move_limit": current["move_limit"],
        "automatic_fallback_used": False,
    }
    proposal_body["proposal_fingerprint"] = domain_sha256_v2(
        GCMMA_PROPOSAL_SCHEMA_NAME, proposal_body
    )
    body = dict(current)
    body.pop("state_fingerprint")
    body.update(
        {
            "lower_asymptotes": [float(item) for item in low.reshape(-1)],
            "upper_asymptotes": [float(item) for item in upp.reshape(-1)],
            "raa0": float(np.asarray(raa0).reshape(-1)[0]),
            "accepted_objective": value,
            "accepted_gradient": physical_gradient,
            "inner_iteration": 0,
            "condition_solves_used": current["condition_solves_used"] + condition_solves,
            "pending_proposal": proposal_body,
            "status": "proposal_pending",
        }
    )
    body["state_fingerprint"] = domain_sha256_v2(GCMMA_STATE_SCHEMA_NAME, body)
    return body


def accept_gcmma_candidate(
    state: object,
    *,
    candidate_objective: object,
    condition_solves: object,
    backend: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Accept only a conservative fresh-forward candidate.

    Non-conservative proposals are rejected explicitly. A subsequent revision
    uses a new durable proposal/evaluation row; it is never hidden as fallback.
    """
    current = normalize_gcmma_state(state)
    proposal = current["pending_proposal"]
    if proposal is None or current["status"] != "proposal_pending":
        raise ValueError("GCMMA has no pending proposal")
    if (
        isinstance(condition_solves, bool)
        or not isinstance(condition_solves, int)
        or condition_solves < 1
    ):
        raise ValueError("condition_solves must be a positive integer")
    used = current["condition_solves_used"] + condition_solves
    if used > current["max_condition_solves"]:
        raise ValueError("GCMMA condition solve budget is exhausted")
    candidate = _finite(candidate_objective, "candidate_objective")
    runtime = (
        verify_mmapy_backend(current["backend_identity"]) if backend is None else dict(backend)
    )
    import numpy as np

    conservative = bool(
        runtime["concheck"](
            0,
            1e-7,
            np.asarray([[proposal["minimization_objective_approximation"]]], dtype=float),
            np.asarray([[-candidate]], dtype=float),
            np.empty((0, 1), dtype=float),
            np.empty((0, 1), dtype=float),
        )
    )
    body = dict(current)
    body.pop("state_fingerprint")
    body["condition_solves_used"] = used
    if not conservative:
        next_inner = current["inner_iteration"] + 1
        body["inner_iteration"] = next_inner
        if next_inner >= current["max_inner_iterations"]:
            body["pending_proposal"] = None
            body["status"] = "complete"
        else:
            xval = _column(np, current["current_normalized"])
            xmma = _column(np, proposal["normalized_values"])
            xmin = np.maximum(np.zeros_like(xval), xval - current["move_limit"])
            xmax = np.minimum(np.ones_like(xval), xval + current["move_limit"])
            low = _column(np, current["lower_asymptotes"])
            upp = _column(np, current["upper_asymptotes"])
            empty_vector: np.ndarray = np.empty((0, 1), dtype=float)
            empty_matrix: np.ndarray = np.empty((0, len(current["variable_ids"])), dtype=float)
            ranges = [
                hi - lo
                for lo, hi in zip(current["lower_bounds"], current["upper_bounds"], strict=True)
            ]
            if current["accepted_gradient"] is None or current["accepted_objective"] is None:
                raise ValueError("GCMMA accepted evidence is incomplete")
            df0dx = _column(
                np,
                [
                    -item * scale
                    for item, scale in zip(current["accepted_gradient"], ranges, strict=True)
                ],
            )
            raa0, raa = runtime["raaupdate"](
                xmma,
                xval,
                xmin,
                xmax,
                low,
                upp,
                np.asarray([[-candidate]], dtype=float),
                empty_vector,
                np.asarray([[proposal["minimization_objective_approximation"]]], dtype=float),
                empty_vector,
                np.asarray([[current["raa0"]]], dtype=float),
                empty_vector,
                1e-5,
                empty_vector,
                1e-7,
            )
            result = runtime["gcmmasub"](
                0,
                len(current["variable_ids"]),
                proposal["outer_iteration"],
                1e-7,
                xval,
                xmin,
                xmax,
                low,
                upp,
                raa0,
                raa,
                np.asarray([[-current["accepted_objective"]]], dtype=float),
                df0dx,
                empty_vector,
                empty_matrix,
                1.0,
                empty_vector,
                empty_vector,
                empty_vector,
            )
            normalized = [float(item) for item in result[0].reshape(-1)]
            values = [
                lo + item * (hi - lo)
                for item, lo, hi in zip(
                    normalized,
                    current["lower_bounds"],
                    current["upper_bounds"],
                    strict=True,
                )
            ]
            revised = {
                "schema_name": GCMMA_PROPOSAL_SCHEMA_NAME,
                "schema_version": GCMMA_PROPOSAL_SCHEMA_VERSION,
                "outer_iteration": proposal["outer_iteration"],
                "inner_iteration": next_inner,
                "base_state_fingerprint": current["state_fingerprint"],
                "revised_from_proposal_fingerprint": proposal["proposal_fingerprint"],
                "rejected_candidate_objective": candidate,
                "normalized_values": normalized,
                "physical_values": values,
                "minimization_objective_approximation": float(result[-2].reshape(-1)[0]),
                "condition_solves_before_proposal": used,
                "move_limit": current["move_limit"],
                "automatic_fallback_used": False,
            }
            revised["proposal_fingerprint"] = domain_sha256_v2(GCMMA_PROPOSAL_SCHEMA_NAME, revised)
            body["raa0"] = float(np.asarray(raa0).reshape(-1)[0])
            body["pending_proposal"] = revised
            body["status"] = "proposal_pending"
        body["state_fingerprint"] = domain_sha256_v2(GCMMA_STATE_SCHEMA_NAME, body)
        return body
    body.update(
        {
            "second_previous_normalized": current["previous_normalized"],
            "previous_normalized": current["current_normalized"],
            "current_normalized": proposal["normalized_values"],
            "current_values": proposal["physical_values"],
            "outer_iteration": proposal["outer_iteration"],
            "inner_iteration": 0,
            "accepted_objective": candidate,
            "accepted_gradient": None,
            "pending_proposal": None,
            "status": (
                "complete"
                if proposal["outer_iteration"] >= current["max_iterations"]
                else "accepted"
            ),
        }
    )
    body["state_fingerprint"] = domain_sha256_v2(GCMMA_STATE_SCHEMA_NAME, body)
    return body


__all__ = [
    "GCMMA_PROPOSAL_SCHEMA_NAME",
    "GCMMA_PROPOSAL_SCHEMA_VERSION",
    "GCMMA_STATE_SCHEMA_NAME",
    "GCMMA_STATE_SCHEMA_VERSION",
    "accept_gcmma_candidate",
    "create_gcmma_state",
    "normalize_gcmma_state",
    "propose_gcmma_candidate",
    "verify_mmapy_backend",
]
