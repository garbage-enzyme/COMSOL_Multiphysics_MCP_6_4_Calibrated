"""Removed research jobs must fail before execution side effects."""

import importlib.util

import pytest

from comsol_mcp.contracts.job_submission import validate_job_submission
from comsol_mcp.jobs.manager import REMOVED_RESEARCH_JOB_TYPES, JobManager


@pytest.mark.parametrize(
    "module",
    ["spectral_worker", "convergence_campaign_worker", "branch_continuation_campaign_worker", "robust_shape_worker"],
)
def test_removed_workers_are_not_importable(module):
    assert importlib.util.find_spec("comsol_mcp.jobs." + module) is None


@pytest.mark.parametrize("job_type", sorted(REMOVED_RESEARCH_JOB_TYPES))
def test_removed_submission_does_not_admit_or_create_job(ascii_tmp_path, job_type):
    def refuse(**_kwargs):
        raise AssertionError("Resource preflight must not run")

    manager = JobManager(ascii_tmp_path / "jobs", preflight=refuse, reconcile_on_start=False)
    before = sorted(manager.store.root.iterdir())
    with pytest.raises(ValueError, match="removed in 0.8.0"):
        manager.submit({"job_type": job_type})
    assert sorted(manager.store.root.iterdir()) == before
    with pytest.raises(ValueError):
        validate_job_submission({"job_type": job_type})


@pytest.mark.parametrize("job_type", sorted(REMOVED_RESEARCH_JOB_TYPES))
def test_removed_resume_preserves_legacy_record(ascii_tmp_path, job_type):
    manager = JobManager(ascii_tmp_path / "jobs", reconcile_on_start=False)
    job_id = manager.store.create(
        {"job_type": job_type, "source_model_path": "missing-source.mph"},
        {"status": "failed", "attempt": 1},
    )
    directory = manager.store.job_dir(job_id)
    before = {
        name: (directory / name).read_bytes()
        for name in ("spec.json", "state.json", "events.jsonl")
    }
    with pytest.raises(ValueError, match="removed in 0.8.0"):
        manager.resume(job_id)
    observed = manager.status(job_id, refresh_probe=True)
    assert observed["status"] == "failed"
    assert observed["legacy_execution"]["analysis_replayed"] is False
    assert observed["legacy_execution"]["supported"] is False
    assert all((directory / name).read_bytes() == data for name, data in before.items())
