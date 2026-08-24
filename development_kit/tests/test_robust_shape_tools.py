"""Public experimental robust-shape tool and evidence tests."""

from __future__ import annotations

import asyncio
import json

import pytest

from comsol_mcp.jobs.robust_shape_rows import append_robust_shape_row
from comsol_mcp.jobs.robust_shape_worker import run as run_robust_worker
from comsol_mcp.server import create_server
from comsol_mcp.tools import robust_shape as robust_tools
from development_kit.tests.mcp_test_support import decode_tool_result
from development_kit.tests.test_robust_shape_optimization import _write_manifest
from development_kit.tests.test_robust_shape_worker import _manager


def _call(server, name, arguments):
    return decode_tool_result(asyncio.run(server.call_tool(name, arguments)))


def _arguments(envelope):
    return {
        "submission_manifest_path": envelope["submission_manifest_path"],
        "submission_manifest_sha256": envelope["submission_manifest_sha256"],
        "cores": envelope["cores"],
        "version": envelope["version"],
        "max_mesh_elements": envelope["resource_policy"]["max_mesh_elements"],
        "comsol_temporary_directory": envelope["comsol_temporary_directory"],
    }


def test_robust_tools_are_experimental_and_default_off():
    names = {
        "robust_shape_plan_preview",
        "robust_shape_job_submit",
        "robust_shape_evidence_inspect",
        "robust_shape_evidence_verify",
    }
    assert names.isdisjoint(create_server("robust-core", profile="core")._tool_manager._tools)
    assert names.isdisjoint(create_server("robust-basic", profile="basic_fem")._tool_manager._tools)
    assert names.isdisjoint(
        create_server("robust-wave", profile="wave_optics")._tool_manager._tools
    )
    assert names <= set(
        create_server("robust-experimental", profile="experimental")._tool_manager._tools
    )
    assert names <= set(create_server("robust-full", profile="full")._tool_manager._tools)


def test_preview_normalizes_without_submission_or_solver(ascii_tmp_path):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    server = create_server("robust-preview", profile="experimental")

    result = _call(server, "robust_shape_plan_preview", _arguments(envelope))

    assert result["success"] is True
    preview = result["preview"]
    assert preview["schema_name"] == "comsol_mcp.robust_shape_plan_preview"
    assert preview["condition_inventory"]["declared"] == 24
    assert preview["variable_inventory"]["count"] == 2
    assert preview["entitlement"] == {
        "live_checked": False,
        "state": "declared_pending_submit_preflight",
    }
    assert preview["effects"] == {
        "submitted": False,
        "filesystem_modified": False,
        "solver_started": False,
        "source_model_mutated": False,
    }
    assert len(preview["preview_sha256"]) == 64


def test_preview_rejects_changed_manifest_without_path_disclosure(ascii_tmp_path):
    envelope, _, manifest = _write_manifest(ascii_tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b" ")
    server = create_server("robust-preview-reject", profile="experimental")

    result = _call(server, "robust_shape_plan_preview", _arguments(envelope))

    assert result["success"] is False
    assert result["reason_code"] == "robust_shape_preview_rejected"
    assert result["error_type"] == "ValueError"
    assert str(ascii_tmp_path) not in result["error"]
    assert result["solver_started"] is False


