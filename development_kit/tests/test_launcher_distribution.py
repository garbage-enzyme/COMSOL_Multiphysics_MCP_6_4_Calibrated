"""Repository-distributed durable launcher acceptance."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
LAUNCHER = REPOSITORY / "launcher"
TESTS = LAUNCHER / "tests"


def _durable_control_module():
    path = LAUNCHER / "python" / "durable_control.py"
    spec = importlib.util.spec_from_file_location("launcher_test_durable_control", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_durable_control_skips_stale_foreign_and_malformed_requests(tmp_path: Path) -> None:
    control = _durable_control_module()
    requests = tmp_path / "control" / "requests"
    requests.mkdir(parents=True)
    (requests / "00-invalid-utf8.json").write_bytes(b"\xff")
    (requests / "01-array.json").write_text("[]", encoding="utf-8")
    (requests / "02-foreign.json").write_text(
        json.dumps(
            {
                "schema_name": control.REQUEST_SCHEMA,
                "action": "pause_after_current_point",
                "request_id": "foreign",
                "job_id": "other-job",
                "expected_spec_id": [],
            }
        ),
        encoding="utf-8",
    )
    valid = {
        "schema_name": control.REQUEST_SCHEMA,
        "action": "pause_after_current_point",
        "request_id": "valid-request",
        "job_id": "job-1",
        "expected_spec_id": "spec-1",
    }
    control.atomic_json(requests / "99-valid.json", valid)

    pending = control.pending_pause_request(
        tmp_path / "control", job_id="job-1", spec_id="spec-1"
    )

    assert pending is not None
    assert pending["request_id"] == "valid-request"
    assert pending["request_path"].endswith("99-valid.json")


def test_durable_control_atomic_json_cleans_failed_temporaries(tmp_path: Path, monkeypatch) -> None:
    control = _durable_control_module()
    target = tmp_path / "state.json"
    target.write_text('{"status":"prior"}\n', encoding="utf-8")

    with pytest.raises(TypeError):
        control.atomic_json(target, {"invalid": object()})
    assert not list(tmp_path.glob("state.json.tmp.*"))
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "prior"}

    def fail_replace(*_args):
        raise PermissionError

    monkeypatch.setattr(control.os, "replace", fail_replace)
    with pytest.raises(PermissionError):
        control.atomic_json(target, {"status": "new"})
    assert not list(tmp_path.glob("state.json.tmp.*"))
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "prior"}


def test_fake_driver_recovers_stale_lock_and_completes_zero_point_fixture(tmp_path: Path) -> None:
    root = tmp_path / "zero-point"
    root.mkdir()
    (root / "run.lock").write_text(
        json.dumps({"pid": 2_147_483_647, "spec_id": "stale"}), encoding="utf-8"
    )
    environment = {
        **os.environ,
        "DURABLE_TEST_ROOT": str(root),
        "DURABLE_TEST_HELPER_DIR": str(LAUNCHER / "python"),
        "DURABLE_TEST_JOB_ID": "zero-point-job",
        "DURABLE_TEST_SPEC_ID": "zero-point-spec",
        "DURABLE_TEST_POINTS": "0",
        "DURABLE_TEST_POINT_SECONDS": "0",
    }

    completed = subprocess.run(
        [sys.executable, str(TESTS / "fake_durable_driver.py")],
        cwd=TESTS,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "complete"
    assert status["completed"] == status["planned"] == 0
    assert status["latest_point_id"] is None
    assert not (root / "run.lock").exists()


def test_launcher_distribution_is_portable_and_outside_runtime_package() -> None:
    expected = {
        "README.md",
        "README_CN.md",
        "powershell/DurableLauncher.psm1",
        "python/durable_control.py",
        "templates/Run_DurableJob.template.ps1",
    }
    relative_files = {
        path.relative_to(LAUNCHER).as_posix()
        for path in LAUNCHER.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert expected <= relative_files
    git = shutil.which("git")
    assert git is not None
    tracked = subprocess.run(
        [git, "ls-files", "--", "launcher"],
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=True,
    )
    tracked_files = set(tracked.stdout.splitlines())
    assert not any("__pycache__" in path or path.endswith(".pyc") for path in tracked_files)
    assert not (REPOSITORY / "comsol_mcp" / "launcher").exists()

    text = "\n".join(
        path.read_text(encoding="utf-8", errors="strict")
        for path in LAUNCHER.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() in {".md", ".ps1", ".psm1", ".py"}
    )
    for forbidden in (
        "C:\\Users\\",
        "D:\\condaenvs\\",
        "tools\\durable_launcher_v1_7",
        "Sun2025",
        "Guo2026",
    ):
        assert forbidden not in text
    assert "$script:DurableLauncherVersion = '1.8.1'" in text
    assert "if ($Name -ieq 'comsol-mcp.exe') { return $false }" in text
    assert "-Run, -Monitor, and -ValidateOnly are mutually exclusive" in text
    assert "MinimumFreeSystemDriveGiB" in text
    assert "MinimumFreeOutputDriveGiB" in text
    assert "MinimumFreeCGiB" not in text
    assert "MinimumFreeDGiB" not in text
    assert "if ($null -eq $Raw) { return }" in text


def _run_powershell(script: str, host: str, arguments: list[str], timeout: int = 90) -> str:
    command = [
        host,
        "-NoLogo",
        "-NoProfile",
    ]
    if Path(host).name.casefold() == "powershell.exe":
        command.extend(["-ExecutionPolicy", "Bypass"])
    command.extend(["-File", str(TESTS / script), *arguments])
    completed = subprocess.run(
        command,
        cwd=REPOSITORY,
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    assert completed.returncode == 0, output[-20_000:]
    return output


@pytest.mark.skipif(sys.platform != "win32", reason="launcher requires Windows")
def test_launcher_acceptance_in_windows_powershell_and_pwsh(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe")
    assert powershell is not None
    hosts = [powershell]
    pwsh = shutil.which("pwsh.exe")
    if pwsh is not None:
        hosts.append(pwsh)
    elif os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail("Windows CI must provide pwsh for the shared launcher acceptance")

    for index, host in enumerate(hosts):
        host_root = tmp_path / f"host-{index}"
        main = _run_powershell(
            "Test_DurableLauncher.ps1",
            host,
            ["-TestRoot", str(host_root / "main"), "-PythonPath", sys.executable],
        )
        assert "DURABLE_LAUNCHER_TEST_PASS" in main
        presentation = _run_powershell(
            "Test_TerminalPresentation.ps1",
            host,
            ["-TestRoot", str(host_root / "presentation")],
        )
        assert "TERMINAL_PRESENTATION_TEST_PASS" in presentation
        banner = _run_powershell(
            "Test_TerminalBanner.ps1",
            host,
            ["-PowerShellPath", host, "-TestRoot", str(host_root / "banner")],
        )
        assert "TERMINAL_BANNER_TEST_PASS" in banner
        hold = _run_powershell(
            "Test_TerminalHold.ps1",
            host,
            ["-PowerShellPath", host, "-TestRoot", str(host_root / "hold")],
        )
        assert "DURABLE_TERMINAL_HOLD_TEST_PASS" in hold
        preflight = _run_powershell(
            "Test_PreflightFailure.ps1",
            host,
            [
                "-PowerShellPath",
                host,
                "-TestRoot",
                str(host_root / "preflight"),
                "-PythonPath",
                sys.executable,
            ],
        )
        assert "DURABLE_PREFLIGHT_FAILURE_TEST_PASS" in preflight
        refresh = _run_powershell(
            "Test_MonitorRefreshFailure.ps1",
            host,
            ["-PowerShellPath", host, "-TestRoot", str(host_root / "refresh")],
        )
        assert "DURABLE_MONITOR_REFRESH_FAILURE_TEST_PASS" in refresh
