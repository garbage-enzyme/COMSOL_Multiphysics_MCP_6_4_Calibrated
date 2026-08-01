"""Solver-free contracts for the explicit standalone licensed acceptance runner."""

from __future__ import annotations

import json
from pathlib import Path

from development_kit.scripts import standalone_licensed_gate as gate


def test_acceptance_runner_composes_pause_resume_and_path_free_receipt(
    ascii_tmp_path: Path, monkeypatch
) -> None:
    output = ascii_tmp_path / "acceptance"
    statuses = iter(
        [
            {"status": "running", "phase": "solving", "completed": 0},
            {"status": "paused", "completed": 1},
            {
                "status": "completed",
                "completed": 3,
                "physical_summary": {
                    "status": "passed",
                    "point_count": 3,
                    "maximum_capacitance_relative_delta": 1e-12,
                    "maximum_energy_over_voltage_squared_relative_delta": 1e-12,
                },
            },
        ]
    )
    launches: list[bool] = []
    monkeypatch.setattr(gate, "_relevant_processes", lambda: [])

    def build(directory: Path) -> dict:
        directory.mkdir()
        return {"launcher": {"sha256": "a" * 64}}

    def launch(_directory: Path, _comsol_root: Path, *, resume: bool = False) -> dict:
        launches.append(resume)
        return {
            "owner": {
                "pid": 1,
                "process_create_time": 1.0,
                "command_signature": "b" * 64,
            }
        }

    monkeypatch.setattr(gate, "build_standalone_executable", build)
    monkeypatch.setattr(gate, "launch_standalone_campaign", launch)
    monkeypatch.setattr(gate, "_wait_for_status", lambda *_args, **_kwargs: next(statuses))
    monkeypatch.setattr(
        gate,
        "request_standalone_pause",
        lambda _directory: {"request_id": "c" * 32},
    )
    monkeypatch.setattr(
        gate,
        "read_standalone_results",
        lambda *_args, **_kwargs: {
            "results_sha256": "d" * 64,
            "rows": [
                {"attempt_id": "1" * 32, "comsol_version": "6.4.0.293"},
                {"attempt_id": "2" * 32, "comsol_version": "6.4.0.293"},
                {"attempt_id": "2" * 32, "comsol_version": "6.4.0.293"},
            ],
            "terminal": {"status": "completed"},
        },
    )

    receipt = gate.run_acceptance(
        comsol_root=ascii_tmp_path / "COMSOL64" / "Multiphysics",
        output_directory=output,
        timeout_seconds=60.0,
    )

    assert launches == [False, True]
    assert receipt["status"] == "passed"
    assert receipt["attempt_row_counts"] == [1, 2]
    assert receipt["python_required_at_target"] is False
    assert receipt["comsol_root_included"] is False
    serialized = (output / gate.RECEIPT_NAME).read_text(encoding="utf-8")
    assert str(ascii_tmp_path) not in serialized
    assert json.loads(serialized) == receipt
