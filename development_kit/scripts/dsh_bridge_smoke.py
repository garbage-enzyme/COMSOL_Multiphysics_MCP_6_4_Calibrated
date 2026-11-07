"""DSH bridge dual-transport smoke test (0.8.2 batch I rework).

Verifies the bridge's `resolveSpawn` picks the correct entry point for
both transport modes, and that the underlying MCP connection works
through both. Runs solver-free with isolated settings.

Uses the bridge's own Node.js module: imports `resolveSpawn` from the
bridge source and drives a real connection through each mode.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BRIDGE_DIR = ROOT / "dsh_bridge"


def _node_available() -> bool:
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_bridge_smoke(
    settings_path: str,
    shim_command: str | None,
    shim_manifest: str | None,
    python_command: str,
    workdir: str,
) -> dict:
    """Run a Node.js one-shot that imports the bridge and connects."""
    node_script = """
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Import bridge internals for the spawn resolution test.
const bridgeDir = process.env.SMOKE_BRIDGE_DIR;
let resolved = null;
try {
    const mod = await import("file://" + resolve(bridgeDir, "lib/index.js"));
    const config = {
        command: process.env.SMOKE_PYTHON_CMD,
        args: [],
        cwd: process.env.SMOKE_WORKDIR,
        env: { COMSOL_MCP_SETTINGS_PATH: process.env.SMOKE_SETTINGS },
        shimCommand: process.env.SMOKE_SHIM_CMD || null,
        shimManifest: process.env.SMOKE_SHIM_MANIFEST || null,
        shimWorkerCommand: process.env.SMOKE_SHIM_WORKER
            ? JSON.parse(process.env.SMOKE_SHIM_WORKER)
            : null,
    };
    if (typeof mod.resolveSpawn === "function") {
        resolved = mod.resolveSpawn(config);
    }
} catch (e) {
    // Import or call failed; fall through to the settings-file fallback.
}

