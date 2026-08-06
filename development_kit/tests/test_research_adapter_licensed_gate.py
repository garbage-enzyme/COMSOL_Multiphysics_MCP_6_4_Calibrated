"""Solver-free contracts for the private S4 licensed gate transport."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from development_kit.scripts import research_adapter_licensed_gate as gate
from development_kit.scripts import research_campaign_licensed_gate as campaign_gate


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"g{uuid.uuid4().hex[:8]}"
        if os.name == "nt"
        else tmp_path / "a70gdry"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_licensed_gate_dry_run_is_bound_and_solver_free(tmp_path: Path, gate_root: Path):
    root = gate_root
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    manifest = root / "manifest.json"
    audit = root / "tree-audit.json"
    manifest.write_text(json.dumps({"manifest": 1}), encoding="utf-8")
    audit.write_text(json.dumps({"audit": 1}), encoding="utf-8")
    args = argparse.Namespace(
        test_root=root,
        source_model=source,
        manifest=manifest,
        tree_audit=audit,
        patch_length_x=9.0e-7,
        patch_length_y=8.0e-7,
        wavelength_um=5.0,
        cores=2,
        dry_run=True,
    )

    spec = gate._spec(args)
    result = gate._dry_run(spec)

    assert result["success"] is True
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
    assert result["paths_included"] is False
    assert result["candidate"] == {
        "patch_length_x": 9.0e-7,
        "patch_length_y": 8.0e-7,
    }
    assert not (root / "runtime").exists()


def test_gate_settings_are_isolated_and_strict(tmp_path: Path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    spec = {
        "runtime": Path("D:/mcp_tests/a70gset/runtime"),
        "artifacts": Path("D:/mcp_tests/a70gset/artifacts"),
        "source": source.resolve(),
    }

    settings = gate._settings(spec)

    assert settings["profile"] == {"name": "full"}
    assert settings["shared_server"] == {"enabled": False}
    assert all(settings["evidence_integrity"]["checks"].values())
    assert settings["paths"]["model_read_roots"] == [str(source.parent.resolve())]


def test_gate_scripts_are_directly_importable_from_repository_root():
    for script in (gate.SERVER, Path(gate.__file__)):
        completed = subprocess.run(
            [sys.executable, str(script), "--help"]
            if script == Path(gate.__file__)
            else [
                sys.executable,
                "-c",
                (f"import runpy; runpy.run_path({str(script)!r}, run_name='gate_import_probe')"),
            ],
            cwd=gate.REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("wavelength", "expected"),
    [
        (
            {
                "complete": True,
                "requested_m": 5.0e-6,
                "evaluated_parameter_m": 5.0e-6,
                "solved_frequency_wavelength_m": 5.0e-6,
            },
            True,
        ),
        (
            {
                "complete": True,
                "requested_m": 5.0e-6,
                "evaluated_parameter_m": 10.0e-6,
                "solved_frequency_wavelength_m": 10.0e-6,
            },
            False,
        ),
    ],
)
def test_gate_requires_requested_evaluated_and_solved_wavelength_identity(
    wavelength: dict, expected: bool
):
    assert gate._wavelength_synchronized({"wavelength": wavelength}) is expected


def test_campaign_gate_dry_run_freezes_budget_grid_and_tolerances(tmp_path: Path, gate_root: Path):
    source = tmp_path / "source.mph"
    source.write_bytes(b"source")
    manifest = gate_root / "manifest.json"
    audit = gate_root / "tree-audit.json"
    manifest.write_text(
        json.dumps(
            {
                "structure_family": "periodic_mim_patch_v1",
                "source_identity": {"source_sha256": "a" * 64},
                "mutable_dimensions": [
                    {
                        "variable_id": "patch_length_x",
                        "baseline": 8.56e-7,
                        "lower": 6.42e-7,
                        "upper": 1.07e-6,
                    },
                    {
                        "variable_id": "patch_length_y",
                        "baseline": 8.56e-7,
                        "lower": 6.42e-7,
                        "upper": 1.07e-6,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    audit.write_text(json.dumps({"audit": 1}), encoding="utf-8")
    args = argparse.Namespace(
        test_root=gate_root,
        source_model=source,
        manifest=manifest,
        tree_audit=audit,
        mode="impossible",
        budget=32,
        cores=4,
        dry_run=True,
    )

    result = campaign_gate._dry_run(campaign_gate._spec(args))

    assert result["success"] is True
    assert result["candidate_evaluation_budget"] == 32
    assert result["wavelength_solve_count_per_candidate"] == 17
    assert result["objective_tolerances"] == {
        "peak_wavelength_m": campaign_gate.PEAK_TOLERANCE_M,
        "quality_factor": campaign_gate.Q_TOLERANCE,
    }
    assert result["solver_started"] is False
    assert result["filesystem_modified"] is False
