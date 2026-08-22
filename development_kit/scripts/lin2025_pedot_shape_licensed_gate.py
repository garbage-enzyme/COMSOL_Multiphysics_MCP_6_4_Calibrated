"""Licensed save/reload gate for the Lin2025 PEDOT-cylinder shape backend."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

_durable = import_module("comsol_mcp.durable")
_backend = import_module("comsol_mcp.research.lin2025_pedot_backend")
_fixture = import_module("comsol_mcp.research.lin2025_pedot_cylinder")
_ownership = import_module("comsol_mcp.tools.ownership")
atomic_write_json = _durable.atomic_write_json
ClientapiLin2025PedotControlBackend = _backend.ClientapiLin2025PedotControlBackend
prepare_lin2025_pedot_shape_controls = _backend.prepare_lin2025_pedot_shape_controls
normalize_lin2025_pedot_cylinder_fixture = _fixture.normalize_lin2025_pedot_cylinder_fixture
SolverOwnership = _ownership.SolverOwnership

SCHEMA_NAME = "comsol_mcp.lin2025_pedot_shape_licensed_gate"
SCHEMA_VERSION = "1.0.0"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--tree-readback", type=Path, required=True)
    parser.add_argument("--support", type=Path, required=True)
    parser.add_argument("--state-tensors", type=Path, required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _git_identity() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable is unavailable")
    revision = subprocess.run(  # noqa: S603
        [git, "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603
        [git, "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    return {"revision": revision, "clean": not status.strip()}


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.resolve(strict=True)
    approved = Path("D:/mcp_tests").resolve(strict=False)
    if os.name == "nt" and (root.parent != approved or len(root.name) > 12):
        raise ValueError("test root must be a direct short child of D:/mcp_tests")
    if not str(root).isascii():
        raise ValueError("test root must be ASCII")
    if isinstance(args.cores, bool) or not isinstance(args.cores, int) or args.cores < 1:
        raise ValueError("cores must be an explicit positive integer")
    available = os.cpu_count()
    if not isinstance(available, int) or args.cores > available:
        raise ValueError("cores exceeds live host capacity or capacity is unavailable")
    source = args.source_model.resolve(strict=True)
    if source.suffix.casefold() != ".mph":
        raise ValueError("source model must be an MPH file")
    fixture = normalize_lin2025_pedot_cylinder_fixture(
        json.loads(args.fixture.resolve(strict=True).read_text(encoding="utf-8"))
    )
    tree = json.loads(args.tree_readback.resolve(strict=True).read_text(encoding="utf-8"))
    support = json.loads(args.support.resolve(strict=True).read_text(encoding="utf-8"))
    state_tensors = json.loads(args.state_tensors.resolve(strict=True).read_text(encoding="utf-8"))
    if (
        not isinstance(state_tensors, dict)
        or set(state_tensors) != {"OX", "MR"}
        or any(
            not isinstance(values, list)
            or len(values) != 9
            or any(not isinstance(value, str) or not value.strip() for value in values)
            for values in state_tensors.values()
        )
    ):
        raise ValueError("state tensors must provide nine expressions for OX and MR")
    source_hash = _sha(source)
    if source_hash != fixture["source_identity"]["source_sha256"]:
        raise ValueError("source model differs from the trusted PEDOT fixture")
    return {
        "root": root,
        "source": source,
        "source_sha256": source_hash,
        "fixture": fixture,
        "tree": tree,
        "support": support,
        "state_tensors": state_tensors,
        "fixture_sha256": hashlib.sha256(args.fixture.read_bytes()).hexdigest(),
        "tree_sha256": hashlib.sha256(args.tree_readback.read_bytes()).hexdigest(),
        "support_sha256": hashlib.sha256(args.support.read_bytes()).hexdigest(),
        "state_tensors_sha256": hashlib.sha256(args.state_tensors.read_bytes()).hexdigest(),
        "cores": args.cores,
        "base_copy": root / "base.mph",
        "configured_copy": root / "configured.mph",
        "receipt": root / "licensed-receipt.json",
        "private_receipt": root / "licensed-private.json",
    }


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "source_sha256": spec["source_sha256"],
        "fixture_sha256": spec["fixture_sha256"],
        "tree_sha256": spec["tree_sha256"],
        "support_sha256": spec["support_sha256"],
        "state_tensors_sha256": spec["state_tensors_sha256"],
        "requested_cores": spec["cores"],
        "solver_started": False,
        "filesystem_modified": False,
        "paths_included": False,
    }


def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["clean"]:
        raise RuntimeError("licensed PEDOT gate requires a clean source tree")
    import mph

    source_before = _sha(spec["source"])
    client = None
    ownership = SolverOwnership()
    lease_acquired = False
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "dry_run": False,
        "source_revision": git["revision"],
        "source_sha256": source_before,
        "fixture_sha256": spec["fixture_sha256"],
        "tree_sha256": spec["tree_sha256"],
        "support_sha256": spec["support_sha256"],
        "state_tensors_sha256": spec["state_tensors_sha256"],
        "requested_cores": spec["cores"],
        "solver_started": False,
        "paths_included": False,
    }
    private: dict[str, Any] = {
        "source": str(spec["source"]),
        "base_copy": str(spec["base_copy"]),
        "configured_copy": str(spec["configured_copy"]),
    }
    try:
        lease = ownership.acquire(
            mode="alpha7.2_lin2025_pedot_shape_gate", model_path=str(spec["source"])
        )
        if not lease.get("success") or not lease.get("acquired"):
            raise RuntimeError("exclusive solver ownership could not be acquired")
        lease_acquired = True
        for path in (spec["base_copy"], spec["configured_copy"]):
            path.unlink(missing_ok=True)
        client = mph.Client(cores=spec["cores"], version="6.4")
        if not ownership.heartbeat(model_path=str(spec["source"]), refresh_server_processes=True):
            raise RuntimeError("solver ownership heartbeat failed after client startup")
        source_model = client.load(str(spec["source"]))
        source_model.java.save(str(spec["base_copy"]), True)
        client.remove(source_model)
        model = client.load(str(spec["base_copy"]))
        control_receipt = prepare_lin2025_pedot_shape_controls(
            ClientapiLin2025PedotControlBackend(model, component_tag="comp1", geometry_tag="geom1"),
            spec["fixture"],
            spec["tree"],
            spec["support"],
        )
        backend = ClientapiLin2025PedotControlBackend(
            model, component_tag="comp1", geometry_tag="geom1"
        )
        state_receipts = [
            backend.apply_material_state(state, spec["state_tensors"][state])
            for state in ("OX", "MR")
        ]
        model.java.save(str(spec["configured_copy"]), True)
        client.remove(model)
        reloaded = client.load(str(spec["configured_copy"]))
        reloaded_backend = ClientapiLin2025PedotControlBackend(
            reloaded, component_tag="comp1", geometry_tag="geom1"
        )
        snapshot = reloaded_backend.snapshot()
        if "dg_pedot72" not in snapshot["physics"]:
            raise ValueError("saved PEDOT deformation interface is absent after reload")
        if snapshot["circle"] != control_receipt["controls"]["circle"]:
            raise ValueError("saved PEDOT circle identity changed after reload")
        if snapshot["material"]["relpermittivity"] != state_receipts[-1]["relpermittivity"]:
            raise ValueError("saved MR material tensor changed after reload")
        client.remove(reloaded)
        receipt.update(
            {
                "success": True,
                "source_unchanged": _sha(spec["source"]) == source_before,
                "base_copy_sha256": _sha(spec["base_copy"]),
                "configured_copy_sha256": _sha(spec["configured_copy"]),
                "control_receipt_fingerprint": control_receipt["receipt_fingerprint"],
                "material_state_ids": [item["state_id"] for item in state_receipts],
                "material_state_receipt_fingerprints": [
                    hashlib.sha256(
                        json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    for item in state_receipts
                ],
                "save_reload_verified": True,
                "native_solve_executed": False,
            }
        )
        private["control_receipt"] = control_receipt
        private["material_state_receipts"] = state_receipts
    except Exception as exc:
        receipt["error"] = {"code": "lin2025_pedot_shape_failed", "type": type(exc).__name__}
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = {
            "client_clear": False,
            "lease_released": not lease_acquired,
        }
        try:
            cleanup["source_unchanged"] = _sha(spec["source"]) == source_before
        except Exception as exc:
            # Never let receipt bookkeeping mask the run outcome or skip
            # lease/client cleanup.
            private["cleanup_error"] = f"source_hash:{type(exc).__name__}: {exc}"
            cleanup["source_unchanged"] = False
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                private["cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if lease_acquired:
            try:
                released = ownership.release()
            except Exception as exc:
                released = {"success": False, "released": False,
                            "error": f"{type(exc).__name__}: {exc}"}
            cleanup["lease_released"] = bool(released.get("success") and released.get("released"))
            if not cleanup["lease_released"]:
                private["lease_cleanup_error"] = released
        receipt["cleanup"] = cleanup
        receipt["success"] = receipt.get("success") is True and all(cleanup.values())
    return receipt, private


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    spec = _spec(args)
    if args.dry_run:
        print(json.dumps(_dry_run(spec), ensure_ascii=True, sort_keys=True))
        return 0
    receipt, private = _run(spec)
    atomic_write_json(spec["receipt"], receipt)
    atomic_write_json(spec["private_receipt"], private)
    print(json.dumps(receipt, ensure_ascii=True, sort_keys=True))
    return 0 if receipt["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
