"""§10 startup-conflict acceptance probe (0.8.2 H/I repair).

Drives the real Python entry point and the real shim binary through the ten
solver-free startup scenarios of the plan. Every check is an explicit oracle
over observed behavior: exit code, the refusal reason code printed by the
entry point, the admission record (owner PID, birth identity, recovery
evidence), process liveness, and a sentinel worker that would leave a marker
file if any forbidden worker were created. No check reads source text.

Usage:
    python startup_conflict_probe.py --shim <comsol-mcp-shim.exe>
        --python <interpreter> --output <results.json> [--work <dir>]
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
sp = importlib.import_module("shim_probe_support")

TIMEOUT_EXIT = 60.0


class Probe:
    def __init__(self, shim: Path, python: Path, work: Path) -> None:
        self.shim = shim.resolve()
        self.python = python
        self.work = work
        self.binary = sp.shim_identity(self.shim)
        self.cases: dict[str, Any] = {}
        self.checks: dict[str, bool] = {}
        self.cleanup: list[Callable[[], None]] = []

    # -- fixtures --------------------------------------------------------

    def directory(self, name: str) -> Path:
        path = self.work / name
        path.mkdir(parents=True, exist_ok=False)
        return path

    def settings(self, settings_path: Path, transport: str, runtime: Path, **binary: Any) -> Path:
        identity = {**self.binary, **binary} if transport == "shim" else None
        document = sp.settings_document(transport=transport, runtime=runtime, binary=identity)
        for key, value in binary.items():
            if value is None:
                document.setdefault("shim_binary", {}).pop(key, None)
        sp.write_settings(settings_path, document)
        return settings_path

    def sentinel(self, directory: Path) -> tuple[str, Path]:
        marker = directory / "forbidden-worker-started.txt"
        argv = [
            str(self.python),
            "-c",
            f"open(r'{marker}', 'w').write('started')",
        ]
        return json.dumps(argv), marker

    def env(self, settings: Path, *, child: str | None = None, **extra: str) -> dict[str, str]:
        environment = sp.base_environment()
        environment["COMSOL_MCP_SETTINGS_PATH"] = str(settings)
        if child is not None:
            environment["COMSOL_MCP_SHIM_CHILD"] = child
        environment.update(extra)
        return environment

    def real_worker(self) -> str:
        return json.dumps([str(self.python), "-m", "comsol_mcp.server"])

    def python_host(self, env: dict[str, str], log: Path) -> sp.Host:
        host = sp.python_host(self.python, env, log)
        self.cleanup.append(host.kill_tree)
        return host

    def shim_host(self, env: dict[str, str], log: Path) -> sp.Host:
        host = sp.shim_host(self.shim, env, log)
        self.cleanup.append(host.kill_tree)
        return host

    def check(self, name: str, passed: bool, evidence: dict[str, Any]) -> None:
        self.checks[name] = bool(passed)
        self.cases.setdefault(name.split(".")[0], {})[name] = {
            "passed": bool(passed),
            **evidence,
        }

    def refused(self, host: sp.Host, runtime: Path, marker: Path | None) -> dict[str, Any]:
        code = host.wait_exit(TIMEOUT_EXIT)
        return {
            "exit_code": code,
            "reason_code": host.reason(),
            "lock_present": (runtime / sp.LOCK_NAME).exists(),
            "forbidden_worker_started": bool(marker and marker.exists()),
            "stderr_tail": host.stderr()[-600:],
        }

    # -- case 1 ------------------------------------------------------------

    def case01_mismatch(self) -> None:
        directory = self.directory("case01")
        runtime = directory / "runtime"
        settings = self.settings(directory / "shim-settings.json", "shim", runtime)
        host = self.python_host(self.env(settings), directory / "python.log")
        observed = self.refused(host, runtime, None)
        self.check(
            "case01.python_with_shim_settings_refused",
            observed["exit_code"] == 2
            and observed["reason_code"] == "transport_mode_mismatch"
            and not observed["lock_present"],
            observed,
        )
        settings = self.settings(directory / "python-settings.json", "python", runtime)
        child, marker = self.sentinel(directory)
        host = self.shim_host(self.env(settings, child=child), directory / "shim.log")
        observed = self.refused(host, runtime, marker)
        self.check(
            "case01.shim_with_python_settings_refused",
            observed["exit_code"] == 2
            and observed["reason_code"] == "transport_mode_mismatch"
            and not observed["lock_present"]
            and not observed["forbidden_worker_started"],
            observed,
        )

    # -- case 2 ------------------------------------------------------------

    def case02_live_old_host(self) -> None:
        directory = self.directory("case02")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "python", runtime)
        old = self.python_host(self.env(settings), directory / "old-python.log")
        lock = sp.wait_for_lock(runtime)
        served_before = old.initialize()
        _lock, before = sp.read_lock(runtime)
        # A hand edit (or a client command that bypasses the GUI) now selects
        # the shim while the old Python host keeps its work.
        self.settings(settings, "shim", runtime)
        child, marker = self.sentinel(directory)
        new = self.shim_host(self.env(settings, child=child), directory / "new-shim.log")
        observed = self.refused(new, runtime, marker)
        _lock, after = sp.read_lock(runtime)
        evidence = {
            **observed,
            "old_host_owned_lock": sp.lock_owned_by(lock, old),
            "old_host_served_before": served_before,
            "old_host_alive_after": old.alive(),
            "old_host_serves_after": old.ping(),
            "lock_unchanged": before == after,
            "refusal_names_owner": bool(lock)
            and f"PID {lock.get('pid')}" in observed["stderr_tail"],
        }
        self.check(
            "case02.live_old_host_preserved_and_new_host_refused",
            observed["exit_code"] == 2
            and observed["reason_code"] == "admission_domain_busy"
            and evidence["old_host_owned_lock"]
            and evidence["old_host_served_before"]
            and evidence["old_host_alive_after"]
            and evidence["old_host_serves_after"]
            and evidence["lock_unchanged"]
            and evidence["refusal_names_owner"]
            and not observed["forbidden_worker_started"],
            evidence,
        )
        code = old.close()
        self.check(
            "case02.old_host_closes_normally_and_releases",
            code == 0 and not (runtime / sp.LOCK_NAME).exists(),
            {"exit_code": code},
        )

    # -- case 3 ------------------------------------------------------------

    def _race(self, directory: Path, launches: list[Callable[[], sp.Host]], runtime: Path) -> dict:
        hosts = [launch() for launch in launches]
        settle = time.time() + TIMEOUT_EXIT
        while time.time() < settle:
            exited = [host for host in hosts if not host.alive()]
            lock = sp.read_lock(runtime)[0]
            if len(exited) == len(hosts) - 1 and lock is not None:
                break
            time.sleep(0.2)
        time.sleep(1.0)  # A late loser must still be refused, not admitted.
        lock = sp.read_lock(runtime)[0]
        alive = [host for host in hosts if host.alive()]
        losers = [host for host in hosts if not host.alive()]
        evidence = {
            "launched": len(hosts),
            "alive": [host.pid for host in alive],
            "lock_pid": lock.get("pid") if lock else None,
            "lock_transport": lock.get("transport_mode") if lock else None,
            "losers": [
                {"pid": host.pid, "exit_code": host.process.returncode, "reason": host.reason()}
                for host in losers
            ],
        }
        passed = (
            len(alive) == 1
            and sp.lock_owned_by(lock, alive[0])
            and all(
                host.process.returncode == 2 and host.reason() == "admission_domain_busy"
                for host in losers
            )
        )
        for host in alive:
            evidence["winner_exit"] = host.close()
        evidence["lock_released"] = not (runtime / sp.LOCK_NAME).exists()
        return {"passed": passed and evidence["lock_released"], **evidence}

    def case03_simultaneous(self) -> None:
        for round_index in range(2):
            directory = self.directory(f"case03-python-{round_index}")
            runtime = directory / "runtime"
            settings = self.settings(directory / "settings.json", "python", runtime)
            result = self._race(
                directory,
                [
                    lambda i=i: self.python_host(self.env(settings), directory / f"p{i}.log")
                    for i in range(3)
                ],
                runtime,
            )
            self.check(
                f"case03.python_python_python_round{round_index}", result.pop("passed"), result
            )

            directory = self.directory(f"case03-shim-{round_index}")
            runtime = directory / "runtime"
            settings = self.settings(directory / "settings.json", "shim", runtime)
            child, marker = self.sentinel(directory)
            result = self._race(
                directory,
                [
                    lambda i=i: self.shim_host(
                        self.env(settings, child=child), directory / f"s{i}.log"
                    )
                    for i in range(3)
                ],
                runtime,
            )
            result["forbidden_worker_started"] = marker.exists()
            self.check(
                f"case03.shim_shim_shim_round{round_index}",
                result.pop("passed") and not result["forbidden_worker_started"],
                result,
            )

            directory = self.directory(f"case03-cross-{round_index}")
            runtime = directory / "runtime"
            python_settings = self.settings(directory / "python.json", "python", runtime)
            shim_settings = self.settings(directory / "shim.json", "shim", runtime)
            child, marker = self.sentinel(directory)
            result = self._race(
                directory,
                [
                    lambda: self.python_host(self.env(python_settings), directory / "p.log"),
                    lambda: self.shim_host(
                        self.env(shim_settings, child=child), directory / "s.log"
                    ),
                ],
                runtime,
            )
            result["forbidden_worker_started"] = marker.exists()
            self.check(
                f"case03.python_shim_cross_round{round_index}",
                result.pop("passed") and not result["forbidden_worker_started"],
                result,
            )

    # -- case 4 and 6a -------------------------------------------------------

    def case04_manual_shim(self) -> None:
        directory = self.directory("case04")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "shim", runtime)
        host = self.shim_host(self.env(settings, child=self.real_worker()), directory / "shim.log")
        lock = sp.wait_for_lock(runtime)
        served = host.initialize(timeout=180)
        recorded = sp.read_lock(runtime)[0] or {}
        workers = recorded.get("owned_processes") or []
        worker_pid = workers[0]["pid"] if workers else None
        code = host.close()
        time.sleep(0.5)
        import psutil

        evidence = {
            "lock_pid": lock.get("pid") if lock else None,
            "shim_pid": host.pid,
            "lock_shim_identity": (lock or {}).get("shim"),
            "initialize_served": served,
            "recorded_worker": workers,
            "binding_verified_log": "Shim worker binding verified" in host.stderr(),
            "exit_code": code,
            "lock_released": not (runtime / sp.LOCK_NAME).exists(),
            "worker_gone": worker_pid is not None and not psutil.pid_exists(worker_pid),
        }
        self.check(
            "case04.manual_shim_with_valid_identity_admitted_and_served",
            lock is not None
            and lock.get("pid") == host.pid
            and lock.get("transport_mode") == "shim"
            and served
            and len(workers) == 1
            and code == 0
            and evidence["lock_released"]
            and evidence["worker_gone"],
            evidence,
        )
        self.check(
            "case06.valid_contained_worker_binding_verified",
            served and evidence["binding_verified_log"],
            {"binding_verified_log": evidence["binding_verified_log"]},
        )

        unrelated = directory / "not-a-shim.txt"
        unrelated.write_text("This is not an executable.", encoding="utf-8")
        copy_dir = directory / "copy"
        copy_dir.mkdir()
        copy = copy_dir / self.shim.name
        shutil.copy2(self.shim, copy)
        variants: list[tuple[str, dict[str, Any] | str, str]] = [
            (
                "missing_binary_object",
                {"path": None, "sha256": None, "version": None, "platform": None},
                "transport_setting_invalid",
            ),
            ("missing_sha256", {"sha256": None}, "transport_setting_invalid"),
            ("missing_version", {"version": None}, "transport_setting_invalid"),
            ("missing_platform", {"platform": None}, "transport_setting_invalid"),
            ("wrong_sha256", {"sha256": "0" * 64}, "shim_binary_mismatch"),
            (
                "unrelated_file_with_its_own_hash",
                {
                    "path": str(unrelated),
                    "sha256": hashlib.sha256(unrelated.read_bytes()).hexdigest(),
                },
                "shim_binary_mismatch",
            ),
            ("identical_copy_at_another_path", {"path": str(copy)}, "shim_binary_mismatch"),
            ("wrong_version", {"version": "9.9.9"}, "shim_binary_mismatch"),
            ("wrong_platform", {"platform": "aarch64-apple-darwin"}, "shim_binary_mismatch"),
            ("runtime_reference", "runtime_reference", "transport_setting_invalid"),
            ("duplicate_keys", "duplicate_keys", "settings_unverifiable"),
        ]
        for name, change, expected in variants:
            case_dir = self.directory(f"case04-{name}")
            case_runtime = case_dir / "runtime"
            path = case_dir / "settings.json"
            if change == "runtime_reference":
                document = sp.settings_document(
                    transport="shim", runtime=case_runtime, binary=self.binary
                )
                document["runtime"]["directory"] = "%PROGRAMDATA%/comsol_mcp/runtime"
                sp.write_settings(path, document)
            elif change == "duplicate_keys":
                document = sp.settings_document(
                    transport="shim", runtime=case_runtime, binary=self.binary
                )
                text = json.dumps(document, indent=2)
                text = text.replace(
                    '"transport": "shim"', '"transport": "python",\n  "transport": "shim"', 1
                )
                sp.write_settings(path, text)
            else:
                if not isinstance(change, dict):
                    raise TypeError(f"unexpected fixture change for {name}")
                self.settings(path, "shim", case_runtime, **change)
                if name == "missing_binary_object":
                    document = json.loads(path.read_text(encoding="utf-8"))
                    document.pop("shim_binary", None)
                    sp.write_settings(path, document)
            child, marker = self.sentinel(case_dir)
            host = self.shim_host(self.env(path, child=child), case_dir / "shim.log")
            observed = self.refused(host, case_runtime, marker)
            self.check(
                f"case04.invalid_identity_{name}",
                observed["exit_code"] == 2
                and observed["reason_code"] == expected
                and not observed["lock_present"]
                and not observed["forbidden_worker_started"],
                {**observed, "expected_reason": expected},
            )

    # -- case 5 ------------------------------------------------------------

    def case05_interactive(self) -> None:
        directory = self.directory("case05")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "shim", runtime)
        child, marker = self.sentinel(directory)
        stderr_file = directory / "interactive-stderr.txt"
        environment = self.env(settings, child=child)
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0  # SW_HIDE: a hidden console, still a terminal.
            command = f'cmd.exe /d /s /c ""{self.shim}" 2>"{stderr_file}""'
            process = subprocess.Popen(  # noqa: S603 - fixed probe command line
                command,
                env=environment,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                startupinfo=startup,
            )
            code = process.wait(timeout=TIMEOUT_EXIT)
        else:
            import pty

            primary, secondary = pty.openpty()
            with stderr_file.open("wb") as handle:
                process = subprocess.Popen(  # noqa: S603 - fixed probe argv
                    [str(self.shim)],
                    env=environment,
                    stdin=secondary,
                    stdout=secondary,
                    stderr=handle,
                )
                code = process.wait(timeout=TIMEOUT_EXIT)
            os.close(primary)
            os.close(secondary)
        text = (
            stderr_file.read_text(encoding="utf-8", errors="replace")
            if stderr_file.exists()
            else ""
        )
        evidence = {
            "exit_code": code,
            "stderr": text[-400:],
            "runtime_created": runtime.exists(),
            "forbidden_worker_started": marker.exists(),
        }
        self.check(
            "case05.interactive_launch_explains_and_exits_before_admission",
            code == 0
            and "not an interactive program" in text
            and not runtime.exists()
            and not marker.exists(),
            evidence,
        )
        version = subprocess.run(  # noqa: S603 - fixed probe argv
            [str(self.shim), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=sp.NO_WINDOW,
        )
        expected = f"comsol-mcp-shim {self.binary['version']} ({self.binary['platform']})"
        self.check(
            "case05.version_report_preserved",
            version.returncode == 0 and version.stdout.strip() == expected,
            {"stdout": version.stdout.strip(), "exit_code": version.returncode},
        )

    # -- case 6 ------------------------------------------------------------

    def case06_spoofed_worker(self) -> None:
        directory = self.directory("case06")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "shim", runtime)
        token = uuid.uuid4().hex * 2
        variants = {
            "flag_only": {"COMSOL_MCP_SHIM_WORKER": "1"},
            "fake_host_without_admission": {
                "COMSOL_MCP_SHIM_WORKER": "1",
                "COMSOL_MCP_SHIM_HOST_PID": str(os.getpid()),
                "COMSOL_MCP_SHIM_WORKER_TOKEN": token,
            },
        }
        expected = {
            "flag_only": "worker_binding_missing",
            "fake_host_without_admission": "worker_binding_invalid",
        }
        for name, extra in variants.items():
            host = self.python_host(self.env(settings, **extra), directory / f"{name}.log")
            observed = self.refused(host, runtime, None)
            self.check(
                f"case06.spoofed_worker_{name}_refused",
                observed["exit_code"] == 2 and observed["reason_code"] == expected[name],
                observed,
            )

        # A live admitted shim with a real worker: a spoof that copies the
        # worker's own launch token and host PID is still not its descendant.
        import psutil

        shim = self.shim_host(self.env(settings, child=self.real_worker()), directory / "shim.log")
        lock = sp.wait_for_lock(runtime)
        served = shim.initialize(timeout=180)
        stolen: dict[str, str] = {}
        try:
            for child in psutil.Process(shim.pid).children(recursive=True):
                environment = child.environ()
                if "COMSOL_MCP_SHIM_WORKER_TOKEN" in environment:
                    stolen = {
                        key: environment[key]
                        for key in (
                            "COMSOL_MCP_SHIM_WORKER",
                            "COMSOL_MCP_SHIM_HOST_PID",
                            "COMSOL_MCP_SHIM_WORKER_TOKEN",
                        )
                    }
                    break
        except psutil.Error as exc:
            stolen = {"error": str(exc)}
        _lock, before = sp.read_lock(runtime)
        spoof = self.python_host(self.env(settings, **stolen), directory / "stolen.log")
        observed = self.refused(spoof, runtime, None)
        _lock, after = sp.read_lock(runtime)
        evidence = {
            **observed,
            "shim_served": served,
            "token_copied": "COMSOL_MCP_SHIM_WORKER_TOKEN" in stolen,
            "host_pid_matches_lock": bool(lock)
            and stolen.get("COMSOL_MCP_SHIM_HOST_PID") == str(lock.get("pid")),
            "shim_alive_after": shim.alive(),
            "lock_unchanged": before == after,
        }
        self.check(
            "case06.spoof_with_copied_token_and_host_pid_refused",
            observed["exit_code"] == 2
            and observed["reason_code"] == "worker_binding_invalid"
            and evidence["token_copied"]
            and evidence["host_pid_matches_lock"]
            and evidence["shim_alive_after"]
            and evidence["lock_unchanged"]
            and "does not descend" in observed["stderr_tail"],
            evidence,
        )
        shim.close()

    # -- case 7 ------------------------------------------------------------

    def _both_entry_points(
        self,
        name: str,
        prepare: Callable[[Path], dict[str, Any]],
        oracle: Callable[[dict[str, Any], dict[str, Any]], bool],
    ) -> None:
        for transport in ("python", "shim"):
            directory = self.directory(f"case07-{name}-{transport}")
            runtime = directory / "runtime"
            runtime.mkdir()
            fixture = prepare(runtime)
            settings = self.settings(directory / "settings.json", transport, runtime)
            child, marker = self.sentinel(directory)
            if transport == "python":
                host = self.python_host(self.env(settings), directory / "host.log")
            else:
                host = self.shim_host(self.env(settings, child=child), directory / "host.log")
            if fixture.get("expect_admission"):
                lock = sp.wait_for_owner(runtime, host, timeout=TIMEOUT_EXIT)
                admitted = sp.lock_owned_by(lock, host)
                code = host.close()
                observed = {
                    "admitted": admitted,
                    "recovered_from": (lock or {}).get("recovered_from"),
                    "exit_code": code,
                    "lock_released": not (runtime / sp.LOCK_NAME).exists(),
                }
            else:
                observed = self.refused(host, runtime, marker)
                current = sp.read_lock(runtime)[1]
                observed["lock_bytes_unchanged"] = current == fixture.get("lock_bytes") or (
                    current == b"<access denied>" and fixture.get("lock_bytes") is not None
                )
            observed["fixture"] = {k: v for k, v in fixture.items() if k != "lock_bytes"}
            self.check(f"case07.{name}_{transport}", oracle(observed, fixture), observed)
            for release in fixture.get("release", []):
                release()

    def _contained_dead_host(self) -> dict[str, Any]:
        """A previous host that adopted containment, recorded it, and exited."""
        import psutil

        script = (
            "import json, os, psutil, sys\n"
            f"sys.path.insert(0, {str(sp.ROOT)!r})\n"
            "from comsol_mcp import host_containment\n"
            "identity = host_containment.host_containment_id(pid=os.getpid())\n"
            "host_containment.adopt_host_containment(identity)\n"
            "host_containment.containment_members(identity)\n"
            "me = psutil.Process()\n"
            "print(json.dumps({'pid': me.pid, 'process_create_time': me.create_time(),"
            " 'cmdline': me.cmdline(), 'containment': identity}), flush=True)\n"
        )
        parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
            [str(self.python), "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            cwd=str(sp.ROOT),
            creationflags=sp.NO_WINDOW,
        )
        report = json.loads(parent.stdout.readline())
        parent.wait(timeout=30)
        deadline = time.time() + 10
        while psutil.pid_exists(report["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        return report

    def _record_of(self, report: dict[str, Any]) -> dict[str, Any]:
        import hashlib as _hashlib

        return sp.admission_record(
            {
                "pid": report["pid"],
                "process_create_time": report["process_create_time"],
                "command_signature": _hashlib.sha256(
                    "\0".join(report["cmdline"]).encode()
                ).hexdigest(),
            },
            containment=report.get("containment"),
        )

    def case07_ownership_evidence(self) -> None:
        import psutil

        def denied(runtime: Path) -> dict[str, Any]:
            helper = sp.sleeper(self.python)
            record = sp.admission_record(sp.birth_identity(helper.pid))
            payload = sp.write_record(runtime, record)
            lock = runtime / sp.LOCK_NAME
            user = os.environ.get("USERNAME") or os.getlogin()
            subprocess.run(  # noqa: S603 - fixed icacls argv
                ["icacls", str(lock), "/deny", f"{user}:(R)"],
                capture_output=True,
                check=True,
                creationflags=sp.NO_WINDOW,
            )

            def restore() -> None:
                subprocess.run(  # noqa: S603 - fixed icacls argv
                    ["icacls", str(lock), "/remove:d", user],
                    capture_output=True,
                    check=False,
                    creationflags=sp.NO_WINDOW,
                )
                helper.kill()

            return {"lock_bytes": payload, "release": [restore]}

        if os.name == "nt":
            self._both_entry_points(
                "access_denied_lock_is_uncertain",
                denied,
                lambda observed, _f: (
                    observed["exit_code"] == 2
                    and observed["reason_code"] == "admission_lock_unreadable"
                ),
            )

        def stale(runtime: Path) -> dict[str, Any]:
            # POSIX: a contained dead host verifies by kernel membership and
            # recovers. Windows: a dead host's job name is gone with its only
            # handle and a containment-less record is UNKNOWN (caller
            # decision 2026-11-10: identical on both platforms).
            if os.name != "nt":
                report = self._contained_dead_host()
                sp.write_record(runtime, self._record_of(report))
                return {"expect_admission": True, "dead_pid": report["pid"]}
            dead = sp.dead_identity(self.python)
            return {"lock_bytes": sp.write_record(runtime, sp.admission_record(dead))}

        if os.name != "nt":
            self._both_entry_points(
                "stale_owner_recovered_after_verified_cleanup",
                stale,
                lambda observed, fixture: (
                    observed["admitted"]
                    and (observed["recovered_from"] or {}).get("pid") == fixture["dead_pid"]
                    and "cleanup" in (observed["recovered_from"] or {})
                    and observed["exit_code"] == 0
                    and observed["lock_released"]
                ),
            )
        else:
            self._both_entry_points(
                "legacy_stale_record_is_unknown_and_kept",
                stale,
                lambda observed, _f: (
                    observed["exit_code"] == 2
                    and observed["reason_code"] == "admission_state_uncertain"
                    and "no containment identity" in observed["stderr_tail"]
                    and "the record was kept" in observed["stderr_tail"]
                    and "comsol_mcp.host_admission --show" in observed["stderr_tail"]
                    and observed["lock_bytes_unchanged"]
                    and not observed["forbidden_worker_started"]
                ),
            )

        def reused(runtime: Path) -> dict[str, Any]:
            helper = sp.sleeper(self.python)
            identity = sp.birth_identity(helper.pid)
            identity["process_create_time"] -= 1000.0
            if os.name != "nt":
                report = self._contained_dead_host()
                record = self._record_of(report)
                record["pid"] = identity["pid"]
                record["process_create_time"] = identity["process_create_time"]
                sp.write_record(runtime, record)
                return {
                    "expect_admission": True,
                    "reused_pid": helper.pid,
                    "release": [helper.kill],
                }
            payload = sp.write_record(runtime, sp.admission_record(identity))
            return {"lock_bytes": payload, "reused_pid": helper.pid, "release": [helper.kill]}

        if os.name != "nt":
            self._both_entry_points(
                "reused_pid_is_stale",
                reused,
                lambda observed, fixture: (
                    observed["admitted"]
                    and (observed["recovered_from"] or {}).get("pid") == fixture["reused_pid"]
                    and observed["exit_code"] == 0
                ),
            )
        else:
            # The reuse is still classified stale (not a live owner), but the
            # containment-less record never recovers automatically.
            self._both_entry_points(
                "reused_pid_is_stale_and_unknown",
                reused,
                lambda observed, _f: (
                    observed["exit_code"] == 2
                    and observed["reason_code"] == "admission_state_uncertain"
                    and "no containment identity" in observed["stderr_tail"]
                    and observed["lock_bytes_unchanged"]
                ),
            )

        def live_worker(runtime: Path) -> dict[str, Any]:
            helper = sp.sleeper(self.python)
            worker = sp.birth_identity(helper.pid)
            record = sp.admission_record(
                sp.dead_identity(self.python),
                transport_mode="shim",
                owned_processes=[
                    {
                        "role": "worker",
                        "pid": worker["pid"],
                        "process_create_time": worker["process_create_time"],
                    }
                ],
            )
            return {"lock_bytes": sp.write_record(runtime, record), "release": [helper.kill]}

        self._both_entry_points(
            "live_recorded_worker_is_unverified_cleanup",
            live_worker,
            lambda observed, _f: (
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_cleanup_unverified"
                and observed["lock_bytes_unchanged"]
            ),
        )

        def orphan(runtime: Path) -> dict[str, Any]:
            script = (
                "import json, subprocess, sys, psutil;"
                "me = psutil.Process();"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],"
                " creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0));"
                "print(json.dumps({'pid': me.pid, 'process_create_time': me.create_time(),"
                " 'cmdline': me.cmdline(), 'child': child.pid}), flush=True)"
            )
            parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
                [str(self.python), "-c", script],
                stdout=subprocess.PIPE,
                text=True,
                creationflags=sp.NO_WINDOW,
            )
            report = json.loads(parent.stdout.readline())
            parent.wait(timeout=30)
            deadline = time.time() + 10
            while psutil.pid_exists(report["pid"]) and time.time() < deadline:
                time.sleep(0.05)
            owner = {
                "pid": report["pid"],
                "process_create_time": report["process_create_time"],
                "command_signature": hashlib.sha256(
                    "\0".join(report["cmdline"]).encode()
                ).hexdigest(),
            }

            def release() -> None:
                try:
                    psutil.Process(report["child"]).kill()
                except psutil.Error:
                    pass

            return {
                "lock_bytes": sp.write_record(runtime, sp.admission_record(owner)),
                "orphan_pid": report["child"],
                "release": [release],
            }

        # Caller decision 2026-11-10, identical on both platforms: a
        # containment-less record is UNKNOWN — refused, kept, with the
        # explicit-recovery diagnostic, and nothing is stopped.
        self._both_entry_points(
            "legacy_record_with_live_descendant_is_unknown_and_kept",
            orphan,
            lambda observed, fixture: (
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_state_uncertain"
                and psutil.pid_exists(fixture["orphan_pid"])
                and "no containment identity" in observed["stderr_tail"]
                and "the record was kept" in observed["stderr_tail"]
                and "comsol_mcp.host_admission --show" in observed["stderr_tail"]
                and observed["lock_bytes_unchanged"]
            ),
        )

        def live_lease(runtime: Path) -> dict[str, Any]:
            helper = sp.sleeper(self.python)
            lease = {
                "schema_version": "3",
                "owner": "comsol-mcp",
                "mode": "local-client",
                **sp.birth_identity(helper.pid),
                "acquisition_id": "a" * 32,
                "comsol_server_processes": [],
            }
            (runtime / "solver_owner.json").write_text(json.dumps(lease), encoding="utf-8")
            return {"lock_bytes": None, "release": [helper.kill]}

        self._both_entry_points(
            "live_retained_solver_owner_blocks_fresh_admission",
            live_lease,
            lambda observed, _f: (
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_domain_busy"
                and not observed["lock_present"]
            ),
        )

        # Incomplete inventory: the Python entry point with an injected
        # inventory failure must refuse instead of recovering a stale record.
        # On POSIX the record is contained, so recovery would succeed without
        # the injection and the refusal proves the fail-closed inventory; on
        # Windows a dead host has no readable containment, so the record is
        # refused regardless and the row keeps the same observable.
        directory = self.directory("case07-incomplete-inventory-python")
        runtime = directory / "runtime"
        if os.name != "nt":
            payload = sp.write_record(runtime, self._record_of(self._contained_dead_host()))
        else:
            payload = sp.write_record(runtime, sp.admission_record(sp.dead_identity(self.python)))
        settings = self.settings(directory / "settings.json", "python", runtime)
        wrapper = directory / "inject_inventory_failure.py"
        wrapper.write_text(
            "import psutil\n"
            "def broken(*args, **kwargs):\n"
            "    raise psutil.AccessDenied(pid=0, name='injected inventory failure')\n"
            "psutil.process_iter = broken\n"
            "from comsol_mcp.server import main\n"
            "main()\n",
            encoding="utf-8",
        )
        host = sp.Host(
            [str(self.python), str(wrapper)],
            self.env(settings, PYTHONPATH=str(sp.ROOT)),
            directory / "host.log",
        )
        self.cleanup.append(host.kill_tree)
        observed = self.refused(host, runtime, None)
        observed["lock_bytes_unchanged"] = sp.read_lock(runtime)[1] == payload
        self.check(
            "case07.incomplete_inventory_python",
            observed["exit_code"] == 2
            and observed["reason_code"] == "admission_state_uncertain"
            and observed["lock_bytes_unchanged"],
            observed,
        )

        # POSIX: a host started as a process-group leader (a shell job)
        # cannot become a session leader. It must refuse rather than record
        # a session it does not lead, which would hide its descendants
        # from every later recovery.
        if os.name == "nt":
            self.case07_windows_decision()
            self.case07_console_in_use()
            return
        for transport in ("python", "shim"):
            directory = self.directory(f"case07-group-leader-{transport}")
            runtime = directory / "runtime"
            runtime.mkdir()
            settings = self.settings(directory / "settings.json", transport, runtime)
            child, marker = self.sentinel(directory)
            if transport == "python":
                host = sp.python_host(
                    self.python, self.env(settings), directory / "host.log", process_group=0
                )
            else:
                host = sp.shim_host(
                    self.shim,
                    self.env(settings, child=child),
                    directory / "host.log",
                    process_group=0,
                )
            self.cleanup.append(host.kill_tree)
            observed = self.refused(host, runtime, marker)
            self.check(
                f"case07.group_leader_host_refuses_a_session_it_cannot_lead_{transport}",
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_state_uncertain"
                and "leads no session it can record" in observed["stderr_tail"]
                and not observed["lock_present"]
                and not observed["forbidden_worker_started"],
                observed,
            )

        self.case07_posix_decision()

    def _cli(self, *arguments: str) -> dict[str, Any]:
        """Run the operator CLI (python -m comsol_mcp.host_admission)."""
        completed = subprocess.run(  # noqa: S603 - fixed interpreter argv
            [str(self.python), "-m", "comsol_mcp.host_admission", *arguments],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(sp.ROOT),
            creationflags=sp.NO_WINDOW,
        )
        try:
            body = json.loads(completed.stdout) if completed.stdout.strip() else None
        except ValueError:
            body = None
        return {
            "exit_code": completed.returncode,
            "json": body,
            "stderr_tail": completed.stderr[-600:],
        }

    def _dead_host_behind_a_lost_job(self) -> dict[str, Any]:
        import psutil

        """A Windows host that exits holding the only job handle, orphan alive.

        The fixture runs under the base interpreter: the venv launcher
        terminates its own descendants when it exits, which would take the
        orphan with it and fake the cleanup the rows must disprove.
        """
        interpreter = getattr(sys, "_base_executable", sys.executable)
        script = (
            "import json, os, psutil, subprocess, sys\n"
            f"sys.path.insert(0, {str(sp.ROOT)!r})\n"
            "from comsol_mcp import host_containment\n"
            "identity = host_containment.host_containment_id(pid=os.getpid())\n"
            "handle = host_containment.adopt_host_containment(identity)\n"
            "assert host_containment.containment_members(identity)\n"
            "orphan = subprocess.Popen(\n"
            '    [sys.executable, "-c", "import time; time.sleep(600)"],\n'
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
            " stderr=subprocess.DEVNULL)\n"
            "del handle  # the job object dies with this last handle\n"
            "me = psutil.Process()\n"
            "print(json.dumps({'pid': me.pid, 'process_create_time':"
            " me.create_time(), 'cmdline': me.cmdline(), 'containment': identity,"
            " 'orphan': orphan.pid}), flush=True)\n"
        )
        parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
            [interpreter, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            cwd=str(sp.ROOT),
            creationflags=sp.NO_WINDOW,
        )
        report = json.loads(parent.stdout.readline())
        parent.wait(timeout=30)
        deadline = time.time() + 10
        while psutil.pid_exists(report["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        return report

    def case07_windows_decision(self) -> None:
        """WIN-CONTAIN-01 and WIN-RECOVER-02 rows: a lost job name never
        proves cleanup, and a confirmation never overrides a visible child.

        The job object dies with its last handle, not its last member, so
        after a host exits its recorded containment can no longer be read.
        Automatic recovery refuses as UNKNOWN with the record kept and names
        the operator flow. The explicit flow refuses while the inventory
        still attributes a live child to the host, and recovers only after
        that child is gone, with a receipt that keeps the confirmation apart
        from machine evidence.
        """
        import psutil

        def lost_job_with_orphan(runtime: Path) -> dict[str, Any]:
            report = self._dead_host_behind_a_lost_job()

            def release() -> None:
                # The orphan keeps the host's own console host alive; an
                # operator's cleanup is complete once both are gone.
                consoles = [
                    process
                    for process in psutil.process_iter(["ppid", "name"])
                    if process.info["ppid"] == report["pid"]
                    and (process.info["name"] or "").lower() == "conhost.exe"
                ]
                for pid in (report["orphan"], *(item.pid for item in consoles)):
                    try:
                        process = psutil.Process(pid)
                        if pid == report["orphan"]:
                            process.kill()
                        process.wait(timeout=10)
                    except psutil.Error:
                        pass

            return {
                "lock_bytes": sp.write_record(runtime, self._record_of(report)),
                "orphan_pid": report["orphan"],
                "release": [release],
            }

        self._both_entry_points(
            "job_name_lost_with_live_orphan_is_unknown_and_kept",
            lost_job_with_orphan,
            lambda observed, fixture: (
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_state_uncertain"
                and "containment cannot be read" in observed["stderr_tail"]
                and "no longer opens" in observed["stderr_tail"]
                and "comsol_mcp.host_admission --show" in observed["stderr_tail"]
                and observed["lock_bytes_unchanged"]
                and not observed["forbidden_worker_started"]
                and psutil.pid_exists(fixture["orphan_pid"])
            ),
        )

        for transport in ("python", "shim"):
            directory = self.directory(f"case07-windows-recovery-{transport}")
            runtime = directory / "runtime"
            runtime.mkdir()
            fixture = lost_job_with_orphan(runtime)
            settings = self.settings(directory / "settings.json", transport, runtime)
            child, marker = self.sentinel(directory)
            if transport == "python":
                host = self.python_host(self.env(settings), directory / "host.log")
            else:
                host = self.shim_host(self.env(settings, child=child), directory / "host.log")
            refused = self.refused(host, runtime, marker)
            refused["lock_bytes_unchanged"] = sp.read_lock(runtime)[1] == fixture["lock_bytes"]
            orphan_pid = fixture["orphan_pid"]
            show = self._cli("--show", str(runtime))
            digest = (show["json"] or {}).get("record_sha256")
            preview = (show["json"] or {}).get("explicit_recovery") or {}
            wrong = self._cli("--recover", str(runtime), "0" * 64, "--confirmed-by", "probe")
            still_kept = sp.read_lock(runtime)[1] == fixture["lock_bytes"]
            # A confirmation never overrides a child the inventory still
            # attributes to the host: refused, record and child untouched.
            with_child = self._cli(
                "--recover", str(runtime), str(digest), "--confirmed-by", "startup probe"
            )
            child_refusal = {
                "refused": refused,
                "show": show,
                "wrong_digest": wrong,
                "record_kept_after_wrong_digest": still_kept,
                "recover_with_live_child": with_child,
                "record_kept_with_live_child": sp.read_lock(runtime)[1] == fixture["lock_bytes"],
                "nothing_archived": not (runtime / "admission_recovered").exists(),
                "child_alive": psutil.pid_exists(orphan_pid),
            }
            self.check(
                f"case07.explicit_recovery_refuses_a_live_direct_child_after_job_loss_{transport}",
                refused["exit_code"] == 2
                and refused["reason_code"] == "admission_state_uncertain"
                and "comsol_mcp.host_admission --show" in refused["stderr_tail"]
                and refused["lock_bytes_unchanged"]
                and show["exit_code"] == 0
                and bool(digest)
                and preview.get("state") == "refused"
                and preview.get("reason_code") == "admission_cleanup_unverified"
                and str(orphan_pid) in str(preview.get("reason"))
                and wrong["exit_code"] == 2
                and still_kept
                and with_child["exit_code"] == 2
                and "admission_cleanup_unverified" in with_child["stderr_tail"]
                and str(orphan_pid) in with_child["stderr_tail"]
                and child_refusal["record_kept_with_live_child"]
                and child_refusal["nothing_archived"]
                and child_refusal["child_alive"],
                child_refusal,
            )
            # The operator's independent cleanup: this probe stops its own
            # fixture child. The recovery tool itself terminates nothing.
            for release in fixture["release"]:
                release()
            child_gone = not psutil.pid_exists(orphan_pid)
            show_again = self._cli("--show", str(runtime))
            ready = (show_again["json"] or {}).get("explicit_recovery") or {}
            recover = self._cli(
                "--recover", str(runtime), str(digest), "--confirmed-by", "startup probe"
            )
            receipt = recover["json"] or {}
            machine = receipt.get("machine_verification") or {}
            confirmation = receipt.get("operator_confirmation") or {}
            archived = (
                runtime
                / "admission_recovered"
                / str((receipt.get("record") or {}).get("archived_as"))
            )
            lock_gone = not (runtime / sp.LOCK_NAME).exists()
            if transport == "python":
                again = self.python_host(self.env(settings), directory / "host-again.log")
            else:
                again = self.shim_host(
                    self.env(settings, child=child), directory / "host-again.log"
                )
            lock = sp.wait_for_owner(runtime, again, timeout=TIMEOUT_EXIT)
            admitted = sp.lock_owned_by(lock, again)
            closed = again.close()
            observed = {
                "child_stopped_by_the_operator": child_gone,
                "show_after_cleanup": show_again,
                "recover": recover,
                "archive_matches_record": archived.is_file()
                and archived.read_bytes() == fixture["lock_bytes"],
                "lock_removed": lock_gone,
                "admitted_after_recovery": admitted,
                "exit_after_recovery": closed,
                "automatic_verification": (receipt.get("automatic_verification") or {}).get(
                    "state"
                ),
                "covered_by_confirmation": [
                    item.get("evidence") for item in confirmation.get("covers") or []
                ],
                "listener_method": (machine.get("listeners") or {}).get("method"),
            }
            self.check(
                f"case07.explicit_recovery_of_a_lost_job_record_{transport}",
                child_gone
                and show_again["exit_code"] == 0
                and (show_again["json"] or {}).get("record_sha256") == digest
                and ready.get("state") == "ready"
                and recover["exit_code"] == 0
                and receipt.get("outcome") == "recovered"
                and observed["automatic_verification"] == "unknown"
                and receipt.get("terminated_processes") == []
                and confirmation.get("confirmed_by") == "startup probe"
                and observed["covered_by_confirmation"] == ["descendants", "listeners"]
                and observed["listener_method"] == "not_machine_verified"
                and "no longer opens" in str((machine.get("descendants") or {}).get("unavailable"))
                and observed["archive_matches_record"]
                and lock_gone
                and admitted
                and closed == 0,
                observed,
            )

    def case07_console_in_use(self) -> None:
        """A dead host's own console still in use refuses explicit recovery.

        The host had its own hidden console; its grandchild (whose parent
        exited first) cannot be attributed through parent links but keeps
        that console, and so its console host, alive. The flow refuses, keeps
        the record and the grandchild, and proceeds once the grandchild is
        stopped by the operator.
        """
        import psutil

        interpreter = getattr(sys, "_base_executable", sys.executable)
        script = (
            "import json, os, psutil, subprocess, sys\n"
            f"sys.path.insert(0, {str(sp.ROOT)!r})\n"
            "from comsol_mcp import host_containment\n"
            "identity = host_containment.host_containment_id(pid=os.getpid())\n"
            "handle = host_containment.adopt_host_containment(identity)\n"
            "code = ('import subprocess, sys; c = subprocess.Popen([sys.argv[1], \"-c\", '\n"
            "        '\"import time; time.sleep(600)\"], stdin=subprocess.DEVNULL, '\n"
            "        'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); '\n"
            "        'print(c.pid, flush=True)')\n"
            "inter = subprocess.Popen([sys.executable, '-c', code, sys.executable],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,"
            " text=True)\n"
            "grandchild = int(inter.stdout.readline())\n"
            "inter.wait(timeout=30)\n"
            "me = psutil.Process()\n"
            "print(json.dumps({'pid': me.pid, 'process_create_time': me.create_time(),"
            " 'cmdline': me.cmdline(), 'containment': identity,"
            " 'grandchild': grandchild}), flush=True)\n"
        )
        parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
            [interpreter, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            cwd=str(sp.ROOT),
            creationflags=sp.NO_WINDOW,
        )
        report = json.loads(parent.stdout.readline())
        parent.wait(timeout=30)
        deadline = time.time() + 10
        while psutil.pid_exists(report["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        directory = self.directory("case07-explicit-recovery-console-in-use")
        runtime = directory / "runtime"
        runtime.mkdir()
        grandchild = report["grandchild"]
        try:
            consoles = [
                process.pid
                for process in psutil.process_iter(["ppid", "name"])
                if process.info["ppid"] == report["pid"]
                and (process.info["name"] or "").lower() == "conhost.exe"
            ]
            payload = sp.write_record(runtime, self._record_of(report))
            digest = hashlib.sha256(payload).hexdigest()
            refused = self._cli(
                "--recover", str(runtime), digest, "--confirmed-by", "startup probe"
            )
            kept = sp.read_lock(runtime)[1] == payload
            alive = psutil.pid_exists(grandchild)
            try:
                stopped = psutil.Process(grandchild)
                stopped.kill()
                stopped.wait(timeout=10)
                for pid in consoles:
                    psutil.Process(pid).wait(timeout=10)
            except psutil.Error:
                pass
            recover = self._cli(
                "--recover", str(runtime), digest, "--confirmed-by", "startup probe"
            )
            receipt = recover["json"] or {}
            observed = {
                "console_hosts_of_the_dead_host": consoles,
                "refused_while_in_use": refused,
                "record_kept": kept,
                "nothing_archived_while_in_use": "admission_recovered"
                not in str(refused.get("json")),
                "grandchild_alive_when_refused": alive,
                "recover_after_cleanup": recover,
            }
            self.check(
                "case07.explicit_recovery_refuses_while_the_dead_hosts_console_is_in_use",
                len(consoles) == 1
                and refused["exit_code"] == 2
                and "admission_cleanup_unverified" in refused["stderr_tail"]
                and "console is still in use" in refused["stderr_tail"]
                and str(consoles[0]) in refused["stderr_tail"]
                and kept
                and alive
                and recover["exit_code"] == 0
                and receipt.get("outcome") == "recovered"
                and receipt.get("terminated_processes") == [],
                observed,
            )
        finally:
            try:
                psutil.Process(grandchild).kill()
            except psutil.Error:
                pass

    def case07_posix_decision(self) -> None:
        """POSIX rows for the caller decision of 2026-11-10.

        A record that cannot prove containment is UNKNOWN: recovery, takeover
        and dependent startup are refused, the record is kept and nothing is
        stopped; the explicit operator flow recovers it after independent
        confirmation and never lifts positive evidence. Valid identities keep
        recovering automatically.
        """
        import psutil

        def legacy_clean(runtime: Path) -> dict[str, Any]:
            return {
                "lock_bytes": sp.write_record(
                    runtime, sp.admission_record(sp.dead_identity(self.python))
                )
            }

        self._both_entry_points(
            "legacy_record_without_descendants_is_unknown_and_kept",
            legacy_clean,
            lambda observed, _f: (
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_state_uncertain"
                and "no containment identity" in observed["stderr_tail"]
                and observed["lock_bytes_unchanged"]
                and not observed["forbidden_worker_started"]
            ),
        )

        def contained_orphan(runtime: Path) -> dict[str, Any]:
            script = (
                "import json, os, psutil, subprocess, sys\n"
                f"sys.path.insert(0, {str(sp.ROOT)!r})\n"
                "from comsol_mcp import host_containment\n"
                "identity = host_containment.host_containment_id(pid=os.getpid())\n"
                "host_containment.adopt_host_containment(identity)\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],"
                " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
                " stderr=subprocess.DEVNULL)\n"
                "me = psutil.Process()\n"
                "print(json.dumps({'pid': me.pid, 'process_create_time': me.create_time(),"
                " 'cmdline': me.cmdline(), 'containment': identity, 'child': child.pid}),"
                " flush=True)\n"
            )
            parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
                [str(self.python), "-c", script],
                stdout=subprocess.PIPE,
                text=True,
                cwd=str(sp.ROOT),
                creationflags=sp.NO_WINDOW,
            )
            report = json.loads(parent.stdout.readline())
            parent.wait(timeout=30)
            deadline = time.time() + 10
            while psutil.pid_exists(report["pid"]) and time.time() < deadline:
                time.sleep(0.05)

            def release() -> None:
                try:
                    psutil.Process(report["child"]).kill()
                except psutil.Error:
                    pass

            return {
                "lock_bytes": sp.write_record(runtime, self._record_of(report)),
                "orphan_pid": report["child"],
                "release": [release],
            }

        self._both_entry_points(
            "contained_record_with_live_orphan_is_unverified_cleanup",
            contained_orphan,
            lambda observed, fixture: (
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_cleanup_unverified"
                and str(fixture["orphan_pid"]) in observed["stderr_tail"]
                and observed["lock_bytes_unchanged"]
                and psutil.pid_exists(fixture["orphan_pid"])
            ),
        )

        # The explicit operator flow, end to end, per entry point.
        for transport in ("python", "shim"):
            directory = self.directory(f"case07-explicit-recovery-{transport}")
            runtime = directory / "runtime"
            runtime.mkdir()
            payload = sp.write_record(runtime, sp.admission_record(sp.dead_identity(self.python)))
            settings = self.settings(directory / "settings.json", transport, runtime)
            child, marker = self.sentinel(directory)
            if transport == "python":
                host = self.python_host(self.env(settings), directory / "host.log")
            else:
                host = self.shim_host(self.env(settings, child=child), directory / "host.log")
            refused = self.refused(host, runtime, marker)
            refused["lock_bytes_unchanged"] = sp.read_lock(runtime)[1] == payload
            show = self._cli("--show", str(runtime))
            digest = (show["json"] or {}).get("record_sha256")
            wrong = self._cli("--recover", str(runtime), "0" * 64, "--confirmed-by", "probe")
            still_kept = sp.read_lock(runtime)[1] == payload
            recover = self._cli(
                "--recover", str(runtime), str(digest), "--confirmed-by", "startup probe"
            )
            receipt = recover["json"] or {}
            archived = (
                runtime
                / "admission_recovered"
                / str((receipt.get("record") or {}).get("archived_as"))
            )
            archive_matches = archived.is_file() and archived.read_bytes() == payload
            lock_gone = not (runtime / sp.LOCK_NAME).exists()
            if transport == "python":
                again = self.python_host(self.env(settings), directory / "host-again.log")
            else:
                again = self.shim_host(
                    self.env(settings, child=child), directory / "host-again.log"
                )
            lock = sp.wait_for_owner(runtime, again, timeout=TIMEOUT_EXIT)
            admitted = sp.lock_owned_by(lock, again)
            closed = again.close()
            observed = {
                "refused": refused,
                "show": show,
                "wrong_digest": wrong,
                "record_kept_after_wrong_digest": still_kept,
                "recover": recover,
                "archive_matches_record": archive_matches,
                "lock_removed": lock_gone,
                "admitted_after_recovery": admitted,
                "exit_after_recovery": closed,
            }
            confirmation = receipt.get("operator_confirmation") or {}
            observed["covered_by_confirmation"] = [
                item.get("evidence") for item in confirmation.get("covers") or []
            ]
            observed["listener_method"] = (
                (receipt.get("machine_verification") or {}).get("listeners") or {}
            ).get("method")
            self.check(
                f"case07.explicit_recovery_after_independent_confirmation_{transport}",
                refused["exit_code"] == 2
                and refused["reason_code"] == "admission_state_uncertain"
                and refused["lock_bytes_unchanged"]
                and show["exit_code"] == 0
                and bool(digest)
                and wrong["exit_code"] == 2
                and "admission_recovery_record_mismatch" in wrong["stderr_tail"]
                and still_kept
                and recover["exit_code"] == 0
                and receipt.get("outcome") == "recovered"
                and receipt.get("terminated_processes") == []
                and confirmation.get("confirmed_by") == "startup probe"
                and observed["covered_by_confirmation"] == ["descendants", "listeners"]
                and observed["listener_method"] == "not_machine_verified"
                and archive_matches
                and lock_gone
                and admitted
                and closed == 0,
                observed,
            )

        # The flow never lifts positive evidence.
        directory = self.directory("case07-explicit-recovery-live-member")
        runtime = directory / "runtime"
        runtime.mkdir()
        fixture = contained_orphan(runtime)
        try:
            digest = hashlib.sha256(fixture["lock_bytes"]).hexdigest()
            refused = self._cli(
                "--recover", str(runtime), digest, "--confirmed-by", "startup probe"
            )
            kept = sp.read_lock(runtime)[1] == fixture["lock_bytes"]
            alive = psutil.pid_exists(fixture["orphan_pid"])
            self.check(
                "case07.explicit_recovery_refuses_a_live_session_member",
                refused["exit_code"] == 2
                and "admission_cleanup_unverified" in refused["stderr_tail"]
                and str(fixture["orphan_pid"]) in refused["stderr_tail"]
                and kept
                and alive,
                {"refused": refused, "record_kept": kept, "orphan_alive": alive},
            )
        finally:
            for release in fixture["release"]:
                release()

        # A dead session leader that recorded no containment: its session
        # members are still attributable to it, so the flow refuses them.
        directory = self.directory("case07-explicit-recovery-session-member")
        runtime = directory / "runtime"
        runtime.mkdir()
        script = (
            "import json, os, psutil, subprocess, sys\n"
            "os.setsid()\n"
            "member = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],"
            " stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
            " stderr=subprocess.DEVNULL)\n"
            "me = psutil.Process()\n"
            "print(json.dumps({'pid': me.pid, 'process_create_time': me.create_time(),"
            " 'cmdline': me.cmdline(), 'member': member.pid}), flush=True)\n"
        )
        parent = subprocess.Popen(  # noqa: S603 - fixed interpreter argv
            [str(self.python), "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            cwd=str(sp.ROOT),
        )
        report = json.loads(parent.stdout.readline())
        parent.wait(timeout=30)
        deadline = time.time() + 10
        while psutil.pid_exists(report["pid"]) and time.time() < deadline:
            time.sleep(0.05)
        try:
            payload = sp.write_record(runtime, self._record_of(report))
            digest = hashlib.sha256(payload).hexdigest()
            refused = self._cli(
                "--recover", str(runtime), digest, "--confirmed-by", "startup probe"
            )
            kept = sp.read_lock(runtime)[1] == payload
            alive = psutil.pid_exists(report["member"])
            self.check(
                "case07.explicit_recovery_refuses_a_live_member_of_the_dead_hosts_session",
                refused["exit_code"] == 2
                and "admission_cleanup_unverified" in refused["stderr_tail"]
                and str(report["member"]) in refused["stderr_tail"]
                and kept
                and not (runtime / "admission_recovered").exists()
                and alive,
                {"refused": refused, "record_kept": kept, "member_alive": alive},
            )
        finally:
            try:
                psutil.Process(report["member"]).kill()
            except psutil.Error:
                pass

    # -- case 8 ------------------------------------------------------------

    def case08_settings_change(self) -> None:
        directory = self.directory("case08-python")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "python", runtime)
        wrapper = directory / "change_settings_during_admission.py"
        wrapper.write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import comsol_mcp.host_admission as admission\n"
            "original = admission.acquire\n"
            "def acquire(*args, **kwargs):\n"
            "    claim = original(*args, **kwargs)\n"
            "    path = Path(os.environ['COMSOL_MCP_SETTINGS_PATH'])\n"
            "    path.write_bytes(path.read_bytes() + b'\\n')\n"
            "    return claim\n"
            "admission.acquire = acquire\n"
            "from comsol_mcp.server import main\n"
            "main()\n",
            encoding="utf-8",
        )
        host = sp.Host(
            [str(self.python), str(wrapper)],
            self.env(settings, PYTHONPATH=str(sp.ROOT)),
            directory / "host.log",
        )
        self.cleanup.append(host.kill_tree)
        observed = self.refused(host, runtime, None)
        self.check(
            "case08.settings_change_during_admission_python",
            observed["exit_code"] == 2
            and observed["reason_code"] == "settings_changed_during_admission"
            and not observed["lock_present"],
            observed,
        )

        if os.name != "nt":
            return
        directory = self.directory("case08-shim")
        runtime = directory / "runtime"
        first = json.dumps(
            sp.settings_document(transport="shim", runtime=runtime, binary=self.binary), indent=2
        ).encode()
        second = first + b"\n"
        pipe = rf"\\.\pipe\comsol-mcp-probe-{uuid.uuid4().hex}"
        served: list[str] = []

        def serve() -> None:
            import _winapi
            import ctypes

            def instance():
                return _winapi.CreateNamedPipe(
                    pipe,
                    _winapi.PIPE_ACCESS_DUPLEX,
                    _winapi.PIPE_WAIT,
                    2,
                    65536,
                    65536,
                    0,
                    _winapi.NULL,
                )

            current = instance()
            for index, payload in enumerate((first, second)):
                _winapi.ConnectNamedPipe(current, _winapi.NULL)
                _winapi.WriteFile(current, payload)
                # Wait until the client has read every byte before the next
                # instance takes over the name.
                ctypes.WinDLL("kernel32").FlushFileBuffers(ctypes.c_void_p(current))
                upcoming = instance() if index == 0 else None
                _winapi.CloseHandle(current)
                served.append(f"snapshot-{index}")
                if upcoming is None:
                    break
                current = upcoming

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        time.sleep(0.2)
        child, marker = self.sentinel(directory)
        environment = self.env(Path(pipe), child=child)
        host = self.shim_host(environment, directory / "shim.log")
        observed = self.refused(host, runtime, marker)
        server.join(timeout=5)
        observed["snapshots_served"] = served
        self.check(
            "case08.settings_change_during_admission_shim",
            observed["exit_code"] == 2
            and observed["reason_code"] == "settings_changed_during_admission"
            and not observed["lock_present"]
            and served == ["snapshot-0", "snapshot-1"]
            and not observed["forbidden_worker_started"],
            observed,
        )

    def case08b_manifest_binding(self) -> None:
        """A discovery manifest is bound to the exact settings bytes."""
        directory = self.directory("case08-manifest")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "shim", runtime)
        manifest = directory / "manifest.json"
        generated = sp.generate_manifest(self.python, settings, manifest)
        child, marker = self.sentinel(directory)
        environment = self.env(settings, child=child, COMSOL_MCP_DISCOVERY_MANIFEST=str(manifest))
        host = self.shim_host(environment, directory / "bound.log")
        sp.wait_for_owner(runtime, host)
        served = host.initialize(timeout=30)
        code = host.close()
        self.check(
            "case08.bound_manifest_serves_discovery_without_python",
            generated.returncode == 0
            and served
            and code == 0
            and not marker.exists()
            and not (runtime / sp.LOCK_NAME).exists(),
            {
                "generated": generated.returncode,
                "served": served,
                "exit_code": code,
                "forbidden_worker_started": marker.exists(),
            },
        )
        document = json.loads(settings.read_text(encoding="utf-8"))
        document["discovery"] = {"pagination_enabled": False}
        sp.write_settings(settings, document)
        host = self.shim_host(environment, directory / "stale.log")
        observed = self.refused(host, runtime, marker)
        self.check(
            "case08.changed_settings_invalidate_the_manifest",
            observed["exit_code"] == 2
            and observed["reason_code"] == "manifest_stale"
            and not observed["lock_present"]
            and not observed["forbidden_worker_started"],
            observed,
        )

    # -- case 9 ------------------------------------------------------------

    def case09_configuration_only(self) -> None:
        sys.path.insert(0, str(sp.ROOT))
        from settings_gui import transport as gui_transport

        def no_processes(_attrs: object) -> Any:
            return iter(())  # The admission record only.

        directory = self.directory("case09")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "shim", runtime)
        idle = gui_transport.describe_modes(
            "shim", gui_transport.observe_hosts(runtime, process_iter=no_processes)
        )
        self.check(
            "case09.saved_shim_mode_is_not_reported_active_without_a_host",
            idle["configured"] == "shim"
            and idle["active_transport"] is None
            and idle["state"] == "none",
            idle,
        )
        self.settings(settings, "python", runtime)
        host = self.python_host(self.env(settings), directory / "python.log")
        # The host re-reads the settings digest after it publishes its record
        # and refuses a rewrite inside that window with
        # settings_changed_during_admission (the designed contract; case08
        # tests it). The record appearing is the start of that window, not
        # its end, so it is no signal that the settings may change. One
        # initialize round trip is: the server answers only after the
        # admission gate returned.
        lock = sp.wait_for_owner(runtime, host)
        owned = sp.lock_owned_by(lock, host)  # live query: capture before close
        served = host.initialize()
        self.settings(settings, "shim", runtime)
        pending = gui_transport.describe_modes(
            "shim", gui_transport.observe_hosts(runtime, process_iter=no_processes)
        )
        host.close()
        self.check(
            "case09.running_python_host_shown_with_pending_restart",
            served
            and owned
            and pending["active_transport"] == "python"
            and pending["pending_restart"] is True,
            {**pending, "host_served_before_the_settings_change": served},
        )
        test = subprocess.run(  # noqa: S603 - fixed pytest argv
            [
                str(self.python),
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                str(directory / "pytest"),
                "settings_gui/tests/test_app.py::test_transport_switch_behaviour_through_the_real_buttons",
            ],
            cwd=str(sp.ROOT),
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=sp.NO_WINDOW,
        )
        self.check(
            "case09.gui_real_buttons_save_configuration_only",
            test.returncode == 0,
            {"exit_code": test.returncode, "summary": test.stdout.strip().splitlines()[-1:]},
        )

    # -- case 10 -----------------------------------------------------------

    def case10_eof_crash_restart(self) -> None:
        import psutil

        directory = self.directory("case10-python")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "python", runtime)
        first = self.python_host(self.env(settings), directory / "first.log")
        lock = sp.wait_for_owner(runtime, first)
        first_owned = sp.lock_owned_by(lock, first)
        served = first.initialize()
        code = first.close()
        released = not (runtime / sp.LOCK_NAME).exists()
        second = self.python_host(self.env(settings), directory / "second.log")
        relock = sp.wait_for_owner(runtime, second)
        second_owned = sp.lock_owned_by(relock, second)
        code2 = second.close()
        self.check(
            "case10.python_normal_eof_releases_and_restart_is_admitted",
            first_owned
            and served
            and code == 0
            and released
            and second_owned
            and relock is not None
            and relock.get("recovered_from") is None
            and code2 == 0,
            {
                "first_owned": first_owned,
                "served": served,
                "first_exit": code,
                "released": released,
                "restart_owned": second_owned,
                "restart_recovered_from": (relock or {}).get("recovered_from"),
                "restart_exit": code2,
            },
        )

        crashed = self.python_host(self.env(settings), directory / "crashed.log")
        lock = sp.wait_for_owner(runtime, crashed)
        crashed_pid = lock.get("pid") if lock else None
        if crashed_pid:
            psutil.Process(crashed_pid).kill()
        crashed.wait_exit(30)
        stale_kept = (runtime / sp.LOCK_NAME).exists()
        restarted = self.python_host(self.env(settings), directory / "restarted.log")
        if os.name == "nt":
            # WIN-CONTAIN-01: the crashed host held the only job handle, so
            # its containment can no longer be read; automatic recovery must
            # refuse as UNKNOWN and keep the record.
            refused = self.refused(restarted, runtime, None)
            refused["lock_bytes_unchanged"] = (runtime / sp.LOCK_NAME).exists()
            self.check(
                "case10.python_crash_then_verified_recovery",
                stale_kept
                and refused["exit_code"] == 2
                and refused["reason_code"] == "admission_state_uncertain"
                and "containment cannot be read" in refused["stderr_tail"]
                and refused["lock_bytes_unchanged"],
                {"stale_kept": stale_kept, "refused": refused},
            )
        else:
            relock = sp.wait_for_owner(runtime, restarted)
            restarted_owned = sp.lock_owned_by(relock, restarted)
            recovered = (relock or {}).get("recovered_from") or {}
            code3 = restarted.close()
            self.check(
                "case10.python_crash_then_verified_recovery",
                stale_kept
                and restarted_owned
                and recovered.get("pid") == crashed_pid
                and "cleanup" in recovered
                and code3 == 0,
                {
                    "stale_kept": stale_kept,
                    "restarted_owned": restarted_owned,
                    "recovered_from": recovered,
                    "restart_exit": code3,
                },
            )

        directory = self.directory("case10-shim")
        runtime = directory / "runtime"
        settings = self.settings(directory / "settings.json", "shim", runtime)
        crashed = self.shim_host(
            self.env(settings, child=self.real_worker()), directory / "crashed.log"
        )
        sp.wait_for_lock(runtime)
        served = crashed.initialize(timeout=180)
        lock = sp.read_lock(runtime)[0] or {}
        workers = lock.get("owned_processes") or []
        worker_pid = workers[0]["pid"] if workers else None
        crashed.process.kill()  # TerminateProcess: no cleanup code runs in the shim.
        crashed.wait_exit(30)
        deadline = time.time() + 15
        while worker_pid and psutil.pid_exists(worker_pid) and time.time() < deadline:
            time.sleep(0.1)
        worker_gone = worker_pid is not None and not psutil.pid_exists(worker_pid)
        stale_kept = (runtime / sp.LOCK_NAME).exists()
        restarted = self.shim_host(
            self.env(settings, child=self.real_worker()), directory / "restarted.log"
        )
        if os.name == "nt":
            # WIN-CONTAIN-01: same as the python row above.
            refused = self.refused(restarted, runtime, None)
            refused["lock_bytes_unchanged"] = (runtime / sp.LOCK_NAME).exists()
            self.check(
                "case10.shim_crash_kills_contained_worker_then_verified_recovery",
                served
                and worker_gone
                and stale_kept
                and refused["exit_code"] == 2
                and refused["reason_code"] == "admission_state_uncertain"
                and "containment cannot be read" in refused["stderr_tail"]
                and refused["lock_bytes_unchanged"],
                {"served": served, "worker_gone": worker_gone, "refused": refused},
            )
            return
        deadline = time.time() + TIMEOUT_EXIT
        relock = None
        while time.time() < deadline:
            relock = sp.read_lock(runtime)[0]
            if relock and relock.get("pid") == restarted.pid:
                break
            if not restarted.alive():
                break
            time.sleep(0.1)
        recovered = (relock or {}).get("recovered_from") or {}
        code = restarted.close()
        owned = ((recovered.get("cleanup") or {}).get("owned_processes")) or []
        self.check(
            "case10.shim_crash_kills_contained_worker_then_verified_recovery",
            served
            and worker_gone
            and stale_kept
            and (relock or {}).get("pid") == restarted.pid
            and recovered.get("pid") == crashed.pid
            and any(item.get("pid") == worker_pid for item in owned)
            and code == 0,
            {
                "served": served,
                "worker_pid": worker_pid,
                "worker_gone": worker_gone,
                "stale_kept": stale_kept,
                "recovered_from": recovered,
                "restart_exit": code,
                "restart_stderr": restarted.stderr()[-400:],
            },
        )

    # -- driver ------------------------------------------------------------

    def run(self, only: list[str] | None = None) -> dict[str, Any]:
        started = time.time()
        steps = [
            self.case01_mismatch,
            self.case02_live_old_host,
            self.case03_simultaneous,
            self.case04_manual_shim,
            self.case05_interactive,
            self.case06_spoofed_worker,
            self.case07_ownership_evidence,
            self.case08_settings_change,
            self.case08b_manifest_binding,
            self.case09_configuration_only,
            self.case10_eof_crash_restart,
        ]
        if only:
            steps = [step for step in steps if any(step.__name__.startswith(o) for o in only)]
        errors: dict[str, str] = {}
        for step in steps:
            try:
                step()
            except Exception:  # noqa: BLE001 - a crashed case is a failed case
                import traceback

                errors[step.__name__] = traceback.format_exc()[-1500:]
                self.checks[f"{step.__name__}.completed"] = False
            finally:
                for release in reversed(self.cleanup):
                    try:
                        release()
                    except Exception:  # noqa: BLE001, S110 - best-effort teardown
                        pass
                self.cleanup.clear()
        return {
            "schema": "comsol_mcp.p06.startup_conflicts/2",
            "source": sp.source_identity(),
            "shim": self.binary,
            "platform": sys.platform,
            "duration_seconds": round(time.time() - started, 1),
            "solver_started": False,
            "checks": self.checks,
            "passed": bool(self.checks) and all(self.checks.values()) and not errors,
            "pass_count": sum(self.checks.values()),
            "total_count": len(self.checks),
            "errors": errors,
            "cases": self.cases,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--only", nargs="*", default=None, help="case prefixes, e.g. case08")
    args = parser.parse_args()
    work = args.work or args.output.parent / f"startup-{time.strftime('%Y%m%dT%H%M%S')}"
    work.mkdir(parents=True, exist_ok=False)
    results = Probe(args.shim, args.python, work).run(args.only)
    results["selected_cases"] = args.only or "all"
    results["work"] = str(work)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps(results["checks"], indent=2))
    print(f"passed={results['passed']} {results['pass_count']}/{results['total_count']}")
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