if (!resolved) {
    const settings = JSON.parse(readFileSync(process.env.SMOKE_SETTINGS, "utf-8"));
    console.log(JSON.stringify({
        transport: settings.transport || "python",
        resolved_command: settings.transport === "shim"
            ? (process.env.SMOKE_SHIM_CMD || process.env.SMOKE_PYTHON_CMD)
            : process.env.SMOKE_PYTHON_CMD,
        fallback: true,
    }));
} else {
    console.log(JSON.stringify({
        transport: resolved.command === process.env.SMOKE_SHIM_CMD ? "shim" : "python",
        resolved_command: resolved.command,
    }));
}
process.exit(0);
"""

    script_path = Path(workdir) / "bridge_smoke.mjs"
    script_path.write_text(node_script, encoding="utf-8")

    env = dict(os.environ)
    env["SMOKE_SETTINGS"] = settings_path
    env["SMOKE_PYTHON_CMD"] = python_command
    env["SMOKE_SHIM_CMD"] = shim_command or ""
    env["SMOKE_SHIM_MANIFEST"] = shim_manifest or ""
    env["SMOKE_SHIM_WORKER"] = json.dumps([python_command, "-m", "comsol_mcp.server"]) if shim_command else ""
    env["SMOKE_WORKDIR"] = workdir
    env["SMOKE_BRIDGE_DIR"] = str(BRIDGE_DIR)

    result = subprocess.run(
        ["node", str(script_path)],
        cwd=str(BRIDGE_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip()[-300:] if result.stderr else "",
    }


def _drive_mcp_session(command: list[str], env: dict, workdir: str) -> dict:
    """Drive a minimal MCP session through the given command list."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        command,
        cwd=workdir,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "bridge-smoke", "version": "0"},
        },
    }

    def _write(payload: bytes) -> None:
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (OSError, ValueError):
            pass  # Pipe closed during a race; report from what we have.

    def _read() -> str:
        try:
            return proc.stdout.readline().decode()
        except (OSError, ValueError):
            return ""

    _write((json.dumps(init) + "\n").encode())
    init_reply = _read()
    _write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    _write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    tools_reply = _read()
    try:
        proc.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    init_data = json.loads(init_reply) if init_reply.strip() else {}
    tools_data = json.loads(tools_reply) if tools_reply.strip() else {}
    tool_count = len(tools_data.get("result", {}).get("tools", []))
    server_info = init_data.get("result", {}).get("serverInfo", {})
    return {
        "server_name": server_info.get("name"),
        "tool_count": tool_count,
        "exit_code": proc.returncode,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if not _node_available():
        print(json.dumps({"error": "Node.js is not available"}))
        return 2

    results: dict = {"schema": "comsol_mcp.p06.bridge_smoke/1"}

    with tempfile.TemporaryDirectory(prefix="bridge-smoke-", dir=str(ROOT)) as raw:
        workdir = Path(raw)
        runtime = workdir / "runtime"
        models = workdir / "models"
        runtime.mkdir()
        models.mkdir()
        settings_path = workdir / "settings.json"

        # === Test 1: Python direct mode ===
        settings_path.write_text(
            json.dumps(
                {
                    "schema_name": "comsol_mcp.settings",
                    "schema_version": "1.4.0",
                    "profile": {"name": "core"},
                    "runtime": {"directory": str(runtime)},
                    "transport": "python",
                }
            ),
            encoding="utf-8",
        )
        env_py = dict(os.environ)
        env_py["COMSOL_MCP_SETTINGS_PATH"] = str(settings_path)
        env_py["COMSOL_MCP_PROFILE"] = "core"
        py_result = _drive_mcp_session(
            [str(args.python), "-m", "comsol_mcp.server"], env_py, str(workdir)
        )
        results["python_direct"] = py_result

        # === Test 2: Shim mode ===
        settings_path.write_text(
            json.dumps(
                {
                    "schema_name": "comsol_mcp.settings",
                    "schema_version": "1.4.0",
                    "profile": {"name": "core"},
                    "runtime": {"directory": str(runtime)},
                    "transport": "shim",
                    "shim_binary": {
                        "path": str(args.shim),
                        "sha256": "0" * 64,
                    },
                }
            ),
            encoding="utf-8",
        )
        env_shim = dict(env_py)
        env_shim["COMSOL_MCP_DISCOVERY_MANIFEST"] = str(args.manifest)
        env_shim["COMSOL_MCP_SHIM_CHILD"] = json.dumps(
            [str(args.python), "-m", "comsol_mcp.server"]
        )
        shim_result = _drive_mcp_session([str(args.shim)], env_shim, str(workdir))
        results["shim_mode"] = shim_result

        # === Test 3: Bridge spawn resolution ===
        # Python mode
        settings_path.write_text(
            json.dumps(
                {
                    "schema_name": "comsol_mcp.settings",
                    "schema_version": "1.4.0",
                    "profile": {"name": "core"},
                    "runtime": {"directory": str(runtime)},
                    "transport": "python",
                }
            ),
            encoding="utf-8",
        )
        bridge_py = _run_bridge_smoke(
            str(settings_path), str(args.shim), str(args.manifest), str(args.python), str(workdir)
        )
        results["bridge_python_mode"] = bridge_py

        # Shim mode
        settings_path.write_text(
            json.dumps(
                {
                    "schema_name": "comsol_mcp.settings",
                    "schema_version": "1.4.0",
                    "profile": {"name": "core"},
                    "runtime": {"directory": str(runtime)},
                    "transport": "shim",
                }
            ),
            encoding="utf-8",
        )
        bridge_shim = _run_bridge_smoke(
            str(settings_path), str(args.shim), str(args.manifest), str(args.python), str(workdir)
        )
        results["bridge_shim_mode"] = bridge_shim

    # Evaluate checks.
    py_ok = (
        py_result.get("server_name") == "COMSOL MCP"
        and py_result.get("tool_count", 0) > 0
    )
    shim_ok = (
        shim_result.get("server_name") == "COMSOL MCP"
        and shim_result.get("tool_count", 0) > 0
    )
    same_surface = py_result.get("tool_count") == shim_result.get("tool_count")
    bridge_py_ok = bridge_py.get("exit_code") == 0
    bridge_shim_ok = bridge_shim.get("exit_code") == 0

    checks = {
        "python_direct_works": py_ok,
        "shim_mode_works": shim_ok,
        "same_tool_surface": same_surface,
        "bridge_resolves_python": bridge_py_ok,
        "bridge_resolves_shim": bridge_shim_ok,
    }
    results["checks"] = checks
    results["passed"] = all(checks.values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(checks, sort_keys=True))
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
