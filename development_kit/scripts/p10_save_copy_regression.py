"""Batch-D native regression: the bounded Save Copy against real COMSOL 6.4.

Starts one real COMSOL session (mph.start — the "external" server this run
never owns or kills), builds a model, then drives the shared-session
manager's public attach → adopt → lock → snapshot flow with the production
bounded writer (capped VHDX volume):

  1. cap = exact copy size  → accepted, manifest hash matches the file
  2. cap = size − 1 byte    → refused (within one cluster: byte check)
  3. cap = one cluster less → refused at write time by the OS (0 bytes)
Every refusal must leave zero bytes at the final target and no artifacts.
The source model file, its parameters, and the mphserver PID must be
unchanged at the end.

Requires an elevated runner (the capped volume needs diskpart). Evidence
lands in one JSON file; nothing outside the work directory is touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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

from comsol_mcp.shared_session.bounded_save import prepare_bounded_writer
from comsol_mcp.shared_session.lifecycle import SharedSessionManager

CLUSTER = 4096
RESULTS: dict = {"schema": "comsol_mcp.p10.native_regression/1"}


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    runtime = _isolate_runtime(work)
    snapshots = work / "snapshots"
    snapshots.mkdir(exist_ok=True)
    volume_root = work / "bounded-volume"
    volume_root.mkdir(exist_ok=True)

    # The attach preflight requires the user's GUI session (COMSOL Desktop)
    # to be running; it is evidence that the server is externally owned.
    desktop_running = any(
        (process.info.get("name") or "").lower() in {"comsolui.exe", "comsol.exe"}
        for process in psutil.process_iter(["name"])
    )
    RESULTS["desktop_running"] = desktop_running
    if not desktop_running:
        RESULTS["error"] = "COMSOL Desktop is not running; start it with the model first"
        _finish(args.output)
        return 1

    client = mph.start(cores=4, version="6.4")
    RESULTS["server_before"] = _server_processes()
    model = _build_model(client)
    reference = work / "reference.mph"
    model.save(str(reference))
    size = reference.stat().st_size
    RESULTS["reference_sha"] = _sha256(reference)
    RESULTS["reference_sha"]
    parameters_before = dict(model.parameters())
    RESULTS["reference_bytes"] = size
    RESULTS["parameters_before"] = parameters_before

    # Environment for the manager's attach gate.

    environment = {
        **os.environ,
        "COMSOL_MCP_SETTINGS_PATH": str(work / "settings.json"),
        "COMSOL_MCP_RUNTIME_DIR": str(runtime),
        "COMSOL_MCP_ENABLE_SHARED_SERVER": "true",
    }
    if str(runtime) != os.environ["COMSOL_MCP_RUNTIME_DIR"]:
        raise RuntimeError("runtime isolation failed")
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
        _finish(args.output)
        return 1

    def provider(cap: int) -> dict:
        return prepare_bounded_writer(cap, volume_root=volume_root)

    manager = SharedSessionManager(
        client_factory=lambda host, port_: _TagClient(client),
        bounded_save_copy_provider=provider,
        snapshot_target_factory=lambda tag: snapshots / f"{tag}-{time.time_ns()}.mph",
        manifest_writer=lambda path, value: path.write_text(json.dumps(value), encoding="utf-8"),
    )
    # The process/listener inventory can be transiently incomplete; retry
    # the bounded preflight like a real caller would.
    attached = None
    last_error = None
    for attempt in range(4):
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
        if attached.get("success") or not attached.get("retryable", False):
            break
        time.sleep(2.0)
    RESULTS["attach_attempts"] = attempt + 1
    RESULTS["attach_last_error"] = last_error
    if attached is None:
        RESULTS["attach"] = {"success": False, "state": "attach_raised", "error": last_error}
        _finish(args.output)
        return 1
    RESULTS["attach"] = {k: attached.get(k) for k in ("success", "state", "error", "preflight")}
    if not attached.get("success"):
        _finish(args.output)
        return 1
    from comsol_mcp.shared_session.lifecycle import _default_model_inventory_reader

    RESULTS["server_inventory"] = _default_model_inventory_reader(client)[0]
    RESULTS["selector"] = {"tag": str(model.java.tag()), "expected_file_path": str(reference)}
    # mph names the model, but the server inventory keys on the java tag
    # (e.g. model1); after save, the label becomes the file name.
    adopted = manager.adopt_model(RESULTS["selector"])
    RESULTS["adopt"] = {k: adopted.get(k) for k in ("success", "state")}
    locked = manager.lock_model(collaboration_mode="interactive_inspection")
    RESULTS["lock"] = {k: locked.get(k) for k in ("success", "state")}
    lock = locked.get("model_lock") or {}

    def snapshot(cap: int) -> dict:
        return manager.snapshot_model(
            expected_lock_sha256=lock.get("lock_sha256", ""),
            expected_revision_sha256=lock.get("revision", {}).get("revision_sha256", ""),
            max_snapshot_bytes=cap,
        )

    before_files = set(snapshots.iterdir())

    # 1) exact cap: accepted, manifest hash matches the file.
    # A Save Copy embeds its own metadata, so its size exceeds the source
    # file's. Discover the actual copy size with a generous cap first; the
    # boundary cases then use that exact size.
    discovery = snapshot(max(4 * size, 64 * 1024 * 1024))
    copy_size = None
    if discovery.get("state") == "snapshot_complete":
        copy_size = Path(discovery["snapshot_path"]).stat().st_size
    RESULTS["case_discovery"] = {
        "state": discovery.get("state"),
        "copy_size": copy_size,
    }
    if copy_size is None:
        RESULTS["passed"] = False
        _finish(args.output)
        return 1

    # 1) exact cap: the accepted copy lands exactly at the declared cap.
    result = snapshot(copy_size)
    target = Path(result["snapshot_path"]) if result.get("snapshot_path") else None
    RESULTS["case_exact_cap"] = {
        "state": result.get("state"),
        "size": (target.stat().st_size if target and target.exists() else None),
        "manifest_hash_matches": (
            json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))["snapshot"][
                "sha256"
            ]
            == _sha256(target)
            if result.get("manifest_path") and target and target.exists()
            else False
        ),
    }

    # 2) one byte under the copy size: byte check refuses (same cluster).
    result = snapshot(copy_size - 1)
    RESULTS["case_one_byte_under"] = {
        "state": result.get("state"),
        "enforcement_point": result.get("enforcement_point"),
        "bytes_at_final_target": result.get("bytes_at_final_target"),
    }

    # 3) one cluster under: the OS refuses at write time.
    aligned = (copy_size + CLUSTER - 1) // CLUSTER * CLUSTER
    result = snapshot(max(aligned - CLUSTER, CLUSTER))
    RESULTS["case_one_cluster_under"] = {
        "state": result.get("state"),
        "enforcement_point": result.get("enforcement_point"),
        "bytes_at_final_target": result.get("bytes_at_final_target"),
    }

    after_files = set(snapshots.iterdir())
    accepted = sorted(p for p in after_files - before_files if p.suffix == ".mph")
    refused_leftovers = [
        str(p) for p in after_files if p.suffix == ".mph" and p not in accepted[:2]
    ]
    RESULTS["artifacts"] = {
        "accepted_snapshots": [p.name for p in accepted[:2]],
        "refused_leftovers": refused_leftovers,
        "manifests": [p.name for p in after_files if p.suffix == ".json"],
    }

    # Immutability and server survival.
    RESULTS["source_file_unchanged"] = _sha256(reference) == RESULTS["reference_sha"]
    RESULTS["model_parameters_unchanged"] = dict(model.parameters()) == parameters_before
    geometry = model / "geometry"
    geometry.run()
    RESULTS["server_alive"] = True
    RESULTS["server_after"] = _server_processes()
    RESULTS["server_pids_unchanged"] = [p["pid"] for p in RESULTS["server_before"]] == [
        p["pid"] for p in RESULTS["server_after"]
    ]

    manager.unlock_model(
        expected_lock_sha256=lock.get("lock_sha256", ""), reason="p10 regression complete"
    )
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
    RESULTS["detach"] = detach

    ok = (
        RESULTS["case_exact_cap"]["state"] == "snapshot_complete"
        and RESULTS["case_exact_cap"]["manifest_hash_matches"]
        and RESULTS["case_one_byte_under"]["state"] == "snapshot_byte_cap_enforced"
        and RESULTS["case_one_byte_under"]["enforcement_point"] == "post_write_byte_check"
        and RESULTS["case_one_cluster_under"]["state"] == "snapshot_byte_cap_enforced"
        and RESULTS["case_one_cluster_under"]["enforcement_point"] == "os_volume"
        and RESULTS["case_one_cluster_under"]["bytes_at_final_target"] == 0
        and RESULTS["artifacts"]["refused_leftovers"] == []
        and RESULTS["source_file_unchanged"]
        and RESULTS["model_parameters_unchanged"]
        and RESULTS["server_alive"]
        and RESULTS["server_pids_unchanged"]
    )
    RESULTS["passed"] = ok
    _finish(args.output)
    print(
        json.dumps(
            {
                k: RESULTS.get(k)
                for k in (
                    "case_exact_cap",
                    "case_one_byte_under",
                    "case_one_cluster_under",
                    "artifacts",
                    "source_file_unchanged",
                    "model_parameters_unchanged",
                    "server_pids_unchanged",
                    "passed",
                )
            },
            indent=2,
        )
    )
    return 0 if ok else 1


def _finish(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(RESULTS, indent=2, default=str), encoding="utf-8")


class _TagClient:
    """Route the manager's client calls to the real mph client."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)


if __name__ == "__main__":
    raise SystemExit(main())
