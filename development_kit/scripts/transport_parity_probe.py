"""Direct-Python versus shim parity probe (0.8.2 P06, batch I).

Compares the capabilities tool result through both transports under the
same isolated settings. Verifies identical deployment identity, profile,
and tool surface metadata.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sp = importlib.import_module("shim_probe_support")


def _capabilities_via_subprocess(env: dict[str, str], command: list[str]) -> dict:
    """Call capabilities through raw stdio and return the parsed result."""
    import json as _json

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
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
            "clientInfo": {"name": "parity-probe", "version": "0"},
        },
    }
    proc.stdin.write((_json.dumps(init) + "\n").encode())
    proc.stdin.flush()
    proc.stdout.readline()  # initialize reply
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "capabilities", "arguments": {}},
    }
    proc.stdin.write((_json.dumps(call) + "\n").encode())
    proc.stdin.flush()
    line = proc.stdout.readline()
    proc.stdin.close()
    proc.wait(timeout=20)
    reply = _json.loads(line)
    content = reply.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else "{}"
    return _json.loads(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="parity-", dir=str(ROOT)) as raw:
        workdir = Path(raw)
        runtime = workdir / "runtime"
        models = workdir / "models"
        manifest = workdir / "manifest.json"
        settings_file = workdir / "settings.json"
        runtime.mkdir()
        models.mkdir()
        sp.write_settings(
            settings_file,
            sp.settings_document(transport="python", runtime=runtime, models=models),
        )

        env = sp.base_environment()
        env["COMSOL_MCP_SETTINGS_PATH"] = str(settings_file)

        # Direct Python capabilities under transport "python".
        direct = _capabilities_via_subprocess(env, [str(args.python), "-m", "comsol_mcp.server"])

        # The same settings with transport "shim" and the binary's identity;
        # the manifest is generated under exactly these settings bytes.
        sp.write_settings(
            settings_file,
            sp.settings_document(transport="shim", runtime=runtime, models=models, shim=args.shim),
        )
        gen = sp.generate_manifest(args.python, settings_file, manifest)
        if gen.returncode != 0:
            print(json.dumps({"error": "manifest generation failed", "stderr": gen.stderr[-400:]}))
            return 2

        shim_env = dict(env)
        shim_env["COMSOL_MCP_DISCOVERY_MANIFEST"] = str(manifest)
        shim_env["COMSOL_MCP_SHIM_CHILD"] = json.dumps(
            [str(args.python), "-m", "comsol_mcp.server"]
        )
        shim = _capabilities_via_subprocess(shim_env, [str(args.shim)])

    # Compare the identity-relevant fields. The capabilities payload has
    # `profile` as a plain string and `tool_count` at top level.
    direct_deployment = direct.get("deployment_identity") or {}
    shim_deployment = shim.get("deployment_identity") or {}
    checks = {
        "same_profile": direct.get("profile") == shim.get("profile"),
        "same_deployment_source": direct_deployment.get("source_classification")
        == shim_deployment.get("source_classification"),
        "same_package_version": direct_deployment.get("package_version")
        == shim_deployment.get("package_version"),
        "same_tool_count": direct.get("tool_count") == shim.get("tool_count"),
    }
    payload = {
        "schema_name": "comsol_mcp.transport_parity_probe",
        "schema_version": "1.0.0",
        "direct_summary": {
            "profile": direct.get("profile"),
            "deployment": direct_deployment.get("source_classification"),
            "version": direct_deployment.get("package_version"),
            "tools": direct.get("tool_count"),
        },
        "shim_summary": {
            "profile": shim.get("profile"),
            "deployment": shim_deployment.get("source_classification"),
            "version": shim_deployment.get("package_version"),
            "tools": shim.get("tool_count"),
        },
        "checks": checks,
        "passed": all(checks.values()),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(checks, sort_keys=True))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
