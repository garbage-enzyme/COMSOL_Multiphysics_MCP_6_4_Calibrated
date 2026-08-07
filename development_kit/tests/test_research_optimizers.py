"""Backend-neutral optimizer protocol and deterministic random baseline."""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from comsol_mcp.durable import domain_sha256_v2
from comsol_mcp.research.optimizers import (
    DeterministicGridOptimizer,
    DeterministicLatinHypercubeOptimizer,
    DeterministicRandomOptimizer,
    ResearchOptimizerProtocol,
)
from development_kit.tests.test_research_contracts import _space


def _mixed_space() -> dict:
    space = _space()
    additions = [
        {
            "variable_id": "mesh_level",
            "kind": "integer",
            "unit": "1",
            "baseline": 2,
            "lower": 1,
            "upper": 4,
            "allowed_values": None,
            "dependency_class": "mesh",
            "adapter_path": "mesh.level",
        },
        {
            "variable_id": "material_state",
            "kind": "categorical",
            "unit": "1",
            "baseline": "gold",
            "lower": None,
            "upper": None,
            "allowed_values": ["gold", "silver"],
            "dependency_class": "material",
            "adapter_path": "material.state",
        },
    ]
    space["variables"].extend(additions)
    space["adapter_mappings"].extend(
        {
            "variable_id": item["variable_id"],
            "adapter_path": item["adapter_path"],
            "unit": item["unit"],
        }
        for item in additions
    )
    return space


def test_same_seed_and_space_replay_identical_sequence_across_fresh_instances():
    first = DeterministicRandomOptimizer(_mixed_space(), seed=17001)
    second = DeterministicRandomOptimizer(_mixed_space(), seed=17001)
    assert [first.ask() for _ in range(8)] == [second.ask() for _ in range(8)]
    assert first.backend_identity == second.backend_identity


def test_random_baseline_respects_continuous_integer_and_categorical_domains():
    optimizer = DeterministicRandomOptimizer(_mixed_space(), seed=17001)
    proposals = [optimizer.ask()["values"] for _ in range(64)]
    assert all(75.0 <= item["patch_length_x"] <= 125.0 for item in proposals)
    assert all(60.0 <= item["patch_length_y"] <= 100.0 for item in proposals)
    assert all(
        type(item["mesh_level"]) is int and 1 <= item["mesh_level"] <= 4 for item in proposals
    )
    assert {item["material_state"] for item in proposals} <= {"gold", "silver"}


def test_different_seed_changes_backend_and_proposal_identity():
    first = DeterministicRandomOptimizer(_space(), seed=1)
    second = DeterministicRandomOptimizer(_space(), seed=2)
    assert first.backend_identity != second.backend_identity
    assert first.ask()["proposal_fingerprint"] != second.ask()["proposal_fingerprint"]


def test_lhs_replays_and_covers_each_continuous_stratum_once():
    sample_count = 16
    first = DeterministicLatinHypercubeOptimizer(
        _mixed_space(), seed=17001, sample_count=sample_count
    )
    second = DeterministicLatinHypercubeOptimizer(
        _mixed_space(), seed=17001, sample_count=sample_count
    )
    proposals = [first.ask() for _ in range(sample_count)]
    assert proposals == [second.ask() for _ in range(sample_count)]
    bounds = {"patch_length_x": (75.0, 125.0), "patch_length_y": (60.0, 100.0)}
    for variable_id, (lower, upper) in bounds.items():
        strata = {
            min(
                sample_count - 1,
                int((proposal["values"][variable_id] - lower) / (upper - lower) * sample_count),
            )
            for proposal in proposals
        }
        assert strata == set(range(sample_count))


def test_lhs_mixed_domains_are_bounded_balanced_and_finite():
    optimizer = DeterministicLatinHypercubeOptimizer(_mixed_space(), seed=9, sample_count=16)
    values = [optimizer.ask()["values"] for _ in range(16)]
    assert all(type(item["mesh_level"]) is int and 1 <= item["mesh_level"] <= 4 for item in values)
    category_counts = {
        category: sum(item["material_state"] == category for item in values)
        for category in {"gold", "silver"}
    }
    assert category_counts == {"gold": 8, "silver": 8}
    with pytest.raises(ValueError, match="sample limit is exhausted"):
        optimizer.ask()


@pytest.mark.parametrize("sample_count", [False, 0, 4097])
def test_lhs_rejects_invalid_sample_count(sample_count):
    with pytest.raises(ValueError, match="sample_count"):
        DeterministicLatinHypercubeOptimizer(_space(), seed=1, sample_count=sample_count)


def test_lhs_checkpoint_restore_preserves_design_and_next_stratum():
    optimizer = DeterministicLatinHypercubeOptimizer(_space(), seed=17001, sample_count=8)
    first = optimizer.ask()
    optimizer.tell(
        first,
        candidate_fingerprint="a" * 64,
        status="completed",
        score_fingerprint="b" * 64,
        losses={"peak": 2.0, "q": 0.5},
    )
    checkpoint = optimizer.checkpoint(
        campaign_fingerprint="c" * 64,
        decision_fingerprint="d" * 64,
        created_at="2026-08-06T00:00:00Z",
    )
    restored = DeterministicLatinHypercubeOptimizer.restore(_space(), checkpoint)
    assert restored.sample_count == 8
    assert restored.observations == optimizer.observations
    assert restored.ask() == optimizer.ask()


