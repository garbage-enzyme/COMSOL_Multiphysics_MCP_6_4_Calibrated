"""Explicitly loaded bounded Gaussian-process expected-improvement acquisition."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from importlib import import_module
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import normalize_design_space
from .optimizers import (
    OPTIMIZER_EXPLANATION_SCHEMA_NAME,
    OPTIMIZER_EXPLANATION_SCHEMA_VERSION,
    OPTIMIZER_PROPOSAL_SCHEMA_NAME,
    OPTIMIZER_PROPOSAL_SCHEMA_VERSION,
    OPTIMIZER_STATE_SCHEMA_NAME,
    OPTIMIZER_STATE_SCHEMA_VERSION,
    _fingerprint,
    _lhs_proposal_values,
    _normalize_losses,
    _normalize_proposal,
    _seed,
)
from .state import normalize_optimizer_checkpoint

MAX_GP_OBSERVATIONS = 256
MAX_GP_CANDIDATES = 4096
GP_BACKEND_NAME = "internal_gaussian_process_expected_improvement"
GP_BACKEND_VERSION = "1.0.0"


def _positive_finite(value: object, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or (number == 0.0 and not allow_zero):
        kind = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {kind} and finite")
    return number


def _continuous_axes(design_space: object) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    space = normalize_design_space(design_space)
    variables = list(space["variables"])
    if not variables or any(item["kind"] != "continuous" for item in variables):
        raise ValueError(
            "Gaussian-process acquisition currently requires a continuous design space"
        )
    return space, variables


def _point(
    value: object,
    variables: Sequence[Mapping[str, Any]],
    name: str,
) -> tuple[dict[str, float], tuple[float, ...]]:
    expected = {item["variable_id"] for item in variables}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} must exactly cover the continuous design space")
    normalized: dict[str, float] = {}
    scaled: list[float] = []
    for variable in variables:
        variable_id = variable["variable_id"]
        raw = value[variable_id]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{name}.{variable_id} must be numeric")
        number = float(raw)
        lower = float(variable["lower"])
        upper = float(variable["upper"])
        if not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError(f"{name}.{variable_id} is outside the frozen design space")
        normalized[variable_id] = number
        scaled.append((number - lower) / (upper - lower))
    return {key: normalized[key] for key in sorted(normalized)}, tuple(scaled)


def select_expected_improvement_candidate(
    design_space: object,
    observations: object,
    candidates: object,
    *,
    length_scale: float = 0.25,
    noise: float = 1.0e-10,
    exploration: float = 0.0,
) -> dict[str, Any]:
    """Select one bounded candidate by deterministic GP expected improvement."""
    space, variables = _continuous_axes(design_space)
    length_scale = _positive_finite(length_scale, "length_scale")
    noise = _positive_finite(noise, "noise")
    exploration = _positive_finite(exploration, "exploration", allow_zero=True)
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)):
        raise ValueError("observations must be a sequence")
    if not 2 <= len(observations) <= MAX_GP_OBSERVATIONS:
        raise ValueError(f"observations must contain 2 through {MAX_GP_OBSERVATIONS} points")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        raise ValueError("candidates must be a sequence")
    if not 1 <= len(candidates) <= MAX_GP_CANDIDATES:
        raise ValueError(f"candidates must contain 1 through {MAX_GP_CANDIDATES} points")

    observed_scaled: list[tuple[float, ...]] = []
    losses: list[float] = []
    for index, item in enumerate(observations):
        if not isinstance(item, Mapping) or set(item) != {"values", "loss"}:
            raise ValueError("each observation must contain exactly values and loss")
        _, scaled = _point(item["values"], variables, f"observations[{index}].values")
        loss = _positive_finite(item["loss"], f"observations[{index}].loss", allow_zero=True)
        observed_scaled.append(scaled)
        losses.append(loss)
    if len(set(observed_scaled)) != len(observed_scaled):
        raise ValueError("observations contain duplicate design points")

    candidate_points: list[dict[str, float]] = []
    candidate_scaled: list[tuple[float, ...]] = []
    for index, item in enumerate(candidates):
        point, scaled = _point(item, variables, f"candidates[{index}]")
        candidate_points.append(point)
        candidate_scaled.append(scaled)
    if len(set(candidate_scaled)) != len(candidate_scaled):
        raise ValueError("candidates contain duplicate design points")
    if set(observed_scaled) & set(candidate_scaled):
        raise ValueError("candidates must exclude observed design points")

    # Keep heavy numerical imports behind the explicit adaptive path.
    np = import_module("numpy")
    scipy_linalg = import_module("scipy.linalg")
    scipy_special = import_module("scipy.special")

    x_train: Any = np.asarray(observed_scaled, dtype=np.float64)
    y_train: Any = np.asarray(losses, dtype=np.float64)
    x_candidates: Any = np.asarray(candidate_scaled, dtype=np.float64)
    y_mean = float(np.mean(y_train))
    y_scale = float(np.std(y_train))
    if not math.isfinite(y_scale) or y_scale <= 1.0e-15:
        y_scale = 1.0
    targets = (y_train - y_mean) / y_scale

    def kernel(left: Any, right: Any) -> Any:
        squared = np.sum((left[:, None, :] - right[None, :, :]) ** 2, axis=2)
        return np.exp(-0.5 * squared / (length_scale * length_scale))

    covariance = kernel(x_train, x_train)
    covariance.flat[:: covariance.shape[0] + 1] += noise
    try:
        factor = scipy_linalg.cho_factor(covariance, lower=True, check_finite=True)
        alpha = scipy_linalg.cho_solve(factor, targets, check_finite=True)
        cross = kernel(x_train, x_candidates)
        posterior_mean = y_mean + y_scale * (cross.T @ alpha)
        solved = scipy_linalg.cho_solve(factor, cross, check_finite=True)
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("Gaussian-process covariance is not solvable") from exc
    posterior_variance = np.maximum(0.0, 1.0 - np.sum(cross * solved, axis=0))
    posterior_std = y_scale * np.sqrt(posterior_variance)
    improvement = float(np.min(y_train)) - posterior_mean - exploration
    expected_improvement = np.zeros_like(improvement)
    positive = posterior_std > 0.0
    z = np.zeros_like(improvement)
    z[positive] = improvement[positive] / posterior_std[positive]
    expected_improvement[positive] = improvement[positive] * scipy_special.ndtr(
        z[positive]
    ) + posterior_std[positive] * np.exp(-0.5 * z[positive] ** 2) / math.sqrt(2.0 * math.pi)
    expected_improvement[~positive] = np.maximum(0.0, improvement[~positive])
    if not (
        np.all(np.isfinite(posterior_mean))
        and np.all(np.isfinite(posterior_std))
        and np.all(np.isfinite(expected_improvement))
    ):
        raise ValueError("Gaussian-process acquisition produced nonfinite evidence")
    selected = int(np.flatnonzero(expected_improvement == np.max(expected_improvement))[0])
    return {
        "selected_index": selected,
        "values": candidate_points[selected],
        "posterior_mean": float(posterior_mean[selected]),
        "posterior_standard_deviation": float(posterior_std[selected]),
        "expected_improvement": float(expected_improvement[selected]),
        "observed_best_loss": float(np.min(y_train)),
        "observation_count": len(observed_scaled),
        "candidate_count": len(candidate_points),
        "space_fingerprint": space["space_fingerprint"],
        "settings": {
            "length_scale": length_scale,
            "noise": noise,
            "exploration": exploration,
        },
    }


class GaussianProcessExpectedImprovementOptimizer:
    """Deterministic LHS warmup followed by bounded GP expected improvement."""

    backend_name = GP_BACKEND_NAME
    backend_version = GP_BACKEND_VERSION
    strategy = "lhs_warmup_then_bounded_expected_improvement"

    def __init__(
        self,
        design_space: object,
        *,
        seed: int,
        warmup_count: int = 8,
        candidate_pool_count: int = 256,
    ) -> None:
        self.space = normalize_design_space(design_space)
        _, variables = _continuous_axes(self.space)
        if (
            not isinstance(warmup_count, int)
            or isinstance(warmup_count, bool)
            or not 2 <= warmup_count <= 256
        ):
            raise ValueError("warmup_count must be an integer from 2 through 256")
        if (
            not isinstance(candidate_pool_count, int)
            or isinstance(candidate_pool_count, bool)
            or not 1 <= candidate_pool_count <= MAX_GP_CANDIDATES
        ):
            raise ValueError(
                f"candidate_pool_count must be an integer from 1 through {MAX_GP_CANDIDATES}"
            )
        if warmup_count > candidate_pool_count:
            raise ValueError("warmup_count cannot exceed candidate_pool_count")
        self.seed = _seed(seed)
        self.warmup_count = warmup_count
        self.candidate_pool_count = candidate_pool_count
        self.backend_identity = domain_sha256_v2(
            "comsol_mcp.research_optimizer_backend",
            {
                "name": GP_BACKEND_NAME,
                "version": GP_BACKEND_VERSION,
                "space_fingerprint": self.space["space_fingerprint"],
                "seed": self.seed,
                "warmup_count": warmup_count,
                "candidate_pool_count": candidate_pool_count,
            },
        )
        self.next_index = 0
        self.observations: dict[str, dict[str, Any]] = {}
        self.proposals: dict[int, dict[str, Any]] = {}

    def _values(self, index: int) -> dict[str, Any]:

        return _lhs_proposal_values(self.space, self.seed, self.candidate_pool_count, index)

    def _proposal(self, index: int, values: Mapping[str, Any]) -> dict[str, Any]:
        body = {
            "schema_name": OPTIMIZER_PROPOSAL_SCHEMA_NAME,
            "schema_version": OPTIMIZER_PROPOSAL_SCHEMA_VERSION,
            "backend_identity": self.backend_identity,
            "space_fingerprint": self.space["space_fingerprint"],
            "proposal_index": index,
            "values": dict(values),
        }
        return {
            **body,
            "proposal_fingerprint": domain_sha256_v2(OPTIMIZER_PROPOSAL_SCHEMA_NAME, body),
        }

    def ask(self) -> dict[str, Any]:
        if self.next_index >= self.candidate_pool_count:
            raise ValueError("Gaussian-process candidate pool is exhausted")
        available = [
            index
            for index in range(self.candidate_pool_count)
            if self._values(index) not in [item["values"] for item in self.proposals.values()]
        ]
        if not available:
            raise ValueError("Gaussian-process candidate pool is exhausted")
        selected = available[0]
        completed = [item for item in self.observations.values() if item["status"] == "completed"]
        if len(completed) >= self.warmup_count:
            observations = [
                {"values": item["values"], "loss": math.fsum(item["losses"].values())}
                for item in completed
            ]
            acquisition = select_expected_improvement_candidate(
                self.space,
                observations,
                [self._values(index) for index in available],
            )
            selected = available[int(acquisition["selected_index"])]
        proposal = self._proposal(self.next_index, self._values(selected))
        self.proposals[self.next_index] = proposal
        self.next_index += 1
        return proposal

    def tell(
        self,
        proposal: object,
        *,
        candidate_fingerprint: str,
        status: str,
        score_fingerprint: str | None,
        losses: object,
    ) -> bool:
        normalized = _normalize_proposal(proposal)
        if (
            normalized["backend_identity"] != self.backend_identity
            or normalized["space_fingerprint"] != self.space["space_fingerprint"]
        ):
            raise ValueError("optimizer proposal belongs to another backend or space")
        index = normalized["proposal_index"]
        if self.proposals.get(index) != normalized:
            raise ValueError("optimizer proposal does not match an exact asked proposal")
        if status not in {"completed", "failed", "infeasible"}:
            raise ValueError("optimizer result status is unsupported")
        candidate = str(_fingerprint(candidate_fingerprint, "candidate_fingerprint"))
        score = _fingerprint(score_fingerprint, "score_fingerprint", optional=True)
        normalized_losses = _normalize_losses(losses, required=status == "completed")
        if status == "completed" and (
            score is None or any(value < 0.0 for value in normalized_losses.values())
        ):
            raise ValueError("completed adaptive results require nonnegative losses and a score")
        if status != "completed" and (score is not None or normalized_losses):
            raise ValueError("unsuccessful optimizer results cannot contain scores or losses")
        observation = {
            "proposal_fingerprint": normalized["proposal_fingerprint"],
            "proposal_index": index,
            "values": normalized["values"],
            "candidate_fingerprint": candidate,
            "status": status,
            "score_fingerprint": score,
            "losses": normalized_losses,
        }
        existing = self.observations.get(candidate)
        if existing is not None:
            if existing != observation:
                raise ValueError("optimizer candidate already has a conflicting result")
            return False
        self.observations[candidate] = observation
        return True

    def state(self) -> dict[str, Any]:
        body = {
            "schema_name": OPTIMIZER_STATE_SCHEMA_NAME,
            "schema_version": OPTIMIZER_STATE_SCHEMA_VERSION,
            "backend": {
                "name": GP_BACKEND_NAME,
                "version": GP_BACKEND_VERSION,
                "identity": self.backend_identity,
            },
            "space_fingerprint": self.space["space_fingerprint"],
            "next_proposal_index": self.next_index,
            "proposal_limit": self.candidate_pool_count,
            "remaining_proposals": self.candidate_pool_count - self.next_index,
            "observation_count": len(self.observations),
            "status_counts": {
                status: sum(item["status"] == status for item in self.observations.values())
                for status in ("completed", "failed", "infeasible")
            },
        }
        return {**body, "state_fingerprint": domain_sha256_v2(OPTIMIZER_STATE_SCHEMA_NAME, body)}

    def explain(self, proposal: object) -> dict[str, Any]:
        normalized = _normalize_proposal(proposal)
        if self.proposals.get(normalized["proposal_index"]) != normalized:
            raise ValueError("optimizer can only explain an exact asked proposal")
        body = {
            "schema_name": OPTIMIZER_EXPLANATION_SCHEMA_NAME,
            "schema_version": OPTIMIZER_EXPLANATION_SCHEMA_VERSION,
            "backend": {
                "name": GP_BACKEND_NAME,
                "version": GP_BACKEND_VERSION,
                "identity": self.backend_identity,
            },
            "space_fingerprint": self.space["space_fingerprint"],
            "proposal_fingerprint": normalized["proposal_fingerprint"],
            "proposal_index": normalized["proposal_index"],
            "strategy": self.strategy,
            "uses_observations": normalized["proposal_index"] >= self.warmup_count,
            "parameters": {
                "warmup_count": self.warmup_count,
                "candidate_pool_count": self.candidate_pool_count,
            },
        }
        return {
            **body,
            "explanation_fingerprint": domain_sha256_v2(OPTIMIZER_EXPLANATION_SCHEMA_NAME, body),
        }

    def checkpoint(
        self, *, campaign_fingerprint: str, decision_fingerprint: str, created_at: str
    ) -> dict[str, Any]:
        observations = [self.observations[key] for key in sorted(self.observations)]
        history = {
            "observations": observations,
            "proposals": [self.proposals[key] for key in sorted(self.proposals)],
        }
        return normalize_optimizer_checkpoint(
            {
                "schema_name": "comsol_mcp.research_optimizer_checkpoint",
                "schema_version": "1.0.0",
                "campaign_fingerprint": campaign_fingerprint,
                "sequence": self.next_index,
                "decision_fingerprint": decision_fingerprint,
                "backend": {
                    "name": GP_BACKEND_NAME,
                    "version": GP_BACKEND_VERSION,
                    "identity": self.backend_identity,
                },
                "random_state": {
                    "seed": self.seed,
                    "warmup_count": self.warmup_count,
                    "candidate_pool_count": self.candidate_pool_count,
                    "next_index": self.next_index,
                },
                "optimizer_state": history,
                "history_fingerprint": domain_sha256_v2(
                    "comsol_mcp.research_optimizer_history", history
                ),
                "candidate_fingerprints": sorted(self.observations),
                "created_at": created_at,
            }
        )

    @classmethod
    def restore(
        cls, design_space: object, checkpoint: object
    ) -> "GaussianProcessExpectedImprovementOptimizer":
        normalized = normalize_optimizer_checkpoint(checkpoint)
        state = normalized["random_state"]
        if not isinstance(state, Mapping):
            raise ValueError("adaptive random state must be an object")
        optimizer = cls(
            design_space,
            seed=state["seed"],
            warmup_count=state["warmup_count"],
            candidate_pool_count=state["candidate_pool_count"],
        )
        if (
            normalized["backend"]["identity"] != optimizer.backend_identity
            or normalized["sequence"] != state["next_index"]
        ):
            raise ValueError("adaptive checkpoint backend identity is incompatible")
        history = normalized["optimizer_state"]
        if (
            not isinstance(history, Mapping)
            or not isinstance(history.get("observations"), list)
            or not isinstance(history.get("proposals"), list)
        ):
            raise ValueError("adaptive checkpoint history is invalid")
        if normalized["history_fingerprint"] != domain_sha256_v2(
            "comsol_mcp.research_optimizer_history", history
        ):
            raise ValueError("adaptive checkpoint history identity is invalid")
        optimizer.next_index = state["next_index"]
        optimizer.proposals = {item["proposal_index"]: item for item in history["proposals"]}
        optimizer.observations = {
            item["candidate_fingerprint"]: item for item in history["observations"]
        }
        return optimizer


__all__ = [
    "MAX_GP_CANDIDATES",
    "MAX_GP_OBSERVATIONS",
    "GP_BACKEND_NAME",
    "GP_BACKEND_VERSION",
    "GaussianProcessExpectedImprovementOptimizer",
    "select_expected_improvement_candidate",
]
