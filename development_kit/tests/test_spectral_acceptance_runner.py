"""Solver-free contract tests for the explicit licensed spectral runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.jobs.spectral_characterization import _SPECTRAL_CHARACTERIZATION_INPUT_FIELDS

from development_kit.tests.integration import spectral_characterization_acceptance as runner
from development_kit.tests.integration.spectral_characterization_acceptance import (
    _scientific_acceptance,
    run_acceptance,
)
from development_kit.tests.spectral_job_fixtures import spectral_job_spec


@pytest.fixture
def ascii_root(ascii_tmp_path):
    return ascii_tmp_path / "spectral-acceptance"


def _raw_spec(spec: dict) -> dict:
    return {
        key: value for key, value in spec.items() if key in _SPECTRAL_CHARACTERIZATION_INPUT_FIELDS
    }


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
            dry_run=False,
            worker_runner=lambda *_args, **_kwargs: pytest.fail("worker must not start"),
        )


def test_receipt_publication_uses_the_directory_durable_exclusive_primitive(tmp_path, monkeypatch):
    output = tmp_path / "receipt.json"
    calls = []

    def publish(path, value):
        calls.append((Path(path), value))
        output.write_text(json.dumps(value), encoding="utf-8")

    monkeypatch.setattr(runner, "atomic_write_json_exclusive", publish)
    runner._write_json(output, {"success": True})

    assert calls == [(output, {"success": True})]


def _scientific_row(index: int = 0) -> dict:
    wavelength = 4.0e-6 + index * 1.0e-9
    return {
        "point_id": f"point-{index}",
        "row_sha256": "a" * 64,
        "requested_wavelength_m": wavelength,
        "evaluated_wavelength_m": wavelength,
        "frequency_wavelength_m": wavelength,
        "R": 0.4,
        "T": 0.5,
        "A": 0.1,
        "mesh_element_count": 10,
        "mesh_vertex_count": 8,
        "solve_seconds": 0.1,
        "audit_artifact": {"path": "audit.json", "sha256": "b" * 64},
    }


def test_spectral_success_requires_complete_physical_row_evidence(tmp_path):
    spec = spectral_job_spec(tmp_path)
    rows = [_scientific_row(index) for index in range(5)]

    accepted = _scientific_acceptance(rows, [{"stage_index": 0}], spec)
    bad_closure = [dict(row) for row in rows]
    bad_closure[0]["A"] = 0.3
    rejected = _scientific_acceptance(bad_closure, [{"stage_index": 0}], spec)

    assert accepted["passed"] is True
    assert all(accepted["checks"].values())
    assert rejected["passed"] is False
    assert rejected["checks"]["power_closure"] is False


def test_spectral_success_rejects_empty_or_incomplete_evidence(tmp_path):
    spec = spectral_job_spec(tmp_path)
    rows = [_scientific_row(index) for index in range(4)]

    no_stage = _scientific_acceptance(rows, [], spec)
    insufficient = _scientific_acceptance(rows, [{"stage_index": 0}], spec)
    rows[0]["mesh_element_count"] = 0
    bad_mesh = _scientific_acceptance([*rows, _scientific_row(4)], [{"stage_index": 0}], spec)

    assert no_stage["checks"]["stage_plan_present"] is False
    assert insufficient["checks"]["minimum_point_count"] is False
    assert bad_mesh["checks"]["mesh_positive"] is False
