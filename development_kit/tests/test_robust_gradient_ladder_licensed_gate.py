"""Solver-free tests for the owned alpha7.2 S4 licensed ladder."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from development_kit.scripts import robust_gradient_ladder_licensed_gate as gate
from development_kit.scripts import verify_robust_gradient_ladder_receipt as verifier


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
        "15",
        "--optimizer-max-solves",
        "105",
        "--total-max-solves",
        "150",
        "--max-iterations",
        "3",
        "--gcmma-optimizer-iterations",
        "3",
        "--gcmma-move-limit",
        "0.05",
        "--validation-max-wall-time-seconds",
        "1800",
        "--optimizer-max-wall-time-seconds",
        "9000",
        "--total-max-wall-time-seconds",
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
        "--deformation-jacobian-expression",
        "reldetjac",
        "--minimum-relative-jacobian",
        "0",
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
                    "--mma-optimizer-iterations",
                    "3",
                    "--mma-move-limit",
                    "0.05",
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
        "total_max_solves",
        "max_iterations",
        "gcmma_optimizer_iterations",
        "gcmma_move_limit",
        "validation_max_wall_time_seconds",
        "optimizer_max_wall_time_seconds",
        "total_max_wall_time_seconds",
        "max_commit_fraction",
        "max_disk_bytes",
        "max_review_items",
        "max_elements_per_model",
        "minimum_element_quality",
        "deformation_jacobian_expression",
        "minimum_relative_jacobian",
        "minimum_available_memory_bytes",
        "minimum_runtime_free_bytes",
        "mma_max_solves",
        "mma_max_iterations",
        "mma_optimizer_iterations",
        "mma_move_limit",
        "mma_max_wall_time_seconds",
    ):
        assert actions[name] is None


def test_mma_requires_its_own_explicit_budget(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    with pytest.raises(ValueError, match="mma_max_solves"):
        gate._spec(_args(gate_root, tmp_path, run_mma=True, include_mma_budgets=False))


def test_mma_requires_its_own_explicit_move_limit(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    arguments = _args(gate_root, tmp_path, run_mma=True)
    arguments.mma_move_limit = None
    with pytest.raises(ValueError, match="mma_move_limit"):
        gate._spec(arguments)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("gcmma_optimizer_iterations", 4, "gcmma_optimizer_iterations exceeds"),
        ("mma_optimizer_iterations", 4, "mma_optimizer_iterations exceeds"),
    ],
)
def test_requested_optimizer_iterations_must_fit_their_budget_caps(
    tmp_path, gate_root, monkeypatch, field, value, message
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    arguments = _args(gate_root, tmp_path, run_mma=True)
    setattr(arguments, field, value)
    with pytest.raises(ValueError, match=message):
        gate._spec(arguments)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("total_max_solves", 149, "solve caps exceed"),
        ("total_max_wall_time_seconds", 14_399, "wall caps exceed"),
    ],
)
def test_base_stage_allocations_must_fit_caller_total_budget(
    tmp_path, gate_root, monkeypatch, field, value, message
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    arguments = _args(gate_root, tmp_path)
    setattr(arguments, field, value)
    with pytest.raises(ValueError, match=message):
        gate._spec(arguments)


def test_dry_run_freezes_serial_fresh_process_plan_without_starting_solver(
    tmp_path, gate_root, monkeypatch
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    spec = gate._spec(_args(gate_root, tmp_path, run_mma=True))
    receipt = gate._dry_run(spec)
    assert receipt["stages"] == ["native", "finite_difference", "directional", "gcmma", "mma"]
    assert receipt["budgets"]["max_commit_fraction"] == 0.9
    assert receipt["budgets"]["total_max_solves"] == 150
    assert receipt["budgets"]["total_max_wall_time_seconds"] == 14_400
    assert receipt["budgets"]["minimum_runtime_free_bytes"] == 100 * 1024**3
    assert receipt["mma_budget"] == {
        "max_solves": 150,
        "max_iterations": 3,
        "max_wall_time_seconds": 14400,
    }
    assert receipt["optimizer_execution"] == {
        "gcmma": {"optimizer_iterations": 3, "move_limit": 0.05},
        "mma": {"optimizer_iterations": 3, "move_limit": 0.05},
    }
    assert receipt["deformation_feasibility_policy"] == {
        "jacobian_expression": "reldetjac",
        "minimum_relative_jacobian": 0.0,
        "comparison": "strictly_greater_than",
        "scope": "fresh_forward_finalist_deformed_geometry",
    }
    assert receipt["solver_started"] is False
    assert receipt["filesystem_modified"] is False


def test_mma_command_uses_its_separate_budget(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    args = _args(gate_root, tmp_path, run_mma=True)
    args.mma_max_solves = 17
    args.mma_max_iterations = 2
    args.mma_optimizer_iterations = 1
    args.mma_max_wall_time_seconds = 900
    command = gate._stage_command(gate._spec(args), "mma")
    assert command[command.index("--max-solves") + 1] == "17"
    assert command[command.index("--max-iterations") + 1] == "2"
    assert command[command.index("--optimizer-iterations") + 1] == "1"
    assert float(command[command.index("--move-limit") + 1]) == 0.05
    assert command[command.index("--max-wall-time-seconds") + 1] == "900"


def _result(stage: str, revision: str, source_sha256: str, spec: dict | None = None):
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
        assert spec is not None
        receipt.update(
            {
                "optimizer_method": "gcmma",
                "requested_optimizer_iterations": spec["gcmma_optimizer_iterations"],
                "requested_move_limit": spec["gcmma_move_limit"],
                "solver_move_limit": {
                    "mmamaxiter": str(spec["gcmma_optimizer_iterations"]),
                    "movelimit": str(spec["gcmma_move_limit"]),
                },
                "budget": gate._optimizer_configuration(spec, "gcmma")["budget"],
                "baseline_objective": 0.2,
                "final_objective": 0.64,
                "fresh_forward_objective": 0.64,
                "fresh_forward_delta": 0.44,
                "mesh_admission_policy": {
                    "max_elements_per_model": spec["max_elements_per_model"],
                    "minimum_element_quality": spec["minimum_element_quality"],
                    "scope": "baseline_and_explicit_finalist_remesh",
                    "internal_optimizer_remesh_callback": False,
                },
                "baseline_mesh": {
                    "element_count": 20_000,
                    "minimum_quality": 0.2,
                    "mean_quality": 0.7,
                    "quality_measure": "volcircum",
                },
                "remesh": {
                    "explicit_rebuild": True,
                    "before": {
                        "element_count": 20_000,
                        "minimum_quality": 0.2,
                        "mean_quality": 0.7,
                        "quality_measure": "volcircum",
                    },
                    "after": {
                        "element_count": 21_000,
                        "minimum_quality": 0.21,
                        "mean_quality": 0.69,
                        "quality_measure": "volcircum",
                    },
                },
                "deformation_feasibility_policy": {
                    "jacobian_expression": spec["deformation_jacobian_expression"],
                    "minimum_relative_jacobian": spec["minimum_relative_jacobian"],
                    "comparison": "strictly_greater_than",
                    "scope": "fresh_forward_finalist_deformed_geometry",
                },
                "deformation_feasibility": {
                    "sample_count": 100,
                    "minimum_relative_jacobian": 0.02,
                    "maximum_relative_jacobian": 1.0,
                    "threshold": spec["minimum_relative_jacobian"],
                    "passed": True,
                },
                "cleanup": {"client_clear": True, "source_unchanged": True},
            }
        )
    else:
        assert spec is not None
        receipt.update(
            {
                "success": False,
                "optimizer_method": "mma",
                "requested_optimizer_iterations": spec["mma_optimizer_iterations"],
                "requested_move_limit": spec["mma_move_limit"],
                "solver_move_limit": {
                    "mmamaxiter": str(spec["mma_optimizer_iterations"]),
                    "movelimit": str(spec["mma_move_limit"]),
                },
                "budget": gate._optimizer_configuration(spec, "mma")["budget"],
                "deformation_feasibility_policy": {
                    "jacobian_expression": spec["deformation_jacobian_expression"],
                    "minimum_relative_jacobian": spec["minimum_relative_jacobian"],
                    "comparison": "strictly_greater_than",
                    "scope": "fresh_forward_finalist_deformed_geometry",
                },
                "cleanup": {"client_clear": True, "source_unchanged": True},
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
        "artifact_bytes": 1024,
        "review_items": gate._stage_review_items(stage, receipt),
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
        child_runner=lambda current, stage: _result(
            stage, revision, current["source_sha256"], current
        ),
        ownership_factory=Ownership,
    )
    assert receipt["success"] is True
    assert receipt["gradient_acceptance"]["passed"] is True
    assert receipt["gradient_acceptance"]["schema_name"] == (
        "comsol_mcp.robust_gradient_acceptance_receipt"
    )
    assert len(receipt["gradient_acceptance"]["receipt_fingerprint"]) == 64
    assert receipt["gcmma"]["disposition"] == "accepted"
    assert receipt["budget_usage"]["artifact_bytes"] == 5 * 1024
    assert receipt["budget_usage"]["max_disk_bytes"] == 2 * 1024**3
    assert receipt["budget_usage"]["review_items"] == 7
    assert receipt["budget_usage"]["max_review_items"] == 20
    assert receipt["budget_usage"]["total_max_wall_time_seconds"] == 14_400
    assert 0.0 <= receipt["budget_usage"]["base_wall_elapsed_seconds"] < 14_400
    assert receipt["mma"]["method"] == "mma"
    assert receipt["mma"]["execution_success"] is False
    assert receipt["mma"]["disposition"] == "rejected"
    assert receipt["mma"]["automatic_fallback_used"] is False
    assert len(receipt["mma"]["receipt_fingerprint"]) == 64
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
        result = _result(stage, revision, current["source_sha256"], current)
        if stage == "directional":
            result["receipt"]["relative_error"] = 0.051
        return result

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, _private = gate._run(spec, child_runner=runner, ownership_factory=Ownership)
    assert receipt["gradient_acceptance"]["passed"] is False
    assert receipt["success"] is False
    assert stages == ["native", "finite_difference", "directional"]


def test_gcmma_deformation_failure_is_structured_without_automatic_recovery(
    tmp_path, gate_root, monkeypatch
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        gate.psutil, "virtual_memory", lambda: type("M", (), {"available": 2**40})()
    )
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda _path: type("D", (), {"free": 2**40})())
    spec = gate._spec(_args(gate_root, tmp_path))
    revision = "9" * 40

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

    def runner(current, stage):
        result = _result(stage, revision, current["source_sha256"], current)
        if stage == "gcmma":
            result["returncode"] = 1
            result["receipt"]["success"] = False
            result["receipt"]["error"] = {
                "code": "deformation_feasibility_failed",
                "type": "FlException",
            }
        return result

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, _private = gate._run(spec, child_runner=runner, ownership_factory=Ownership)
    assert receipt["success"] is False
    assert receipt["error"]["code"] == "deformation_feasibility_failed"
    assert receipt["automatic_move_reduction_used"] is False
    assert receipt["automatic_method_fallback_used"] is False
    assert "mma" not in receipt["stage_receipts"]


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


@pytest.mark.parametrize(
    ("budget_field", "budget_value", "message", "expected_stages"),
    [
        ("max_disk_bytes", 1024, "disk budget", ["native", "finite_difference"]),
        ("max_review_items", 3, "review-item budget", ["native", "finite_difference"]),
    ],
)
def test_ladder_stops_before_next_stage_when_cumulative_budget_is_exceeded(
    tmp_path,
    gate_root,
    monkeypatch,
    budget_field,
    budget_value,
    message,
    expected_stages,
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        gate.psutil, "virtual_memory", lambda: type("M", (), {"available": 2**40})()
    )
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda _path: type("D", (), {"free": 2**40})())
    arguments = _args(gate_root, tmp_path)
    setattr(arguments, budget_field, budget_value)
    spec = gate._spec(arguments)
    revision = "a" * 40
    stages = []

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

    def runner(current, stage):
        stages.append(stage)
        return _result(stage, revision, current["source_sha256"], current)

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, private = gate._run(spec, child_runner=runner, ownership_factory=Ownership)
    assert receipt["success"] is False
    assert message in private["error"]
    assert stages == expected_stages


def test_ladder_stops_before_optimizer_when_total_wall_budget_is_exceeded(
    tmp_path, gate_root, monkeypatch
):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        gate.psutil, "virtual_memory", lambda: type("M", (), {"available": 2**40})()
    )
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda _path: type("D", (), {"free": 2**40})())
    times = iter((0.0, 5_000.0, 10_000.0, 15_000.0))
    monkeypatch.setattr(gate.time, "monotonic", lambda: next(times))
    spec = gate._spec(_args(gate_root, tmp_path))
    revision = "a" * 40
    stages = []

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

    def runner(current, stage):
        stages.append(stage)
        return _result(stage, revision, current["source_sha256"], current)

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, private = gate._run(spec, child_runner=runner, ownership_factory=Ownership)
    assert receipt["success"] is False
    assert "total wall budget" in private["error"]
    assert stages == ["native", "finite_difference", "directional"]


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
        child_runner=lambda current, stage: _result(
            stage, revision, current["source_sha256"], current
        ),
        ownership_factory=Ownership,
    )
    assert receipt["success"] is False
    assert receipt["error"]["type"] == "RuntimeError"
    assert "residue remains after native" in private["error"]


def _verifier_args(root: Path, tmp_path: Path, revision: str, *, run_mma: bool = True):
    values = [
        "--test-root",
        str(root),
        "--source-model",
        str(tmp_path / "source.mph"),
        "--expected-revision",
        revision,
        "--cores",
        "3",
        "--validation-max-solves",
        "15",
        "--optimizer-max-solves",
        "105",
        "--total-max-solves",
        "150",
        "--max-iterations",
        "3",
        "--gcmma-optimizer-iterations",
        "3",
        "--gcmma-move-limit",
        "0.05",
        "--validation-max-wall-time-seconds",
        "1800",
        "--optimizer-max-wall-time-seconds",
        "9000",
        "--total-max-wall-time-seconds",
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
        "--deformation-jacobian-expression",
        "reldetjac",
        "--minimum-relative-jacobian",
        "0",
        "--minimum-available-memory-bytes",
        "1073741824",
        "--minimum-runtime-free-bytes",
        "107374182400",
    ]
    if run_mma:
        values.extend(
            [
                "--run-mma",
                "--mma-max-solves",
                "150",
                "--mma-max-iterations",
                "3",
                "--mma-optimizer-iterations",
                "3",
                "--mma-move-limit",
                "0.05",
                "--mma-max-wall-time-seconds",
                "14400",
            ]
        )
    return verifier._parser().parse_args(values)


def _write_verifiable_ladder(tmp_path, gate_root, monkeypatch):
    monkeypatch.setattr(gate.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        gate.psutil, "virtual_memory", lambda: type("M", (), {"available": 2**40})()
    )
    monkeypatch.setattr(gate.shutil, "disk_usage", lambda _path: type("D", (), {"free": 2**40})())
    spec = gate._spec(_args(gate_root, tmp_path, run_mma=True))
    revision = "f" * 40

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

    def runner(current, stage):
        result = _result(stage, revision, current["source_sha256"], current)
        stage_root = current["child_roots"][stage]
        stage_root.mkdir(parents=False, exist_ok=False)
        receipt_path = stage_root / gate._RECEIPT_NAMES[stage]
        gate.atomic_write_json(receipt_path, result["receipt"])
        stdout_path = current["root"] / f"{stage}.stdout.log"
        stderr_path = current["root"] / f"{stage}.stderr.log"
        stdout_path.write_bytes(f"{stage} stdout\n".encode())
        stderr_path.write_bytes(f"{stage} stderr\n".encode())
        result.update(
            {
                "receipt_sha256": gate._sha(receipt_path),
                "stdout_sha256": gate._sha(stdout_path),
                "stderr_sha256": gate._sha(stderr_path),
                "artifact_bytes": (
                    gate._bounded_tree_bytes(stage_root)
                    + stdout_path.stat().st_size
                    + stderr_path.stat().st_size
                ),
            }
        )
        return result

    monkeypatch.setattr(gate, "_git_identity", lambda: {"revision": revision, "clean": True})
    receipt, _private = gate._run(
        spec,
        child_runner=runner,
        ownership_factory=Ownership,
    )
    gate.atomic_write_json(gate_root / "ladder-receipt.json", receipt)
    return verifier._expected(_verifier_args(gate_root, tmp_path, revision)), receipt


def test_independent_ladder_verifier_reopens_all_receipts_and_logs(
    tmp_path, gate_root, monkeypatch
):
    expected, _receipt = _write_verifiable_ladder(tmp_path, gate_root, monkeypatch)
    verification = verifier.verify(expected)
    assert verification["passed"] is True
    assert verification["stages"] == [
        "native",
        "finite_difference",
        "directional",
        "gcmma",
        "mma",
    ]
    assert verification["automatic_fallback_used"] is False
    assert verification["budget_usage"]["review_items"] == 7


@pytest.mark.parametrize(
    "mutation",
    [
        "stage_order",
        "budget",
        "execution",
        "deformation_policy",
        "cleanup",
        "gradient",
        "stdout",
    ],
)
def test_independent_ladder_verifier_rejects_tampering_and_false_success(
    tmp_path, gate_root, monkeypatch, mutation
):
    expected, receipt = _write_verifiable_ladder(tmp_path, gate_root, monkeypatch)
    if mutation == "stage_order":
        receipt["stages"] = list(reversed(receipt["stages"]))
    elif mutation == "budget":
        receipt["declared_budgets"]["max_review_items"] = 19
    elif mutation == "execution":
        receipt["declared_optimizer_execution"]["gcmma"]["optimizer_iterations"] = 2
    elif mutation == "deformation_policy":
        receipt["declared_deformation_feasibility_policy"]["minimum_relative_jacobian"] = 0.01
    elif mutation == "cleanup":
        receipt["cleanup"]["source_unchanged"] = False
    elif mutation == "gradient":
        receipt["gradient_acceptance"]["passed"] = False
    else:
        (gate_root / "native.stdout.log").write_bytes(b"tampered\n")
    if mutation != "stdout":
        gate.atomic_write_json(gate_root / "ladder-receipt.json", receipt)
    with pytest.raises(ValueError):
        verifier.verify(expected)
