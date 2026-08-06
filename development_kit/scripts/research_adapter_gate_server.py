"""Repository-only stdio server with private S4 licensed-gate calls."""

from __future__ import annotations

import multiprocessing as mp
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

_adapters = import_module("comsol_mcp.research.adapters")
ClientapiPeriodicMimPatchBackend = _adapters.ClientapiPeriodicMimPatchBackend
adapter_state_sha256 = _adapters.adapter_state_sha256
apply_periodic_mim_patch_candidate = _adapters.apply_periodic_mim_patch_candidate
create_server = import_module("comsol_mcp.server").create_server
get_derived_geometry_record = import_module(
    "comsol_mcp.tools.derived_geometry"
).get_derived_geometry_record
session_manager = import_module("comsol_mcp.tools.session").session_manager


def _backend(model_name: str, derived_model_id: str, manifest: dict[str, Any]):
    model = session_manager.get_model(model_name)
    if model is None:
        raise ValueError("derived model is not loaded")
    record = get_derived_geometry_record(derived_model_id, model_name)
    return ClientapiPeriodicMimPatchBackend(model, record, manifest)


def register_gate_tools(server: Any) -> None:
    """Register two private calls used only by the repository licensed gate."""

    @server.tool()
    def research_adapter_gate_state(
        model_name: str,
        derived_model_id: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            backend = _backend(model_name, derived_model_id, manifest)
            snapshot = backend.snapshot()
            return {
                "success": True,
                "snapshot": snapshot,
                "state_sha256": adapter_state_sha256(manifest, snapshot),
            }
        except Exception as exc:
            return {"success": False, "error_type": type(exc).__name__}

    @server.tool()
    def research_adapter_gate_apply(
        model_name: str,
        derived_model_id: str,
        manifest: dict[str, Any],
        tree_audit: dict[str, Any],
        candidate: dict[str, float],
        expected_state_sha256: str,
    ) -> dict[str, Any]:
        try:
            backend = _backend(model_name, derived_model_id, manifest)
            result = apply_periodic_mim_patch_candidate(
                backend,
                manifest,
                tree_audit,
                candidate,
                expected_state_sha256=expected_state_sha256,
            )
            return {**result, "snapshot": backend.snapshot() if result["success"] else None}
        except Exception as exc:
            return {"success": False, "error_type": type(exc).__name__}


def main() -> None:
    server = create_server("COMSOL MCP research adapter licensed gate", profile="full")
    register_gate_tools(server)
    server.run()


if __name__ == "__main__" and mp.current_process().name == "MainProcess":
    main()
