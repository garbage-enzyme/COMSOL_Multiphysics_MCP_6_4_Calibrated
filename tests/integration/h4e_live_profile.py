"""Fresh-stdio H4e profile discovery and public semantic-tool acceptance."""

from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
import uuid

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).parents[2]
PYTHON = Path(sys.executable)
OUTPUT = Path("D:/comsol_runtime/H4e/live_profile.json")
MODEL = Path("D:/comsol_semantic/models/all-MiniLM-L6-v2/1110a243fdf4706b3f48f1d95db1a4f5529b4d41")
PROFILE_COUNTS = {
    "core": 38,
    "basic_fem": 71,
    "wave_optics": 62,
    "semantic_docs": 41,
    "experimental": 64,
    "full": 119,
}


def _decode(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool returned an error: {result}")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        value = structured.get("result", structured)
        if isinstance(value, dict):
            return value
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
    raise ValueError("MCP result did not contain an object")


def _server(profile: str) -> StdioServerParameters:
    environment = os.environ.copy()
    environment.update({
        "COMSOL_MCP_PROFILE": profile,
        "COMSOL_MCP_RUNTIME_DIR": "D:/comsol_runtime",
        "COMSOL_SEMANTIC_ROOT": "D:/comsol_semantic",
        "COMSOL_SEMANTIC_LEXICAL_INDEX": "D:/comsol_docs_fts/manuals.sqlite3",
        "COMSOL_SEMANTIC_MODEL_PATH": str(MODEL),
    })
    return StdioServerParameters(
        command=str(PYTHON), args=["-m", "src.server"], cwd=ROOT, env=environment,
    )


async def _call(session: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=30))
    payload = _decode(result)
    return {
        "payload": payload,
        "elapsed_seconds": time.perf_counter() - started,
        "response_bytes": len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
    }


async def _discover(profile: str) -> dict[str, Any]:
    async with stdio_client(_server(profile)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = sorted(tool.name for tool in listed.tools)
            capabilities = await _call(session, "capabilities", {})
    assert len(names) == PROFILE_COUNTS[profile]
    assert capabilities["payload"]["tool_count"] == PROFILE_COUNTS[profile]
    assert capabilities["payload"]["profile"] == profile
    return {"profile": profile, "tool_count": len(names), "tools": names}


async def _semantic_flow() -> dict[str, Any]:
    async with stdio_client(_server("semantic_docs")) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            solver_before = await _call(session, "solver_status", {})
            cold = await _call(session, "semantic_status", {"warm": False})
            warm = await _call(session, "semantic_status", {"warm": True})
            search = await _call(session, "semantic_search", {
                "query": "How can periodic boundary faces use identical discretization?",
                "module": "Wave_Optics_Module",
                "limit": 5,
            })
            capabilities = await _call(session, "capabilities", {})
            reset = await _call(session, "semantic_worker_reset", {})
            stopped = await _call(session, "semantic_status", {"warm": False})
            lexical = await _call(session, "manual_search", {
                "query": "CopyFace source destination",
                "limit": 3,
            })
            solver_after = await _call(session, "solver_status", {})

    assert cold["payload"]["configured"] is True
    assert cold["payload"]["worker"]["state"] == "stopped"
    assert cold["payload"]["available"] is False
    assert warm["payload"]["available"] is True
    assert warm["payload"]["worker"]["health"]["status"]["load_count"] == 1
    assert search["payload"]["success"] is True and search["payload"]["results"]
    assert all(item["module"] == "Wave_Optics_Module" for item in search["payload"]["results"])
    assert capabilities["payload"]["semantic_search"]["available"] is True
    assert reset["payload"]["success"] is True
    assert stopped["payload"]["worker"]["state"] == "stopped"
    assert lexical["payload"]["success"] is True and lexical["payload"]["results"]
    for ownership in (solver_before["payload"], solver_after["payload"]):
        assert ownership["lease"]["state"] == "absent"
        assert ownership["external_solver_processes"] == []
        assert ownership["collision"] is False
    return {
        "solver_before": solver_before,
        "cold_status": cold,
        "warm_status": warm,
        "search": search,
        "capabilities_after_health": capabilities,
        "reset": reset,
        "stopped_status": stopped,
        "lexical_after_reset": lexical,
        "solver_after": solver_after,
    }


async def _run() -> dict[str, Any]:
    discovery = []
    for profile in PROFILE_COUNTS:
        discovery.append(await _discover(profile))
    semantic = await _semantic_flow()
    return {"schema_version": "1", "success": True, "discovery": discovery, "semantic": semantic}


def main() -> None:
    output = anyio.run(_run)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_name(f".tmp-{uuid.uuid4().hex[:8]}")
    try:
        with temporary.open("wb") as handle:
            handle.write(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({
        "success": True,
        "profiles": {item["profile"]: item["tool_count"] for item in output["discovery"]},
        "cold_status_seconds": output["semantic"]["cold_status"]["elapsed_seconds"],
        "warm_status_seconds": output["semantic"]["warm_status"]["elapsed_seconds"],
        "search_seconds": output["semantic"]["search"]["elapsed_seconds"],
        "search_top": [
            [item["source"], item["page"]]
            for item in output["semantic"]["search"]["payload"]["results"][:3]
        ],
        "semantic_available_after_health": output["semantic"]["capabilities_after_health"]["payload"]["semantic_search"]["available"],
        "worker_state_after_reset": output["semantic"]["stopped_status"]["payload"]["worker"]["state"],
        "lexical_after_reset_count": output["semantic"]["lexical_after_reset"]["payload"]["count"],
        "artifact": str(OUTPUT),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
