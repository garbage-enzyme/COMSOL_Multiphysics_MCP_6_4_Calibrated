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
            dead = sp.dead_identity(self.python)
            sp.write_record(runtime, sp.admission_record(dead))
            return {"expect_admission": True, "dead_pid": dead["pid"]}

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

        def reused(runtime: Path) -> dict[str, Any]:
            helper = sp.sleeper(self.python)
            identity = sp.birth_identity(helper.pid)
            identity["process_create_time"] -= 1000.0
            sp.write_record(runtime, sp.admission_record(identity))
            return {"expect_admission": True, "reused_pid": helper.pid, "release": [helper.kill]}

        self._both_entry_points(
            "reused_pid_is_stale",
            reused,
            lambda observed, fixture: (
                observed["admitted"]
                and (observed["recovered_from"] or {}).get("pid") == fixture["reused_pid"]
                and observed["exit_code"] == 0
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

        self._both_entry_points(
            "live_descendant_of_stale_owner_is_unverified_cleanup",
            orphan,
            lambda observed, fixture: (
                observed["exit_code"] == 2
                and observed["reason_code"] == "admission_cleanup_unverified"
                and str(fixture["orphan_pid"]) in observed["stderr_tail"]
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
        directory = self.directory("case07-incomplete-inventory-python")
        runtime = directory / "runtime"
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
        sp.wait_for_lock(runtime)
        self.settings(settings, "shim", runtime)
        pending = gui_transport.describe_modes(
            "shim", gui_transport.observe_hosts(runtime, process_iter=no_processes)
        )
        host.close()
        self.check(
            "case09.running_python_host_shown_with_pending_restart",
            pending["active_transport"] == "python" and pending["pending_restart"] is True,
            pending,
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
