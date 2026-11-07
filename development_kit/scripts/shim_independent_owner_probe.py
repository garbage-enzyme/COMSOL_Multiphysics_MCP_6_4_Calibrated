"""Shim containment versus independent owners (solver-free).

A worker behind the real shim starts two children: one ordinary console
child and one independent owner launched exactly like a durable job worker
(``popen_independent_owner`` with breakaway enabled, as a verified shim
worker does). The shim is then hard-killed (no cleanup code runs). The
oracle: the ordinary child dies with the shim's kill-on-close job; the
independent owner survives, as the 0.8.1 contract requires of durable
workers across a host exit.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sp = importlib.import_module("shim_probe_support")


def run(shim: Path, python: Path, work: Path) -> dict:
    import psutil

    pids = work / "children.json"
    worker = work / "fake_worker.py"
    worker.write_text(
        f"""
import json, subprocess, sys
sys.path.insert(0, r"{sp.ROOT}")
from comsol_mcp import process_launch
from comsol_mcp.jobs.manager import _detached_creation_flags
process_launch.enable_job_breakaway()
contained = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
owner, outcome = process_launch.popen_independent_owner(
    subprocess.Popen, [sys.executable, "-c", "import time; time.sleep(300)"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    close_fds=True, creationflags=_detached_creation_flags())
open(r"{pids}", "w").write(json.dumps({{"contained": contained.pid, "owner": owner.pid,
                                      "outcome": outcome}}))
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    result = {{"protocolVersion": "2025-11-25", "capabilities": {{}},
               "serverInfo": {{"name": "fake", "version": "0"}}}}
    reply = {{"jsonrpc": "2.0", "id": message["id"], "result": result}}
    sys.stdout.write(json.dumps(reply) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )
    runtime = work / "runtime"
    settings = work / "settings.json"
    sp.write_settings(settings, sp.settings_document(transport="shim", runtime=runtime, shim=shim))
    env = sp.base_environment()
    env["COMSOL_MCP_SETTINGS_PATH"] = str(settings)
    env["COMSOL_MCP_SHIM_CHILD"] = json.dumps([str(python), str(worker)])
    host = sp.shim_host(shim, env, work / "shim.log")
    sp.wait_for_owner(runtime, host)
    served = host.initialize(timeout=60)
    deadline = time.time() + 30
    while not pids.exists() and time.time() < deadline:
        time.sleep(0.1)
    children = json.loads(pids.read_text()) if pids.exists() else {}

    def alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    before = {name: alive(children.get(name)) for name in ("contained", "owner")}
    host.process.kill()
    host.wait_exit(30)
    time.sleep(3)
    after = {name: alive(children.get(name)) for name in ("contained", "owner")}
    for name in ("contained", "owner"):
        if after[name]:
            try:
                psutil.Process(children[name]).kill()
            except psutil.Error:
                pass
    return {
        "served": served,
        "children": children,
        "alive_before_shim_kill": before,
        "alive_after_shim_kill": after,
        "passed": served
        and children.get("outcome") == "requested"
        and before == {"contained": True, "owner": True}
        and after == {"contained": False, "owner": True},
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    work = args.output.parent / f"breakaway-{time.strftime('%Y%m%dT%H%M%S')}"
    work.mkdir(parents=True)
    result = {
        "schema": "comsol_mcp.p06.shim_independent_owner/1",
        "source": sp.source_identity(),
        "shim": sp.shim_identity(args.shim),
        **run(args.shim, args.python, work),
    }
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {k: result[k] for k in ("alive_before_shim_kill", "alive_after_shim_kill", "passed")}
        )
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
