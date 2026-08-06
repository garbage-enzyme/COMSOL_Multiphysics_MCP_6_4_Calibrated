"""Solver-free gates for bounded adaptive campaign execution and stop truth."""

from __future__ import annotations

import json

from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.research.adaptive_acquisition import GaussianProcessExpectedImprovementOptimizer
from comsol_mcp.research.campaign_loop import BoundedResearchCampaignLoop
from comsol_mcp.research.compiler import compile_campaign_manifest
from comsol_mcp.research.coordinator import ResearchCampaignCoordinator
from development_kit.tests.test_research_contracts import _approval, _goal, _space


def _manifest(max_evaluations=4):
    goal = _goal()
    goal["resource_budget"]["max_fem_evaluations"] = max_evaluations
    return compile_campaign_manifest(goal, _space(), _approval())


def _candidate(proposal, manifest):
    return {
        "schema_name": "comsol_mcp.research_candidate",
        "schema_version": "1.0.0",
        "candidate_id": f"candidate-{proposal['proposal_index']:03d}",
        "campaign_fingerprint": manifest["campaign_fingerprint"],
        "requested_values": proposal["values"],
        "parent_candidate_ids": [],
        "proposal_reason": "Bounded adaptive proposal.",
        "hypothesis": None,
        "preflight_results": [],
        "predicted_resource_class": "fem_standard",
        "requested_fidelity": "coarse_fem",
        "producer_identity": "a" * 64,
        "optimizer_identity": proposal["backend_identity"],
        "random_seed": 17001,
        "lifecycle_state": "proposed",
        "terminal_reason": None,
    }


def _score(response):
    loss = abs(float(response["peak_wavelength_nm"]) - 1550.0) / 5.0
    body = {"peak": loss}
    return {
        "score_fingerprint": domain_sha256_v2("test.score", body),
        "losses": body,
        "success": loss <= 1.0,
    }


def _loop(tmp_path, manifest, evaluator, optimizer=None):
    coordinator = ResearchCampaignCoordinator(
        tmp_path,
        manifest,
        evaluator,
        evaluator_identity="b" * 64,
        clock=lambda: 1000.0,
    )
    optimizer = optimizer or GaussianProcessExpectedImprovementOptimizer(
        manifest["design_space"], seed=17001, warmup_count=2, candidate_pool_count=8
    )
    return BoundedResearchCampaignLoop(coordinator, optimizer, _candidate, _score)


def test_campaign_loop_stops_only_on_measured_success_and_checkpoints(ascii_tmp_path):
    loop = _loop(ascii_tmp_path, _manifest(), lambda _values: {"peak_wavelength_nm": 1550.0})
    result = loop.run(max_steps=4)
    assert result["success"] is True
    assert result["stop_reason"] == "success"
    assert result["step_count"] == 1
    assert (ascii_tmp_path / "optimizer_checkpoint.json").is_file()


def test_impossible_campaign_exhausts_budget_without_manufacturing_success(ascii_tmp_path):
    loop = _loop(
        ascii_tmp_path,
        _manifest(max_evaluations=2),
        lambda _values: {"peak_wavelength_nm": 2000.0},
    )
    result = loop.run(max_steps=4)
    assert result["success"] is False
    assert result["stop_reason"] == "budget_exhausted"
    assert result["step_count"] == 3
    assert loop.coordinator.status()["started_evaluations"] == 2


def test_checkpoint_restart_replays_exact_next_adaptive_decision(ascii_tmp_path):
    manifest = _manifest(max_evaluations=4)

    def evaluator(values):
        return {"peak_wavelength_nm": 1600.0 + values["patch_length_x"] / 100.0}

    interrupted_root = ascii_tmp_path / "interrupted"
    interrupted = _loop(interrupted_root, manifest, evaluator)
    assert interrupted.step()["stop_reason"] == "continue"
    checkpoint = json.loads(
        (interrupted_root / "optimizer_checkpoint.json").read_text(encoding="utf-8")
    )
    restored_optimizer = GaussianProcessExpectedImprovementOptimizer.restore(
        manifest["design_space"], checkpoint
    )
    resumed = _loop(interrupted_root, manifest, evaluator, restored_optimizer)
    resumed_second = resumed.step()

    uninterrupted = _loop(ascii_tmp_path / "uninterrupted", manifest, evaluator)
    assert uninterrupted.step()["stop_reason"] == "continue"
    uninterrupted_second = uninterrupted.step()

    assert resumed_second["score"] == uninterrupted_second["score"]
    assert (
        resumed_second["evaluation"]["response"] == uninterrupted_second["evaluation"]["response"]
    )
    assert resumed.optimizer.state() == uninterrupted.optimizer.state()
