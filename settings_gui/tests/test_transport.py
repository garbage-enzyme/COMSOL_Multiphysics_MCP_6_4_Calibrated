"""MCP Connection tab logic (settings_gui.transport), without Tk."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comsol_mcp.host_admission import EXPECTED_SHIM_VERSION
from comsol_mcp.settings import default_settings_document, normalize_settings_document
from settings_gui import transport

ROOT = Path(__file__).resolve().parents[2]
BUILT_SHIM = (
    ROOT
    / "shim"
    / "target"
    / "release"
    / ("comsol-mcp-shim.exe" if sys.platform == "win32" else "comsol-mcp-shim")
)
PLATFORM = "x86_64-pc-windows-msvc"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runner(stdout: str, returncode: int = 0):
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "comsol-mcp-shim.exe"
    binary.write_bytes(b"MZ fake shim payload")
    return binary


def test_missing_path_and_relative_path_are_refused():
    assert transport.check_shim_binary("", "").reason_code == "path_missing"
    assert transport.check_shim_binary("relative/shim.exe", "a" * 64).reason_code == "path_relative"


def test_absent_file_is_refused_without_running_anything(tmp_path: Path):
    runner = _runner("unused")
    result = transport.check_shim_binary(str(tmp_path / "absent.exe"), "0" * 64, runner=runner)
    assert (result.ok, result.reason_code) == (False, "file_not_found")
    assert runner.calls == []


def test_missing_or_wrong_hash_is_refused_before_execution(fake_binary: Path):
    runner = _runner(f"comsol-mcp-shim {EXPECTED_SHIM_VERSION} ({PLATFORM})")
    missing = transport.check_shim_binary(str(fake_binary), "", runner=runner)
    assert (missing.ok, missing.reason_code) == (False, "sha256_missing")
    assert missing.sha256 == _digest(fake_binary)
    wrong = transport.check_shim_binary(str(fake_binary), "0" * 64, runner=runner)
    assert (wrong.ok, wrong.reason_code) == (False, "sha256_mismatch")
    assert runner.calls == [], "an unverified file is never executed"


@pytest.mark.parametrize(
    ("stdout", "returncode", "reason"),
    [
        ("", 1, "version_unrecognized"),
        ("some other program 1.0", 0, "version_unrecognized"),
        ("comsol-mcp-shim 0.8.2-dev", 0, "version_unrecognized"),
        (f"comsol-mcp-shim 9.9.9 ({PLATFORM})", 0, "version_mismatch"),
        (f"comsol-mcp-shim {EXPECTED_SHIM_VERSION} (aarch64-apple-darwin)", 0, "platform_mismatch"),
    ],
)
def test_self_reported_identity_must_match_exactly(fake_binary, stdout, returncode, reason):
    result = transport.check_shim_binary(
        str(fake_binary),
        _digest(fake_binary),
        runner=_runner(stdout, returncode),
        platforms=(PLATFORM,),
    )
    assert (result.ok, result.reason_code) == (False, reason)


def test_unrunnable_file_is_not_verified(tmp_path: Path):
    text_file = tmp_path / "not-a-shim.txt"
    text_file.write_text("This is not an executable.", encoding="utf-8")

    def run(_argv, **_kwargs):
        raise OSError("not a valid application")

    result = transport.check_shim_binary(str(text_file), _digest(text_file), runner=run)
    assert (result.ok, result.reason_code) == (False, "not_runnable")


def test_matching_identity_is_verified(fake_binary: Path):
    result = transport.check_shim_binary(
        str(fake_binary),
        _digest(fake_binary).upper(),
        runner=_runner(f"comsol-mcp-shim {EXPECTED_SHIM_VERSION} ({PLATFORM})\n"),
        platforms=(PLATFORM,),
    )
    assert result.ok is True
    assert (result.version, result.platform, result.sha256) == (
        EXPECTED_SHIM_VERSION,
        PLATFORM,
        _digest(fake_binary),
    )


@pytest.mark.skipif(not BUILT_SHIM.is_file(), reason="the shim binary is not built")
def test_the_built_shim_verifies_and_a_text_file_with_its_own_hash_does_not(tmp_path: Path):
    verified = transport.check_shim_binary(str(BUILT_SHIM), _digest(BUILT_SHIM))
    assert verified.ok is True, verified
    text_file = tmp_path / "not-a-shim.txt"
    text_file.write_text("This is not an executable.", encoding="utf-8")
    refused = transport.check_shim_binary(str(text_file), _digest(text_file), runner=subprocess.run)
    assert refused.ok is False
    assert refused.reason_code in {"not_runnable", "version_unrecognized"}


def _process(pid: int, name: str, cmdline):
    return SimpleNamespace(info={"pid": pid, "name": name, "cmdline": cmdline})


def _observe(rows, runtime_dir: Path):
    return transport.observe_hosts(
        runtime_dir,
        process_iter=lambda _attrs: iter(rows),
        own_pid=1,
    )


def test_unreadable_python_command_line_is_unknown_not_absent(tmp_path: Path):
    observation = _observe([_process(4242, "python.exe", None)], tmp_path)
    assert observation.state == "unknown"
    assert "4242" in observation.unknowns[0]


@pytest.mark.parametrize(
    "row",
    [
        _process(10, "python.exe", ["python.exe", "-m", "comsol_mcp.server"]),
        _process(11, "pythonw.exe", ["pythonw.exe", "D:/env/Scripts/comsol-mcp.exe"]),
        _process(12, "comsol-mcp.exe", None),
        _process(13, "comsol-mcp-shim.exe", None),
    ],
)
def test_every_host_entry_point_is_detected(tmp_path: Path, row):
    observation = _observe([row], tmp_path)
    assert observation.state == "running"


@pytest.mark.parametrize(
    "row",
    [
        _process(20, "python.exe", ["python.exe", "-m", "settings_gui"]),
        _process(21, "comsol-mcp-settings.exe", None),
        _process(22, "notepad.exe", None),
        _process(1, "python.exe", None),
    ],
)
def test_non_hosts_and_this_process_are_ignored(tmp_path: Path, row):
    assert _observe([row], tmp_path).state == "none"


def test_an_incomplete_inventory_is_unknown(tmp_path: Path):
    def broken(_attrs):
        raise RuntimeError("inventory interrupted")

    observation = transport.observe_hosts(tmp_path, process_iter=broken, own_pid=1)
    assert observation.state == "unknown"


def test_an_unresolved_domain_is_unknown():
    observation = transport.observe_hosts(None, process_iter=lambda _attrs: iter(()), own_pid=1)
    assert observation.state == "unknown"


def test_a_corrupt_admission_record_is_unknown(tmp_path: Path):
    (tmp_path / "host_admission.lock").write_text("{", encoding="utf-8")
    observation = _observe([], tmp_path)
    assert observation.state == "unknown"


def _verified(path: str) -> transport.BinaryCheck:
    return transport.BinaryCheck(
        True,
        "verified",
        path=path,
        sha256="a" * 64,
        version=EXPECTED_SHIM_VERSION,
        platform=PLATFORM,
    )


def test_plan_refuses_shim_without_a_verified_binary():
    document = default_settings_document()
    with pytest.raises(transport.SwitchRefused) as error:
        transport.plan_switch(document, "shim", None)
    assert error.value.reason_code == "binary_unverified"
    unverified = transport.BinaryCheck(False, "sha256_mismatch")
    with pytest.raises(transport.SwitchRefused):
        transport.plan_switch(document, "shim", unverified)


def test_plan_records_identity_and_pins_a_literal_runtime_directory():
    document = default_settings_document()
    document["runtime"]["directory"] = "%PROGRAMDATA%/comsol_mcp/runtime"
    planned = transport.plan_switch(document, "shim", _verified(sys.executable))
    assert planned["transport"] == "shim"
    assert planned["shim_binary"]["version"] == EXPECTED_SHIM_VERSION
    runtime = planned["runtime"]["directory"]
    assert "%" not in runtime and Path(runtime).is_absolute()
    assert normalize_settings_document(planned)["errors"] == []
    assert document["transport"] == "python", "the input document is never mutated"


def test_plan_back_to_python_keeps_the_rest_of_the_document():
    document = default_settings_document()
    planned = transport.plan_switch(document, "python", None)
    assert planned["transport"] == "python"
    assert planned["paths"] == document["paths"]


def test_modes_are_described_separately_and_never_claimed_from_configuration():
    idle = transport.describe_modes("shim", transport.HostObservation())
    assert idle["configured"] == "shim"
    assert idle["state"] == "none"
    assert idle["active_transport"] is None
    assert idle["pending_restart"] is False
    running = transport.HostObservation(
        hosts=["python host PID 7 (verified admission record)"],
        admission={"state": "active", "transport_mode": "python"},
    )
    pending = transport.describe_modes("shim", running)
    assert pending["active_transport"] == "python"
    assert pending["pending_restart"] is True