def test_lhs_rejects_random_checkpoint_and_cross_backend_proposal():
    random_optimizer = DeterministicRandomOptimizer(_space(), seed=1)
    random_proposal = random_optimizer.ask()
    random_checkpoint = random_optimizer.checkpoint(
        campaign_fingerprint="c" * 64,
        decision_fingerprint="d" * 64,
        created_at="2026-08-06T00:00:00Z",
    )
    lhs = DeterministicLatinHypercubeOptimizer(_space(), seed=1, sample_count=8)
    lhs.ask()
    with pytest.raises(ValueError, match="another backend"):
        lhs.tell(
            random_proposal,
            candidate_fingerprint="a" * 64,
            status="failed",
            score_fingerprint=None,
            losses={},
        )
    with pytest.raises(ValueError, match="fields mismatch"):
        DeterministicLatinHypercubeOptimizer.restore(_space(), random_checkpoint)


def test_grid_enumerates_exact_unique_cartesian_product_and_exhausts():
    optimizer = DeterministicGridOptimizer(_mixed_space(), levels=3)
    proposals = [optimizer.ask() for _ in range(54)]
    values = [tuple(proposal["values"].items()) for proposal in proposals]
    assert len(values) == len(set(values)) == 54
    assert {proposal["values"]["patch_length_x"] for proposal in proposals} == {
        75.0,
        100.0,
        125.0,
    }
    assert {proposal["values"]["patch_length_y"] for proposal in proposals} == {
        60.0,
        80.0,
        100.0,
    }
    assert {proposal["values"]["mesh_level"] for proposal in proposals} == {1, 3, 4}
    assert {proposal["values"]["material_state"] for proposal in proposals} == {
        "gold",
        "silver",
    }
    with pytest.raises(ValueError, match="grid sample limit is exhausted"):
        optimizer.ask()


@pytest.mark.parametrize("levels", [False, 0, 65])
def test_grid_rejects_invalid_levels(levels):
    with pytest.raises(ValueError, match="levels"):
        DeterministicGridOptimizer(_space(), levels=levels)


def test_grid_rejects_oversized_cartesian_product():
    with pytest.raises(ValueError, match="more than 4096 samples"):
        DeterministicGridOptimizer(_mixed_space(), levels=32)


def test_grid_checkpoint_restore_preserves_next_cartesian_point():
    optimizer = DeterministicGridOptimizer(_space(), levels=3)
    first = optimizer.ask()
    optimizer.tell(
        first,
        candidate_fingerprint="a" * 64,
        status="completed",
        score_fingerprint="b" * 64,
        losses={"peak": 2.0, "q": 0.5},
    )
    checkpoint = optimizer.checkpoint(
        campaign_fingerprint="c" * 64,
        decision_fingerprint="d" * 64,
        created_at="2026-08-06T00:00:00Z",
    )
    restored = DeterministicGridOptimizer.restore(_space(), checkpoint)
    assert restored.axes == optimizer.axes
    assert restored.observations == optimizer.observations
    assert restored.ask() == optimizer.ask()
    explanation = restored.explain(restored.ask())
    assert explanation["parameters"]["sample_count"] == 9
    assert set(explanation["parameters"]["axis_positions"]) == {
        "patch_length_x",
        "patch_length_y",
    }


@pytest.mark.parametrize(
    "optimizer",
    [
        DeterministicRandomOptimizer(_space(), seed=1),
        DeterministicLatinHypercubeOptimizer(_space(), seed=1, sample_count=8),
        DeterministicGridOptimizer(_space(), levels=3),
    ],
)
def test_baselines_implement_runtime_optimizer_protocol(optimizer):
    assert isinstance(optimizer, ResearchOptimizerProtocol)


def test_state_is_fingerprinted_and_tracks_exact_observation_counts():
    optimizer = DeterministicRandomOptimizer(_space(), seed=1)
    initial = optimizer.state()
    assert initial["next_proposal_index"] == 0
    assert initial["observation_count"] == 0
    assert initial["status_counts"] == {"completed": 0, "failed": 0, "infeasible": 0}
    proposal = optimizer.ask()
    optimizer.tell(
        proposal,
        candidate_fingerprint="a" * 64,
        status="infeasible",
        score_fingerprint=None,
        losses={},
    )
    observed = optimizer.state()
    assert observed["next_proposal_index"] == 1
    assert observed["remaining_proposals"] == observed["proposal_limit"] - 1
    assert observed["observation_count"] == 1
    assert observed["status_counts"]["infeasible"] == 1
    assert observed["state_fingerprint"] != initial["state_fingerprint"]


