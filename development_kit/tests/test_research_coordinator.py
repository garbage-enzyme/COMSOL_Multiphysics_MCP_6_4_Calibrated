"""Solver-free durable research coordinator behavior."""

from __future__ import annotations

import json
import threading

import pytest

from comsol_mcp.research.compiler import compile_campaign_manifest
from comsol_mcp.research.coordinator import ResearchCampaignCoordinator
from comsol_mcp.research.journal import recover_research_journal
from development_kit.tests.test_research_candidates import _candidate
from development_kit.tests.test_research_contracts import _approval, _goal, _space


def _manifest(*, max_evaluations: int = 32, wall_seconds: int = 8 * 3600) -> dict:
    goal = _goal()
    goal["resource_budget"]["max_fem_evaluations"] = max_evaluations
    goal["resource_budget"]["max_wall_time_seconds"] = wall_seconds
    return compile_campaign_manifest(goal, _space(), _approval())


def _bound_candidate(manifest: dict, candidate_id: str = "candidate-001") -> dict:
    value = _candidate()
    value["candidate_id"] = candidate_id
    value["campaign_fingerprint"] = manifest["campaign_fingerprint"]
    return value


def _response(_values) -> dict:
    return {"peak_wavelength_nm": 1550.0, "q_factor": 20.0, "power_closure": 1.0}


def test_completed_candidate_replays_after_fresh_coordinator_without_reevaluation(tmp_path):
    manifest = _manifest()
    calls = []

    def evaluator(values):
        calls.append(dict(values))
        return _response(values)

    first = ResearchCampaignCoordinator(
        tmp_path, manifest, evaluator, evaluator_identity="d" * 64, clock=lambda: 1000.0
    )
    completed = first.evaluate(_bound_candidate(manifest))
    assert completed["status"] == "completed"
    restarted = ResearchCampaignCoordinator(
        tmp_path, manifest, evaluator, evaluator_identity="d" * 64, clock=lambda: 1001.0
    )
    replayed = restarted.evaluate(_bound_candidate(manifest))
    assert replayed["status"] == "duplicate_terminal"
    assert replayed["replayed"] is True
    assert len(calls) == 1


def test_started_budget_is_committed_before_evaluator_and_exactly_enforced(tmp_path):
    manifest = _manifest(max_evaluations=1)
    coordinator = None
    observed = []

    def evaluator(values):
        records = recover_research_journal(coordinator.journal_path)["records"]
        observed.append(
            sum(
                1
                for record in records
                if record["kind"] == "evaluation" and record["payload"]["status"] == "started"
            )
        )
        return _response(values)

    coordinator = ResearchCampaignCoordinator(
        tmp_path, manifest, evaluator, evaluator_identity="d" * 64, clock=lambda: 1000.0
    )
    assert coordinator.evaluate(_bound_candidate(manifest))["status"] == "completed"
    second = _bound_candidate(manifest, "candidate-002")
    second["requested_values"]["patch_length_x"] = 101.0
    assert coordinator.evaluate(second)["status"] == "budget_exhausted"
    assert observed == [1]


class _InjectedCrash(BaseException):
    pass


def test_crash_leaves_started_authority_and_restart_never_implicitly_repeats(tmp_path):
    manifest = _manifest()

    def crash(_values):
        raise _InjectedCrash()

    coordinator = ResearchCampaignCoordinator(
        tmp_path, manifest, crash, evaluator_identity="d" * 64, clock=lambda: 1000.0
    )
    with pytest.raises(_InjectedCrash):
        coordinator.evaluate(_bound_candidate(manifest))
    calls = []

    def evaluator(values):
        calls.append(dict(values))
        return _response(values)

    restarted = ResearchCampaignCoordinator(
        tmp_path, manifest, evaluator, evaluator_identity="d" * 64, clock=lambda: 1001.0
    )
    assert restarted.evaluate(_bound_candidate(manifest))["status"] == "orphaned_started"
    assert calls == []
    finalized = restarted.finalize_orphaned()
    assert len(finalized) == 1
    assert finalized[0]["status"] == "failed"
    retried = restarted.evaluate(_bound_candidate(manifest), retry_failed=True)
    assert retried["status"] == "completed"
    assert len(calls) == 1


