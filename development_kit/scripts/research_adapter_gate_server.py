"""Repository-only stdio server with private S4 licensed-gate calls."""

from __future__ import annotations

import math
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
_derived_geometry = import_module("comsol_mcp.tools.derived_geometry")
get_derived_geometry_record = _derived_geometry.get_derived_geometry_record
session_manager = import_module("comsol_mcp.tools.session").session_manager
_parameters = import_module("comsol_mcp.tools.parameters")
_mim_patch = import_module("comsol_mcp.tools.mim_patch")


def _backend(model_name: str, derived_model_id: str, manifest: dict[str, Any]):
    model = session_manager.get_model(model_name)
    if model is None:
        raise ValueError("derived model is not loaded")
    record = get_derived_geometry_record(derived_model_id, model_name)
    return ClientapiPeriodicMimPatchBackend(model, record, manifest)


def register_gate_tools(server: Any) -> None:
    """Register private calls used only by the repository licensed gate."""

    @server.tool()
    def research_adapter_gate_clone(
        source_model_name: str,
        new_name: str,
    ) -> dict[str, Any]:
        source = session_manager.get_model(source_model_name)
        client = session_manager.client
        if source is None or client is None:
            return {"success": False, "error_type": "SourceUnavailable"}
        clone = None
        record = None
        registered_name = None
        try:
            clone, record = _derived_geometry.create_derived_geometry_clone(
                source, client, new_name=new_name
            )
            registered_name = session_manager.add_model(clone, cleanup_path=record.backing_path)
            record.model_name = registered_name
            with _derived_geometry._DERIVED_LOCK:
                _derived_geometry._DERIVED[record.derived_model_id] = record
            return {
                "success": True,
                "derived_model_id": record.derived_model_id,
                "model_name": registered_name,
                "source_sha256": record.source_sha256,
                "derived_backing_sha256": record.backing_sha256,
            }
        except Exception as exc:
            cleanup_errors = []
            if registered_name is not None:
                if not session_manager.remove_model(registered_name):
                    cleanup_errors.append("session_remove_failed")
            elif clone is not None:
                try:
                    client.remove(clone)
                except Exception as cleanup_exc:
                    cleanup_errors.append(type(cleanup_exc).__name__)
            if record is not None:
                try:
                    _derived_geometry._cleanup_clone_artifact(record.backing_path)
                except Exception as cleanup_exc:
                    cleanup_errors.append(type(cleanup_exc).__name__)
            return {
                "success": False,
                "error_type": type(exc).__name__,
                "cleanup_errors": cleanup_errors,
            }

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
            return {
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:2048],
            }

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
            return {
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:2048],
            }

    @server.tool()
    def research_adapter_gate_spectrum(
        model_name: str,
        wavelengths_m: list[float],
        wavelength_sync_abs_m: float = 1.0e-15,
    ) -> dict[str, Any]:
        """Run one derived-copy wavelength sweep and restore its study controls."""
        model = session_manager.get_model(model_name)
        if model is None:
            return {"success": False, "error_type": "ModelUnavailable"}
        try:
            values = [float(value) for value in wavelengths_m]
            tolerance = float(wavelength_sync_abs_m)
        except (OverflowError, TypeError, ValueError) as exc:
            return {"success": False, "error_type": type(exc).__name__}
        if (
            not 3 <= len(values) <= 129
            or any(not math.isfinite(value) or value <= 0.0 for value in values)
            or any(right <= left for left, right in zip(values, values[1:]))
            or not math.isfinite(tolerance)
            or tolerance < 0.0
        ):
            return {"success": False, "error_type": "InvalidWavelengthGrid"}

        study = model.java.study("std1")
        features = study.feature()
        sweep = features.get("sweep1")
        wavelength_step = features.get("step1")
        if sweep is None or wavelength_step is None:
            return {"success": False, "error_type": "StudyContractUnavailable"}
        sweep_before = _parameters._sweep_state(sweep)
        step_before = str(wavelength_step.getString("plist"))
        restored = False
        try:
            expressions = [
                "ewfd.Rtotal",
                "ewfd.Ttotal",
                "ewfd.Atotal",
                "wl",
                "c_const/ewfd.freq",
            ]
            rows = []
            for index, requested_value in enumerate(values):
                value_expression = format(requested_value, ".17g")
                planned = {
                    "pname": ["wl"],
                    "plistarr": [value_expression],
                    "punit": ["m"],
                    "sweeptype": "sparse",
                    "active": True,
                }
                _parameters._set_sweep_state(sweep, planned)
                wavelength_step.set("plist", value_expression)
                if _parameters._sweep_state(sweep) != planned:
                    raise ValueError("parametric sweep readback mismatch")
                if str(wavelength_step.getString("plist")) != value_expression:
                    raise ValueError("wavelength step readback mismatch")
                study.run()
                raw = model.evaluate(expressions)
                normalized = _mim_patch._normalize_spectral_rows(raw, len(expressions))
                if len(normalized) != 1:
                    raise ValueError("one-point solve produced an ambiguous spectral result")
                numeric = [float(value) for value in normalized[0]]
                requested = numeric[3]
                solved = numeric[4]
                rows.append(
                    {
                        "point_index": index,
                        "R": numeric[0],
                        "T": numeric[1],
                        "A": numeric[2],
                        "requested_wavelength_m": requested,
                        "evaluated_wavelength_m": requested,
                        "solved_frequency_wavelength_m": solved,
                        "wavelength_sync_abs_m": abs(requested - solved),
                        "closure_abs": abs(numeric[0] + numeric[1] + numeric[2] - 1.0),
                    }
                )
            if any(row["wavelength_sync_abs_m"] > tolerance for row in rows):
                raise ValueError("requested and solved wavelength grids are not synchronized")
            result = {"success": True, "rows": rows}
        except Exception as exc:
            result = {
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:2048],
            }
        finally:
            try:
                _parameters._set_sweep_state(sweep, sweep_before)
                wavelength_step.set("plist", step_before)
                restored = (
                    _parameters._sweep_state(sweep) == sweep_before
                    and str(wavelength_step.getString("plist")) == step_before
                )
            except Exception:
                restored = False
        result["study_controls_restored"] = restored
        result["success"] = result["success"] is True and restored
        return result


def main() -> None:
    server = create_server("COMSOL MCP research adapter licensed gate", profile="full")
    register_gate_tools(server)
    server.run()


if __name__ == "__main__" and mp.current_process().name == "MainProcess":
    main()
