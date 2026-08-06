"""Backend-neutral optimizer protocol and deterministic random baseline."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _timestamp, normalize_design_space
from .state import normalize_optimizer_checkpoint

OPTIMIZER_PROPOSAL_SCHEMA_NAME = "comsol_mcp.research_optimizer_proposal"
OPTIMIZER_PROPOSAL_SCHEMA_VERSION = "1.0.0"
OPTIMIZER_STATE_SCHEMA_NAME = "comsol_mcp.research_optimizer_state"
OPTIMIZER_STATE_SCHEMA_VERSION = "1.0.0"
OPTIMIZER_EXPLANATION_SCHEMA_NAME = "comsol_mcp.research_optimizer_explanation"
OPTIMIZER_EXPLANATION_SCHEMA_VERSION = "1.0.0"
RANDOM_BACKEND_NAME = "deterministic_random"
RANDOM_BACKEND_VERSION = "1.0.0"
LHS_BACKEND_NAME = "deterministic_latin_hypercube"
LHS_BACKEND_VERSION = "1.0.0"
GRID_BACKEND_NAME = "deterministic_grid"
GRID_BACKEND_VERSION = "1.0.0"
MAX_PROPOSALS = 1_000_000_000
MAX_LHS_SAMPLES = 4096
MAX_GRID_LEVELS = 64
MAX_GRID_SAMPLES = 4096
MAX_OBJECTIVE_LOSSES = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESULT_STATUSES = {"completed", "failed", "infeasible"}


@runtime_checkable
class ResearchOptimizerProtocol(Protocol):
    """Backend-neutral ask/tell/state/explain/checkpoint contract."""

    backend_identity: str

    def ask(self) -> dict[str, Any]: ...

    def tell(
        self,
        proposal: object,
        *,
        candidate_fingerprint: str,
        status: str,
        score_fingerprint: str | None,
        losses: object,
    ) -> bool: ...

    def state(self) -> dict[str, Any]: ...

    def explain(self, proposal: object) -> dict[str, Any]: ...

    def checkpoint(
        self,
        *,
        campaign_fingerprint: str,
        decision_fingerprint: str,
        created_at: str,
    ) -> dict[str, Any]: ...


def _fingerprint(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return value


def _seed(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= (1 << 63) - 1:
        raise ValueError("seed must be a bounded nonnegative integer")
    return value


def _stream(seed: int, index: int, variable_id: str, purpose: str) -> int:
    payload = f"{seed}\0{index}\0{variable_id}\0{purpose}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def _unit_interval(seed: int, index: int, variable_id: str, purpose: str = "unit") -> float:
    return (_stream(seed, index, variable_id, purpose) >> (256 - 53)) / float(1 << 53)


def _sample_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LHS_SAMPLES:
        raise ValueError(f"sample_count must be an integer from 1 to {MAX_LHS_SAMPLES}")
    return value


def _lhs_stratum(seed: int, sample_count: int, index: int, variable_id: str) -> int:
    if sample_count == 1:
        return 0
    multiplier = _stream(seed, sample_count, variable_id, "lhs_multiplier") % sample_count
    multiplier = max(1, multiplier)
    while math.gcd(multiplier, sample_count) != 1:
        multiplier += 1
    offset = _stream(seed, sample_count, variable_id, "lhs_offset") % sample_count
    return (multiplier * index + offset) % sample_count


def _proposal_values(space: Mapping[str, Any], seed: int, index: int) -> dict[str, Any]:
    digits = space["canonicalization"]["float_digits"]
    values: dict[str, Any] = {}
    for variable in space["variables"]:
        variable_id = variable["variable_id"]
        kind = variable["kind"]
        if kind in {"categorical", "ordinal"}:
            allowed = variable["allowed_values"]
            position = _stream(seed, index, variable_id, "category") % len(allowed)
            values[variable_id] = allowed[position]
        elif kind == "integer":
            integer_lower = int(variable["lower"])
            integer_upper = int(variable["upper"])
            values[variable_id] = integer_lower + (
                _stream(seed, index, variable_id, "integer") % (integer_upper - integer_lower + 1)
            )
        else:
            continuous_lower = float(variable["lower"])
            continuous_upper = float(variable["upper"])
            unit = _unit_interval(seed, index, variable_id)
            values[variable_id] = round(
                continuous_lower + (continuous_upper - continuous_lower) * unit, digits
            )
    return {key: values[key] for key in sorted(values)}


def _lhs_proposal_values(
    space: Mapping[str, Any], seed: int, sample_count: int, index: int
) -> dict[str, Any]:
    digits = space["canonicalization"]["float_digits"]
    values: dict[str, Any] = {}
    for variable in space["variables"]:
        variable_id = variable["variable_id"]
        kind = variable["kind"]
        stratum = _lhs_stratum(seed, sample_count, index, variable_id)
        if kind in {"categorical", "ordinal"}:
            allowed = variable["allowed_values"]
            values[variable_id] = allowed[stratum % len(allowed)]
            continue
        jitter = _unit_interval(seed, index, variable_id, "lhs_jitter")
        unit = (stratum + jitter) / sample_count
        if kind == "integer":
            integer_lower = int(variable["lower"])
            integer_upper = int(variable["upper"])
            level_count = integer_upper - integer_lower + 1
            values[variable_id] = integer_lower + min(level_count - 1, int(unit * level_count))
            continue
        continuous_lower = float(variable["lower"])
        continuous_upper = float(variable["upper"])
        values[variable_id] = round(
            continuous_lower + (continuous_upper - continuous_lower) * unit, digits
        )
    return {key: values[key] for key in sorted(values)}


def _grid_levels(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_GRID_LEVELS:
        raise ValueError(f"levels must be an integer from 1 to {MAX_GRID_LEVELS}")
    return value


def _grid_axes(space: Mapping[str, Any], levels: int) -> dict[str, list[Any]]:
    digits = space["canonicalization"]["float_digits"]
    axes: dict[str, list[Any]] = {}
    for variable in space["variables"]:
        variable_id = variable["variable_id"]
        kind = variable["kind"]
        if kind in {"categorical", "ordinal"}:
            axis = list(variable["allowed_values"])
        elif levels == 1:
            axis = [variable["baseline"]]
        elif kind == "integer":
            integer_lower = int(variable["lower"])
            integer_upper = int(variable["upper"])
            available = integer_upper - integer_lower + 1
            count = min(levels, available)
            axis = [
                integer_lower + round(index * (available - 1) / (count - 1))
                for index in range(count)
            ]
        else:
            continuous_lower = float(variable["lower"])
            continuous_upper = float(variable["upper"])
            axis = [
                round(
                    continuous_lower + index * (continuous_upper - continuous_lower) / (levels - 1),
                    digits,
                )
                for index in range(levels)
            ]
        if len(axis) != len({repr(item) for item in axis}):
            raise ValueError("grid levels collapse under design-space canonicalization")
        axes[variable_id] = axis
    sample_count = math.prod(len(axis) for axis in axes.values())
    if sample_count > MAX_GRID_SAMPLES:
        raise ValueError(f"grid contains more than {MAX_GRID_SAMPLES} samples")
    return {key: axes[key] for key in sorted(axes)}


def _grid_axis_positions(axes: Mapping[str, list[Any]], index: int) -> dict[str, int]:
    positions: dict[str, int] = {}
    remaining = index
    for variable_id in reversed(list(axes)):
        size = len(axes[variable_id])
        positions[variable_id] = remaining % size
        remaining //= size
    return {key: positions[key] for key in sorted(positions)}


def _grid_proposal_values(axes: Mapping[str, list[Any]], index: int) -> dict[str, Any]:
    positions = _grid_axis_positions(axes, index)
    return {key: axes[key][positions[key]] for key in sorted(axes)}


def _normalize_proposal(value: object) -> dict[str, Any]:
    bounded = _bounded_json(value, "optimizer proposal", 256 * 1024)
    supplied_fingerprint = None
    if isinstance(bounded, dict) and "proposal_fingerprint" in bounded:
        supplied_fingerprint = bounded.pop("proposal_fingerprint")
    raw = _object(
        bounded,
        {
            "schema_name",
            "schema_version",
            "backend_identity",
            "space_fingerprint",
            "proposal_index",
            "values",
        },
        "optimizer proposal",
    )
    if (
        raw["schema_name"] != OPTIMIZER_PROPOSAL_SCHEMA_NAME
        or raw["schema_version"] != OPTIMIZER_PROPOSAL_SCHEMA_VERSION
    ):
        raise ValueError("optimizer proposal schema identity is unsupported")
    index = raw["proposal_index"]
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_PROPOSALS:
        raise ValueError("proposal_index is invalid")
    body = {
        "schema_name": OPTIMIZER_PROPOSAL_SCHEMA_NAME,
        "schema_version": OPTIMIZER_PROPOSAL_SCHEMA_VERSION,
        "backend_identity": _fingerprint(raw["backend_identity"], "backend_identity"),
        "space_fingerprint": _fingerprint(raw["space_fingerprint"], "space_fingerprint"),
        "proposal_index": index,
        "values": _bounded_json(raw["values"], "proposal.values", 64 * 1024),
    }
    calculated = domain_sha256_v2(OPTIMIZER_PROPOSAL_SCHEMA_NAME, body)
    if supplied_fingerprint is not None and supplied_fingerprint != calculated:
        raise ValueError("optimizer proposal fingerprint is invalid")
    return {**body, "proposal_fingerprint": calculated}


def _normalize_losses(value: object, *, required: bool) -> dict[str, float]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError("losses must be an object with string keys")
    if len(value) > MAX_OBJECTIVE_LOSSES or (required and not value):
        raise ValueError("losses must have a bounded required size")
    normalized: dict[str, float] = {}
    for key, raw in value.items():
        objective_id = _identifier(key, "losses objective_id")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("loss values must be finite numbers")
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError("loss values must be finite numbers")
        normalized[objective_id] = number
    return {key: normalized[key] for key in sorted(normalized)}


class DeterministicRandomOptimizer:
    """Dependency-free ask/tell baseline with replayable counter-based streams."""

    backend_name = RANDOM_BACKEND_NAME
    backend_version = RANDOM_BACKEND_VERSION
    strategy = "independent_sha256_counter_samples"

    def __init__(self, design_space: object, *, seed: int) -> None:
        self.space = normalize_design_space(design_space)
        self.seed = _seed(seed)
        backend_body = {
            "name": RANDOM_BACKEND_NAME,
            "version": RANDOM_BACKEND_VERSION,
            "space_fingerprint": self.space["space_fingerprint"],
            "seed": self.seed,
        }
        self.backend_identity = domain_sha256_v2(
            "comsol_mcp.research_optimizer_backend", backend_body
        )
        self.next_index = 0
        self.observations: dict[str, dict[str, Any]] = {}

    def _proposal(self, index: int) -> dict[str, Any]:
        body = {
            "schema_name": OPTIMIZER_PROPOSAL_SCHEMA_NAME,
            "schema_version": OPTIMIZER_PROPOSAL_SCHEMA_VERSION,
            "backend_identity": self.backend_identity,
            "space_fingerprint": self.space["space_fingerprint"],
            "proposal_index": index,
            "values": _proposal_values(self.space, self.seed, index),
        }
        return {
            **body,
            "proposal_fingerprint": domain_sha256_v2(OPTIMIZER_PROPOSAL_SCHEMA_NAME, body),
        }

    def ask(self) -> dict[str, Any]:
        if self.next_index >= MAX_PROPOSALS:
            raise ValueError("optimizer proposal limit is exhausted")
        proposal = self._proposal(self.next_index)
        self.next_index += 1
        return proposal

    def _proposal_limit(self) -> int:
        return MAX_PROPOSALS

    def _explanation_parameters(self, index: int) -> dict[str, Any]:
        return {"counter_index": index}

    def state(self) -> dict[str, Any]:
        status_counts = {
            status: sum(item["status"] == status for item in self.observations.values())
            for status in sorted(_RESULT_STATUSES)
        }
        body = {
            "schema_name": OPTIMIZER_STATE_SCHEMA_NAME,
            "schema_version": OPTIMIZER_STATE_SCHEMA_VERSION,
            "backend": {
                "name": self.backend_name,
                "version": self.backend_version,
                "identity": self.backend_identity,
            },
            "space_fingerprint": self.space["space_fingerprint"],
            "next_proposal_index": self.next_index,
            "proposal_limit": self._proposal_limit(),
            "remaining_proposals": self._proposal_limit() - self.next_index,
            "observation_count": len(self.observations),
            "status_counts": status_counts,
        }
        return {
            **body,
            "state_fingerprint": domain_sha256_v2(OPTIMIZER_STATE_SCHEMA_NAME, body),
        }

    def explain(self, proposal: object) -> dict[str, Any]:
        normalized = _normalize_proposal(proposal)
        index = normalized["proposal_index"]
        if (
            normalized["backend_identity"] != self.backend_identity
            or normalized["space_fingerprint"] != self.space["space_fingerprint"]
        ):
            raise ValueError("optimizer proposal belongs to another backend or space")
        if index >= self.next_index or normalized != self._proposal(index):
            raise ValueError("optimizer can only explain an exact proposal already asked")
        body = {
            "schema_name": OPTIMIZER_EXPLANATION_SCHEMA_NAME,
            "schema_version": OPTIMIZER_EXPLANATION_SCHEMA_VERSION,
            "backend": {
                "name": self.backend_name,
                "version": self.backend_version,
                "identity": self.backend_identity,
            },
            "space_fingerprint": self.space["space_fingerprint"],
            "proposal_fingerprint": normalized["proposal_fingerprint"],
            "proposal_index": index,
            "strategy": self.strategy,
            "uses_observations": False,
            "parameters": self._explanation_parameters(index),
        }
        return {
            **body,
            "explanation_fingerprint": domain_sha256_v2(OPTIMIZER_EXPLANATION_SCHEMA_NAME, body),
        }

    def tell(
        self,
        proposal: object,
        *,
        candidate_fingerprint: str,
        status: str,
        score_fingerprint: str | None,
        losses: object,
    ) -> bool:
        normalized_proposal = _normalize_proposal(proposal)
        if (
            normalized_proposal["backend_identity"] != self.backend_identity
            or normalized_proposal["space_fingerprint"] != self.space["space_fingerprint"]
        ):
            raise ValueError("optimizer proposal belongs to another backend or space")
        if normalized_proposal["proposal_index"] >= self.next_index:
            raise ValueError("optimizer cannot accept a proposal that was not asked")
        if normalized_proposal != self._proposal(normalized_proposal["proposal_index"]):
            raise ValueError("optimizer proposal does not match the deterministic ask stream")
        if status not in _RESULT_STATUSES:
            raise ValueError("optimizer result status is unsupported")
        candidate = str(_fingerprint(candidate_fingerprint, "candidate_fingerprint"))
        score = _fingerprint(score_fingerprint, "score_fingerprint", optional=True)
        normalized_losses = _normalize_losses(losses, required=status == "completed")
        if status == "completed" and score is None:
            raise ValueError("completed optimizer results require a score fingerprint")
        if status != "completed" and (score is not None or normalized_losses):
            raise ValueError("unsuccessful optimizer results cannot contain scores or losses")
        observation = {
            "proposal_fingerprint": normalized_proposal["proposal_fingerprint"],
            "proposal_index": normalized_proposal["proposal_index"],
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
        if any(
            item["proposal_fingerprint"] == observation["proposal_fingerprint"]
            for item in self.observations.values()
        ):
            raise ValueError("optimizer proposal already belongs to another candidate")
        self.observations[candidate] = observation
        return True

    def checkpoint(
        self,
        *,
        campaign_fingerprint: str,
        decision_fingerprint: str,
        created_at: str,
    ) -> dict[str, Any]:
        observations = [self.observations[key] for key in sorted(self.observations)]
        history_fingerprint = domain_sha256_v2(
            "comsol_mcp.research_optimizer_history", observations
        )
        return normalize_optimizer_checkpoint(
            {
                "schema_name": "comsol_mcp.research_optimizer_checkpoint",
                "schema_version": "1.0.0",
                "campaign_fingerprint": campaign_fingerprint,
                "sequence": self.next_index,
                "decision_fingerprint": decision_fingerprint,
                "backend": {
                    "name": RANDOM_BACKEND_NAME,
                    "version": RANDOM_BACKEND_VERSION,
                    "identity": self.backend_identity,
                },
                "random_state": {"seed": self.seed, "next_index": self.next_index},
                "optimizer_state": {"observations": observations},
                "history_fingerprint": history_fingerprint,
                "candidate_fingerprints": sorted(self.observations),
                "created_at": _timestamp(created_at, "created_at"),
            }
        )

    @classmethod
    def restore(cls, design_space: object, checkpoint: object) -> "DeterministicRandomOptimizer":
        normalized = normalize_optimizer_checkpoint(checkpoint)
        random_state = _object(
            normalized["random_state"], {"seed", "next_index"}, "checkpoint.random_state"
        )
        optimizer = cls(design_space, seed=_seed(random_state["seed"]))
        if (
            normalized["backend"]["name"] != RANDOM_BACKEND_NAME
            or normalized["backend"]["version"] != RANDOM_BACKEND_VERSION
            or normalized["backend"]["identity"] != optimizer.backend_identity
        ):
            raise ValueError("optimizer checkpoint backend identity is incompatible")
        next_index = random_state["next_index"]
        if (
            isinstance(next_index, bool)
            or not isinstance(next_index, int)
            or not 0 <= next_index < MAX_PROPOSALS
            or next_index != normalized["sequence"]
        ):
            raise ValueError("optimizer checkpoint proposal sequence is invalid")
        state = _object(normalized["optimizer_state"], {"observations"}, "optimizer_state")
        observations = state["observations"]
        if not isinstance(observations, list):
            raise ValueError("optimizer checkpoint observations must be a list")
        restored: dict[str, dict[str, Any]] = {}
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("optimizer checkpoint observations must be objects")
            candidate = _fingerprint(
                observation.get("candidate_fingerprint"), "candidate_fingerprint"
            )
            restored[str(candidate)] = dict(observation)
        ordered = [restored[key] for key in sorted(restored)]
        if normalized["history_fingerprint"] != domain_sha256_v2(
            "comsol_mcp.research_optimizer_history", ordered
        ) or normalized["candidate_fingerprints"] != sorted(restored):
            raise ValueError("optimizer checkpoint history identity is invalid")
        optimizer.next_index = next_index
        optimizer.observations = restored
        return optimizer


class DeterministicLatinHypercubeOptimizer(DeterministicRandomOptimizer):
    """Finite deterministic Latin-hypercube baseline for mixed design spaces."""

    backend_name = LHS_BACKEND_NAME
    backend_version = LHS_BACKEND_VERSION
    strategy = "per_variable_affine_permutation_with_stratum_jitter"

    def __init__(self, design_space: object, *, seed: int, sample_count: int) -> None:
        self.space = normalize_design_space(design_space)
        self.seed = _seed(seed)
        self.sample_count = _sample_count(sample_count)
        backend_body = {
            "name": LHS_BACKEND_NAME,
            "version": LHS_BACKEND_VERSION,
            "space_fingerprint": self.space["space_fingerprint"],
            "seed": self.seed,
            "sample_count": self.sample_count,
        }
        self.backend_identity = domain_sha256_v2(
            "comsol_mcp.research_optimizer_backend", backend_body
        )
        self.next_index = 0
        self.observations: dict[str, dict[str, Any]] = {}

    def _proposal(self, index: int) -> dict[str, Any]:
        body = {
            "schema_name": OPTIMIZER_PROPOSAL_SCHEMA_NAME,
            "schema_version": OPTIMIZER_PROPOSAL_SCHEMA_VERSION,
            "backend_identity": self.backend_identity,
            "space_fingerprint": self.space["space_fingerprint"],
            "proposal_index": index,
            "values": _lhs_proposal_values(self.space, self.seed, self.sample_count, index),
        }
        return {
            **body,
            "proposal_fingerprint": domain_sha256_v2(OPTIMIZER_PROPOSAL_SCHEMA_NAME, body),
        }

    def ask(self) -> dict[str, Any]:
        if self.next_index >= self.sample_count:
            raise ValueError("Latin-hypercube sample limit is exhausted")
        proposal = self._proposal(self.next_index)
        self.next_index += 1
        return proposal

    def _proposal_limit(self) -> int:
        return self.sample_count

    def _explanation_parameters(self, index: int) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "strata": {
                variable["variable_id"]: _lhs_stratum(
                    self.seed, self.sample_count, index, variable["variable_id"]
                )
                for variable in self.space["variables"]
            },
        }

    def checkpoint(
        self,
        *,
        campaign_fingerprint: str,
        decision_fingerprint: str,
        created_at: str,
    ) -> dict[str, Any]:
        observations = [self.observations[key] for key in sorted(self.observations)]
        history_fingerprint = domain_sha256_v2(
            "comsol_mcp.research_optimizer_history", observations
        )
        return normalize_optimizer_checkpoint(
            {
                "schema_name": "comsol_mcp.research_optimizer_checkpoint",
                "schema_version": "1.0.0",
                "campaign_fingerprint": campaign_fingerprint,
                "sequence": self.next_index,
                "decision_fingerprint": decision_fingerprint,
                "backend": {
                    "name": LHS_BACKEND_NAME,
                    "version": LHS_BACKEND_VERSION,
                    "identity": self.backend_identity,
                },
                "random_state": {
                    "seed": self.seed,
                    "sample_count": self.sample_count,
                    "next_index": self.next_index,
                },
                "optimizer_state": {"observations": observations},
                "history_fingerprint": history_fingerprint,
                "candidate_fingerprints": sorted(self.observations),
                "created_at": _timestamp(created_at, "created_at"),
            }
        )

    @classmethod
    def restore(
        cls, design_space: object, checkpoint: object
    ) -> "DeterministicLatinHypercubeOptimizer":
        normalized = normalize_optimizer_checkpoint(checkpoint)
        random_state = _object(
            normalized["random_state"],
            {"seed", "sample_count", "next_index"},
            "checkpoint.random_state",
        )
        optimizer = cls(
            design_space,
            seed=_seed(random_state["seed"]),
            sample_count=_sample_count(random_state["sample_count"]),
        )
        if (
            normalized["backend"]["name"] != LHS_BACKEND_NAME
            or normalized["backend"]["version"] != LHS_BACKEND_VERSION
            or normalized["backend"]["identity"] != optimizer.backend_identity
        ):
            raise ValueError("optimizer checkpoint backend identity is incompatible")
        next_index = random_state["next_index"]
        if (
            isinstance(next_index, bool)
            or not isinstance(next_index, int)
            or not 0 <= next_index <= optimizer.sample_count
            or next_index != normalized["sequence"]
        ):
            raise ValueError("optimizer checkpoint proposal sequence is invalid")
        state = _object(normalized["optimizer_state"], {"observations"}, "optimizer_state")
        observations = state["observations"]
        if not isinstance(observations, list):
            raise ValueError("optimizer checkpoint observations must be a list")
        restored: dict[str, dict[str, Any]] = {}
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("optimizer checkpoint observations must be objects")
            candidate = _fingerprint(
                observation.get("candidate_fingerprint"), "candidate_fingerprint"
            )
            restored[str(candidate)] = dict(observation)
        ordered = [restored[key] for key in sorted(restored)]
        if normalized["history_fingerprint"] != domain_sha256_v2(
            "comsol_mcp.research_optimizer_history", ordered
        ) or normalized["candidate_fingerprints"] != sorted(restored):
            raise ValueError("optimizer checkpoint history identity is invalid")
        optimizer.next_index = next_index
        optimizer.observations = restored
        return optimizer


class DeterministicGridOptimizer(DeterministicRandomOptimizer):
    """Finite deterministic Cartesian-grid baseline for mixed design spaces."""

    backend_name = GRID_BACKEND_NAME
    backend_version = GRID_BACKEND_VERSION
    strategy = "lexicographic_cartesian_product"

    def __init__(self, design_space: object, *, levels: int) -> None:
        self.space = normalize_design_space(design_space)
        self.levels = _grid_levels(levels)
        self.axes = _grid_axes(self.space, self.levels)
        self.sample_count = math.prod(len(axis) for axis in self.axes.values())
        axes_fingerprint = domain_sha256_v2("comsol_mcp.research_optimizer_grid_axes", self.axes)
        backend_body = {
            "name": GRID_BACKEND_NAME,
            "version": GRID_BACKEND_VERSION,
            "space_fingerprint": self.space["space_fingerprint"],
            "levels": self.levels,
            "axes_fingerprint": axes_fingerprint,
        }
        self.backend_identity = domain_sha256_v2(
            "comsol_mcp.research_optimizer_backend", backend_body
        )
        self.next_index = 0
        self.observations: dict[str, dict[str, Any]] = {}

    def _proposal(self, index: int) -> dict[str, Any]:
        body = {
            "schema_name": OPTIMIZER_PROPOSAL_SCHEMA_NAME,
            "schema_version": OPTIMIZER_PROPOSAL_SCHEMA_VERSION,
            "backend_identity": self.backend_identity,
            "space_fingerprint": self.space["space_fingerprint"],
            "proposal_index": index,
            "values": _grid_proposal_values(self.axes, index),
        }
        return {
            **body,
            "proposal_fingerprint": domain_sha256_v2(OPTIMIZER_PROPOSAL_SCHEMA_NAME, body),
        }

    def ask(self) -> dict[str, Any]:
        if self.next_index >= self.sample_count:
            raise ValueError("grid sample limit is exhausted")
        proposal = self._proposal(self.next_index)
        self.next_index += 1
        return proposal

    def _proposal_limit(self) -> int:
        return self.sample_count

    def _explanation_parameters(self, index: int) -> dict[str, Any]:
        return {
            "levels": self.levels,
            "sample_count": self.sample_count,
            "axis_positions": _grid_axis_positions(self.axes, index),
        }

    def checkpoint(
        self,
        *,
        campaign_fingerprint: str,
        decision_fingerprint: str,
        created_at: str,
    ) -> dict[str, Any]:
        observations = [self.observations[key] for key in sorted(self.observations)]
        history_fingerprint = domain_sha256_v2(
            "comsol_mcp.research_optimizer_history", observations
        )
        return normalize_optimizer_checkpoint(
            {
                "schema_name": "comsol_mcp.research_optimizer_checkpoint",
                "schema_version": "1.0.0",
                "campaign_fingerprint": campaign_fingerprint,
                "sequence": self.next_index,
                "decision_fingerprint": decision_fingerprint,
                "backend": {
                    "name": GRID_BACKEND_NAME,
                    "version": GRID_BACKEND_VERSION,
                    "identity": self.backend_identity,
                },
                "random_state": {"levels": self.levels, "next_index": self.next_index},
                "optimizer_state": {"observations": observations},
                "history_fingerprint": history_fingerprint,
                "candidate_fingerprints": sorted(self.observations),
                "created_at": _timestamp(created_at, "created_at"),
            }
        )

    @classmethod
    def restore(cls, design_space: object, checkpoint: object) -> "DeterministicGridOptimizer":
        normalized = normalize_optimizer_checkpoint(checkpoint)
        random_state = _object(
            normalized["random_state"], {"levels", "next_index"}, "checkpoint.random_state"
        )
        optimizer = cls(design_space, levels=_grid_levels(random_state["levels"]))
        if (
            normalized["backend"]["name"] != GRID_BACKEND_NAME
            or normalized["backend"]["version"] != GRID_BACKEND_VERSION
            or normalized["backend"]["identity"] != optimizer.backend_identity
        ):
            raise ValueError("optimizer checkpoint backend identity is incompatible")
        next_index = random_state["next_index"]
        if (
            isinstance(next_index, bool)
            or not isinstance(next_index, int)
            or not 0 <= next_index <= optimizer.sample_count
            or next_index != normalized["sequence"]
        ):
            raise ValueError("optimizer checkpoint proposal sequence is invalid")
        state = _object(normalized["optimizer_state"], {"observations"}, "optimizer_state")
        observations = state["observations"]
        if not isinstance(observations, list):
            raise ValueError("optimizer checkpoint observations must be a list")
        restored: dict[str, dict[str, Any]] = {}
        for observation in observations:
            if not isinstance(observation, dict):
                raise ValueError("optimizer checkpoint observations must be objects")
            candidate = _fingerprint(
                observation.get("candidate_fingerprint"), "candidate_fingerprint"
            )
            restored[str(candidate)] = dict(observation)
        ordered = [restored[key] for key in sorted(restored)]
        if normalized["history_fingerprint"] != domain_sha256_v2(
            "comsol_mcp.research_optimizer_history", ordered
        ) or normalized["candidate_fingerprints"] != sorted(restored):
            raise ValueError("optimizer checkpoint history identity is invalid")
        optimizer.next_index = next_index
        optimizer.observations = restored
        return optimizer


__all__ = [
    "GRID_BACKEND_NAME",
    "GRID_BACKEND_VERSION",
    "LHS_BACKEND_NAME",
    "LHS_BACKEND_VERSION",
    "OPTIMIZER_EXPLANATION_SCHEMA_NAME",
    "OPTIMIZER_EXPLANATION_SCHEMA_VERSION",
    "OPTIMIZER_PROPOSAL_SCHEMA_NAME",
    "OPTIMIZER_PROPOSAL_SCHEMA_VERSION",
    "OPTIMIZER_STATE_SCHEMA_NAME",
    "OPTIMIZER_STATE_SCHEMA_VERSION",
    "DeterministicGridOptimizer",
    "DeterministicLatinHypercubeOptimizer",
    "DeterministicRandomOptimizer",
    "ResearchOptimizerProtocol",
]
