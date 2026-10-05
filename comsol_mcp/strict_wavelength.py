"""Apply explicit wavelength controls and restore the original study properties."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any

from comsol_mcp.contracts.wavelength_policy import (
    StrictWavelengthPolicy as StrictWavelengthPolicy,
)
from comsol_mcp.contracts.wavelength_policy import (
    normalize_policy as normalize_policy,
)
from comsol_mcp.contracts.wavelength_policy import (
    requested_metres as requested_metres,
)
from comsol_mcp.contracts.wavelength_policy import (
    verify_metres as verify_metres,
)


def restore_strict_study_controls(function: Any) -> Any:
    """Validate strict inputs before work and restore the explicit study step."""
    signature = inspect.signature(function)

    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        raw = arguments.get("strict_wavelength_policy")
        if raw is None:
            result = function(*args, **kwargs)
            if isinstance(result, dict):
                recorded = arguments.get("record_wavelength_controls")
                if recorded is None:
                    recorded = arguments["parameter_name"].casefold() in {"wl", "wavelength"}
                result["wavelength_validation"] = {
                    "mode": "record_only" if recorded else "not_requested",
                    "verified": False,
                }
            return result
        from comsol_mcp.tools.properties import _read_property

        policy = normalize_policy(raw)
        if arguments["parameter_name"] not in {"wl", "wavelength"}:
            raise ValueError("strict wavelength mode requires a wavelength parameter")
        if arguments.get("record_wavelength_controls") is False:
            raise ValueError("strict wavelength mode requires recorded controls")
        study = arguments.get("study_name")
        step_tag = arguments.get("study_step_tag")
        if not study or not step_tag:
            raise ValueError("strict wavelength mode requires explicit study and step tags")
        if (
            arguments["study_step_property"] != "plist"
            or arguments["study_step_unit_property"] != "punit"
        ):
            raise ValueError("strict wavelength mode requires plist and punit controls")
        for value in arguments["parameter_values"]:
            requested_metres(value, arguments["parameter_unit"])
        from comsol_mcp.adapter.wavelength_controls import resolve_nodes

        step, dataset, bound_study = resolve_nodes(
            arguments["model"], study, step_tag, policy["dataset_tag"]
        )
        if str(step.getType()) != "Wavelength":
            raise ValueError("strict wavelength mode requires a Wavelength step")
        if bound_study != study:
            raise ValueError("wavelength dataset is not bound to the selected study")
        baseline = {name: _read_property(step, name) for name in ("plist", "punit")}
        kwargs["strict_wavelength_policy"] = policy
        try:
            result = function(*args, **kwargs)
            result["wavelength_validation"] = {
                "mode": "strict",
                "verified": bool(result["success"])
                and not result.get("stopped_early", False)
                and not result.get("n_hook_skipped", 0),
                "control_unit": "m",
                "dataset_tag": policy["dataset_tag"],
            }
            return result
        finally:
            errors = []
            for name, (value, value_type) in baseline.items():
                try:
                    step.set(name, value)
                    if _read_property(step, name) != (value, value_type):
                        raise RuntimeError("study control restoration readback failed")
                except Exception as exc:
                    errors.append(exc)
            if errors:
                raise RuntimeError(
                    "strict wavelength study control restoration failed"
                ) from errors[0]

    return guarded
