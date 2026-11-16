"""Transport ownership-gate sequence probe (0.8.2 H/I repair).

Implements the plan's six-step transport acceptance sequence in one
solver-free ownership domain with observed-behavior oracles:

1. Start Python direct; verify its admitted host identity, its process tree,
   and that no solver lease exists.
2. Request a switch to the shim while Python is live: the GUI switch logic
   refuses (the admission record shows a running host). Close Python through
   normal EOF.
3. Verify the Python host, its descendants, its listeners, and the lease are
   cleared and the admission record is released.
4. Save the shim mode through the GUI planning path, start the shim, and
   verify one admitted shim host plus exactly one contained worker.
5. Switch back: close the shim through EOF, verify the same cleanup, save
   Python mode, start Python, and verify its identity again.
6. While Python is live, attempt an overlapping shim start in the same
   domain and verify fail-closed refusal before a second host exists. Then a
   stale record whose recorded worker is still alive must block admission as
   unverified cleanup.

Uses isolated runtime/settings; never starts COMSOL.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sp = importlib.import_module("shim_probe_support")

sys.path.insert(0, str(sp.ROOT))


def _tree(pid: int) -> list[int]:
    import psutil

    try:
        return [pid, *[child.pid for child in psutil.Process(pid).children(recursive=True)]]
    except psutil.Error:
        return []


def _workers(pid: int, *, recursive: bool) -> list[int]:
    """Child processes other than the console host Windows attaches to a
    console-subsystem program (conhost.exe is not a worker)."""
    import psutil

    try:
        children = psutil.Process(pid).children(recursive=recursive)
    except psutil.Error:
        return []
    workers = []
    for child in children:
        try:
            if child.name().lower() == "conhost.exe":
                continue
        except psutil.Error:
            pass
        workers.append(child.pid)
    return workers


def _alive(pids: list[int]) -> list[int]:
    import psutil

    alive = []
    for pid in pids:
        try:
            if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                alive.append(pid)
        except psutil.Error:
            pass
    return alive


def _listeners(pids: list[int]) -> list[dict[str, Any]]:
    import psutil

    found = []
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.Error as exc:
        return [{"error": str(exc)}]
    for connection in connections:
        if connection.pid in pids and connection.status == psutil.CONN_LISTEN:
            found.append({"pid": connection.pid, "address": str(connection.laddr)})
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shim", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    from settings_gui import transport as gui_transport

    work = args.output.parent / f"ownership-gate-{time.strftime('%Y%m%dT%H%M%S')}"
    work.mkdir(parents=True)
    runtime = work / "runtime"
    models = work / "models"
    models.mkdir()
    settings = work / "settings.json"
    manifest = work / "manifest.json"
    binary = sp.shim_identity(args.shim)
    results: dict[str, Any] = {
        "schema": "comsol_mcp.p06.ownership_gate/2",
        "source": sp.source_identity(),
        "shim": binary,
        "work": str(work),
    }
    checks: dict[str, bool] = {}

    def no_processes(_attrs: object) -> Any:
        return iter(())  # The admission record only.

    def env(**extra: str) -> dict[str, str]:
        environment = sp.base_environment()
        environment["COMSOL_MCP_SETTINGS_PATH"] = str(settings)
        environment.update(extra)
        return environment

    def save_mode(mode: str) -> None:
        document = json.loads(settings.read_text(encoding="utf-8"))
        check = None
        if mode == "shim":
            check = gui_transport.check_shim_binary(binary["path"], binary["sha256"])
            if not check.ok:
                raise RuntimeError(f"binary check failed: {check}")
        planned = gui_transport.plan_switch(document, mode, check)
        sp.write_settings(settings, planned)

    sp.write_settings(
        settings, sp.settings_document(transport="python", runtime=runtime, models=models)
    )

    # Step 1: Python direct.
    python = sp.python_host(args.python, env(), work / "python-1.log")
    lock = sp.wait_for_owner(runtime, python)
    served = python.initialize()
    tree = _tree(python.pid)
    results["step1"] = {
        "lock": lock,
        "served": served,
        "tree": tree,
        "lease_present": (runtime / "solver_owner.json").exists(),
    }
    checks["step1_python_admitted_and_serving"] = (
        sp.lock_owned_by(lock, python)
        and (lock or {}).get("transport_mode") == "python"
        and served
        and not results["step1"]["lease_present"]
    )

    # Step 2: a switch request while Python is live is refused.
    observation = gui_transport.observe_hosts(runtime, process_iter=no_processes)
    results["step2_observation"] = {"state": observation.state, "hosts": observation.hosts}
    checks["step2_switch_refused_while_python_live"] = observation.state == "running"
    code = python.close()

    # Step 3: Python cleanup.
    time.sleep(0.5)
    leftover = _alive(tree)
    results["step3"] = {
        "exit_code": code,
        "leftover": leftover,
        "listeners": _listeners(tree),
        "lock_present": (runtime / sp.LOCK_NAME).exists(),
        "lease_present": (runtime / "solver_owner.json").exists(),
    }
    checks["step3_python_cleared"] = (
        code == 0
        and not leftover
        and not results["step3"]["listeners"]
        and not results["step3"]["lock_present"]
        and not results["step3"]["lease_present"]
    )

    # Step 4: shim mode through the GUI planning path.
    save_mode("shim")
    generated = sp.generate_manifest(args.python, settings, manifest)
    worker = json.dumps([str(args.python), "-m", "comsol_mcp.server"])
    shim = sp.shim_host(
        args.shim,
        env(COMSOL_MCP_DISCOVERY_MANIFEST=str(manifest), COMSOL_MCP_SHIM_CHILD=worker),
        work / "shim.log",
    )
    lock = sp.wait_for_owner(runtime, shim)
    discovery_served = shim.initialize()
    children_before_call = _workers(shim.pid, recursive=True)
    reply = shim.request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "capabilities", "arguments": {}},
        },
        timeout=180,
    )
    recorded = sp.read_lock(runtime)[0] or {}
    direct_children = _workers(shim.pid, recursive=False)
    tree = _tree(shim.pid)
    owned = recorded.get("owned_processes") or []
    results["step4"] = {
        "manifest_generated": generated.returncode == 0,
        "lock_pid": (lock or {}).get("pid"),
        "discovery_served": discovery_served,
        "children_before_first_call": children_before_call,
        "forwarded_call_ok": bool(reply) and "result" in reply,
        "direct_children": direct_children,
        "recorded_workers": owned,
    }
    checks["step4_one_shim_host_one_contained_worker"] = (
        generated.returncode == 0
        and (lock or {}).get("pid") == shim.pid
        and discovery_served
        and not children_before_call
        and results["step4"]["forwarded_call_ok"]
        and len(direct_children) == 1
        and len(owned) == 1
        and owned[0].get("pid") == direct_children[0]
    )

    # Step 5: switch back with verified cleanup.
    code = shim.close()
    time.sleep(0.5)
    leftover = _alive(tree)
    results["step5_shim_cleanup"] = {
        "exit_code": code,
        "leftover": leftover,
        "listeners": _listeners(tree),
        "lock_present": (runtime / sp.LOCK_NAME).exists(),
    }
    checks["step5_shim_cleared"] = (
        code == 0
        and not leftover
        and not results["step5_shim_cleanup"]["listeners"]
        and not results["step5_shim_cleanup"]["lock_present"]
    )
    save_mode("python")
    python = sp.python_host(args.python, env(), work / "python-2.log")
    lock = sp.wait_for_owner(runtime, python)
    served = python.initialize()
    checks["step5_python_readmitted"] = sp.lock_owned_by(lock, python) and served

    # Step 6: an overlapping shim start in the same domain is refused.
    overlap_settings = work / "overlap-settings.json"
    sp.write_settings(
        overlap_settings,
        sp.settings_document(transport="shim", runtime=runtime, models=models, binary=binary),
    )
    overlap_env = sp.base_environment()
    overlap_env["COMSOL_MCP_SETTINGS_PATH"] = str(overlap_settings)
    overlap_env["COMSOL_MCP_SHIM_CHILD"] = worker
    overlap = sp.shim_host(args.shim, overlap_env, work / "overlap.log")
    overlap_code = overlap.wait_exit(60)
    results["step6_overlap"] = {
        "exit_code": overlap_code,
        "reason": overlap.reason(),
        "python_alive": python.alive(),
        "python_still_owns": sp.lock_owned_by(sp.read_lock(runtime)[0], python),
        "lease_present": (runtime / "solver_owner.json").exists(),
    }
    checks["step6_overlapping_start_refused"] = (
        overlap_code == 2
        and overlap.reason() == "admission_domain_busy"
        and results["step6_overlap"]["python_alive"]
        and results["step6_overlap"]["python_still_owns"]
        and not results["step6_overlap"]["lease_present"]
    )
    python.close()

    # Step 6b: a stale record whose recorded worker is alive is UNKNOWN cleanup.
    helper = sp.sleeper(args.python)
    worker_identity = sp.birth_identity(helper.pid)
    sp.write_record(
        runtime,
        sp.admission_record(
            sp.dead_identity(args.python),
            transport_mode="shim",
            owned_processes=[
                {
                    "role": "worker",
                    "pid": worker_identity["pid"],
                    "process_create_time": worker_identity["process_create_time"],
                }
            ],
        ),
    )
    blocked = sp.python_host(args.python, env(), work / "python-3.log")
    blocked_code = blocked.wait_exit(60)
    results["step6_unknown_cleanup"] = {"exit_code": blocked_code, "reason": blocked.reason()}
    checks["step6_unverified_cleanup_blocks_admission"] = (
        blocked_code == 2 and blocked.reason() == "admission_cleanup_unverified"
    )
    helper.kill()

    results["checks"] = checks
    results["passed"] = all(checks.values())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(json.dumps(checks, indent=2))
    return 0 if results["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
