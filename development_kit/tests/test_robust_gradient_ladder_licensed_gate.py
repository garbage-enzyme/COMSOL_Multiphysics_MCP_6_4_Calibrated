"""Solver-free tests for the owned alpha7.2 S4 licensed ladder."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from development_kit.scripts import robust_gradient_ladder_licensed_gate as gate


@pytest.fixture
def gate_root(tmp_path: Path):
    root = (
        Path("D:/mcp_tests") / f"l{uuid.uuid4().hex[:8]}" if os.name == "nt" else tmp_path / "a72s4"
    )
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        for suffix in gate._SUFFIXES.values():
            shutil.rmtree(root.with_name(root.name + suffix), ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _args(
    root: Path,
    tmp_path: Path,
    *,
    run_mma: bool = False,
    include_mma_budgets: bool = True,
):
    source = tmp_path / "source.mph"
    manifest = tmp_path / "manifest.json"
    audit = tmp_path / "audit.json"
    source.write_bytes(b"source")
    manifest.write_text("{}", encoding="utf-8")
    audit.write_text("{}", encoding="utf-8")
    values = [
        "--test-root",
        str(root),
        "--source-model",
        str(source),
        "--manifest",
        str(manifest),
        "--tree-audit",
        str(audit),
        "--cores",
        "3",
        "--validation-max-solves",
        "20",
        "--optimizer-max-solves",
        "150",
        "--max-iterations",
        "3",
        "--max-wall-time-seconds",
        "14400",
        "--max-commit-fraction",
        "0.9",
        "--max-disk-bytes",
        "2147483648",
        "--max-review-items",
        "20",
        "--max-elements-per-model",
        "300000",
        "--minimum-element-quality",
        "0.1",
        "--minimum-available-memory-bytes",
        "1073741824",
        "--minimum-runtime-free-bytes",
        "107374182400",
    ]
    if run_mma:
        values.append("--run-mma")
        if include_mma_budgets:
            values.extend(
                [
                    "--mma-max-solves",
                    "150",
                    "--mma-max-iterations",
                    "3",
                    "--mma-max-wall-time-seconds",
                    "14400",
                ]
            )
    return gate._parser().parse_args(values)


def test_parser_has_no_caller_budget_or_resource_defaults():
    actions = {action.dest: action.default for action in gate._parser()._actions}
    for name in (
        "cores",
        "validation_max_solves",
        "optimizer_max_solves",
        "max_iterations",
        "max_wall_time_seconds",
        "max_commit_fraction",
        "max_disk_bytes",
        "max_review_items",
        "max_elements_per_model",
        "minimum_element_quality",
        "minimum_available_memory_bytes",
        "minimum_runtime_free_bytes",
        "mma_max_solves",
        "mma_max_iterations",
        "mma_max_wall_time_seconds",
    ):
        assert actions[name] is None


def test_mma_requires_its_own_explicit_budget(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    with pytest.raises(ValueError, match="mma_max_solves"):
        gate._spec(_args(gate_root, tmp_path, run_mma=True, include_mma_budgets=False))


def test_dry_run_freezes_serial_fresh_process_plan_without_starting_solver(
    tmp_path, gate_root, monkeypatch
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, tmp_path, run_mma=True))
    receipt = gate._dry_run(spec)
    assert receipt["stages"] == ["native", "finite_difference", "directional", "gcmma", "mma"]
    assert receipt["budgets"]["max_commit_fraction"] == 0.9
    assert receipt["budgets"]["minimum_runtime_free_bytes"] == 100 * 1024**3
    assert receipt["mma_budget"] == {
        "max_solves": 150,
        "max_iterations": 3,
        "max_wall_time_seconds": 14400,
    }
    assert receipt["solver_started"] is False
    assert receipt["filesystem_modified"] is False


def test_mma_command_uses_its_separate_budget(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    args = _args(gate_root, tmp_path, run_mma=True)
    args.mma_max_solves = 17
    args.mma_max_iterations = 2
    args.mma_max_wall_time_seconds = 900
    command = gate._stage_command(gate._spec(args), "mma")
    assert command[command.index("--max-solves") + 1] == "17"
    assert command[command.index("--max-iterations") + 1] == "2"
    assert command[command.index("--max-wall-time-seconds") + 1] == "900"


def _result(stage: str, revision: str, source_sha256: str):
    receipt = {"source_revision": revision, "source_sha256": source_sha256, "success": True}
    if stage == "native":
        receipt["derivatives"] = [
            {"variable_id": "patch_length_x", "accepted_real": 2.0},
            {"variable_id": "patch_length_y", "accepted_real": -1.0},
        ]
    elif stage == "finite_difference":
        derivatives = []
        for name, error in (("patch_length_x", 0.002), ("patch_length_y", 0.082)):
            steps = [
                {
                    "relative_step": step,
                    "relative_error": error + index * 0.001,
                    "sign_agreement": True,
                }
                for index, step in enumerate((0.01, 0.003, 0.001))
            ]
            derivatives.append({"variable_id": name, "selected": steps[0], "steps": steps})
        receipt.update(
            {
                "cosine_similarity": 0.9996,
                "derivatives": derivatives,
            }
        )
    elif stage == "directional":
        receipt.update(
            {
                "variables": ["patch_length_x", "patch_length_y"],
                "relative_error": 0.039,
                "sign_agreement": True,
            }
        )
    elif stage == "gcmma":
        receipt.update({"optimizer_method": "gcmma", "fresh_forward_delta": 0.44})
    else:
        receipt.update(
            {
                "success": False,
                "optimizer_method": "mma",
                "fresh_forward_delta": -0.15,
            }
        )
    return {
        "returncode": 0 if receipt["success"] else 1,
        "receipt": receipt,
        "receipt_sha256": {
            "native": "a",
            "finite_difference": "b",
            "directional": "c",
            "gcmma": "d",
            "mma": "e",
        }[stage]
        * 64,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
    }


def test_ladder_accepts_gcmma_and_records_failed_mma_without_fallback(
    tmp_path, gate_root, monkeypatch
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        gate.psutil, "virtual_memory", lambda: type("M", (), {"available": 2**40})()
    )
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda _path: type("D", (), {"free": 2**40})())
    spec = gate._spec(_args(gate_root, tmp_path, run_mma=True))
    revision = "c" * 40
    events = []

    class Ownership:
        def status(self, **kwargs):
            events.append(("status", kwargs))
            acquired = any(event[0] == "acquire" for event in events)
            return {
                "process_inventory": {"complete": True},
                "lease": (
                    {
                        "state": "active",
                        "owned_by_current_process": True,
                        "lease": {"comsol_server_processes": []},
                    }
                    if acquired
                    else {"state": "absent"}
                ),
                "external_solver_processes": [],
                "durable_jobs": {"available": True, "active_count": 0},
            }

        def acquire(self, **kwargs):
            events.append(("acquire", kwargs))
            return {"success": True, "acquired": True}

        def heartbeat(self, **kwargs):
            events.append(("heartbeat", kwargs))
            return True

        def release(self):
            events.append(("release", {}))
            return {"success": True, "released": True}

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, _private = gate._run(
        spec,
        child_runner=lambda current, stage: _result(stage, revision, current["source_sha256"]),
        ownership_factory=Ownership,
    )
    assert receipt["success"] is True
    assert receipt["gradient_acceptance"]["passed"] is True
    assert receipt["gradient_acceptance"]["schema_name"] == (
        "comsol_mcp.robust_gradient_acceptance_receipt"
    )
    assert len(receipt["gradient_acceptance"]["receipt_fingerprint"]) == 64
    assert receipt["gcmma"]["disposition"] == "accepted"
    assert receipt["mma"] == {
        "method": "mma",
        "execution_success": False,
        "fresh_forward_delta": -0.15,
        "disposition": "rejected",
        "automatic_fallback_used": False,
    }
    assert events[0][0] == "status"
    assert events[-1][0] == "release"


def test_gradient_threshold_failure_prevents_ladder_success(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        gate.psutil, "virtual_memory", lambda: type("M", (), {"available": 2**40})()
    )
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda _path: type("D", (), {"free": 2**40})())
    spec = gate._spec(_args(gate_root, tmp_path))
    revision = "d" * 40

    class Ownership:
        acquired = False

        def status(self, **_kwargs):
            return {
                "process_inventory": {"complete": True},
                "lease": (
                    {
                        "state": "active",
                        "owned_by_current_process": True,
                        "lease": {"comsol_server_processes": []},
                    }
                    if self.acquired
                    else {"state": "absent"}
                ),
                "external_solver_processes": [],
                "durable_jobs": {"available": True, "active_count": 0},
            }

        def acquire(self, **_kwargs):
            self.acquired = True
            return {"success": True, "acquired": True}

        def heartbeat(self, **_kwargs):
            return True

        def release(self):
            return {"success": True, "released": True}

    stages = []

    def runner(current, stage):
        stages.append(stage)
        result = _result(stage, revision, current["source_sha256"])
        if stage == "directional":
            result["receipt"]["relative_error"] = 0.051
        return result

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, _private = gate._run(spec, child_runner=runner, ownership_factory=Ownership)
    assert receipt["gradient_acceptance"]["passed"] is False
    assert receipt["success"] is False
    assert stages == ["native", "finite_difference", "directional"]


def test_gradient_checks_reject_consistent_but_wrong_fixture_variable_order():
    results = {
        stage: _result(stage, "a" * 40, "b" * 64)
        for stage in ("native", "finite_difference", "directional")
    }
    replacements = ["width", "height"]
    for row, replacement in zip(
        results["native"]["receipt"]["derivatives"], replacements, strict=True
    ):
        row["variable_id"] = replacement
    for row, replacement in zip(
        results["finite_difference"]["receipt"]["derivatives"],
        replacements,
        strict=True,
    ):
        row["variable_id"] = replacement
    results["directional"]["receipt"]["variables"] = replacements
    with pytest.raises(ValueError, match="frozen fixture"):
        gate._gradient_checks(results)


def test_ladder_rejects_stage_process_residue(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        gate.psutil, "virtual_memory", lambda: type("M", (), {"available": 2**40})()
    )
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda _path: type("D", (), {"free": 2**40})())
    spec = gate._spec(_args(gate_root, tmp_path))
    revision = "e" * 40

    class Ownership:
        acquired = False
        heartbeat_count = 0

        def status(self, **_kwargs):
            residue = self.heartbeat_count >= 2
            return {
                "process_inventory": {"complete": True},
                "lease": (
                    {
                        "state": "active",
                        "owned_by_current_process": True,
                        "lease": {"comsol_server_processes": ([{"pid": 42}] if residue else [])},
                    }
                    if self.acquired
                    else {"state": "absent"}
                ),
                "external_solver_processes": [],
                "durable_jobs": {"available": True, "active_count": 0},
            }

        def acquire(self, **_kwargs):
            self.acquired = True
            return {"success": True, "acquired": True}

        def heartbeat(self, **_kwargs):
            self.heartbeat_count += 1
            return True

        def release(self):
            return {"success": True, "released": True}

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, private = gate._run(
        spec,
        child_runner=lambda current, stage: _result(stage, revision, current["source_sha256"]),
        ownership_factory=Ownership,
    )
    assert receipt["success"] is False
    assert receipt["error"]["type"] == "RuntimeError"
    assert "residue remains after native" in private["error"]
