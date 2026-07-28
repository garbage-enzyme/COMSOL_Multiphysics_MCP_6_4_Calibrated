"""Solver-free contract tests for the licensed continuation runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from development_kit.tests.integration.branch_continuation_campaign_acceptance import (
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
    assert set(receipt["incidence_readbacks"]) == {"angle-0", "angle-1", "angle-2"}
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
