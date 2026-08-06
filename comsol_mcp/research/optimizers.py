"""Backend-neutral optimizer protocol and deterministic random baseline."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping
from typing import Any

from comsol_mcp.durable import domain_sha256_v2

from .contracts import _bounded_json, _identifier, _object, _timestamp, normalize_design_space
from .state import normalize_optimizer_checkpoint

OPTIMIZER_PROPOSAL_SCHEMA_NAME = "comsol_mcp.research_optimizer_proposal"
OPTIMIZER_PROPOSAL_SCHEMA_VERSION = "1.0.0"
RANDOM_BACKEND_NAME = "deterministic_random"
RANDOM_BACKEND_VERSION = "1.0.0"
MAX_PROPOSALS = 1_000_000_000
MAX_OBJECTIVE_LOSSES = 128
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESULT_STATUSES = {"completed", "failed", "infeasible"}


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


def _unit_interval(seed: int, index: int, variable_id: str) -> float:
    return (_stream(seed, index, variable_id, "unit") >> (256 - 53)) / float(1 << 53)


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


__all__ = [
    "OPTIMIZER_PROPOSAL_SCHEMA_NAME",
    "OPTIMIZER_PROPOSAL_SCHEMA_VERSION",
    "DeterministicRandomOptimizer",
]
