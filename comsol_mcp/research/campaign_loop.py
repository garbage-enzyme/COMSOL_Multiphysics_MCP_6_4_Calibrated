"""Bounded optimizer-to-coordinator loop with explicit honest stop outcomes."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from comsol_mcp.durable import atomic_write_json, domain_sha256_v2

from .coordinator import ResearchCampaignCoordinator
from .optimizers import ResearchOptimizerProtocol

CandidateFactory = Callable[[Mapping[str, Any], Mapping[str, Any]], object]
ScoreEvaluator = Callable[[Mapping[str, Any]], object]
MAX_LOOP_STEPS = 4096


def _score(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "score_fingerprint",
        "losses",
        "success",
    }:
        raise ValueError("score evaluator must return exactly score_fingerprint, losses, success")
    fingerprint = value["score_fingerprint"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("score_fingerprint must be a lowercase SHA-256")
    losses = value["losses"]
    if not isinstance(losses, Mapping) or not losses:
        raise ValueError("score losses must be a nonempty object")
    normalized: dict[str, float] = {}
    for key, raw in losses.items():
        if not isinstance(key, str) or isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("score losses must be finite nonnegative numbers")
        number = float(raw)
        if not math.isfinite(number) or number < 0.0:
            raise ValueError("score losses must be finite nonnegative numbers")
        normalized[key] = number
    if not isinstance(value["success"], bool):
        raise ValueError("score success must be a boolean")
    return {
        "score_fingerprint": fingerprint,
        "losses": {key: normalized[key] for key in sorted(normalized)},
        "success": value["success"],
    }


class BoundedResearchCampaignLoop:
    """Advance one durable candidate at a time and checkpoint every optimizer update."""

    def __init__(
        self,
        coordinator: ResearchCampaignCoordinator,
        optimizer: ResearchOptimizerProtocol,
        candidate_factory: CandidateFactory,
        score_evaluator: ScoreEvaluator,
        *,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        if not isinstance(optimizer, ResearchOptimizerProtocol):
            raise ValueError("optimizer must implement the research optimizer protocol")
        self.coordinator = coordinator
        self.optimizer = optimizer
        self.candidate_factory = candidate_factory
        self.score_evaluator = score_evaluator
        self.checkpoint_path = (
            Path(checkpoint_path)
            if checkpoint_path is not None
            else coordinator.root / "optimizer_checkpoint.json"
        )
        if self.checkpoint_path.parent.resolve(strict=False) != coordinator.root.resolve(
            strict=False
        ):
            raise ValueError("optimizer checkpoint must remain in the campaign root")

    def _checkpoint(
        self, proposal: Mapping[str, Any], evaluation: Mapping[str, Any]
    ) -> dict[str, Any]:
        decision = domain_sha256_v2(
            "comsol_mcp.research_campaign_loop_decision",
            {
                "proposal_fingerprint": proposal["proposal_fingerprint"],
                "evaluation_fingerprint": evaluation["evaluation_fingerprint"],
            },
        )
        checkpoint = self.optimizer.checkpoint(
            campaign_fingerprint=self.coordinator.campaign_fingerprint,
            decision_fingerprint=decision,
            created_at=evaluation["completed_at"],
        )
        atomic_write_json(self.checkpoint_path, checkpoint)
        return checkpoint

    def step(self) -> dict[str, Any]:
        if self.optimizer.state()["remaining_proposals"] <= 0:
            return {"stop_reason": "optimizer_exhausted", "success": False}
        proposal = self.optimizer.ask()
        candidate = self.candidate_factory(proposal, self.coordinator.manifest)
        result = self.coordinator.evaluate(candidate)
        status = result["status"]
        if status in {"budget_exhausted", "cancel_requested", "orphaned_started"}:
            return {"stop_reason": status, "success": False, "result": result}
        evaluation = result["evaluation"]
        if status in {"failed", "infeasible", "cancelled"}:
            tell_status = "failed" if status == "cancelled" else status
            self.optimizer.tell(
                proposal,
                candidate_fingerprint=evaluation["candidate_fingerprint"],
                status=tell_status,
                score_fingerprint=None,
                losses={},
            )
            checkpoint = self._checkpoint(proposal, evaluation)
            return {
                "stop_reason": "continue",
                "success": False,
                "evaluation": evaluation,
                "checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
            }
        if status not in {"completed", "duplicate_terminal"}:
            raise ValueError("coordinator returned an unsupported campaign-loop status")
        response = evaluation.get("response")
        if not isinstance(response, Mapping):
            raise ValueError("completed evaluation response must be an object")
        score = _score(self.score_evaluator(response))
        self.optimizer.tell(
            proposal,
            candidate_fingerprint=evaluation["candidate_fingerprint"],
            status="completed",
            score_fingerprint=score["score_fingerprint"],
            losses=score["losses"],
        )
        checkpoint = self._checkpoint(proposal, evaluation)
        return {
            "stop_reason": "success" if score["success"] else "continue",
            "success": score["success"],
            "evaluation": evaluation,
            "score": score,
            "checkpoint_fingerprint": checkpoint["checkpoint_fingerprint"],
        }

    def run(self, *, max_steps: int) -> dict[str, Any]:
        if (
            isinstance(max_steps, bool)
            or not isinstance(max_steps, int)
            or not 1 <= max_steps <= MAX_LOOP_STEPS
        ):
            raise ValueError(f"max_steps must be an integer from 1 through {MAX_LOOP_STEPS}")
        steps: list[dict[str, Any]] = []
        for _index in range(max_steps):
            result = self.step()
            steps.append(result)
            if result["stop_reason"] != "continue":
                return {
                    "success": result["success"],
                    "stop_reason": result["stop_reason"],
                    "step_count": len(steps),
                    "steps": steps,
                }
        return {
            "success": False,
            "stop_reason": "step_limit",
            "step_count": len(steps),
            "steps": steps,
        }


__all__ = ["BoundedResearchCampaignLoop", "CandidateFactory", "MAX_LOOP_STEPS", "ScoreEvaluator"]
