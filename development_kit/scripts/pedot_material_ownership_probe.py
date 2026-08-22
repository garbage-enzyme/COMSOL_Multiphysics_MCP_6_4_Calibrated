"""Read-only licensed probe for material ownership of one trusted patch domain."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from importlib import import_module
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

_durable = import_module("comsol_mcp.durable")
_ownership = import_module("comsol_mcp.tools.ownership")
atomic_write_json = _durable.atomic_write_json
SolverOwnership = _ownership.SolverOwnership

SCHEMA_NAME = "comsol_mcp.pedot_material_ownership_probe"
SCHEMA_VERSION = "1.0.0"
_PROPERTIES = ("relpermittivity", "electricconductivity", "relpermeability")


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
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--component-tag", required=True)
    parser.add_argument("--patch-domain", type=int, required=True)
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
    clean = not subprocess.run(  # noqa: S603
        [git, "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    return {"revision": revision, "clean": clean}


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.resolve(strict=True)
    approved = Path("D:/mcp_tests").resolve(strict=False)
    if os.name == "nt" and (root.parent != approved or len(root.name) > 12):
        raise ValueError("test root must be a direct short child of D:/mcp_tests")
    if not str(root).isascii():
        raise ValueError("test root must be ASCII")
    if isinstance(args.cores, bool) or args.cores < 1:
        raise ValueError("cores must be an explicit positive integer")
    available = os.cpu_count()
    if not isinstance(available, int) or args.cores > available:
        raise ValueError("cores exceeds live host capacity or capacity is unavailable")
    if isinstance(args.patch_domain, bool) or args.patch_domain < 1:
        raise ValueError("patch domain must be an explicit positive integer")
    source = args.source_model.resolve(strict=True)
    source_hash = _sha(source)
    if source.suffix.casefold() != ".mph" or source_hash != args.source_sha256.lower():
        raise ValueError("source model identity changed")
    component = args.component_tag.strip()
    if not component or len(component) > 128:
        raise ValueError("component tag must be bounded")
    return {
        "root": root,
        "source": source,
        "source_sha256": source_hash,
        "component_tag": component,
        "patch_domain": args.patch_domain,
        "cores": args.cores,
        "receipt": root / "material-ownership.json",
        "private_receipt": root / "material-ownership-private.json",
    }


def _tags(collection: Any) -> list[str]:
    return [str(tag) for tag in list(collection.tags())]


def _selection(node: Any) -> tuple[list[int], str]:
    try:
        entities = sorted({int(item) for item in list(node.selection().entities())})
    except Exception:
        return [], "unavailable"
    return entities, "measured"


def _property_value(group: Any, name: str) -> list[str] | None:
    try:
        values = [str(item) for item in list(group.getStringArray(name))]
        if values:
            return values[:16]
    except Exception:
        values = []
    try:
        value = str(group.getString(name))
    except Exception:
        return None
    return [value] if value else None


def _material(collection: Any, tag: str, *, scope: str) -> dict[str, Any]:
    node = collection.get(tag)
    entities, selection_state = _selection(node)
    groups = []
    try:
        group_collection = node.propertyGroup()
        for group_tag in _tags(group_collection)[:32]:
            group = group_collection.get(group_tag)
            values = {
                name: value
                for name in _PROPERTIES
                if (value := _property_value(group, name)) is not None
            }
            groups.append({"tag": group_tag, "properties": values})
    except Exception:
        groups = []
    try:
        label = str(node.label())
    except Exception:
        label = ""
    return {
        "scope": scope,
        "tag": tag,
        "label": label[:256],
        "selection_state": selection_state,
        "domains": entities[:4096],
        "property_groups": groups,
    }


def _inventory(model: Any, *, component_tag: str, patch_domain: int) -> dict[str, Any]:
    jm = model.java
    materials = []
    global_collection = jm.material()
    for tag in _tags(global_collection)[:128]:
        materials.append(_material(global_collection, tag, scope="global"))
    component_collection = jm.component(component_tag).material()
    for tag in _tags(component_collection)[:128]:
        materials.append(_material(component_collection, tag, scope="component"))
    owners = [
        {"scope": item["scope"], "tag": item["tag"], "label": item["label"]}
        for item in materials
        if patch_domain in item["domains"]
    ]
    return {
        "component_tag": component_tag,
        "patch_domain": patch_domain,
        "materials": materials,
        "patch_domain_owners": owners,
        "owner_count": len(owners),
        "ownership_disposition": "unique" if len(owners) == 1 else "ambiguous_or_missing",
    }


def _dry_run(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "source_sha256": spec["source_sha256"],
        "component_tag": spec["component_tag"],
        "patch_domain": spec["patch_domain"],
        "requested_cores": spec["cores"],
        "solver_started": False,
        "paths_included": False,
    }


def _run(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    git = _git_identity()
    if not git["clean"]:
        raise RuntimeError("material ownership probe requires a clean source tree")
    import mph

    source_before = _sha(spec["source"])
    client = None
    model = None
    ownership = SolverOwnership()
    lease_acquired = False
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "dry_run": False,
        "source_revision": git["revision"],
        "source_sha256": source_before,
        "component_tag": spec["component_tag"],
        "patch_domain": spec["patch_domain"],
        "requested_cores": spec["cores"],
        "native_solve_executed": False,
        "paths_included": False,
    }
    private = {"source": str(spec["source"])}
    try:
        lease = ownership.acquire(mode="alpha7.2_s5_material_probe", model_path=str(spec["source"]))
        if not lease.get("success") or not lease.get("acquired"):
            raise RuntimeError("exclusive solver ownership could not be acquired")
        lease_acquired = True
        client = mph.Client(cores=spec["cores"], version="6.4")
        if not ownership.heartbeat(model_path=str(spec["source"]), refresh_server_processes=True):
            raise RuntimeError("solver ownership heartbeat failed after client startup")
        model = client.load(str(spec["source"]))
        inventory = _inventory(
            model,
            component_tag=spec["component_tag"],
            patch_domain=spec["patch_domain"],
        )
        receipt.update(
            {
                "success": True,
                "source_unchanged": _sha(spec["source"]) == source_before,
                "material_inventory": inventory,
            }
        )
    except Exception as exc:
        receipt["error"] = {"code": "material_ownership_probe_failed", "type": type(exc).__name__}
        private["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup = {
            "model_removed": model is None,
            "client_clear": client is None,
            "lease_released": not lease_acquired,
        }
        try:
            cleanup["source_unchanged"] = _sha(spec["source"]) == source_before
        except Exception as exc:
            # Keep the receipt writable even when source hashing fails.
            private["cleanup_error"] = f"source_hash:{type(exc).__name__}: {exc}"
            cleanup["source_unchanged"] = False
        if client is not None and model is not None:
            try:
                client.remove(model)
                cleanup["model_removed"] = True
            except Exception as exc:
                private["model_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        if client is not None:
            try:
                client.clear()
                cleanup["client_clear"] = True
            except Exception as exc:
                private["client_cleanup_error"] = f"{type(exc).__name__}: {exc}"
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
