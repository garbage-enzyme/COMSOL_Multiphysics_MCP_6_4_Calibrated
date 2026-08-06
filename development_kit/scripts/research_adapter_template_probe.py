"""Isolated source-tree stdio probe for one immutable COMSOL template.

The probe is repository-only and licensed.  ``--dry-run`` validates and emits
the complete isolation contract without importing MPh or starting COMSOL.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SCHEMA_NAME = "comsol_mcp.research_adapter_template_probe"
SCHEMA_VERSION = "1.0.0"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTICS_BYTES = 8 * 1024 * 1024
START_RESPONSE_SECONDS = 15.0
STARTUP_SECONDS = 180.0
CALL_SECONDS = 120.0
MAX_PROPERTY_QUERIES = 32
_TAG = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_PROPERTY_CONTAINERS = frozenset(
    {"geometry_feature", "physics_feature", "mesh_feature", "study_step", "result_feature"}
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit one immutable COMSOL template through an isolated source-tree MCP host."
    )
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--source-model", type=Path, required=True)
    parser.add_argument("--cores", type=int, required=True)
    parser.add_argument("--version", default="6.4")
    parser.add_argument(
        "--property-query",
        action="append",
        default=[],
        metavar="COMPONENT|CONTAINER|PARENT/CHILD|PROPERTY",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _atomic_write_json(path: Path, value: Any, *, maximum_bytes: int) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )
    if len(payload) > maximum_bytes:
        raise ValueError(f"JSON artifact exceeds {maximum_bytes} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalized_spec(args: argparse.Namespace) -> dict[str, Any]:
    root = args.test_root.expanduser().resolve(strict=False)
    source = args.source_model.expanduser().resolve(strict=True)
    if not root.is_absolute() or not str(root).isascii():
        raise ValueError("test root must be an absolute ASCII path")
    if os.name == "nt":
        approved_parent = Path("D:/mcp_tests").resolve(strict=False)
        if root.parent != approved_parent or len(root.name) > 12:
            raise ValueError("test root must be a direct child of D:/mcp_tests with leaf <= 12")
    if source.suffix.casefold() != ".mph" or not source.is_file():
        raise ValueError("source model must be one existing .mph file")
    if isinstance(args.cores, bool) or not 1 <= args.cores <= 64:
        raise ValueError("cores must be an integer from 1 through 64")
    if args.version != "6.4":
        raise ValueError("this acceptance probe is calibrated only for COMSOL 6.4")
    property_queries = _normalize_property_queries(args.property_query)
    return {
        "test_root": root,
        "source_model": source,
        "source_sha256": _sha256(source),
        "source_size_bytes": source.stat().st_size,
        "cores": args.cores,
        "version": args.version,
        "property_queries": property_queries,
        "runtime_root": root / "runtime",
        "artifact_root": root / "artifacts",
        "settings_path": root / "settings.json",
        "receipt_path": root / "receipt.json",
        "diagnostics_path": root / "private-diagnostics.json",
        "stderr_path": root / "server-stderr.log",
    }


def _normalize_property_queries(values: list[str]) -> list[dict[str, str]]:
    if len(values) > MAX_PROPERTY_QUERIES:
        raise ValueError(f"at most {MAX_PROPERTY_QUERIES} property queries are allowed")
    result = []
    identities = set()
    for value in values:
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError("property query must be one bounded string")
        parts = value.split("|")
        if len(parts) != 4:
            raise ValueError("property query must use COMPONENT|CONTAINER|PARENT/CHILD|PROPERTY")
        component, container, feature_tag, property_name = parts
        feature_parts = feature_tag.split("/")
        if (
            not _TAG.fullmatch(component)
            or container not in _PROPERTY_CONTAINERS
            or len(feature_parts) != 2
            or not all(_TAG.fullmatch(part) for part in feature_parts)
            or not _TAG.fullmatch(property_name)
        ):
            raise ValueError("property query contains an unsupported clientapi target")
        identity = (component, container, feature_tag, property_name)
        if identity in identities:
            raise ValueError("property queries must not contain duplicates")
        identities.add(identity)
        result.append(
            {
                "component_name": component,
                "container": container,
                "feature_tag": feature_tag,
                "property_name": property_name,
            }
        )
    return result


def _settings_document(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_name": "comsol_mcp.settings",
        "schema_version": "1.3.0",
        "profile": {"name": "full"},
        "runtime": {"directory": str(spec["runtime_root"]), "jobs_directory": None},
        "paths": {
            "model_read_roots": [str(spec["source_model"].parent)],
            "artifact_write_root": str(spec["artifact_root"]),
        },
        "shared_server": {"enabled": False},
        "evidence_integrity": {
            "checks": {
                "outcome_contract_validation": True,
                "artifact_chain_verification": True,
                "summary_claim_verification": True,
                "producer_driver_compatibility": True,
            }
        },
        "manuals": {"root": None},
        "lexical_docs": {"enabled": False, "index_path": None},
        "semantic_docs": {"enabled": False, "root": None, "model_path": None},
        "ownership": {"owner": "research-adapter-template-probe"},
        "java": {"java_home": None, "jdk_home": None},
        "comsol": {"installation_root": None},
        "gui": {"language": "en", "scale": "system"},
    }


def _stdio_environment(settings_path: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("COMSOL_MCP_")
    }
    environment.pop("PYTHONPATH", None)
    environment["COMSOL_MCP_SETTINGS_PATH"] = str(settings_path)
    return environment


def _git_identity() -> dict[str, Any]:
    git = shutil.which("git")
    if git is None:
        return {"success": False, "commit": None, "worktree_clean": False}
    revision = subprocess.run(  # noqa: S603 - resolved Git executes fixed arguments
        [git, "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    worktree = subprocess.run(  # noqa: S603 - resolved Git executes fixed arguments
        [git, "status", "--porcelain"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    commit = revision.stdout.strip()
    return {
        "success": revision.returncode == 0 and len(commit) == 40 and worktree.returncode == 0,
        "commit": commit if revision.returncode == 0 else None,
        "worktree_clean": worktree.returncode == 0 and not worktree.stdout,
    }


def _sdk_attribute(value: Any, snake_case: str, legacy_alias: str, default: Any = None) -> Any:
    if hasattr(value, snake_case):
        return getattr(value, snake_case)
    return getattr(value, legacy_alias, default)


def _tool_payload(result: Any) -> dict[str, Any]:
    if _sdk_attribute(result, "is_error", "isError", False):
        raise RuntimeError("MCP tool returned an error result")
    structured = _sdk_attribute(result, "structured_content", "structuredContent")
    if isinstance(structured, dict):
        payload = structured.get("result", structured)
        if isinstance(payload, dict):
            return payload
    objects = []
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            nested = payload.get("result", payload)
            if isinstance(nested, dict):
                objects.append(nested)
    if len(objects) != 1:
        raise RuntimeError("MCP tool result did not contain exactly one JSON object")
    return objects[0]


def _public_call(name: str, payload: dict[str, Any], elapsed: float) -> dict[str, Any]:
    encoded = _canonical_bytes(payload)
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError(f"{name} response exceeds the probe bound")
    summary = {
        "tool": name,
        "success": payload.get("success", True) is True,
        "elapsed_seconds": elapsed,
        "response_bytes": len(encoded),
        "response_sha256": hashlib.sha256(encoded).hexdigest(),
    }
    for key in ("count", "physics_count", "multiphysics_count"):
        if isinstance(payload.get(key), int):
            summary[key] = payload[key]
    return summary


async def _probe(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    source = spec["source_model"]
    source_stat = source.stat()
    git_identity = _git_identity()
    if not git_identity["success"] or not git_identity["worktree_clean"]:
        raise RuntimeError("source-tree probe requires one clean Git revision")
    diagnostics: dict[str, Any] = {"calls": [], "paths": {}}
    query_bytes = _canonical_bytes(spec["property_queries"])
    receipt: dict[str, Any] = {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": False,
        "source": {
            "sha256": spec["source_sha256"],
            "size_bytes": spec["source_size_bytes"],
            "path_included": False,
        },
        "source_tree": git_identity,
        "isolation": {
            "profile": "full",
            "shared_server_enabled": False,
            "strict_evidence_checks_requested": True,
            "model_read_root_count": 1,
            "property_query_count": len(spec["property_queries"]),
            "property_queries_sha256": hashlib.sha256(query_bytes).hexdigest(),
            "paths_included": False,
        },
        "calls": [],
        "cleanup": {"passed": False, "steps": {}},
    }
    diagnostics["paths"] = {
        "repository_root": str(REPOSITORY_ROOT),
        "test_root": str(spec["test_root"]),
        "source_model": str(source),
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "comsol_mcp.server"],
        cwd=REPOSITORY_ROOT,
        env=_stdio_environment(spec["settings_path"]),
    )
    transport_usable = True
    model_name: str | None = None
    start_requested = False

    async def call(session: ClientSession, name: str, arguments: dict[str, Any], timeout: float):
        nonlocal transport_usable
        started = time.perf_counter()
        try:
            result = await session.call_tool(name, arguments, read_timeout_seconds=timeout)
        except Exception:
            transport_usable = False
            raise
        payload = _tool_payload(result)
        elapsed = time.perf_counter() - started
        public = _public_call(name, payload, elapsed)
        receipt["calls"].append(public)
        diagnostics["calls"].append({**public, "arguments": arguments, "payload": payload})
        return payload

    with spec["stderr_path"].open("w", encoding="utf-8") as errlog:
        async with stdio_client(parameters, errlog=errlog) as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=CALL_SECONDS
            ) as session:
                await session.initialize()
                try:
                    capabilities = await call(session, "capabilities", {}, CALL_SECONDS)
                    if capabilities.get("profile") != "full":
                        raise RuntimeError("candidate host did not activate the full profile")
                    deployment = capabilities.get("deployment_identity", {})
                    if deployment.get("source_classification") != "source_tree":
                        raise RuntimeError("candidate host did not resolve the current source tree")
                    evidence = capabilities.get("evidence_integrity", {})
                    if evidence.get("strict_verification_active") is not True:
                        raise RuntimeError("candidate host did not retain strict evidence checks")
                    cold_status = await call(session, "comsol_status", {}, CALL_SECONDS)
                    if cold_status.get("connected") or cold_status.get("starting"):
                        raise RuntimeError("candidate host was not cold at preflight")
                    cold_solver = await call(session, "solver_status", {}, CALL_SECONDS)
                    if cold_solver.get("collision") is True:
                        raise RuntimeError("solver ownership preflight found a collision")
                    preflight = await call(
                        session,
                        "solver_preflight",
                        {"model_path": str(source), "requested_version": spec["version"]},
                        CALL_SECONDS,
                    )
                    if preflight.get("ready") is not True:
                        raise RuntimeError("solver preflight rejected the isolated template audit")
                    start_requested = True
                    started = await call(
                        session,
                        "comsol_start",
                        {"cores": spec["cores"], "version": spec["version"]},
                        START_RESPONSE_SECONDS,
                    )
                    if started.get("success") is False:
                        raise RuntimeError("COMSOL start request was rejected")
                    deadline = time.monotonic() + STARTUP_SECONDS
                    while True:
                        status = await call(session, "comsol_status", {}, CALL_SECONDS)
                        if status.get("connected") is True:
                            break
                        if status.get("starting") is not True:
                            raise RuntimeError("COMSOL startup ended without a connected client")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("COMSOL startup exceeded the bounded deadline")
                        await asyncio.sleep(2.0)
                    loaded = await call(
                        session, "model_load", {"file_path": str(source)}, CALL_SECONDS
                    )
                    if loaded.get("success") is not True:
                        raise RuntimeError("candidate template load failed")
                    model_name = str(loaded["model"]["name"])
                    if (
                        str(loaded["model"].get("source_sha256", "")).casefold()
                        != spec["source_sha256"]
                    ):
                        raise RuntimeError("loaded source identity did not match the pinned bytes")
                    for name, arguments in (
                        ("model_inspect", {"model_name": model_name}),
                        ("geometry_list_features", {"model_name": model_name}),
                        ("geometry_probe_domains", {"model_name": model_name}),
                        ("physics_list", {"model_name": model_name}),
                        ("mesh_info", {"model_name": model_name}),
                        ("study_list", {"model_name": model_name}),
                        ("datasets_list", {"model_name": model_name}),
                    ):
                        observed = await call(session, name, arguments, CALL_SECONDS)
                        if observed.get("success") is not True:
                            raise RuntimeError(f"read-only template inspection failed: {name}")
                    for query in spec["property_queries"]:
                        observed = await call(
                            session,
                            "clientapi_property_get",
                            {**query, "model_name": model_name},
                            CALL_SECONDS,
                        )
                        if observed.get("success") is not True:
                            raise RuntimeError("read-only clientapi property query failed")
                    receipt["success"] = True
                except Exception as exc:
                    receipt["error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc)[:512],
                    }
                    diagnostics["error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc)[:4096],
                    }
                finally:
                    steps = receipt["cleanup"]["steps"]
                    if transport_usable and model_name is not None:
                        try:
                            removed = await call(
                                session, "model_remove", {"model_name": model_name}, CALL_SECONDS
                            )
                            steps["model_remove"] = {"passed": removed.get("success") is True}
                        except Exception as exc:
                            steps["model_remove"] = {
                                "passed": False,
                                "error_type": type(exc).__name__,
                            }
                    else:
                        steps["model_remove"] = {
                            "passed": model_name is None,
                            "not_applicable": model_name is None,
                        }
                    if transport_usable and start_requested:
                        try:
                            disconnected = await call(
                                session, "comsol_disconnect", {}, CALL_SECONDS
                            )
                            steps["comsol_disconnect"] = {
                                "passed": disconnected.get("success") is True
                            }
                        except Exception as exc:
                            steps["comsol_disconnect"] = {
                                "passed": False,
                                "error_type": type(exc).__name__,
                            }
                    else:
                        steps["comsol_disconnect"] = {
                            "passed": not start_requested,
                            "not_applicable": not start_requested,
                        }
                    if transport_usable:
                        try:
                            final_solver = await call(session, "solver_status", {}, CALL_SECONDS)
                            lease = final_solver.get("lease", {})
                            steps["solver_lease_absent"] = {
                                "passed": lease.get("state") == "absent"
                            }
                        except Exception as exc:
                            steps["solver_lease_absent"] = {
                                "passed": False,
                                "error_type": type(exc).__name__,
                            }
                    else:
                        steps["solver_lease_absent"] = {
                            "passed": False,
                            "error_type": "TransportUncertain",
                        }
    final_stat = source.stat()
    source_unchanged = (
        _sha256(source) == spec["source_sha256"]
        and final_stat.st_size == source_stat.st_size
        and final_stat.st_mtime_ns == source_stat.st_mtime_ns
    )
    receipt["cleanup"]["steps"]["source_unchanged"] = {"passed": source_unchanged}
    receipt["cleanup"]["passed"] = all(
        step.get("passed") is True for step in receipt["cleanup"]["steps"].values()
    )
    receipt["success"] = receipt["success"] is True and receipt["cleanup"]["passed"] is True
    diagnostics["stderr"] = {
        "size_bytes": spec["stderr_path"].stat().st_size,
        "sha256": _sha256(spec["stderr_path"]),
    }
    return receipt, diagnostics


def _dry_run_receipt(spec: dict[str, Any]) -> dict[str, Any]:
    settings = _settings_document(spec)
    query_bytes = _canonical_bytes(spec["property_queries"])
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "success": True,
        "dry_run": True,
        "source": {
            "sha256": spec["source_sha256"],
            "size_bytes": spec["source_size_bytes"],
            "path_included": False,
        },
        "isolation": {
            "profile": "full",
            "runtime_inside_test_root": True,
            "artifacts_inside_test_root": True,
            "model_read_root_count": 1,
            "shared_server_enabled": False,
            "strict_evidence_checks_requested": True,
            "settings_sha256": hashlib.sha256(_canonical_bytes(settings)).hexdigest(),
            "property_query_count": len(spec["property_queries"]),
            "property_queries_sha256": hashlib.sha256(query_bytes).hexdigest(),
            "paths_included": False,
        },
        "solver_started": False,
        "filesystem_modified": False,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        spec = _normalized_spec(args)
        if args.dry_run:
            print(json.dumps(_dry_run_receipt(spec), sort_keys=True))
            return 0
        spec["test_root"].mkdir(parents=True, exist_ok=False)
        spec["runtime_root"].mkdir()
        spec["artifact_root"].mkdir()
        settings = _settings_document(spec)
        _atomic_write_json(spec["settings_path"], settings, maximum_bytes=64 * 1024)
        receipt, diagnostics = asyncio.run(_probe(spec))
        _atomic_write_json(
            spec["diagnostics_path"], diagnostics, maximum_bytes=MAX_DIAGNOSTICS_BYTES
        )
        _atomic_write_json(spec["receipt_path"], receipt, maximum_bytes=512 * 1024)
        print(json.dumps(receipt, sort_keys=True))
        return 0 if receipt["success"] else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        _parser().error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
