"""P00 scenario runner: cold-launch memory and latency measurement for COMSOL MCP.

This repository-only script drives the installed ``comsol-mcp`` console entry
point over real MCP stdio transport, samples per-process memory identities at
defined stages, and writes evidence artifacts into an external evidence root.

The runner is measurement-only. It does not modify server behavior, does not
touch production settings or runtime directories, and never starts COMSOL.
Every launched process tree is verified terminal before the run ends.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

PAGE_SIZE = 4096
SCHEMA = "comsol_mcp.p00.scenario_runner/1"
COMMIT_CEILING = 0.75
SOLVER_PROCESS_NAMES = ("comsolmphserver", "comsol", "comsolui")
IDLE_HOLD_S = 10.0
EXIT_GRACE_S = 8.0
PREDICTED_SERVER_COMMIT_BYTES = 1_700_000_000
_PER_SESSION_OVERRIDE_MB: float | None = None


class _PERF_INFO(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD)] + [
        (name, ctypes.c_size_t)
        for name in (
            "CommitTotal",
            "CommitLimit",
            "CommitPeak",
            "PhysicalTotal",
            "PhysicalAvailable",
            "SystemCache",
            "KernelTotal",
            "KernelPaged",
            "KernelNonpaged",
            "Handles",
            "ProcessCount",
            "ThreadCount",
        )
    ]


def machine_commit() -> dict[str, Any]:
    """Return whole-host commit, physical memory, and process counts."""
    info = _PERF_INFO()
    info.cb = ctypes.sizeof(_PERF_INFO)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        raise RuntimeError("GetPerformanceInfo failed")
    vm = psutil.virtual_memory()
    return {
        "commit_total_bytes": info.CommitTotal * PAGE_SIZE,
        "commit_limit_bytes": info.CommitLimit * PAGE_SIZE,
        "commit_fraction": round(info.CommitTotal / info.CommitLimit, 4),
        "physical_available_bytes": vm.available,
        "physical_total_bytes": vm.total,
        "process_count": info.ProcessCount,
        "thread_count": info.ThreadCount,
        "handle_count": info.Handles,
    }


def solver_processes_present() -> list[str]:
    """Return names of live COMSOL solver processes, if any."""
    present: list[str] = []
    for proc in psutil.process_iter(["name"]):
        name = (proc.info.get("name") or "").lower()
        if name.removesuffix(".exe") in SOLVER_PROCESS_NAMES:
            present.append(proc.info.get("name") or name)
    return present


@dataclass(frozen=True)
class Variant:
    """One measurement settings and environment variant."""

    name: str
    profile: str
    manuals_root: str | None
    lexical_enabled: bool
    env: dict[str, str] = field(default_factory=dict)


MANUALS_ROOT = "D:/COMSOL64/Multiphysics/doc"
LEXICAL_INDEX = "D:/comsol_docs_fts/manuals.sqlite3"

VARIANTS: dict[str, Variant] = {
    "prod-like": Variant("prod-like", "wave_optics", MANUALS_ROOT, True),
    "prod-like-blas1": Variant(
        "prod-like-blas1", "wave_optics", MANUALS_ROOT, True,
        {"OPENBLAS_NUM_THREADS": "1"},
    ),
    "wave-nolex": Variant("wave-nolex", "wave_optics", MANUALS_ROOT, False),
    "wave-nomanual": Variant("wave-nomanual", "wave_optics", None, True),
    "core-minimal": Variant("core-minimal", "core", None, False),
}


def settings_document(variant: Variant, root: Path) -> dict[str, Any]:
    """Build one isolated settings document for the variant."""
    runtime_dir = root / "runtime" / variant.name
    return {
        "schema_name": "comsol_mcp.settings",
        "schema_version": "1.3.0",
        "profile": {"name": variant.profile},
        "runtime": {"directory": str(runtime_dir), "jobs_directory": None},
        "paths": {
            "model_read_roots": [str(root / "models")],
            "artifact_write_root": str(root / "artifacts"),
        },
        "shared_server": {"enabled": False},
        "discovery": {"pagination_enabled": False},
        "model_manager": {"enabled": False, "upload_enabled": False},
        "evidence_integrity": {
            "checks": {
                "outcome_contract_validation": True,
                "artifact_chain_verification": True,
                "summary_claim_verification": True,
                "producer_driver_compatibility": True,
            }
        },
        "manuals": {"root": variant.manuals_root},
        "lexical_docs": {
            "enabled": variant.lexical_enabled,
            "index_path": LEXICAL_INDEX if variant.lexical_enabled else None,
        },
        "semantic_docs": {"enabled": False, "root": None, "model_path": None},
        "ownership": {"owner": None},
        "java": {"java_home": None, "jdk_home": None},
        "comsol": {"installation_root": None},
        "gui": {"language": "zh-cn", "scale": "system"},
    }


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def run_stamp() -> str:
    """Filesystem-safe UTC stamp for run identifiers."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class SampleWriter:
    """Append timestamped per-process samples to process-samples.jsonl."""

    def __init__(self, root: Path) -> None:
        self.path = root / "process-samples.jsonl"

    def write(
        self,
        run_id: str,
        stage: str,
        processes: list[dict[str, Any]],
        machine: dict[str, Any],
        extra: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        with self.path.open("a", encoding="utf-8") as handle:
            for proc in processes:
                record = {
                    "schema": SCHEMA,
                    "run_id": run_id,
                    "stage": stage,
                    "captured_at_utc": now,
                    "machine": machine,
                    "process": proc,
                }
                if extra:
                    record["extra"] = extra
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def process_identity(proc: psutil.Process) -> dict[str, Any]:
    """Collect read-only memory and identity fields for one process."""
    with proc.oneshot():
        memory = proc.memory_info()
        try:
            full = proc.memory_full_info()
            uss = full.uss
        except Exception:
            uss = None
        try:
            cpu = proc.cpu_times()
            cpu_s = round(cpu.user + cpu.system, 3)
        except Exception:
            cpu_s = None
        try:
            cmdline = proc.cmdline()
        except Exception:
            cmdline = None
        return {
            "pid": proc.pid,
            "ppid": proc.ppid(),
            "name": proc.name(),
            "create_time_utc": datetime.fromtimestamp(
                proc.create_time(), tz=timezone.utc
            ).isoformat(),
            "cmdline": cmdline,
            "private_commit_bytes": memory.vms,
            "rss_bytes": memory.rss,
            "uss_bytes": uss,
            "cpu_time_s": cpu_s,
            "num_threads": proc.num_threads(),
            "num_handles": proc.num_handles(),
        }


def descendants(root_pid: int) -> list[psutil.Process]:
    """Return live descendant processes of one root pid."""
    children_of: dict[int, list[int]] = {}
    table: dict[int, psutil.Process] = {}
    for proc in psutil.process_iter(["ppid"]):
        ppid = proc.info.get("ppid")
        if ppid is None:
            continue
        children_of.setdefault(ppid, []).append(proc.pid)
        table[proc.pid] = proc
    found: list[psutil.Process] = []
    frontier = [root_pid]
    seen = {root_pid}
    while frontier:
        current = frontier.pop()
        for child in children_of.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            proc = table.get(child)
            if proc is not None:
                found.append(proc)
                frontier.append(child)
    return found


def snapshot_tree(
    run_id: str, stage: str, root_pid: int, writer: SampleWriter
) -> dict[str, Any]:
    """Sample one process tree plus machine state at a named stage."""
    machine = machine_commit()
    identities: list[dict[str, Any]] = []
    try:
        identities.append(process_identity(psutil.Process(root_pid)))
    except psutil.NoSuchProcess:
        pass
    for proc in descendants(root_pid):
        try:
            identities.append(process_identity(proc))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    writer.write(run_id, stage, identities, machine)
    return {
        "stage": stage,
        "captured_at_utc": utc_now(),
        "process_count": len(identities),
        "tree_private_commit_bytes": sum(
            p["private_commit_bytes"] or 0 for p in identities
        ),
        "machine_commit_fraction": machine["commit_fraction"],
    }


def append_run_summary(root: Path, summary: dict[str, Any]) -> None:
    with (root / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")


def preflight(
    sessions: int,
    variant: Variant | None = None,
    ceiling: float = COMMIT_CEILING,
) -> dict[str, Any]:
    """Refuse measurement when policy gates are not satisfied."""
    machine = machine_commit()
    machine["effective_ceiling"] = ceiling
    solvers = solver_processes_present()
    per_session = PREDICTED_SERVER_COMMIT_BYTES
    if _PER_SESSION_OVERRIDE_MB is not None:
        per_session = int(_PER_SESSION_OVERRIDE_MB * 1_000_000)
    elif variant is not None and variant.env.get("OPENBLAS_NUM_THREADS") == "1":
        per_session = 300_000_000
    predicted = (
        machine["commit_total_bytes"] + sessions * per_session
    ) / machine["commit_limit_bytes"]
    if machine["commit_fraction"] > ceiling:
        machine["refusal"] = "commit_fraction_above_ceiling"
    elif solvers:
        machine["refusal"] = "solver_processes_present"
        machine["solver_processes"] = solvers
    elif predicted > ceiling:
        machine["refusal"] = "predicted_fraction_above_ceiling"
        machine["predicted_fraction"] = round(predicted, 4)
    return machine


async def wait_for_server_tree(timeout_s: float = 20.0) -> int | None:
    """Find the comsol-mcp child tree root spawned by this process."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for proc in descendants(os.getpid()):
            try:
                cmdline = " ".join(proc.cmdline() or []).lower()
            except Exception:
                continue
            if "comsol-mcp" in (proc.name() or "").lower() or "comsol-mcp" in cmdline or "comsol_mcp.server" in cmdline:
                return proc.pid
        await asyncio.sleep(0.2)
    return None


async def wait_tree_exit(root_pid: int, grace_s: float) -> dict[str, Any]:
    """Wait for a tree to exit; terminate only our own leftovers."""
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not psutil.pid_exists(root_pid) and not descendants(root_pid):
            return {"exited_clean": True, "forced": False}
        await asyncio.sleep(0.25)
    victims = descendants(root_pid)
    try:
        root = psutil.Process(root_pid)
        victims.append(root)
    except psutil.NoSuchProcess:
        pass
    for proc in victims:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            continue
    gone, alive = psutil.wait_procs(victims, timeout=3)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            continue
    psutil.wait_procs(alive, timeout=3)
    return {
        "exited_clean": False,
        "forced": True,
        "terminated_pids": [p.pid for p in victims],
    }


async def drive_session(
    command: str,
    env: dict[str, str],
    run_id: str,
    writer: SampleWriter,
    errlog_path: Path,
    server_args: list[str] | None = None,
) -> dict[str, Any]:
    """Drive one MCP stdio session with staged sampling and latencies."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    latencies: dict[str, Any] = {}
    stages: list[dict[str, Any]] = []
    errlog = open(errlog_path, "ab")
    params = StdioServerParameters(
        command=command, args=server_args or [], env=env, errlog=errlog
    )
    async with stdio_client(params) as (reader, writer_transport):
        server_pid = await wait_for_server_tree()
        latencies["server_tree_root_pid"] = server_pid
        if server_pid is None:
            return {"run_id": run_id, "error": "server_tree_not_found", "stages": stages}
        stages.append(snapshot_tree(run_id, "spawned", server_pid, writer))
        async with ClientSession(reader, writer_transport) as session:
            start = time.perf_counter()
            initialized = await asyncio.wait_for(session.initialize(), timeout=90)
            latencies["initialize_s"] = round(time.perf_counter() - start, 4)
            server_info = getattr(initialized, "server_info", None) or getattr(
                initialized, "serverInfo", None
            )
            latencies["server_version"] = getattr(server_info, "version", None)
            stages.append(snapshot_tree(run_id, "after_initialize", server_pid, writer))

            start = time.perf_counter()
            tools = await asyncio.wait_for(session.list_tools(), timeout=120)
            latencies["tools_list_s"] = round(time.perf_counter() - start, 4)
            latencies["tool_count"] = len(tools.tools)
            stages.append(snapshot_tree(run_id, "after_discovery", server_pid, writer))

            start = time.perf_counter()
            first = await asyncio.wait_for(
                session.call_tool("capabilities", {}), timeout=120
            )
            latencies["first_call_s"] = round(time.perf_counter() - start, 4)
            latencies["first_call_ok"] = not getattr(
                first, "is_error", getattr(first, "isError", True)
            )
            stages.append(snapshot_tree(run_id, "after_first_call", server_pid, writer))

            start = time.perf_counter()
            warm = await asyncio.wait_for(
                session.call_tool("capabilities", {}), timeout=120
            )
            latencies["warm_call_s"] = round(time.perf_counter() - start, 4)
            latencies["warm_call_ok"] = not getattr(
                warm, "is_error", getattr(warm, "isError", True)
            )
            stages.append(snapshot_tree(run_id, "after_warm_call", server_pid, writer))

            await asyncio.sleep(IDLE_HOLD_S)
            stages.append(snapshot_tree(run_id, "idle_hold_end", server_pid, writer))
    cleanup = await wait_tree_exit(server_pid, EXIT_GRACE_S) if server_pid else {}
    stages.append(
        {
            "stage": "after_disconnect",
            "captured_at_utc": utc_now(),
            "process_count": len(descendants(server_pid)) if server_pid else 0,
            "tree_private_commit_bytes": 0,
            "machine_commit_fraction": machine_commit()["commit_fraction"],
        }
    )
    errlog.close()
    return {
        "run_id": run_id,
        "stages": stages,
        "latencies": latencies,
        "cleanup": cleanup,
    }


async def run_server_scenario(
    args: argparse.Namespace, variant: Variant, root: Path, writer: SampleWriter
) -> None:
    """Run the cold-launch server scenario for one variant."""
    command = args.command or str(Path(sys.executable).parent / "comsol-mcp.exe")
    server_args = args.server_args.split() if args.server_args else None
    settings_path = root / f"settings-{variant.name}.json"
    for launch in range(args.launches):
        run_id = f"{variant.name}-{args.sessions}s-l{launch:02d}-{run_stamp()}"
        machine = preflight(args.sessions, variant, args.ceiling)
        if "refusal" in machine:
            append_run_summary(
                root,
                {
                    "schema": SCHEMA,
                    "run_id": run_id,
                    "variant": variant.name,
                    "scenario": "server",
                    "refused": machine["refusal"],
                    "machine": machine,
                },
            )
            print(f"REFUSED {run_id}: {machine['refusal']}")
            return
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        base_env = dict(os.environ)
        base_env.update(variant.env)
        base_env["COMSOL_MCP_SETTINGS_PATH"] = str(settings_path)
        if args.sessions == 1:
            results = [
                await drive_session(
                    command,
                    base_env,
                    run_id,
                    writer,
                    run_dir / "server-stderr.log",
                    server_args,
                )
            ]
        else:
            tasks = [
                drive_session(
                    command,
                    base_env,
                    f"{run_id}-s{index}",
                    writer,
                    run_dir / f"server-stderr-s{index}.log",
                    server_args,
                )
                for index in range(args.sessions)
            ]
            results = await asyncio.gather(*tasks)
        result = {
            "schema": SCHEMA,
            "scenario": "server",
            "variant": variant.name,
            "sessions": args.sessions,
            "launch_index": launch,
            "command": command,
            "server_args": server_args or [],
            "settings_path": str(settings_path),
            "env_overrides": variant.env,
                "machine_before": machine,
                "machine_after": machine_commit(),
                "ceiling_override": None if args.ceiling == COMMIT_CEILING else args.ceiling,
                "session_results": results,
        }
        append_run_summary(root, result)
        print(f"DONE {run_id}: " + json.dumps(result.get("latencies", {})))
        await asyncio.sleep(2.0)


HOLD_SUFFIX = "; import time as _t; _t.sleep(30)"

IMPORT_STAGES: list[tuple[str, str]] = [
    ("bare", "pass" + HOLD_SUFFIX),
    ("mcp-sdk", "import mcp.server" + HOLD_SUFFIX),
    ("numpy", "import numpy" + HOLD_SUFFIX),
    ("numpy-scipy", "import numpy, scipy.interpolate" + HOLD_SUFFIX),
    ("server-import", "import comsol_mcp.server" + HOLD_SUFFIX),
    ("register-wave", "import comsol_mcp.server as s; s.register_all_tools(profile='wave_optics')" + HOLD_SUFFIX),
    ("register-core", "import comsol_mcp.server as s; s.register_all_tools(profile='core')" + HOLD_SUFFIX),
]


async def run_import_scenario(
    args: argparse.Namespace, variant: Variant, root: Path, writer: SampleWriter
) -> None:
    """Run import-chain attribution probes in fresh child processes."""
    settings_path = root / f"settings-{variant.name}.json"
    for label, code in IMPORT_STAGES:
        run_id = f"import-{variant.name}-{label}-{run_stamp()}"
        machine = preflight(sessions=1)
        if "refusal" in machine:
            append_run_summary(
                root,
                {
                    "schema": SCHEMA,
                    "run_id": run_id,
                    "scenario": "import",
                    "refused": machine["refusal"],
                    "machine": machine,
                },
            )
            print(f"REFUSED {run_id}: {machine['refusal']}")
            return
        env = dict(os.environ)
        env.update(variant.env)
        env["COMSOL_MCP_SETTINGS_PATH"] = str(settings_path)
        start = time.perf_counter()
        child = psutil.Popen(
            [sys.executable, "-I", "-c", code or "pass"],
            env=env,
            cwd=str(root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        elapsed_spawn = time.perf_counter() - start
        await asyncio.sleep(4.0)
        stages = [snapshot_tree(run_id, "steady", child.pid, writer)]
        try:
            child.terminate()
            child.wait(timeout=5)
        except psutil.NoSuchProcess:
            pass
        except psutil.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        cleanup = {"terminated_by_runner": True, "returncode": child.returncode}
        append_run_summary(
            root,
            {
                "schema": SCHEMA,
                "scenario": "import",
                "run_id": run_id,
                "variant": variant.name,
                "stage_label": label,
                "code": code,
                "spawn_s": round(elapsed_spawn, 4),
                "stages": stages,
                "cleanup": cleanup,
                "machine": machine,
            },
        )
        print(f"DONE {run_id}: commit={stages[0]['tree_private_commit_bytes'] / 2**20:.1f}MB")
        await asyncio.sleep(1.0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--scenario", choices=("server", "import"), required=True)
    parser.add_argument("--variant", default="prod-like", choices=sorted(VARIANTS))
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--launches", type=int, default=3)
    parser.add_argument("--command", default=None)
    parser.add_argument(
        "--server-args",
        default=None,
        help="Space-separated server arguments; use with --command to launch a "
        "source checkout, for example '-m comsol_mcp.server' with the "
        "repository root as the working directory.",
    )
    parser.add_argument(
        "--per-session-commit-mb",
        type=float,
        default=None,
        help="Override the predicted per-session commit estimate in MB when "
        "the server under test is known to commit less than the legacy "
        "default (for example a source checkout with the BLAS bound).",
    )
    parser.add_argument(
        "--ceiling",
        type=float,
        default=COMMIT_CEILING,
        help="Effective commit-fraction ceiling for this invocation. "
        "Values above the standing ceiling require explicit caller "
        "authorization and are recorded in every run summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    global _PER_SESSION_OVERRIDE_MB
    _PER_SESSION_OVERRIDE_MB = args.per_session_commit_mb
    if args.sessions < 1 or args.launches < 1:
        raise SystemExit("sessions and launches must be positive")
    root: Path = args.evidence_root
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("models", "artifacts", "runs"):
        (root / sub).mkdir(exist_ok=True)
    variant = VARIANTS[args.variant]
    settings_path = root / f"settings-{variant.name}.json"
    settings_path.write_text(
        json.dumps(settings_document(variant, root), indent=2), encoding="utf-8"
    )
    writer = SampleWriter(root)
    if args.scenario == "server":
        asyncio.run(run_server_scenario(args, variant, root, writer))
    else:
        asyncio.run(run_import_scenario(args, variant, root, writer))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