def test_cancel_before_start_consumes_no_budget_and_cancel_during_evaluator_is_terminal(tmp_path):
    manifest = _manifest()
    coordinator = ResearchCampaignCoordinator(
        tmp_path / "before",
        manifest,
        _response,
        evaluator_identity="d" * 64,
        clock=lambda: 1000.0,
    )
    coordinator.request_cancel("cancel-before")
    assert coordinator.evaluate(_bound_candidate(manifest))["status"] == "cancel_requested"
    assert coordinator.status()["started_evaluations"] == 0

    during = None

    def evaluator(values):
        during.request_cancel("cancel-during")
        return _response(values)

    during = ResearchCampaignCoordinator(
        tmp_path / "during",
        manifest,
        evaluator,
        evaluator_identity="d" * 64,
        clock=lambda: 1000.0,
    )
    result = during.evaluate(_bound_candidate(manifest))
    assert result["status"] == "cancelled"
    assert result["evaluation"]["response"] is None
    assert during.status()["cancel_requested"] is False
    assert not during.control_path.exists()
    assert not during.lock_path.exists()


def test_concurrent_duplicate_callers_share_one_started_evaluation(tmp_path):
    manifest = _manifest()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def evaluator(values):
        calls.append(dict(values))
        entered.set()
        assert release.wait(timeout=5)
        return _response(values)

    coordinator = ResearchCampaignCoordinator(
        tmp_path, manifest, evaluator, evaluator_identity="d" * 64, clock=lambda: 1000.0
    )
    results = []

    def run():
        results.append(coordinator.evaluate(_bound_candidate(manifest)))

    first = threading.Thread(target=run)
    second = threading.Thread(target=run)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    assert sorted(item["status"] for item in results) == ["completed", "duplicate_terminal"]


def test_invalid_response_becomes_failed_terminal_not_orphan(tmp_path):
    manifest = _manifest()
    coordinator = ResearchCampaignCoordinator(
        tmp_path,
        manifest,
        lambda _values: {"invalid": float("nan")},
        evaluator_identity="d" * 64,
        clock=lambda: 1000.0,
    )
    result = coordinator.evaluate(_bound_candidate(manifest))
    assert result["status"] == "failed"
    records = recover_research_journal(tmp_path / "research_journal.jsonl")["records"]
    assert [
        record["payload"]["status"] for record in records if record["kind"] == "evaluation"
    ] == [
        "started",
        "failed",
    ]


def test_runtime_refuses_manifest_reuse_and_non_ascii_root(tmp_path):
    manifest = _manifest()
    ResearchCampaignCoordinator(
        tmp_path / "owned",
        manifest,
        _response,
        evaluator_identity="d" * 64,
        clock=lambda: 1000.0,
    )
    changed = _manifest(max_evaluations=31)
    with pytest.raises(ValueError, match="different manifest"):
        ResearchCampaignCoordinator(
            tmp_path / "owned",
            changed,
            _response,
            evaluator_identity="d" * 64,
            clock=lambda: 1000.0,
        )
    with pytest.raises(ValueError, match="ASCII"):
        ResearchCampaignCoordinator(
            tmp_path / "非ascii",
            manifest,
            _response,
            evaluator_identity="d" * 64,
            clock=lambda: 1000.0,
        )


def test_corrupt_or_foreign_cancel_control_blocks_evaluation_without_budget_use(tmp_path):
    manifest = _manifest()
    coordinator = ResearchCampaignCoordinator(
        tmp_path,
        manifest,
        _response,
        evaluator_identity="d" * 64,
        clock=lambda: 1000.0,
    )
    coordinator.control_path.write_bytes(b"{corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        coordinator.evaluate(_bound_candidate(manifest))
    assert not coordinator.journal_path.exists()
    coordinator.control_path.write_text(
        json.dumps(
            {
                "campaign_fingerprint": "0" * 64,
                "request_id": "foreign",
                "requested_at": "2026-08-06T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="different campaign"):
        coordinator.evaluate(_bound_candidate(manifest))
    assert not coordinator.journal_path.exists()


@pytest.mark.parametrize("invalid", [True, -1.0, float("nan"), float("inf"), "1000"])
def test_invalid_clock_fails_before_runtime_or_evaluation_side_effect(tmp_path, invalid):
    manifest = _manifest()
    root = tmp_path / str(type(invalid).__name__)
    with pytest.raises(ValueError, match="finite nonnegative"):
        ResearchCampaignCoordinator(
            root,
            manifest,
            _response,
            evaluator_identity="d" * 64,
            clock=lambda: invalid,
        )
    assert not (root / "campaign_runtime.json").exists()
