"""Solver-free contract tests for the explicit licensed spectral runner."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid

import pytest

from development_kit.tests.integration.spectral_characterization_acceptance import (
    run_acceptance,
)
from development_kit.tests.spectral_job_fixtures import spectral_job_spec


@pytest.fixture
def ascii_root():
    root = Path("D:/comsol_runtime_test") / f"pytest-spectral-acceptance-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _raw_spec(spec: dict) -> dict:
    allowed = {
        "job_type", "source_model_path", "source_model_relative_identity",
        "configuration_sha256", "parameter_state", "wavelength_parameter",
        "initial_grid", "refinement_policy", "expansion_policy", "maximum_points",
        "collector", "analysis_policy", "measurement_configuration", "resource_policy",
        "cores", "version", "max_retries", "continue_on_error",
    }
    return {key: value for key, value in spec.items() if key in allowed}


def test_dry_run_normalizes_exact_identity_without_starting_comsol(tmp_path, ascii_root):
    spec = spectral_job_spec(tmp_path)
    output = tmp_path / "dry-run.json"
    receipt = run_acceptance(
        raw_spec=_raw_spec(spec),
        runtime_root=ascii_root,
        output=output,
        dry_run=True,
        worker_runner=lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )
    assert receipt["success"] is True
    assert receipt["comsol_client_started"] is False
    assert receipt["spec_fingerprint"] == spec["spec_fingerprint"]
    assert json.loads(output.read_text(encoding="utf-8")) == receipt


def test_receipt_output_is_never_overwritten(tmp_path, ascii_root):
    spec = spectral_job_spec(tmp_path)
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="overwrite"):
        run_acceptance(
            raw_spec=_raw_spec(spec),
            runtime_root=ascii_root,
            output=output,
            dry_run=True,
        )


def test_non_ascii_runtime_root_fails_before_worker_start(tmp_path):
    spec = spectral_job_spec(tmp_path)
    with pytest.raises(ValueError, match="ASCII"):
        run_acceptance(
            raw_spec=_raw_spec(spec),
            runtime_root=tmp_path / "运行时",
            output=tmp_path / "receipt.json",
            dry_run=True,
        )