def test_submit_routes_only_through_durable_manager(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    captured = []

    class Manager:
        def submit(self, spec):
            captured.append(spec)
            return {"success": True, "job_id": "job-robust", "status": "submitted"}

    monkeypatch.setattr(robust_tools, "job_manager", Manager())
    server = create_server("robust-submit", profile="experimental")

    result = _call(server, "robust_shape_job_submit", _arguments(envelope))

    assert result["success"] is True
    assert result["job_id"] == "job-robust"
    assert result["status"] == "submitted"
    assert captured == [envelope]


def test_evidence_inspect_and_verify_complete_synthetic_job(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    assert run_robust_worker(str(manager.store.root), submitted["job_id"]) == 0

    for guard in manager.store.job_dir(submitted["job_id"]).glob("*.guard"):
        guard.unlink()
    files_before = {
        path.name: path.stat().st_mtime_ns
        for path in manager.store.job_dir(submitted["job_id"]).iterdir()
    }
    summary = robust_tools.inspect_robust_shape_evidence(
        submitted["job_id"], limit=4, manager=manager
    )
    verification = robust_tools.verify_robust_shape_evidence(submitted["job_id"], manager=manager)
    files_after = {
        path.name: path.stat().st_mtime_ns
        for path in manager.store.job_dir(submitted["job_id"]).iterdir()
    }

    assert summary["job_status"] == "completed"
    assert summary["condition_progress"] == {
        "declared": 24,
        "completed": 24,
        "pending": 0,
    }
    assert len(summary["recent_rows"]) == 4
    assert summary["redaction"] == {
        "artifact_paths_included": False,
        "material_values_included": False,
        "raw_errors_included": False,
    }
    assert verification["verified_complete"] is True
    assert verification["reason_codes"] == []
    assert all(verification["checks"].values())
    assert files_after == files_before
    assert str(ascii_tmp_path) not in json.dumps(summary, sort_keys=True)


def test_inspector_does_not_count_undeclared_condition_rows(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    assert run_robust_worker(str(manager.store.root), submitted["job_id"]) == 0
    spec = manager.store.read_spec(submitted["job_id"])
    state = manager.store.read_state(submitted["job_id"])
    append_robust_shape_row(
        manager.store.job_dir(submitted["job_id"]) / "robust_shape_rows.jsonl",
        job_fingerprint=spec["spec_fingerprint"],
        attempt=state["attempt"],
        kind="condition",
        payload={
            "iteration_id": "iteration-foreign",
            "condition_id": "condition-foreign",
            "condition_order": 24,
            "status": "completed",
            "observation_fingerprint": "a" * 64,
            "objective_contribution": 1.0,
            "reason_code": "completed",
        },
    )

    summary = robust_tools.inspect_robust_shape_evidence(submitted["job_id"], manager=manager)

    assert summary["condition_progress"] == {
        "declared": 24,
        "completed": 24,
        "pending": 0,
    }


def test_public_errors_redact_os_paths(ascii_tmp_path, monkeypatch):
    def reject_read(job_id):
        raise PermissionError(str(ascii_tmp_path / "private-job"))

    monkeypatch.setattr(robust_tools.job_manager.store, "read_spec", reject_read)
    server = create_server("robust-os-error", profile="experimental")

    result = _call(server, "robust_shape_evidence_inspect", {"job_id": "job-private"})

    assert result["success"] is False
    assert result["reason_code"] == "robust_shape_evidence_inspection_rejected"
    assert result["error_type"] == "PermissionError"
    assert str(ascii_tmp_path) not in result["error"]


def test_verifier_reports_incomplete_failed_job_without_claiming_success(
    ascii_tmp_path, monkeypatch
):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    manager.store.update_state(
        submitted["job_id"],
        "failed",
        patch={"last_error": {"type": "InjectedFailure"}},
    )

    verification = robust_tools.verify_robust_shape_evidence(submitted["job_id"], manager=manager)

    assert verification["job_status"] == "failed"
    assert verification["verified_complete"] is False
    assert "all_conditions_complete" in verification["reason_codes"]
    assert verification["checks"]["row_chain_valid"] is True


def test_readonly_verifier_rejects_and_preserves_crash_tail(ascii_tmp_path, monkeypatch):
    envelope, _, _ = _write_manifest(ascii_tmp_path)
    manager = _manager(ascii_tmp_path / "jobs", monkeypatch)
    submitted = manager.submit(envelope)
    assert run_robust_worker(str(manager.store.root), submitted["job_id"]) == 0
    journal = manager.store.job_dir(submitted["job_id"]) / "robust_shape_rows.jsonl"
    with journal.open("ab") as handle:
        handle.write(b'{"incomplete"')
    before = journal.read_bytes()

    with pytest.raises(ValueError, match="journal is incomplete"):
        robust_tools.verify_robust_shape_evidence(submitted["job_id"], manager=manager)

    assert journal.read_bytes() == before


def test_public_schemas_are_closed_and_bounded():
    server = create_server("robust-schema", profile="experimental")
    for name in (
        "robust_shape_plan_preview",
        "robust_shape_job_submit",
        "robust_shape_evidence_inspect",
        "robust_shape_evidence_verify",
    ):
        schema = server._tool_manager._tools[name].parameters
        assert schema["additionalProperties"] is False
        assert json.dumps(schema, sort_keys=True)


def test_null_adapter_configuration_stays_inside_the_bounded_contract(tmp_path, monkeypatch):
    spec = {
        "condition_table": {"conditions": []},
        "spec_fingerprint": "0" * 64,
        "adapter_configuration": None,
    }
    monkeypatch.setattr(
        robust_tools,
        "_robust_job",
        lambda _manager, _job_id: (spec, {"status": "running"}, tmp_path),
    )

    verification = robust_tools.verify_robust_shape_evidence("job-x", manager=object())

    assert verification["job_status"] == "running"
    assert verification["checks"]["spec_fingerprint_valid"] is False
    assert "validated_gradient_present" in verification["reason_codes"]
    assert verification["verified_complete"] is False


def test_completed_condition_orders_must_form_the_canonical_zero_based_sequence():
    from comsol_mcp.tools.robust_shape import _completed_orders_valid

    assert _completed_orders_valid([0, 1, 2]) is True
    assert _completed_orders_valid([0, 1]) is True  # contiguous partial run
    assert _completed_orders_valid([0, 2]) is False  # gap: a row is missing
    assert _completed_orders_valid([0, 0, 2]) is False  # duplicated order
    assert _completed_orders_valid(["0", "1"]) is False
    assert _completed_orders_valid([True, False]) is False
