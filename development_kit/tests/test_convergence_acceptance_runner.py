"""Solver-free contract tests for the licensed convergence runner."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from development_kit.tests.integration.convergence_campaign_acceptance import run_acceptance
from development_kit.tests.test_convergence_campaign_job import _raw_campaign


@pytest.fixture
def ascii_root(ascii_tmp_path):
    return ascii_tmp_path / "convergence-acceptance"


def _raw(tmp_path):
    value = _raw_campaign(tmp_path / "sources")
    value["convergence_policy"]["declared_cap_reached"] = False
    return value


def test_dry_run_binds_every_source_without_starting_worker(tmp_path, ascii_root):
    output = tmp_path / "dry-run.json"
    receipt = run_acceptance(
        raw_spec=_raw(tmp_path),
        runtime_root=ascii_root,
        output=output,
        dry_run=True,
        worker_runner=lambda *_args, **_kwargs: pytest.fail("worker must not start"),
    )
    assert receipt["success"] is True
    assert receipt["comsol_client_started"] is False
    assert set(receipt["source_model_sha256"]) == {"mesh-0", "mesh-1", "mesh-2"}
    assert json.loads(output.read_text(encoding="utf-8")) == receipt


def test_receipt_is_never_overwritten(tmp_path, ascii_root):
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")
    with pytest.raises(FileExistsError, match="overwrite"):
        run_acceptance(
            raw_spec=_raw(tmp_path), runtime_root=ascii_root, output=output, dry_run=True
        )


def test_receipt_publish_refuses_target_created_after_precheck(tmp_path, monkeypatch):
    from development_kit.tests.integration import convergence_campaign_acceptance as runner

    output = tmp_path / "raced.json"
    real_link = runner.os.link

    def publish_competitor_then_link(source, destination):
        output.write_text("competitor", encoding="ascii")
        return real_link(source, destination)

    monkeypatch.setattr(runner.os, "link", publish_competitor_then_link)
    with pytest.raises(FileExistsError):
        runner._write_json(output, {"ours": True})

    assert output.read_text(encoding="ascii") == "competitor"


def test_concurrent_receipt_publishers_keep_the_successful_payload(tmp_path, monkeypatch):
    from development_kit.tests.integration import convergence_campaign_acceptance as runner

    output = tmp_path / "shared.json"
    first_staged, second_staged, first_published = Event(), Event(), Event()
    real_link = runner.os.link

    def ordered_link(source, destination):
        publisher = json.loads(Path(source).read_text())["publisher"]
        if publisher == "a":
            first_staged.set()
            assert second_staged.wait(5)
            try:
                return real_link(source, destination)
            finally:
                first_published.set()
        second_staged.set()
        assert first_published.wait(5)
        return real_link(source, destination)

    monkeypatch.setattr(runner.os, "link", ordered_link)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(runner._write_json, output, {"publisher": "a"})
        assert first_staged.wait(5)
        second = executor.submit(runner._write_json, output, {"publisher": "b"})
        assert first.result(timeout=6) is None
        with pytest.raises(FileExistsError):
            second.result(timeout=6)
    assert json.loads(output.read_text()) == {"publisher": "a"}
    assert not list(tmp_path.glob("*.tmp"))


def test_receipt_preserves_caller_fixed_temporary_file(tmp_path):
    from development_kit.tests.integration import convergence_campaign_acceptance as runner

    output = tmp_path / "owned.json"
    caller_file = output.with_suffix(".json.tmp")
    caller_file.write_bytes(b"caller-owned bytes")
    runner._write_json(output, {"success": True})
    assert caller_file.read_bytes() == b"caller-owned bytes"
    assert json.loads(output.read_text()) == {"success": True}


def test_receipt_fsync_failure_removes_only_its_owned_staging(tmp_path, monkeypatch):
    from development_kit.tests.integration import convergence_campaign_acceptance as runner

    output = tmp_path / "failed.json"
    caller_file = tmp_path / "caller.bin"
    caller_file.write_bytes(b"preserve")
    monkeypatch.setattr(runner.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError, match="fsync"):
        runner._write_json(output, {"success": True})
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert caller_file.read_bytes() == b"preserve"


def test_non_ascii_runtime_fails_before_worker(tmp_path):
    with pytest.raises(ValueError, match="ASCII"):
        run_acceptance(
            raw_spec=_raw(tmp_path),
            runtime_root=tmp_path / "运行时",
            output=tmp_path / "receipt.json",
            dry_run=True,
            worker_runner=lambda *_args, **_kwargs: pytest.fail("worker must not start"),
        )


def test_real_execution_requires_confirmation_at_importable_boundary(tmp_path, ascii_root):
    worker_called = False

    def worker(*_args, **_kwargs):
        nonlocal worker_called
        worker_called = True
        return 0

    with pytest.raises(ValueError, match="explicit RUN_REAL_COMSOL confirmation"):
        run_acceptance(
            raw_spec=_raw(tmp_path),
            runtime_root=ascii_root,
            output=tmp_path / "receipt.json",
            worker_runner=worker,
        )

    assert worker_called is False
    assert not (ascii_root / "jobs").exists()


def test_worker_failure_publishes_a_bounded_failure_receipt(tmp_path, ascii_root):
    output = tmp_path / "failure.json"

    receipt = run_acceptance(
        raw_spec=_raw(tmp_path),
        runtime_root=ascii_root,
        output=output,
        confirmation="RUN_REAL_COMSOL",
        worker_runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected worker failure")
        ),
    )

    assert receipt["success"] is False
    assert receipt["error"] == {
        "type": "RuntimeError",
        "message": "injected worker failure",
    }
    assert json.loads(output.read_text(encoding="utf-8")) == receipt


def test_completed_state_requires_one_row_per_declared_level(tmp_path, ascii_root, monkeypatch):
    from development_kit.tests.integration import convergence_campaign_acceptance as runner

    monkeypatch.setattr(
        runner.JobStore,
        "read_state",
        lambda _self, _job_id: {"status": "completed", "cleanup": {"verified": True}},
    )
    monkeypatch.setattr(runner, "read_convergence_campaign_levels", lambda *_args, **_kwargs: [])
    output = tmp_path / "empty-levels.json"

    receipt = run_acceptance(
        raw_spec=_raw(tmp_path),
        runtime_root=ascii_root,
        output=output,
        confirmation="RUN_REAL_COMSOL",
        worker_runner=lambda *_args, **_kwargs: 0,
    )

    assert receipt["success"] is False
    assert receipt["levels_complete"] is False
