"""Solver-free contract tests for the licensed continuation runner."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from development_kit.tests.integration import branch_continuation_campaign_acceptance as acceptance
from development_kit.tests.integration.branch_continuation_campaign_acceptance import (
    _write_json,
    run_acceptance,
)
from development_kit.tests.test_branch_continuation_campaign_job import _raw_campaign


def test_dry_run_binds_every_source_and_readback_without_starting_worker(tmp_path, ascii_tmp_path):
    output = tmp_path / "dry-run.json"
    raw_spec = _raw_campaign(tmp_path / "sources")
    expected_hashes = {
        state["state_id"]: hashlib.sha256(
            Path(state["spectral_job"]["source_model_path"]).read_bytes()
        ).hexdigest()
        for state in raw_spec["states"]
    }
    receipt = run_acceptance(
        raw_spec=raw_spec,
        runtime_root=ascii_tmp_path,
        output=output,
        dry_run=True,
        worker_runner=lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )
    assert receipt["success"] is True
    assert receipt["comsol_client_started"] is False
    assert receipt["source_model_sha256"] == expected_hashes
    assert receipt["incidence_evidence"]["source"] == "normalized_spec_declaration"
    assert receipt["incidence_evidence"]["observed_execution"] is False
    assert set(receipt["incidence_evidence"]["declared_readbacks"]) == {
        "angle-0",
        "angle-1",
        "angle-2",
    }
    assert json.loads(output.read_text(encoding="utf-8")) == receipt


def test_receipt_is_never_overwritten(tmp_path, ascii_tmp_path):
    output = tmp_path / "existing.json"
    sentinel = b'{"sentinel":"preserve-exact-bytes"}\r\n'
    output.write_bytes(sentinel)
    with pytest.raises(FileExistsError, match="overwrite"):
        run_acceptance(
            raw_spec=_raw_campaign(tmp_path / "sources"),
            runtime_root=ascii_tmp_path,
            output=output,
            dry_run=True,
        )
    assert output.read_bytes() == sentinel


def test_non_ascii_runtime_fails_before_worker(tmp_path):
    with pytest.raises(ValueError, match="ASCII"):
        run_acceptance(
            raw_spec=_raw_campaign(tmp_path / "sources"),
            runtime_root=tmp_path / "runtime-nonascii-测试",
            output=tmp_path / "receipt.json",
            dry_run=True,
        )


def test_runtime_rejection_precedes_spec_normalization_and_source_hashing(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        acceptance,
        "normalize_branch_continuation_campaign_spec",
        lambda _raw: pytest.fail("spec normalization must not run"),
    )
    monkeypatch.setattr(
        acceptance,
        "_sha256_file",
        lambda _path: pytest.fail("source hashing must not run"),
    )

    with pytest.raises(ValueError, match="ASCII"):
        run_acceptance(
            raw_spec={"invalid": True},
            runtime_root=tmp_path / "runtime-nonascii-测试",
            output=tmp_path / "receipt.json",
            dry_run=True,
        )


def test_receipt_publication_is_exclusive_under_concurrency(tmp_path):
    output = tmp_path / "receipt.json"

    def publish(index):
        try:
            _write_json(output, {"publisher": index})
            return "published"
        except FileExistsError:
            return "collision"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(publish, range(8)))

    assert outcomes.count("published") == 1
    assert outcomes.count("collision") == 7
    assert json.loads(output.read_text(encoding="utf-8"))["publisher"] in range(8)
    assert list(tmp_path.iterdir()) == [output]


class _FakeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._directory = self.root / "job-acceptance"
        self._directory.mkdir()

    def create(self, _spec, _state):
        return "job-acceptance"

    def job_dir(self, _job_id):
        return self._directory

    def read_state(self, _job_id):
        return {"status": "completed", "cleanup": {"passed": True}}


def _clean_ownership(*, external=()):
    return {
        "process_inventory": {"complete": True, "fresh": True},
        "collision": False,
        "lease": {"state": "absent"},
        "external_solver_processes": list(external),
    }


def _install_fake_completed_job(monkeypatch, *, omit_last_state=False):
    monkeypatch.setattr(acceptance, "JobStore", _FakeStore)

    def rows(_path, spec, *, artifact_root):
        assert artifact_root.name == "job-acceptance"
        states = spec["states"][:-1] if omit_last_state else spec["states"]
        return [{"state_id": state["state_id"]} for state in states]

    monkeypatch.setattr(acceptance, "read_branch_continuation_campaign_states", rows)


def test_post_submission_failure_still_writes_job_bound_receipt(
    tmp_path, ascii_tmp_path, monkeypatch
):
    _install_fake_completed_job(monkeypatch)
    output = tmp_path / "failed.json"

    receipt = run_acceptance(
        raw_spec=_raw_campaign(tmp_path / "sources"),
        runtime_root=ascii_tmp_path,
        output=output,
        worker_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected post-submission failure")
        ),
        ownership_provider=lambda _runtime: _clean_ownership(),
    )

    assert receipt["success"] is False
    assert receipt["job_id"] == "job-acceptance"
    assert receipt["phase_error"]["type"] == "RuntimeError"
    assert json.loads(output.read_text(encoding="utf-8")) == receipt


def test_external_solver_process_blocks_success_even_when_lease_is_absent(
    tmp_path, ascii_tmp_path, monkeypatch
):
    _install_fake_completed_job(monkeypatch)

    receipt = run_acceptance(
        raw_spec=_raw_campaign(tmp_path / "sources"),
        runtime_root=ascii_tmp_path,
        output=tmp_path / "external.json",
        worker_runner=lambda *_args, **_kwargs: 0,
        ownership_provider=lambda _runtime: _clean_ownership(
            external=[{"pid": 43001, "name": "comsol"}]
        ),
    )

    assert receipt["cleanup"]["lease_absent"] is True
    assert receipt["cleanup"]["external_processes_absent"] is False
    assert receipt["success"] is False


def test_completed_state_requires_exact_journal_coverage(tmp_path, ascii_tmp_path, monkeypatch):
    _install_fake_completed_job(monkeypatch, omit_last_state=True)

    receipt = run_acceptance(
        raw_spec=_raw_campaign(tmp_path / "sources"),
        runtime_root=ascii_tmp_path,
        output=tmp_path / "incomplete.json",
        worker_runner=lambda *_args, **_kwargs: 0,
        ownership_provider=lambda _runtime: _clean_ownership(),
    )

    assert receipt["state"]["status"] == "completed"
    assert receipt["state_coverage"]["exact_once"] is False
    assert receipt["success"] is False


def test_complete_journal_and_clean_process_evidence_can_pass(
    tmp_path, ascii_tmp_path, monkeypatch
):
    _install_fake_completed_job(monkeypatch)

    receipt = run_acceptance(
        raw_spec=_raw_campaign(tmp_path / "sources"),
        runtime_root=ascii_tmp_path,
        output=tmp_path / "complete.json",
        worker_runner=lambda *_args, **_kwargs: 0,
        ownership_provider=lambda _runtime: _clean_ownership(),
    )

    assert receipt["state_coverage"]["exact_once"] is True
    assert receipt["cleanup"]["passed"] is True
    assert receipt["success"] is True
