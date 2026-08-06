"""Solver-free tests for licensed acceptance cleanup accounting."""

from __future__ import annotations

from pathlib import Path

from development_kit.scripts.acceptance_cleanup import CleanupRecorder, lease_released
from development_kit.tests.integration import incidence_configuration_acceptance as incidence_gate

ROOT = Path(__file__).parents[2]


def test_cleanup_steps_continue_after_failure_and_change_success():
    result = {"success": True}
    calls = []
    cleanup = CleanupRecorder(result)

    def fail():
        calls.append("failed")
        raise OSError("controlled cleanup failure")

    cleanup.run("model_remove", fail)
    cleanup.run("client_clear", lambda: calls.append("cleared"))
    cleanup.run(
        "lease_release",
        lambda: {"success": True, "released": True},
        passed=lease_released,
    )

    assert cleanup.finalize() == 1
    assert result["success"] is False
    assert result["cleanup"]["passed"] is False
    assert result["cleanup"]["steps"]["model_remove"]["error_type"] == "OSError"
    assert calls == ["failed", "cleared"]


def test_success_requires_actual_lease_release():
    for release in (
        {"success": False, "released": False},
        {"success": True, "released": False},
    ):
        result = {"success": True}
        cleanup = CleanupRecorder(result)
        cleanup.run("lease_release", lambda: release, passed=lease_released)
        assert cleanup.finalize() == 1
        assert result["success"] is False


def test_complete_cleanup_preserves_success_and_zero_exit():
    result = {"success": True}
    cleanup = CleanupRecorder(result)
    cleanup.run("client_clear", lambda: None)
    cleanup.run(
        "lease_release",
        lambda: {"success": True, "released": True},
        passed=lease_released,
    )

    assert cleanup.finalize() == 0
    assert result["success"] is True
    assert result["cleanup"]["passed"] is True


def test_default_cleanup_predicate_rejects_explicit_failure_sentinels():
    for sentinel in (False, {"success": False}):
        result = {"success": True}
        cleanup = CleanupRecorder(result)
        cleanup.run("reported_failure", lambda: sentinel)

        assert cleanup.finalize() == 1
        assert result["cleanup"]["steps"]["reported_failure"]["passed"] is False


def test_zero_resource_cleanup_does_not_invent_a_failure():
    result = {"success": True}
    cleanup = CleanupRecorder(result)

    assert cleanup.finalize() == 0
    assert result["cleanup"] == {"passed": True, "steps": {}}


def test_target_acceptance_scripts_finalize_after_cleanup():
    for relative in (
        "development_kit/tests/integration/derived_geometry_acceptance.py",
        "development_kit/tests/integration/incidence_configuration_acceptance.py",
        "development_kit/tests/integration/wave_optics_preflight_acceptance.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "cleanup = CleanupRecorder(result)" in source
        assert "exit_code = cleanup.finalize()" in source
        assert "exit_code = 0" not in source


def test_incidence_receipts_are_unique_and_use_exclusive_publication(tmp_path, monkeypatch):
    first = incidence_gate._new_result_path(tmp_path)
    second = incidence_gate._new_result_path(tmp_path)
    calls = []
    monkeypatch.setattr(
        incidence_gate,
        "atomic_write_json_exclusive",
        lambda path, value: calls.append((path, value)),
    )

    incidence_gate._publish_result(first, {"success": True})

    assert first != second
    assert first.parent == second.parent == tmp_path
    assert calls == [(first, {"success": True})]
