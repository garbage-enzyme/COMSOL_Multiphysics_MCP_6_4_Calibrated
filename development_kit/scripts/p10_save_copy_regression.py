"""Batch-D native regression: the bounded Save Copy against real COMSOL 6.4.

Starts one real COMSOL session (mph.start — the "external" server this run
never owns or kills), builds a model, then drives the shared-session
manager's public attach → adopt → lock → snapshot flow with the production
bounded writer (capped virtual-disk volume). The write-time bound is the
largest 512-byte multiple not above the cap, so:

  1. unaligned tail: cap = the natural copy size (not a 512 multiple)
     → refused during the write; native output never passed the bound;
  2. the copy size is then aligned to 512 by a deterministic model comment
     (unlock → comment → relock, the user-turn protocol) and re-measured
     for stability;
  3. exact cap: cap = the aligned copy size → accepted, manifest hash matches;
  4. one byte under: cap = size − 1 → refused during the write (0 bytes).

Every refusal must leave zero bytes at the final target, every volume must
be torn down without residue, and the source file, model parameters and
mphserver PID must be unchanged at the end.

Requires an elevated runner (the capped volume needs diskpart) and a
running COMSOL Desktop (``--launch-desktop`` starts one and closes only
that one). Evidence lands in one JSON file; nothing outside the work
directory is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _isolate_runtime(work: Path) -> Path:
    """Pin this process to a fresh regression runtime before any import.

    A fresh directory per run means no lease from a crashed earlier attempt
    can leak into this one.
    """
    runtime = work / f"runtime-{time.strftime('%Y%m%dT%H%M%S')}"
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ["COMSOL_MCP_RUNTIME_DIR"] = str(runtime)
    os.environ["COMSOL_MCP_SETTINGS_PATH"] = str(work / "settings.json")
    return runtime


import mph
import psutil

from comsol_mcp.shared_session.bounded_save import CLUSTER_BYTES, prepare_bounded_writer
from comsol_mcp.shared_session.lifecycle import SharedSessionManager

DESKTOP_NAMES = {"comsolui.exe", "comsol.exe"}
GENEROUS_CAP = 64 * 1024 * 1024
RESULTS: dict = {"schema": "comsol_mcp.p10.native_regression/2", "granularity": CLUSTER_BYTES}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _server_processes() -> list[dict]:
    return [
        {"pid": p.pid, "name": p.name(), "create_time": p.create_time()}
        for p in psutil.process_iter(["name"])
        if "mphserver" in (p.info["name"] or "").lower()
    ]


def _desktop_pids() -> set[int]:
    return {
        process.pid
        for process in psutil.process_iter(["name"])
        if (process.info.get("name") or "").lower() in DESKTOP_NAMES
    }


def _launch_desktop() -> dict:
    """Start one COMSOL Desktop minimized without focus; return its record."""
    root = Path(mph.discovery.backend("6.4")["root"])
    executable = root / "bin" / "win64" / "comsol.exe"
    before = _desktop_pids()
    startup = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startup.dwFlags |= 0x00000001  # STARTF_USESHOWWINDOW
    startup.wShowWindow = 7  # SW_SHOWMINNOACTIVE: no focus theft
    started = time.time()
    launcher = subprocess.Popen(  # noqa: S603 - fixed installed executable
        [str(executable)],
        startupinfo=startup,
        creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = started + 180
    while time.time() < deadline:
        new = _desktop_pids() - before - {launcher.pid}
        ui = [pid for pid in new if (psutil.Process(pid).name() or "").lower() == "comsolui.exe"]
        if ui:
            time.sleep(45)  # let the Desktop finish starting its session
            return {"launcher_pid": launcher.pid, "ui_pids": ui, "pre_existing": sorted(before)}
        time.sleep(2)
    return {"launcher_pid": launcher.pid, "ui_pids": [], "pre_existing": sorted(before)}


def _close_launched_desktop(record: dict) -> dict:
    """Terminate only the Desktop processes this run started."""
    closed = []
    for pid in [record.get("launcher_pid"), *record.get("ui_pids", [])]:
        if pid is None or pid in record.get("pre_existing", []):
            continue
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            continue
        for child in process.children(recursive=True):
            if "mphserver" in (child.name() or "").lower():
                continue  # never touch a server
            child.terminate()
            closed.append(child.pid)
        process.terminate()
        closed.append(pid)
    return {"terminated": closed}


def _build_model(client) -> object:
    model = client.create("p10-regression")
    model.parameter("L", "1[m]")
    model.parameter("w", "0.3")
    geometry = model.create("geometry", 3)
    block = geometry.create("Block", name="blk1")
    block.property("size", ["L", "L*w", "L"])
    geometry.run()
    (model / "mesh").run()
    return model


def _attached_images_under(root: Path) -> list[str]:
    done = subprocess.run(
        [
            "pwsh.exe",
            "-NoProfile",
            "-NoLogo",
            "-NonInteractive",
            "-Command",
            "@(Get-Disk | Where-Object { $_.BusType -eq 'File Backed Virtual' }).Location",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=0x08000000,
    )
    prefix = str(root).lower()
    return [
        line.strip()
        for line in done.stdout.splitlines()
        if line.strip() and line.strip().lower().startswith(prefix)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--launch-desktop", action="store_true")
    args = parser.parse_args()
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    runtime = _isolate_runtime(work)
    discovery_dir = work / "discovery"
    discovery_dir.mkdir(exist_ok=True)
    snapshots = work / "snapshots"
    snapshots.mkdir(exist_ok=True)
    volume_root = work / "bounded-volume"
    volume_root.mkdir(exist_ok=True)
    desktop = None

    try:
        if args.launch_desktop:
            desktop = _launch_desktop()
            RESULTS["desktop_launch"] = desktop
        # The attach preflight requires the user's GUI session (COMSOL
        # Desktop) to be running; it is evidence the server is externally owned.
        RESULTS["desktop_running"] = bool(_desktop_pids())
        if not RESULTS["desktop_running"]:
            RESULTS["error"] = "COMSOL Desktop is not running"
            return 1
        try:
            return _run(work, runtime, discovery_dir, snapshots, volume_root, args)
        except BaseException as exc:  # noqa: BLE001 - the receipt keeps the failure
            import traceback

            RESULTS["error"] = f"{type(exc).__name__}: {exc}"
            RESULTS["traceback"] = traceback.format_exc()
            print(RESULTS["traceback"], file=sys.stderr)
            return 1
    finally:
        if desktop is not None:
            RESULTS["desktop_close"] = _close_launched_desktop(desktop)
        _finish(args.output)


def _run(work, runtime, discovery_dir, snapshots, volume_root, args) -> int:
    client = mph.start(cores=4, version="6.4")
    RESULTS["server_before"] = _server_processes()
    model = _build_model(client)
    reference = work / "reference.mph"
    model.save(str(reference))
    RESULTS["reference_sha"] = _sha256(reference)
    RESULTS["reference_bytes"] = reference.stat().st_size

    (work / "settings.json").write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.settings",
                "schema_version": "1.4.0",
                "profile": {"name": "core"},
                "runtime": {"directory": str(runtime)},
                "shared_server": {"enabled": True},
                "paths": {
                    "model_read_roots": [str(work)],
                    "artifact_write_root": str(work),
                },
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "COMSOL_MCP_SETTINGS_PATH": str(work / "settings.json"),
        "COMSOL_MCP_RUNTIME_DIR": str(runtime),
        "COMSOL_MCP_ENABLE_SHARED_SERVER": "true",
    }

    # mph.start passes -port 0 (ephemeral); the real port is the server's
    # listening socket.
    port = None
    for process in psutil.process_iter(["name"]):
        if "mphserver" not in (process.info.get("name") or "").lower():
            continue
        for connection in process.net_connections(kind="inet"):
            if connection.status == psutil.CONN_LISTEN and connection.laddr:
                port = connection.laddr.port
                break
        if port is not None:
            break
    RESULTS["port"] = port
    if port is None:
        RESULTS["error"] = "the mph server port was not found in the process table"
        return 1

    target_dir = {"path": discovery_dir}

    def reconnecting_client(host, port_):
        # The manager disconnects its client after a refused attach; a
        # retry reconnects the same client to the same server.
        if getattr(client, "port", None) is None:
            client.connect(port_, host)
        return client

    manager = SharedSessionManager(
        client_factory=reconnecting_client,
        bounded_save_copy_provider=lambda cap: prepare_bounded_writer(cap, volume_root=volume_root),
        snapshot_target_factory=lambda tag: target_dir["path"] / f"{tag}-{time.time_ns()}.mph",
        manifest_writer=lambda path, value: path.write_text(json.dumps(value), encoding="utf-8"),
    )
    attached = None
    last_error = None
    attempt = 0
    for attempt in range(6):
        try:
            attached = manager.attach(
                {"endpoint": {"host": "127.0.0.1", "port": port}, "user_confirmed": True},
                profile="core",
                environ=environment,
            )
        except Exception as exc:  # noqa: BLE001 - transient inventory failures retry
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2.0)
            continue
        last_error = None
        if attached.get("success"):
            break
        # The preflight process/listener inventory can be transiently
        # incomplete while the Desktop settles; the manager refuses
        # fail-closed, so retry like a real caller would.
        if not (
            attached.get("retryable", False)
            or "inventory must be complete" in str(attached.get("error", ""))
        ):
            break
        RESULTS.setdefault("attach_refusals", []).append(attached.get("error"))
        time.sleep(3.0)
    RESULTS["attach_attempts"] = attempt + 1
    RESULTS["attach_last_error"] = last_error
    if attached is None or not attached.get("success"):
        RESULTS["attach"] = attached or {"success": False, "error": last_error}
        return 1
    RESULTS["attach"] = {k: attached.get(k) for k in ("success", "state")}

    # mph names the model, but the server inventory keys on the java tag.
    selector = {"tag": str(model.java.tag()), "expected_file_path": str(reference)}
    RESULTS["selector"] = selector
    RESULTS["adopt"] = {k: manager.adopt_model(selector).get(k) for k in ("success", "state")}
    state = {"lock": {}}

    def relock() -> None:
        # A relabel invalidates the adopted identity: the user-turn protocol
        # after changing the model is to re-adopt the (tag, file path) pair
        # and lock again. The preflight inventory can be transiently
        # incomplete on a busy server; retry like a real caller would.
        last = None
        for _attempt in range(4):
            adopted = manager.adopt_model(selector)
            if adopted.get("success"):
                locked = manager.lock_model(collaboration_mode="interactive_inspection")
                if locked.get("success"):
                    state["lock"] = locked["model_lock"]
                    return
                last = f"lock failed: {locked}"
            else:
                last = f"re-adopt failed: {adopted}"
            if "inventory must be complete" not in last:
                break
            time.sleep(2.0)
        raise RuntimeError(last or "relock failed")

    def unlock(reason: str) -> None:
        unlocked = manager.unlock_model(
            expected_lock_sha256=state["lock"].get("lock_sha256", ""), reason=reason
        )
        if not unlocked.get("success"):
            raise RuntimeError(f"unlock failed: {unlocked}")

    def snapshot(cap: int) -> dict:
        return manager.snapshot_model(
            expected_lock_sha256=state["lock"].get("lock_sha256", ""),
            expected_revision_sha256=state["lock"].get("revision", {}).get("revision_sha256", ""),
            max_snapshot_bytes=cap,
        )

    def boundary_snapshot(cap: int) -> dict:
        result: dict = {}
        for _attempt in range(4):
            result = snapshot(cap)
            if not transient(result):
                return result
            row = summary(result)
            row["retried_as_transient"] = True
            RESULTS.setdefault("boundary_retries", []).append(row)
            time.sleep(2.0)
        return result

    def summary(result: dict) -> dict:
        keys = (
            "state",
            "cap_bytes",
            "write_time_bound_bytes",
            "partial_bytes_observed",
            "enforcement_point",
            "bytes_at_final_target",
            "snapshot_size_bytes",
            "error",
        )
        row = {key: result.get(key) for key in keys if key in result}
        row["volume_residue_free"] = (result.get("volume_cleanup") or {}).get("residue_free")
        return row

    def transient(result: dict) -> bool:
        # The shared-session verification re-reads the process and listener
        # inventory, which can be transiently incomplete on a busy host; the
        # manager then refuses fail-closed and removes its artifacts.
        state_name = result.get("state")
        return state_name == "snapshot_precondition_failed" or (
            state_name == "snapshot_incomplete"
            and "changed during Save Copy" in str(result.get("error", ""))
        )

    def measure() -> int | None:
        target_dir["path"] = discovery_dir
        result: dict = {}
        for _attempt in range(4):
            result = snapshot(GENEROUS_CAP)
            if not transient(result):
                break
            row = summary(result)
            row["retried_as_transient"] = True
            RESULTS.setdefault("discoveries", []).append(row)
            time.sleep(2.0)
        RESULTS.setdefault("discoveries", []).append(summary(result))
        if result.get("state") != "snapshot_complete":
            return None
        return int(result["snapshot_size_bytes"])

    relock()
    RESULTS["lock"] = True
    parameters_before = dict(model.parameters())

    # 1) the natural copy and its unaligned tail.
    natural = measure()
    RESULTS["natural_copy_size"] = natural
    if natural is None:
        RESULTS["passed"] = False
        return 1
    target_dir["path"] = snapshots
    if natural % CLUSTER_BYTES:
        RESULTS["case_unaligned_tail"] = summary(boundary_snapshot(natural))
    else:
        RESULTS["case_unaligned_tail"] = {"skipped": "the natural copy is already aligned"}

    # 2) align the copy size with a deterministic model label suffix. Native
    # probes with the model history compacted before every save: consecutive
    # copies are byte-identical, the copy is independent of the save path,
    # and each label character shifts the copy by exactly one byte. Measure
    # once, step the label by the residue, and correct any remainder.
    original_label = str(model.java.label())
    RESULTS["original_label"] = original_label
    base_label = "p10-regression"
    aligned = None
    trail = []

    def size_with_label(label: str) -> int | None:
        unlock("align the copy size")
        model.java.label(label)
        model.java.resetHist()
        relock()
        size = measure()
        trail.append({"label_chars": len(label), "copy_size": size})
        return size

    label = base_label
    size = size_with_label(label)
    for _correction in range(6):
        if size is None:
            break
        if size % CLUSTER_BYTES == 0:
            aligned = size
            break
        grow = (-size) % CLUSTER_BYTES
        suffix_chars = len(label) - len(base_label)
        if suffix_chars == 0:
            # the first suffix adds its separator too
            label = f"{base_label}-{'a' * (grow - 1)}"
        else:
            label = f"{base_label}-{'a' * (suffix_chars - 1 + grow)}"
        size = size_with_label(label)
    RESULTS["alignment_trail"] = trail
    RESULTS["aligned_copy_size"] = aligned
    if aligned is None:
        RESULTS["passed"] = False
        return 1
    stable = measure()
    RESULTS["aligned_size_stable"] = stable == aligned
    parameters_aligned = dict(model.parameters())

    # 3) exact cap: accepted, at exactly the declared cap.
    target_dir["path"] = snapshots
    before_files = set(snapshots.iterdir())
    result = boundary_snapshot(aligned)
    target = Path(result["snapshot_path"]) if result.get("snapshot_path") else None
    row = summary(result)
    row["size_on_disk"] = target.stat().st_size if target and target.exists() else None
    row["manifest_hash_matches"] = bool(
        result.get("manifest_path")
        and target
        and target.exists()
        and json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))["snapshot"][
            "sha256"
        ]
        == _sha256(target)
    )
    RESULTS["case_exact_cap"] = row

    # 4) one byte under: the bound drops one cluster; refused during the write.
    RESULTS["case_one_byte_under"] = summary(boundary_snapshot(aligned - 1))

    after_files = set(snapshots.iterdir())
    new_files = sorted(after_files - before_files)
    RESULTS["artifacts"] = {
        "snapshots_dir": sorted(p.name for p in after_files),
        "new_since_exact_cap": [p.name for p in new_files],
    }
    accepted = [p for p in new_files if p.suffix == ".mph"]
    RESULTS["artifacts"]["refused_leftovers"] = [
        p.name for p in after_files if p.suffix == ".mph" and p not in accepted
    ]

    # Immutability and server survival.
    RESULTS["source_file_unchanged"] = _sha256(reference) == RESULTS["reference_sha"]
    RESULTS["model_parameters_unchanged"] = (
        dict(model.parameters()) == parameters_before == parameters_aligned
    )
    (model / "geometry").run()
    RESULTS["server_after"] = _server_processes()
    RESULTS["server_pids_unchanged"] = [p["pid"] for p in RESULTS["server_before"]] == [
        p["pid"] for p in RESULTS["server_after"]
    ]

    if state["lock"]:
        unlock("native regression complete")
    else:  # a mid-run failure already released the lock; never double-unlock
        RESULTS["final_unlock_skipped"] = "the model was not locked at the end"
    # The harness relabeled the model to align the copy; restore the label
    # the attach-time inventory recorded so detach sees the same inventory.
    model.java.label(original_label)
    RESULTS["label_restored"] = str(model.java.label()) == original_label
    detach = manager.detach()
    for _attempt in range(3):
        violations = detach.get("violations") or []
        probe_only = set(violations) <= {
            "external_server_identity_unavailable_after_detach",
            "external_listener_not_preserved",
        }
        if detach.get("success") or not probe_only:
            break
        time.sleep(2.0)
        detach = manager.detach()
    RESULTS["detach"] = {k: detach.get(k) for k in ("success", "state", "violations")}

    # No volume may survive: no run directory, no attached image.
    RESULTS["volume_root_residue"] = sorted(p.name for p in volume_root.iterdir())
    RESULTS["attached_images_under_root"] = _attached_images_under(work)
    all_rows = [
        *RESULTS.get("discoveries", []),
        RESULTS["case_exact_cap"],
        RESULTS["case_one_byte_under"],
    ]
    if "state" in RESULTS["case_unaligned_tail"]:
        all_rows.append(RESULTS["case_unaligned_tail"])
    RESULTS["every_volume_residue_free"] = all(
        row.get("volume_residue_free") is True for row in all_rows
    )

    tail = RESULTS["case_unaligned_tail"]
    tail_ok = "skipped" in tail or (
        tail["state"] == "snapshot_byte_cap_enforced"
        and tail["enforcement_point"] == "os_volume"
        and tail["bytes_at_final_target"] == 0
        and tail["write_time_bound_bytes"] == natural // CLUSTER_BYTES * CLUSTER_BYTES
        and (tail.get("partial_bytes_observed") or 0) <= tail["write_time_bound_bytes"]
    )
    under = RESULTS["case_one_byte_under"]
    exact = RESULTS["case_exact_cap"]
    ok = (
        tail_ok
        and RESULTS["aligned_size_stable"]
        and exact["state"] == "snapshot_complete"
        and exact["size_on_disk"] == aligned
        and exact["write_time_bound_bytes"] == aligned
        and exact["manifest_hash_matches"]
        and under["state"] == "snapshot_byte_cap_enforced"
        and under["enforcement_point"] == "os_volume"
        and under["bytes_at_final_target"] == 0
        and under["write_time_bound_bytes"] == aligned - CLUSTER_BYTES
        and (under.get("partial_bytes_observed") or 0) <= aligned - CLUSTER_BYTES
        and RESULTS["artifacts"]["refused_leftovers"] == []
        and RESULTS["every_volume_residue_free"]
        and RESULTS["volume_root_residue"] == []
        and RESULTS["attached_images_under_root"] == []
        and RESULTS["source_file_unchanged"]
        and RESULTS["model_parameters_unchanged"]
        and RESULTS["server_pids_unchanged"]
        and RESULTS["label_restored"]
        and RESULTS["detach"].get("success") is True
    )
    RESULTS["passed"] = ok
    print(
        json.dumps(
            {
                key: RESULTS.get(key)
                for key in (
                    "natural_copy_size",
                    "case_unaligned_tail",
                    "aligned_copy_size",
                    "aligned_size_stable",
                    "case_exact_cap",
                    "case_one_byte_under",
                    "every_volume_residue_free",
                    "volume_root_residue",
                    "attached_images_under_root",
                    "server_pids_unchanged",
                    "passed",
                )
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def _finish(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(RESULTS, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
