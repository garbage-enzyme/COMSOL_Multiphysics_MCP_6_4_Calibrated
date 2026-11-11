"""Installed-state shim probe (0.8.2 P06, batch I).

Drives the Rust shim through a full MCP session with a real discovery
manifest and the Python module server as the lazy worker. Verifies:

- static discovery (initialize + tools/list) is served without Python;
- the worker spawns exactly once on the first forwarded call;
- capabilities deployment identity is the INSTALLED site package;
- a warm forwarded call reuses the retained worker;
- clean disconnect exits 0 with no leftover process.

Solver-free; isolated settings/runtime; no production paths touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _settings_document(runtime: Path, models: Path, shim: Path) -> str:
    """Shim transport settings with the binary's own complete identity."""
    import shim_probe_support as sp

    document = sp.settings_document(transport="shim", runtime=runtime, models=models, shim=shim)
    document["discovery"] = {"pagination_enabled": False}
    return json.dumps(document)


async def _probe_session(shim: str, manifest: str, environment: dict[str, str]) -> dict[str, Any]:
    import psutil
    from mcp import Client, StdioServerParameters

    observations: dict[str, Any] = {}
    parameters = StdioServerParameters(command=shim, args=[], env=environment, cwd=str(ROOT))
    async with Client(parameters, read_timeout_seconds=60.0) as client:
        # Find the shim process tree.
        shim_pid = None
        for _ in range(100):
            for proc in psutil.Process().children(recursive=True):
                try:
                    name = (proc.name() or "").lower()
                    cmdline = " ".join(proc.cmdline() or []).lower()
                except psutil.NoSuchProcess, psutil.AccessDenied:
                    continue
                if "comsol-mcp-shim" in name or "comsol-mcp-shim" in cmdline:
                    shim_pid = proc.pid
                    break
            if shim_pid is not None:
                break
            await asyncio.sleep(0.1)
        observations["shim_found"] = shim_pid is not None

        # Static discovery: no python child should exist yet.
        if shim_pid is not None:
            try:
                parent = psutil.Process(shim_pid)
                py_before = [
                    c
                    for c in parent.children(recursive=True)
                    if "python" in (c.name() or "").lower()
                ]
                observations["python_free_discovery"] = len(py_before) == 0
            except psutil.NoSuchProcess:
                observations["python_free_discovery"] = False

        listed = await client.list_tools()
        observations["tool_count"] = len(listed.tools)

        # First forwarded call: spawns the worker.
        first = await client.call_tool("capabilities", {})
        payload = json.loads(first.content[0].text) if first.content else {}
        deployment = payload.get("deployment_identity", {})
        observations["first_call_ok"] = not getattr(
            first, "is_error", getattr(first, "isError", True)
        )
        observations["deployment_source"] = deployment.get("source_classification")
        observations["deployment_version"] = deployment.get("package_version")

        # The worker must exist now.
        if shim_pid is not None:
            await asyncio.sleep(0.5)
            try:
                parent = psutil.Process(shim_pid)
                py_after = [
                    c
                    for c in parent.children(recursive=True)
                    if "python" in (c.name() or "").lower()
                ]
                observations["worker_pids_after_first"] = [c.pid for c in py_after]
                # Exactly one worker, and it did not exist before the call.
                observations["worker_spawned_once"] = len(py_after) == 1 and len(py_before) == 0
            except psutil.NoSuchProcess:
                observations["worker_spawned_once"] = False

        # Warm call: retained worker - still exactly one, no extra spawn.
        warm = await client.call_tool("capabilities", {})
        observations["warm_call_ok"] = not getattr(warm, "is_error", getattr(warm, "isError", True))
        if shim_pid is not None:
            await asyncio.sleep(0.5)
            try:
                parent = psutil.Process(shim_pid)
                py_warm = [
                    c
                    for c in parent.children(recursive=True)
                    if "python" in (c.name() or "").lower()
                ]
                observations["worker_pids_after_warm"] = [c.pid for c in py_warm]
            except psutil.NoSuchProcess:
                observations["worker_pids_after_warm"] = None

    # After disconnect: shim must be gone.
    if shim_pid is not None:
        await asyncio.sleep(1.0)
        observations["shim_exited_cleanly"] = not psutil.pid_exists(shim_pid)
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="shim-probe-", dir=str(ROOT)) as raw:
        workdir = Path(raw)
        settings = workdir / "settings.json"
        runtime = workdir / "runtime"
        models = workdir / "models"
        models.mkdir()
        runtime.mkdir()
        settings.write_text(_settings_document(runtime, models, args.shim), encoding="utf-8")
        manifest_path = workdir / "manifest.json"

        base = {k: v for k, v in os.environ.items() if not k.startswith("COMSOL_MCP_")}
        base.pop("PYTHONPATH", None)

        # Generate the discovery manifest under the same environment.
        generate = subprocess.run(
            [
                sys.executable,
                "-m",
                "comsol_mcp.discovery_manifest",
                "generate",
                "--output",
                str(manifest_path),
            ],
            cwd=str(ROOT),
            env={
                **base,
                "COMSOL_MCP_SETTINGS_PATH": str(settings),
                "COMSOL_MCP_PROFILE": "core",
            },
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        if generate.returncode != 0:
            print(
                json.dumps(
                    {"error": "manifest generation failed", "stderr": generate.stderr[-400:]}
                )
            )
            return 2

        environment = dict(os.environ)
        environment["COMSOL_MCP_DISCOVERY_MANIFEST"] = str(manifest_path)
        environment["COMSOL_MCP_SHIM_CHILD"] = json.dumps(
            [str(args.python), "-m", "comsol_mcp.server"]
        )
        environment["COMSOL_MCP_SETTINGS_PATH"] = str(settings)
        environment["COMSOL_MCP_PROFILE"] = "core"
        environment["PYTHONIOENCODING"] = "utf-8"

        result = asyncio.run(_probe_session(str(args.shim), str(manifest_path), environment))

    checks = {
        "shim_found": result.get("shim_found") is True,
        "python_free_discovery": result.get("python_free_discovery") is True,
        "tool_count_positive": bool(result.get("tool_count")),
        "first_call_ok": result.get("first_call_ok") is True,
        # Batch I: the probe must prove the INSTALLED state. A source-tree
        # classification here means the probe ran the wrong interpreter.
        "deployment_source_is_installed": result.get("deployment_source")
        == "installed_site_package",
        "worker_spawned_once": result.get("worker_spawned_once") is True,
        "warm_call_ok": result.get("warm_call_ok") is True,
        "worker_retained_exactly_one": result.get("worker_pids_after_warm")
        == result.get("worker_pids_after_first")
        and isinstance(result.get("worker_pids_after_warm"), list)
        and len(result["worker_pids_after_warm"]) == 1,
        "shim_exited_cleanly": result.get("shim_exited_cleanly") is True,
    }
    payload = {
        "schema_name": "comsol_mcp.installed_shim_probe",
        "schema_version": "1.0.0",
        "observations": result,
        "checks": checks,
        "passed": all(checks.values()),
        "shim": str(args.shim),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(checks, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
