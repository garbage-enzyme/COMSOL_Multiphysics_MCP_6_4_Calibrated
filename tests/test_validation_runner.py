from __future__ import annotations

import json
from pathlib import Path

from src.jobs.validation_matrix import normalize_validation_matrix_spec
from src.jobs.validation_rows import read_validation_rows
from src.jobs.validation_runner import run_pending_validation_points


def _spec(tmp_path, *, continue_on_error=False):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"controlled")
    points = []
    for index, point_id in enumerate(("off", "target")):
        points.append(
            {
                "point_id": point_id,
                "configuration_sha256": str(index + 1) * 64,
                "wavelength": {
                    "value": 5.1 + index * 0.1,
                    "unit": "um",
                    "parameter": "wl",
                },
                "collectors": [
                    {"name": "wave_optics_point_audit", "inputs": {"tag": point_id}}
                ],
                "expected_artifact_ids": [f"audit-{point_id}"],
            }
        )
    return normalize_validation_matrix_spec(
        {
            "job_type": "validation_matrix",
            "source_model_path": str(source),
            "points": points,
            "point_limit": 2,
            "cores": 1,
            "continue_on_error": continue_on_error,
            "resource_policy": {
                "wall_time_budget_seconds": 120,
                "minimum_next_point_seconds": 30,
                "max_mesh_elements": 100_000,
            },
        }
    )


def _complete_executor(point, _collector, artifact_directory: Path):
    manifest = artifact_directory / "manifest.json"
    manifest.write_text(json.dumps({"point_id": point["point_id"]}), encoding="utf-8")
    return {
        "success": True,
        "audit_status": "measurement_complete",
        "artifacts": {"manifest": str(manifest)},
        "large_inline_value": "not persisted in the row" * 1000,
    }


def test_runner_persists_bounded_summaries_and_skips_exact_completed_points(tmp_path):
    spec = _spec(tmp_path)
    directory = tmp_path / "job"
    observed = []
    first = run_pending_validation_points(
        spec,
        directory,
        attempt=1,
        collector_executor=_complete_executor,
        on_durable_row=observed.append,
    )
    replay = run_pending_validation_points(
        spec,
        directory,
        attempt=2,
        collector_executor=lambda *_args: (_ for _ in ()).throw(AssertionError("must skip")),
    )

    assert first == {
        "success": True,
        "stop_reason": None,
        "processed": 2,
        "skipped_completed": 0,
        "errors": 0,
        "remaining": 0,
        "last_row_sha256": observed[-1]["row_sha256"],
    }
    assert replay["processed"] == 0
    assert replay["skipped_completed"] == 2
    rows = read_validation_rows(directory / "matrix_rows.jsonl", spec)
    assert len(rows) == 2
    assert "large_inline_value" not in rows[0]


def test_incomplete_collector_is_a_retryable_error_row(tmp_path):
    spec = _spec(tmp_path)
    directory = tmp_path / "job"

    def partial(_point, _collector, artifact_directory):
        manifest = artifact_directory / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        return {
            "success": True,
            "audit_status": "integrity_blocked",
            "artifacts": {"manifest": str(manifest)},
        }

    failed = run_pending_validation_points(
        spec,
        directory,
        attempt=1,
        collector_executor=partial,
    )
    recovered = run_pending_validation_points(
        spec,
        directory,
        attempt=2,
        collector_executor=_complete_executor,
    )

    assert failed["success"] is False
    assert failed["errors"] == 1
    assert recovered["success"] is True
    rows = read_validation_rows(directory / "matrix_rows.jsonl", spec)
    assert [row["status"] for row in rows] == ["error", "ok", "ok"]


def test_continue_on_error_preserves_failure_and_runs_later_points(tmp_path):
    spec = _spec(tmp_path, continue_on_error=True)
    directory = tmp_path / "job"

    def fail_off(point, collector, artifact_directory):
        if point["point_id"] == "off":
            raise RuntimeError("expected failure")
        return _complete_executor(point, collector, artifact_directory)

    result = run_pending_validation_points(
        spec,
        directory,
        attempt=1,
        collector_executor=fail_off,
    )

    assert result["success"] is False
    assert result["processed"] == 2
    assert result["errors"] == 1
    assert [row["status"] for row in read_validation_rows(directory / "matrix_rows.jsonl", spec)] == [
        "error",
        "ok",
    ]


def test_control_request_stops_before_materializing_another_point(tmp_path):
    spec = _spec(tmp_path)
    directory = tmp_path / "job"
    calls = 0

    def should_stop():
        return calls >= 1

    def executor(point, collector, artifact_directory):
        nonlocal calls
        result = _complete_executor(point, collector, artifact_directory)
        calls += 1
        return result

    result = run_pending_validation_points(
        spec,
        directory,
        attempt=1,
        collector_executor=executor,
        should_stop=should_stop,
    )

    assert result["stop_reason"] == "control_request"
    assert result["processed"] == 1
    assert result["remaining"] == 1
    assert len(read_validation_rows(directory / "matrix_rows.jsonl", spec)) == 1


def test_manifest_must_exist_inside_the_exact_attempt_artifact_root(tmp_path):
    spec = _spec(tmp_path)
    directory = tmp_path / "job"
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    result = run_pending_validation_points(
        spec,
        directory,
        attempt=1,
        collector_executor=lambda *_args: {
            "success": True,
            "audit_status": "measurement_complete",
            "artifacts": {"manifest": str(outside)},
        },
    )

    assert result["success"] is False
    row = read_validation_rows(directory / "matrix_rows.jsonl", spec)[0]
    assert row["status"] == "error"
    assert "escapes" in row["error"]["message"]


def test_resource_refusal_before_point_writes_no_false_error_row(tmp_path):
    spec = _spec(tmp_path)
    directory = tmp_path / "job"
    result = run_pending_validation_points(
        spec,
        directory,
        attempt=1,
        collector_executor=lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
        before_point_hook=lambda context: {
            "action": "checkpoint_no_start",
            "start_authorized": False,
            "point_id": context["point_id"],
        },
    )

    assert result["stop_reason"] == "before_point_checkpoint_no_start"
    assert result["processed"] == 0
    assert not (directory / "matrix_rows.jsonl").exists()


def test_resource_stop_after_fsync_preserves_completed_row_then_stops(tmp_path):
    spec = _spec(tmp_path)
    directory = tmp_path / "job"
    after_calls = []

    def after(context):
        after_calls.append(context)
        return {
            "action": "await_confirmation",
            "start_authorized": False,
            "point_id": context["point_id"],
        }

    result = run_pending_validation_points(
        spec,
        directory,
        attempt=1,
        collector_executor=_complete_executor,
        after_durable_row_hook=after,
    )

    assert result["stop_reason"] == "after_durable_row_await_confirmation"
    assert result["processed"] == 1
    assert result["remaining"] == 1
    assert len(read_validation_rows(directory / "matrix_rows.jsonl", spec)) == 1
    assert after_calls[0]["stage"] == "post_solve"


def test_resource_hook_identity_and_authorization_must_match(tmp_path):
    spec = _spec(tmp_path)
    for result in (
        {"action": "start_point", "start_authorized": False},
        {"action": "unknown", "start_authorized": False},
        {"action": "start_point", "start_authorized": True, "point_id": "wrong"},
    ):
        try:
            run_pending_validation_points(
                spec,
                tmp_path / result["action"],
                attempt=1,
                collector_executor=_complete_executor,
                before_point_hook=lambda _context, value=result: value,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid resource hook output must fail closed")
