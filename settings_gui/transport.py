"""MCP Connection tab logic: binary checks, host observation, and the switch.

Every function returns structured results with a stable ``reason_code``;
the Tk layer only renders them. Nothing here claims that a configured mode
is active: the active host is reported only from verified runtime evidence
(the host admission record and a process inventory), and every unreadable
observation is reported as UNKNOWN instead of "no host".
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from comsol_mcp import host_admission
from comsol_mcp.settings import (
    SETTINGS_SCHEMA,
    SETTINGS_VERSION,
    SettingsError,
    is_concrete_runtime_path,
    normalize_settings_document,
)

VERSION_LINE = re.compile(r"comsol-mcp-shim (\S+) \((\S+)\)")
_HEX64 = re.compile(r"[0-9a-f]{64}")
VERSION_TIMEOUT_SECONDS = 10
# Console scripts of this package that are never MCP hosts.
_NON_HOST_SCRIPTS = ("comsol-mcp-settings", "comsol-mcp-settings-gui")


@dataclass(frozen=True)
class BinaryCheck:
    """Outcome of verifying one standalone shim program."""

    ok: bool
    reason_code: str
    path: str = ""
    sha256: str | None = None
    version: str | None = None
    platform: str | None = None
    detail: str = ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_shim_binary(
    path_text: str,
    expected_sha256: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    expected_version: str = host_admission.EXPECTED_SHIM_VERSION,
    platforms: tuple[str, ...] | None = None,
) -> BinaryCheck:
    """Verify path, published SHA-256, then the program's own identity.

    The file is hashed before it is executed, and only a file whose digest
    equals the published hash is run (``--version``). A program that does
    not report the exact ``comsol-mcp-shim <version> (<triple>)`` line, or
    whose version or platform differs from this package's expectation, is
    refused.
    """
    supported = expected_shim_platforms() if platforms is None else platforms
    raw = path_text.strip()
    expected = expected_sha256.strip().lower()
    if not raw:
        return BinaryCheck(False, "path_missing")
    path = Path(raw)
    if not path.is_absolute():
        return BinaryCheck(False, "path_relative", path=raw)
    if not path.is_file():
        return BinaryCheck(False, "file_not_found", path=raw)
    try:
        actual = _sha256_file(path)
    except OSError as exc:
        return BinaryCheck(False, "file_unreadable", path=raw, detail=str(exc))
    if _HEX64.fullmatch(expected) is None:
        return BinaryCheck(False, "sha256_missing", path=raw, sha256=actual)
    if actual != expected:
        return BinaryCheck(False, "sha256_mismatch", path=raw, sha256=actual)
    try:
        result = runner(  # noqa: S603 - the verified binary with a fixed argument
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return BinaryCheck(False, "not_runnable", path=raw, sha256=actual, detail=str(exc))
    output = (result.stdout or "").strip()
    match = VERSION_LINE.fullmatch(output) if result.returncode == 0 else None
    if match is None:
        return BinaryCheck(
            False,
            "version_unrecognized",
            path=raw,
            sha256=actual,
            detail=output[:200] or f"exit code {result.returncode}",
        )
    version, platform = match.groups()
    if version != expected_version:
        return BinaryCheck(
            False,
            "version_mismatch",
            path=raw,
            sha256=actual,
            version=version,
            platform=platform,
            detail=expected_version,
        )
    if platform not in supported:
        return BinaryCheck(
            False,
            "platform_mismatch",
            path=raw,
            sha256=actual,
            version=version,
            platform=platform,
            detail=", ".join(supported) or "none",
        )
    return BinaryCheck(
        True,
        "verified",
        path=str(path),
        sha256=actual,
        version=version,
        platform=platform,
    )


def expected_shim_platforms() -> tuple[str, ...]:
    return host_admission.expected_shim_platforms()


@dataclass
class HostObservation:
    """Verified hosts, UNKNOWN observations, and the admission record."""

    hosts: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    admission: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        if self.hosts:
            return "running"
        if self.unknowns:
            return "unknown"
        return "none"

    @property
    def active_transport(self) -> str | None:
        if self.admission.get("state") == "active":
            return self.admission.get("transport_mode")
        return None


def configured_runtime_dir(document: Mapping[str, Any]) -> Path | None:
    """The ownership domain a settings document configures, or None."""
    try:
        report = normalize_settings_document(deepcopy(dict(document)))
    except Exception:  # noqa: BLE001 - any failure means the domain is unknown
        return None
    directory = report["settings"].get("runtime", {}).get("directory")
    return Path(directory) if directory else None


def _is_host_command(cmdline: list[str]) -> bool:
    for index, argument in enumerate(cmdline):
        if argument == "-m" and index + 1 < len(cmdline):
            if cmdline[index + 1] in {"comsol_mcp.server", "comsol_mcp"}:
                return True
        name = Path(argument).name.lower()
        if name in {"comsol-mcp", "comsol-mcp.exe", "comsol-mcp-script.py"}:
            return True
    return False


def observe_hosts(
    runtime_dir: Path | None,
    *,
    process_iter: Callable[..., Any] | None = None,
    own_pid: int | None = None,
) -> HostObservation:
    """Collect verified MCP hosts and UNKNOWN observations.

    Two sources must both be clear: the admission record of the configured
    domain (authoritative for 0.8.2 hosts, including the solver lease that a
    retained-version host holds) and a process inventory (hosts in any
    domain, of either transport). A process whose identity cannot be read
    is UNKNOWN, never "absent".
    """
    observation = HostObservation()
    me = os.getpid() if own_pid is None else own_pid

    if runtime_dir is None:
        observation.unknowns.append("the configured ownership domain cannot be resolved")
    else:
        status = host_admission.status(runtime_dir, assess_cleanup=True)
        observation.admission = status
        state = status.get("state")
        if state == "active":
            observation.hosts.append(
                f"{status.get('transport_mode')} host PID {status.get('pid')} "
                "(verified admission record)"
            )
        elif state == "uncertain":
            observation.unknowns.append(f"admission record: {status.get('reason')}")
        elif state == "stale":
            cleanup = status.get("cleanup") or {}
            if cleanup.get("state") != "verified":
                observation.unknowns.append(
                    f"a previous {status.get('transport_mode')} host (PID {status.get('pid')}) "
                    f"exited without verified cleanup: {cleanup.get('reason')}"
                )
        try:
            host_admission.solver_lease_evidence(runtime_dir)
        except host_admission.HostAdmissionError as exc:
            if exc.reason_code == "admission_domain_busy":
                observation.hosts.append(f"solver owner: {exc}")
            else:
                observation.unknowns.append(f"solver lease: {exc}")

    try:
        import psutil

        iterator = process_iter or psutil.process_iter
        for proc in iterator(["pid", "name", "cmdline"]):
            info = proc.info
            pid = info.get("pid")
            if pid == me:
                continue
            name = str(info.get("name") or "").lower()
            stem = name.removesuffix(".exe")
            if stem.startswith("comsol-mcp-shim"):
                observation.hosts.append(f"{info.get('name')} (PID {pid})")
                continue
            if stem.startswith("comsol-mcp") and stem not in _NON_HOST_SCRIPTS:
                observation.hosts.append(f"{info.get('name')} (PID {pid})")
                continue
            if not stem.startswith("python"):
                continue
            cmdline = info.get("cmdline")
            if not cmdline:
                observation.unknowns.append(
                    f"{info.get('name')} (PID {pid}): its command line cannot be read"
                )
                continue
            if _is_host_command(list(cmdline)):
                observation.hosts.append(f"{info.get('name')} (PID {pid})")
    except Exception as exc:  # noqa: BLE001 - an incomplete inventory is UNKNOWN
        observation.unknowns.append(f"the process inventory is incomplete: {exc}")
    return observation


class SwitchRefused(Exception):
    """A transport switch was refused with a stable reason code."""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail or reason_code)


def plan_switch(
    document: Mapping[str, Any],
    mode: str,
    binary: BinaryCheck | None,
) -> dict[str, Any]:
    """Return the complete settings document for the requested mode.

    Shim mode records the verified binary identity (path, SHA-256, version,
    platform) and pins ``runtime.directory`` to one literal absolute path so
    the shim resolves the same ownership domain as Python. The result must
    pass the real settings validator; nothing is written here.
    """
    if mode not in host_admission.TRANSPORT_MODES:
        raise SwitchRefused("mode_invalid", mode)
    planned = deepcopy(dict(document))
    planned["schema_name"] = SETTINGS_SCHEMA
    planned["schema_version"] = SETTINGS_VERSION
    planned["transport"] = mode
    if mode == "shim":
        if binary is None or not binary.ok:
            raise SwitchRefused("binary_unverified", binary.reason_code if binary else "")
        planned["shim_binary"] = {
            "path": binary.path,
            "sha256": binary.sha256,
            "version": binary.version,
            "platform": binary.platform,
        }
        runtime = planned.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}
            planned["runtime"] = runtime
        if not is_concrete_runtime_path(runtime.get("directory")):
            resolved = configured_runtime_dir(document)
            if resolved is None or not is_concrete_runtime_path(str(resolved)):
                raise SwitchRefused(
                    "runtime_unresolved",
                    "the runtime directory cannot be pinned to one literal path",
                )
            runtime["directory"] = str(resolved)
    try:
        report = normalize_settings_document(deepcopy(planned))
    except SettingsError as exc:
        raise SwitchRefused("settings_invalid", str(exc)) from exc
    if report["errors"]:
        raise SwitchRefused(
            "settings_invalid",
            "; ".join(f"{error['path']}: {error['message']}" for error in report["errors"]),
        )
    return planned


def describe_modes(configured: str, observation: HostObservation) -> dict[str, Any]:
    """Configured mode and verified active host, kept separate on purpose."""
    pending = (
        observation.active_transport is not None and observation.active_transport != configured
    )
    return {
        "configured": configured,
        "state": observation.state,
        "hosts": list(observation.hosts),
        "unknowns": list(observation.unknowns),
        "active_transport": observation.active_transport,
        "pending_restart": pending,
    }