def test_explain_is_deterministic_backend_specific_and_observation_independent():
    first = DeterministicLatinHypercubeOptimizer(_space(), seed=7, sample_count=8)
    second = DeterministicLatinHypercubeOptimizer(_space(), seed=7, sample_count=8)
    first_proposal = first.ask()
    second_proposal = second.ask()
    explanation = first.explain(first_proposal)
    assert explanation == second.explain(second_proposal)
    assert explanation["uses_observations"] is False
    assert explanation["parameters"]["sample_count"] == 8
    assert set(explanation["parameters"]["strata"]) == {
        "patch_length_x",
        "patch_length_y",
    }


def test_explain_rejects_unasked_foreign_and_forged_proposals():
    optimizer = DeterministicRandomOptimizer(_space(), seed=1)
    unasked = DeterministicRandomOptimizer(_space(), seed=1).ask()
    with pytest.raises(ValueError, match="already asked"):
        optimizer.explain(unasked)
    asked = optimizer.ask()
    foreign = DeterministicRandomOptimizer(_space(), seed=2).ask()
    with pytest.raises(ValueError, match="another backend"):
        optimizer.explain(foreign)
    forged = copy.deepcopy(asked)
    forged["values"]["patch_length_x"] = 75.0
    forged.pop("proposal_fingerprint")
    with pytest.raises(ValueError, match="already asked"):
        optimizer.explain(forged)


def test_tell_is_idempotent_for_exact_result_and_rejects_conflicts():
    optimizer = DeterministicRandomOptimizer(_space(), seed=17001)
    proposal = optimizer.ask()
    arguments = {
        "candidate_fingerprint": "a" * 64,
        "status": "completed",
        "score_fingerprint": "b" * 64,
        "losses": {"peak": 2.0, "q": 0.5},
    }
    assert optimizer.tell(proposal, **arguments) is True
    assert optimizer.tell(proposal, **arguments) is False
    changed = dict(arguments)
    changed["losses"] = {"peak": 3.0, "q": 0.5}
    with pytest.raises(ValueError, match="conflicting result"):
        optimizer.tell(proposal, **changed)


def test_tell_rejects_unasked_forged_and_nonfinite_results():
    optimizer = DeterministicRandomOptimizer(_space(), seed=17001)
    proposal = optimizer.ask()
    future = copy.deepcopy(proposal)
    future["proposal_index"] = 1
    future.pop("proposal_fingerprint")
    with pytest.raises(ValueError, match="was not asked"):
        optimizer.tell(
            future,
            candidate_fingerprint="a" * 64,
            status="failed",
            score_fingerprint=None,
            losses={},
        )
    forged = copy.deepcopy(proposal)
    forged["values"]["patch_length_x"] = 75.0
    body = {key: value for key, value in forged.items() if key != "proposal_fingerprint"}
    forged["proposal_fingerprint"] = domain_sha256_v2(
        "comsol_mcp.research_optimizer_proposal", body
    )
    with pytest.raises(ValueError, match="deterministic ask stream"):
        optimizer.tell(
            forged,
            candidate_fingerprint="a" * 64,
            status="failed",
            score_fingerprint=None,
            losses={},
        )
    with pytest.raises(ValueError, match="finite numbers"):
        optimizer.tell(
            proposal,
            candidate_fingerprint="c" * 64,
            status="completed",
            score_fingerprint="d" * 64,
            losses={"peak": float("nan")},
        )


def test_checkpoint_restore_replays_exact_next_proposal_and_history():
    optimizer = DeterministicRandomOptimizer(_space(), seed=17001)
    first = optimizer.ask()
    optimizer.ask()
    optimizer.tell(
        first,
        candidate_fingerprint="a" * 64,
        status="completed",
        score_fingerprint="b" * 64,
        losses={"peak": 2.0, "q": 0.5},
    )
    checkpoint = optimizer.checkpoint(
        campaign_fingerprint="c" * 64,
        decision_fingerprint="d" * 64,
        created_at="2026-08-06T00:00:00Z",
    )
    restored = DeterministicRandomOptimizer.restore(_space(), checkpoint)
    assert restored.observations == optimizer.observations
    assert restored.ask() == optimizer.ask()


def test_checkpoint_rejects_tampered_history_or_backend_identity():
    optimizer = DeterministicRandomOptimizer(_space(), seed=17001)
    optimizer.ask()
    checkpoint = optimizer.checkpoint(
        campaign_fingerprint="c" * 64,
        decision_fingerprint="d" * 64,
        created_at="2026-08-06T00:00:00Z",
    )
    tampered = copy.deepcopy(checkpoint)
    tampered["random_state"]["seed"] = 2
    with pytest.raises(ValueError, match="fingerprint is invalid"):
        DeterministicRandomOptimizer.restore(_space(), tampered)
    tampered = copy.deepcopy(checkpoint)
    tampered.pop("checkpoint_fingerprint")
    tampered["backend"]["identity"] = "0" * 64
    with pytest.raises(ValueError, match="backend identity"):
        DeterministicRandomOptimizer.restore(_space(), tampered)


def test_optimizer_module_has_no_rng_numpy_scipy_or_solver_imports():
    path = Path(__file__).parents[2] / "comsol_mcp" / "research" / "optimizers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported = {
        node.names[0].name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported.isdisjoint({"random", "numpy", "scipy", "mph", "jpype"})
