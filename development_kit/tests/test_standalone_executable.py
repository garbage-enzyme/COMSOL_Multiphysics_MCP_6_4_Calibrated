"""Python-free Windows launcher build and solver-free inspection tests."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from pathlib import Path

import psutil
import pytest

import comsol_mcp.standalone.builder as builder_module
import comsol_mcp.standalone.control as control_module
import comsol_mcp.standalone.inspection as inspection_module
from comsol_mcp.durable.io import append_jsonl_record, atomic_write_json
from comsol_mcp.standalone.builder import (
    BUILD_SCHEMA,
    EXECUTABLE_NAME,
    MANIFEST_NAME,
    build_standalone_executable,
)
from comsol_mcp.standalone.control import (
    launch_standalone_campaign,
    read_standalone_results,
)
from comsol_mcp.standalone.inspection import (
    read_campaign_results,
    read_campaign_status,
    read_campaign_terminal,
    tail_campaign_log,
    verify_standalone_deployment,
)


def _workstation_build_available() -> bool:
    try:
        builder_module._validate_build_host(builder_module._default_csc_path())
    except builder_module.PlatformError, FileNotFoundError:
        return False
    return True


requires_windows_workstation_build = pytest.mark.skipif(
    not _workstation_build_available(),
    reason="requires a supported Windows 10/11 x64 workstation build host",
)


def _status(*, state: str = "running", completed: int = 1) -> dict[str, object]:
    return {
        "schema_name": "comsol_mcp.standalone_status",
        "schema_version": "1.0.0",
        "status": state,
        "completed": completed,
        "total": 3,
        "launcher_sha256": "5" * 64,
        "campaign_spec_sha256": "6" * 64,
        "comsol_version": "6.4.0.293",
        "comsol_compile_sha256": "7" * 64,
        "comsol_batch_sha256": "8" * 64,
    }


def _result(point_id: str, voltage: float) -> dict[str, object]:
    return {
        "schema_name": "comsol_mcp.standalone_driver_event",
        "schema_version": "1.0.0",
        "event": "point_result",
        "point_id": point_id,
        "voltage_v": voltage,
        "capacitance_pf": 1.8593794419540608,
        "energy_j": voltage * voltage * 9.296897209770304e-13,
        "solver_started": True,
        "status": "passed",
        "attempt_id": "a" * 32,
        "driver_java_sha256": "1" * 64,
        "driver_class_sha256": "2" * 64,
        "process_log_sha256": "3" * 64,
        "comsol_batch_log_sha256": "4" * 64,
        "launcher_sha256": "5" * 64,
        "campaign_spec_sha256": "6" * 64,
        "comsol_version": "6.4.0.293",
        "comsol_compile_sha256": "7" * 64,
        "comsol_batch_sha256": "8" * 64,
    }


def _campaign(root: Path) -> Path:
    (root / "assets" / "state").mkdir(parents=True)
    (root / "assets" / "data").mkdir(parents=True)
    (root / "assets" / "logs").mkdir(parents=True)
    return root


@requires_windows_workstation_build
def test_packaged_sources_build_one_x64_executable_without_comsol(ascii_tmp_path: Path) -> None:
    output = ascii_tmp_path / "standalone-build"

    receipt = build_standalone_executable(output)

    assert receipt["schema_name"] == BUILD_SCHEMA
    assert receipt["status"] == "passed"
    assert receipt["python_required_at_runtime"] is False
    assert receipt["external_java_required_at_runtime"] is False
    assert receipt["windows_inbox_dotnet_framework_required"] is True
    assert receipt["separate_dotnet_runtime_required"] is False
    assert receipt["separate_dotnet_sdk_required"] is False
    assert receipt["visual_studio_required"] is False
    assert receipt["network_download_required"] is False
    assert receipt["local_comsol_installation_required"] is True
    assert receipt["target_os"] == ["Windows 10 x64", "Windows 11 x64"]
    assert receipt["target_comsol"] == "6.4 release line"
    assert receipt["comsol_runtime_bundled"] is False
    assert receipt["runtime_architecture"] == [
        "licensed COMSOL 6.4 installation",
        "COMSOL-compiled Java point driver",
        "native Windows x64 launcher",
    ]
    assert receipt["compiler"]["source"] == "Windows inbox .NET Framework 4.x x64"
    assert receipt["compiler"]["locator"] == "%WINDIR%/Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    executable = output / EXECUTABLE_NAME
    assert executable.is_file()
    assert 1 <= executable.stat().st_size <= 8 * 1024 * 1024
    assert json.loads((output / MANIFEST_NAME).read_text(encoding="utf-8")) == receipt
    assert not any(path.suffix in {".py", ".pyc"} for path in output.rglob("*"))

    verified = verify_standalone_deployment(output)
    assert verified["status"] == "verified"
    assert verified["launcher_sha256"] == receipt["launcher"]["sha256"]


def test_builder_rejects_nonempty_output_before_compiler_launch(
    ascii_tmp_path: Path,
) -> None:
    output = ascii_tmp_path / "nonempty"
    output.mkdir()
    (output / "owned.txt").write_text("preserve", encoding="utf-8")

    with pytest.raises(FileExistsError, match="must be empty"):
        build_standalone_executable(output)

    assert (output / "owned.txt").read_text(encoding="utf-8") == "preserve"


def test_builder_rejects_windows_server_even_with_an_x64_compiler(
    ascii_tmp_path: Path, monkeypatch
) -> None:
    compiler = ascii_tmp_path / "csc.exe"
    compiler.write_bytes(b"fixture")
    workstation_version = type(
        "WindowsVersion",
        (),
        {"major": 10, "build": 20348, "product_type": 3},
    )()
    monkeypatch.setattr(
        builder_module.sys,
        "getwindowsversion",
        lambda: workstation_version,
    )

    with pytest.raises(builder_module.PlatformError, match="Windows 10 or 11 workstation"):
        builder_module._validate_build_host(compiler)


def test_builder_failure_preserves_bounded_logs_and_no_false_manifest(
    ascii_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compiler = ascii_tmp_path / "csc.exe"
    compiler.write_bytes(b"fake-compiler")
    output = ascii_tmp_path / "failed-build"
    monkeypatch.setattr(builder_module, "_validate_build_host", lambda _path: None)

    def fail(command, **kwargs):
        assert command[0] == str(compiler)
        return subprocess.CompletedProcess(command, 1, b"compile-out", b"compile-error")

    with pytest.raises(RuntimeError, match="compilation failed"):
        build_standalone_executable(output, csc_path=compiler, run_command=fail)

    assert (output / "build.stdout.log").read_bytes() == b"compile-out"
    assert (output / "build.stderr.log").read_bytes() == b"compile-error"
    assert not (output / MANIFEST_NAME).exists()

    def succeed(command, **kwargs):
        executable = Path(next(item[5:] for item in command if item.startswith("/out:")))
        executable.write_bytes(b"fixture-executable")
        return subprocess.CompletedProcess(command, 0, b"retry-out", b"")

    receipt = build_standalone_executable(output, csc_path=compiler, run_command=succeed)
    assert receipt["status"] == "passed"
    assert (output / "build.stdout.log").read_bytes() == b"retry-out"


def test_default_compiler_capture_is_file_backed_and_bounded(
    ascii_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(builder_module, "MAX_BUILD_LOG_BYTES", 16)
    completed, stdout, stderr, overflow = builder_module._run_compiler_bounded(
        [os.sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 64)"],
        cwd=ascii_tmp_path,
        run_command=subprocess.run,
    )
    assert completed.returncode == 0
    assert overflow is True
    assert len(stdout) <= 17
    assert stderr == b""


def test_status_and_results_are_bounded_schema_checked_snapshots(ascii_tmp_path: Path) -> None:
    campaign = _campaign(ascii_tmp_path / "campaign")
    atomic_write_json(campaign / "assets" / "state" / "status.json", _status())
    results = campaign / "assets" / "data" / "results.jsonl"
    append_jsonl_record(results, _result("voltage_1V", 1.0))
    append_jsonl_record(results, _result("voltage_2V", 2.0))

    status = read_campaign_status(campaign)
    outcome = read_campaign_results(campaign, limit=1)
    status["status"] = "tampered-copy"
    outcome["rows"][0]["point_id"] = "tampered-copy"

    assert read_campaign_status(campaign)["status"] == "running"
    assert read_campaign_results(campaign, limit=1)["rows"][0]["point_id"] == "voltage_2V"
    assert outcome["state"] == "current_valid"
    assert outcome["total_rows"] == 2


def test_control_output_is_file_backed_and_checked_before_decode(
    ascii_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        control_module,
        "verify_standalone_deployment",
        lambda _path: {"launcher_sha256": "a" * 64},
    )

    def noisy(command, **kwargs):
        kwargs["stdout"].write(b"x" * (inspection_module.MAX_STATUS_BYTES + 1))
        kwargs["stdout"].flush()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(control_module.subprocess, "run", noisy)

    with pytest.raises(RuntimeError, match="control output exceeded"):
        control_module._run_control(ascii_tmp_path, "status")


def test_partial_result_tail_is_reported_and_never_returned(ascii_tmp_path: Path) -> None:
    campaign = _campaign(ascii_tmp_path / "partial")
    results = campaign / "assets" / "data" / "results.jsonl"
    append_jsonl_record(results, _result("voltage_1V", 1.0))
    with results.open("ab") as handle:
        handle.write(b'{"schema_name":"partial"')

    outcome = read_campaign_results(campaign)

    assert outcome["state"] == "incomplete"
    assert outcome["total_rows"] == 1
    assert outcome["rows"][0]["point_id"] == "voltage_1V"
    assert outcome["trailing_byte_count"] > 0
    assert outcome["terminal"] is None


def test_invalid_status_schema_and_result_outcome_fail_closed(ascii_tmp_path: Path) -> None:
    campaign = _campaign(ascii_tmp_path / "invalid")
    atomic_write_json(
        campaign / "assets" / "state" / "status.json",
        {**_status(), "schema_version": "2.0.0"},
    )
    append_jsonl_record(
        campaign / "assets" / "data" / "results.jsonl",
        {**_result("voltage_1V", 1.0), "status": "failed"},
    )

    with pytest.raises(ValueError, match="status schema"):
        read_campaign_status(campaign)
    with pytest.raises(ValueError, match="not an accepted point"):
        read_campaign_results(campaign)


def test_result_identity_mixing_and_duplicate_points_fail_closed(
    ascii_tmp_path: Path,
) -> None:
    mixed = _campaign(ascii_tmp_path / "mixed")
    append_jsonl_record(
        mixed / "assets" / "data" / "results.jsonl",
        _result("voltage_1V", 1.0),
    )
    append_jsonl_record(
        mixed / "assets" / "data" / "results.jsonl",
        {**_result("voltage_2V", 2.0), "comsol_batch_sha256": "9" * 64},
    )
    with pytest.raises(ValueError, match="mixed execution identities"):
        read_campaign_results(mixed)

    duplicate = _campaign(ascii_tmp_path / "duplicate")
    append_jsonl_record(
        duplicate / "assets" / "data" / "results.jsonl",
        _result("voltage_1V", 1.0),
    )
    append_jsonl_record(
        duplicate / "assets" / "data" / "results.jsonl",
        _result("voltage_1V", 1.0),
    )
    with pytest.raises(ValueError, match="absent or duplicated"):
        read_campaign_results(duplicate)


def test_tail_accepts_only_allowlisted_bounded_utf8_logs(ascii_tmp_path: Path) -> None:
    campaign = _campaign(ascii_tmp_path / "logs")
    log = campaign / "assets" / "logs" / "launcher.log"
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")

    outcome = tail_campaign_log(campaign, lines=2)

    assert outcome["lines"] == ["two", "three"]
    with pytest.raises(ValueError, match="allowlisted"):
        tail_campaign_log(campaign, log_name="arbitrary.log")
    with pytest.raises(ValueError, match="from 1 through"):
        tail_campaign_log(campaign, lines=501)


def test_terminal_receipt_is_separate_and_schema_checked(ascii_tmp_path: Path) -> None:
    campaign = _campaign(ascii_tmp_path / "terminal")
    terminal = {
        **_status(state="completed", completed=3),
        "schema_name": "comsol_mcp.standalone_terminal",
        "status_schema_name": "comsol_mcp.standalone_status",
        "status_schema_version": "1.0.0",
    }
    atomic_write_json(campaign / "assets" / "state" / "terminal.json", terminal)

    assert read_campaign_terminal(campaign) == terminal

    terminal["status_schema_name"] = "unknown"
    atomic_write_json(campaign / "assets" / "state" / "terminal.json", terminal)
    with pytest.raises(ValueError, match="bind the status schema"):
        read_campaign_terminal(campaign)

    terminal["status_schema_name"] = "comsol_mcp.standalone_status"
    terminal["status_schema_version"] = "2.0.0"
    atomic_write_json(campaign / "assets" / "state" / "terminal.json", terminal)
    with pytest.raises(ValueError, match="status schema version"):
        read_campaign_terminal(campaign)


def test_completed_terminal_must_bind_the_exact_result_journal(
    ascii_tmp_path: Path,
) -> None:
    campaign = _campaign(ascii_tmp_path / "terminal-result-binding")
    append_jsonl_record(
        campaign / "assets" / "data" / "results.jsonl",
        _result("voltage_1V", 1.0),
    )
    terminal = {
        **_status(state="completed", completed=1),
        "schema_name": "comsol_mcp.standalone_terminal",
        "status_schema_name": "comsol_mcp.standalone_status",
        "status_schema_version": "1.0.0",
        "results_sha256": "0" * 64,
    }
    atomic_write_json(campaign / "assets" / "state" / "terminal.json", terminal)

    with pytest.raises(ValueError, match="does not bind the result journal"):
        read_campaign_results(campaign)


def test_terminal_identity_must_match_result_rows(ascii_tmp_path: Path) -> None:
    campaign = _campaign(ascii_tmp_path / "terminal-identity-binding")
    results = campaign / "assets" / "data" / "results.jsonl"
    append_jsonl_record(results, _result("voltage_1V", 1.0))
    terminal = {
        **_status(state="completed", completed=1),
        "schema_name": "comsol_mcp.standalone_terminal",
        "status_schema_name": "comsol_mcp.standalone_status",
        "status_schema_version": "1.0.0",
        "results_sha256": inspection_module._sha256(results.read_bytes()),
        "campaign_spec_sha256": "9" * 64,
    }
    atomic_write_json(campaign / "assets" / "state" / "terminal.json", terminal)

    with pytest.raises(ValueError, match="terminal and result identities disagree"):
        read_campaign_results(campaign)


def test_result_rows_and_hash_share_one_file_snapshot(ascii_tmp_path, monkeypatch) -> None:
    campaign = _campaign(ascii_tmp_path / "single-snapshot")
    results_path = campaign / "assets" / "data" / "results.jsonl"
    append_jsonl_record(results_path, _result("voltage_1V", 1.0))
    original_read = inspection_module.read_file_bytes_bounded
    reads = 0

    def counted_read(path, *, max_bytes):
        nonlocal reads
        if Path(path).samefile(results_path):
            reads += 1
        return original_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(inspection_module, "read_file_bytes_bounded", counted_read)
    result = read_campaign_results(campaign)

    assert reads == 1
    assert result["results_sha256"] == inspection_module._sha256(results_path.read_bytes())
    assert result["rows"][0]["point_id"] == "voltage_1V"


def test_embedded_java_driver_has_no_unusable_model_return() -> None:
    source = builder_module._resource_bytes("CapacitorPointTemplate.java").decode("utf-8")

    assert "public static void run()" in source
    assert "return null;" not in source


def test_embedded_launcher_preserves_bounded_durable_process_and_resume_contracts() -> None:
    source = builder_module._resource_bytes("Launcher.cs").decode("utf-8")

    assert "streamsDrained = Task.WaitAll(new Task[] {stdout, stderr}, 5000);" in source
    assert 'throw new TimeoutException("owned_process_timeout", drainFailure);' in source
    assert '"owned_process_streams_not_drained", drainFailure' in source
    assert "StandardOutputEncoding = new UTF8Encoding(false, true)" in source
    assert "StandardErrorEncoding = new UTF8Encoding(false, true)" in source
    assert "FileShare.ReadWrite | FileShare.Delete" in source
    assert "ValidatePointPhysics(value, Points[expectedIndex]);" in source
    assert 'throw new InvalidDataException("campaign_reference_physics_is_not_positive")' in source
    assert "TryWriteFailureStatus(exception.Message, committedCount);" in source
    assert '{"completed", completed}' in source


@requires_windows_workstation_build
def test_deployment_verification_rejects_executable_or_source_identity_tampering(
    ascii_tmp_path: Path,
) -> None:
    executable_tamper = ascii_tmp_path / "tampered-executable"
    build_standalone_executable(executable_tamper)
    with (executable_tamper / EXECUTABLE_NAME).open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(ValueError, match="does not match its build manifest"):
        verify_standalone_deployment(executable_tamper)

    source_tamper = ascii_tmp_path / "tampered-source"
    build_standalone_executable(source_tamper)
    manifest_path = source_tamper / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sources"]["Launcher.cs"]["sha256"] = "0" * 64
    atomic_write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="source identity"):
        verify_standalone_deployment(source_tamper)

    runtime_tamper = ascii_tmp_path / "tampered-runtime-boundary"
    build_standalone_executable(runtime_tamper)
    manifest_path = runtime_tamper / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["separate_dotnet_runtime_required"] = True
    atomic_write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match=".NET runtime boundary"):
        verify_standalone_deployment(runtime_tamper)

    result_mismatch = ascii_tmp_path / "result-mismatch"
    build_standalone_executable(result_mismatch)
    (result_mismatch / "assets" / "data").mkdir(parents=True)
    append_jsonl_record(
        result_mismatch / "assets" / "data" / "results.jsonl",
        _result("voltage_1V", 1.0),
    )
    with pytest.raises(RuntimeError, match="reviewed deployment"):
        read_standalone_results(result_mismatch, limit=1)


@requires_windows_workstation_build
def test_mcp_launch_uses_fixed_arguments_and_writes_path_free_record(
    ascii_tmp_path: Path,
) -> None:
    deployment = ascii_tmp_path / "deployment"
    build_standalone_executable(deployment)
    comsol_root = ascii_tmp_path / "COMSOL64" / "Multiphysics"
    for relative in (
        Path("bin/win64/comsolcompile.exe"),
        Path("bin/win64/comsolbatch.exe"),
        Path("java/win64/jre/bin/java.exe"),
    ):
        target = comsol_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = os.getpid()

        @staticmethod
        def poll() -> int:
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    result = launch_standalone_campaign(
        deployment,
        comsol_root,
        popen_factory=fake_popen,
        identity_provider=lambda pid: {
            "pid": pid,
            "process_create_time": 1.0,
            "command_signature": "a" * 64,
        },
    )

    assert result["success"] is True
    assert captured["command"] == [
        str(deployment / EXECUTABLE_NAME),
        "run",
        "--comsol-path",
        str(comsol_root.resolve()),
    ]
    records = list((deployment / "assets" / "mcp-launches").glob("*.json"))
    assert len(records) == 1
    serialized = records[0].read_text(encoding="utf-8")
    assert str(comsol_root) not in serialized
    assert json.loads(serialized)["comsol_root_included"] is False


@requires_windows_workstation_build
def test_failed_identity_capture_terminates_process_and_removes_launch_residue(
    ascii_tmp_path: Path,
) -> None:
    deployment = ascii_tmp_path / "failed-launch"
    build_standalone_executable(deployment)
    comsol_root = ascii_tmp_path / "COMSOL64" / "Multiphysics"
    for relative in (
        Path("bin/win64/comsolcompile.exe"),
        Path("bin/win64/comsolbatch.exe"),
        Path("java/win64/jre/bin/java.exe"),
    ):
        target = comsol_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture")

    class FakeProcess:
        pid = 12345
        running = True
        terminated = False

        def poll(self):
            return None if self.running else 1

        def terminate(self):
            self.terminated = True
            self.running = False

        def wait(self, *, timeout):
            return 1

    process = FakeProcess()
    with pytest.raises(psutil.AccessDenied):
        launch_standalone_campaign(
            deployment,
            comsol_root,
            popen_factory=lambda *_args, **_kwargs: process,
            identity_provider=lambda _pid: (_ for _ in ()).throw(psutil.AccessDenied(12345)),
        )

    assert process.terminated is True
    assert not list((deployment / "assets" / "mcp-launches").iterdir())


def test_launch_record_limit_is_serialized_across_concurrent_callers(
    ascii_tmp_path: Path,
    monkeypatch,
) -> None:
    deployment = ascii_tmp_path / "launch-limit"
    launch_root = deployment / "assets" / "mcp-launches"
    launch_root.mkdir(parents=True)
    for index in range(control_module.MAX_MCP_LAUNCH_RECORDS - 1):
        (launch_root / f"{index:032x}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        control_module,
        "verify_standalone_deployment",
        lambda _path: {"launcher_sha256": "a" * 64},
    )
    monkeypatch.setattr(control_module, "_validate_comsol_root", lambda path: Path(path))
    monkeypatch.setattr(
        control_module, "process_identity_state", lambda _identity: ("active", "fixture")
    )
    pids = count(20000)

    class FakeProcess:
        def __init__(self):
            self.pid = next(pids)

        @staticmethod
        def poll():
            return 0

    def launch():
        try:
            return launch_standalone_campaign(
                deployment,
                ascii_tmp_path / "comsol",
                popen_factory=lambda *_args, **_kwargs: FakeProcess(),
                identity_provider=lambda pid: {
                    "pid": pid,
                    "process_create_time": 1.0,
                    "command_signature": "b" * 64,
                },
            )
        except RuntimeError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: launch(), range(2)))

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert outcomes.count("standalone MCP launch record limit reached") == 1
    launch_ids = {
        path.name.split(".", 1)[0]
        for path in launch_root.iterdir()
        if len(path.name.split(".", 1)[0]) == 32
    }
    assert len(launch_ids) == control_module.MAX_MCP_LAUNCH_RECORDS


def test_stale_launch_records_are_retired_with_their_logs(
    ascii_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch_root = ascii_tmp_path / "launch-records"
    launch_root.mkdir()
    launch_id = "a" * 32
    atomic_write_json(
        launch_root / f"{launch_id}.json",
        {"owner": {"pid": 123, "process_create_time": 1.0, "command_signature": "b" * 64}},
    )
    (launch_root / f"{launch_id}.stdout.log").write_bytes(b"done")
    monkeypatch.setattr(
        control_module, "process_identity_state", lambda _identity: ("stale", "exited")
    )

    control_module._retire_stale_launch_records(launch_root)

    assert not list(launch_root.iterdir())
