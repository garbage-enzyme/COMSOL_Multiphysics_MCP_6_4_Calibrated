"""Fake-process gates for mandatory H1 serial release orchestration."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts.run_real_release_gate import run_release_gate
from src.evidence.real_fixture import MODEL_ENV


def _args(tmp_path, **overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "controlled.mph"
    source.write_bytes(b"fixture")
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps(
            {
                "source_model_path": str(source),
                "wavelength": {"value": 5.292, "unit": "um"},
                "reference_air": {
                    "top_air_domain_ids": [6],
                    "top_air_coordinate_range": {
                        "x": [0.0, 1.0], "y": [0.0, 1.0], "z": [0.5, 1.0]
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    values = {
        "output": tmp_path / "release.json",
        "require_h1": True,
        "h1_spec": spec,
        "h1_cores": 8,
        "h1_timeout_seconds": 300.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _clean(_owner):
    return {"collision": False, "lease": {"state": "absent"}}


class FakeRunner:
    def __init__(self, *, h1_success=True, suite_success=True):
        self.h1_success = h1_success
        self.suite_success = suite_success
        self.commands = []
        self.kwargs = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.kwargs.append(kwargs)
        if "h1_real_physical_evidence.py" in " ".join(command):
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "success": self.h1_success,
                        "cleanup": {"passed": self.h1_success},
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0 if self.h1_success else 1, "h1", "")
        return subprocess.CompletedProcess(command, 0 if self.suite_success else 1, "suite", "")


def test_h1_runs_before_regression_and_both_receipts_are_required(tmp_path):
    runner = FakeRunner()
    receipt = run_release_gate(
        _args(tmp_path),
        command_runner=runner,
        owner=object(),
        pid_provider=lambda: {10},
        wait_clean=_clean,
    )

    assert receipt["returncode"] == 0
    assert len(runner.commands) == 2
    assert "h1_real_physical_evidence.py" in " ".join(runner.commands[0])
    assert "tests/integration/test_real_comsol.py" in runner.commands[1]
    assert Path(runner.kwargs[1]["env"][MODEL_ENV]).name == "controlled.mph"
    assert receipt["phases"]["h1"]["passed"] is True
    assert len(receipt["phases"]["h1"]["receipt_sha256"]) == 64
    assert receipt["phases"]["licensed_regression"]["passed"] is True


def test_h1_failure_skips_remaining_licensed_suite_and_release_fails(tmp_path):
    runner = FakeRunner(h1_success=False)
    receipt = run_release_gate(
        _args(tmp_path),
        command_runner=runner,
        owner=object(),
        pid_provider=lambda: set(),
        wait_clean=_clean,
    )

    assert receipt["returncode"] == 1
    assert len(runner.commands) == 1
    assert receipt["phases"]["h1"]["passed"] is False
    assert receipt["phases"]["licensed_regression"]["started"] is False
    assert receipt["phases"]["licensed_regression"]["skipped_reason"] == "H1 did not pass"


def test_missing_or_timed_out_h1_receipt_cannot_pass_release(tmp_path):
    class MissingReceiptRunner:
        def __call__(self, command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, "no receipt", "")

    missing = run_release_gate(
        _args(tmp_path / "missing"),
        command_runner=MissingReceiptRunner(),
        owner=object(),
        pid_provider=lambda: set(),
        wait_clean=_clean,
    )

    class TimeoutRunner:
        def __call__(self, command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 1.0, output="partial")

    timed_out = run_release_gate(
        _args(tmp_path / "timeout"),
        command_runner=TimeoutRunner(),
        owner=object(),
        pid_provider=lambda: set(),
        wait_clean=_clean,
    )

    assert missing["returncode"] == 1
    assert missing["phases"]["h1"]["receipt_sha256"] is None
    assert timed_out["returncode"] == 1
    assert timed_out["phases"]["h1"]["timed_out"] is True
    assert timed_out["phases"]["licensed_regression"]["started"] is False


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"h1_spec": None}, "requires"),
        ({"h1_cores": 0}, "1..64"),
        ({"h1_timeout_seconds": 8000.0}, "1..7200"),
    ],
)
def test_mandatory_h1_mode_rejects_missing_or_unbounded_inputs(tmp_path, overrides, match):
    with pytest.raises(ValueError, match=match):
        run_release_gate(
            _args(tmp_path, **overrides),
            command_runner=FakeRunner(),
            owner=object(),
            pid_provider=lambda: set(),
            wait_clean=_clean,
        )


def test_outer_cleanup_uncertainty_blocks_release_even_after_both_phases_pass(tmp_path):
    runner = FakeRunner()
    pid_sets = iter(({10}, {11}))
    receipt = run_release_gate(
        _args(tmp_path),
        command_runner=runner,
        owner=object(),
        pid_provider=lambda: next(pid_sets),
        wait_clean=_clean,
    )

    assert receipt["returncode"] == 1
    assert receipt["cleanup"]["comsol_pid_set_unchanged"] is False
    assert receipt["cleanup"]["passed"] is False
