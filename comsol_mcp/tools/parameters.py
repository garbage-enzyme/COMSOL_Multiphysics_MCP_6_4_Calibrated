"""Parameter management tools for COMSOL MCP Server."""

from typing import Any, Optional, Sequence, Union

from mcp.server.mcpserver import MCPServer

from .session import session_manager
from .study import _resolve_study_tag


def _java_string_array(values: Sequence[str]):
    """Build the Java ``String[]`` required by clientapi array properties."""
    from jpype import JArray, JString

    return JArray(JString)([str(value) for value in values])


_SWEEP_ARRAY_PROPERTIES = ("pname", "plistarr", "punit")


def _sweep_state(sweep: Any) -> dict[str, Any]:
    arrays = {}
    for name in _SWEEP_ARRAY_PROPERTIES:
        arrays[name] = [str(value) for value in sweep.getStringArray(name)]
    return {
        **arrays,
        "sweeptype": str(sweep.getString("sweeptype")),
        "active": bool(sweep.isActive()),
    }


def _set_sweep_state(sweep: Any, state: dict[str, Any]) -> None:
    for name in _SWEEP_ARRAY_PROPERTIES:
        sweep.set(name, _java_string_array(state[name]))
    sweep.set("sweeptype", state["sweeptype"])
    sweep.active(state["active"])


def _remove_sweep(feature_list: Any, sweep_tag: str) -> None:
    feature_list.remove(sweep_tag)


def set_parameter(
    model: Any,
    name: str,
    value: str,
    *,
    description: Optional[str],
) -> dict[str, Any]:
    """Set one parameter and description as a readback-proved transaction."""
    existing = {str(key): str(item) for key, item in model.parameters(evaluate=False).items()}
    existed = name in existing
    old_value = existing.get(name)
    old_description = str(model.description(name)) if existed else None
    try:
        model.parameter(name, value)
        if description is not None:
            model.description(name, description)
        actual_value = str(model.parameter(name, evaluate=False))
        actual_description = str(model.description(name))
        description_matches = (
            description is None and (not existed or actual_description == old_description)
        ) or (description is not None and actual_description == description)
        if actual_value != value or not description_matches:
            raise ValueError("parameter readback mismatch")
    except Exception as exc:
        rollback_errors = []
        try:
            if existed:
                model.parameter(name, old_value)
                model.description(name, old_description)
                if (
                    str(model.parameter(name, evaluate=False)) != old_value
                    or str(model.description(name)) != old_description
                ):
                    raise ValueError("restored parameter readback mismatch")
            else:
                model.java.param().remove(name)
                if name in {str(key) for key in model.parameters(evaluate=False)}:
                    raise ValueError("new parameter survived rollback")
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc)[:300])
        return {
            "success": False,
            "error": f"Failed to set parameter: {str(exc)[:300]}",
            "rolled_back": not rollback_errors,
            "rollback_errors": rollback_errors,
        }
    return {
        "success": True,
        "parameter": name,
        "value": actual_value,
        "description": actual_description,
    }


def setup_parametric_sweep(
    model,
    parameter_name: str,
    values: Sequence[Union[str, float]],
    *,
    study_name: Optional[str] = None,
    parameter_unit: Optional[str] = None,
) -> dict:
    """Create or update an active clientapi Parametric study feature."""
    if not parameter_name.strip():
        return {"success": False, "error": "parameter_name must not be empty."}
    if not values:
        return {"success": False, "error": "values must not be empty."}

    jm = model.java
    study_tags = list(jm.study().tags())
    if not study_tags:
        return {"success": False, "error": "No studies found in model."}
    study_tag = (
        _resolve_study_tag(model, study_name)
        if study_name
        else str(study_tags[0])
    )
    study = jm.study(study_tag)

    feature_list = study.feature()
    candidates = []
    for raw_tag in list(feature_list.tags()):
        tag = str(raw_tag)
        feature = feature_list.get(raw_tag)
        try:
            feature_type = str(feature.getType())
        except Exception:
            feature_type = ""
        if feature_type.casefold() == "parametric":
            candidates.append((tag, feature))
    if len(candidates) > 1:
        return {
            "success": False,
            "error": "Multiple Parametric sweep features found; selection is ambiguous.",
        }

    created = not candidates
    if candidates:
        sweep_tag, sweep = candidates[0]
        try:
            before = _sweep_state(sweep)
        except Exception as exc:
            return {"success": False, "error": f"Cannot snapshot Parametric sweep: {exc}"}
    else:
        existing = {str(tag) for tag in feature_list.tags()}
        index = 1
        sweep_tag = f"param{index}"
        while sweep_tag in existing:
            index += 1
            sweep_tag = f"param{index}"
        sweep = study.create(sweep_tag, "Parametric")
        before = None

    value_list = " ".join(str(value) for value in values)
    planned = {
        "pname": [parameter_name],
        "plistarr": [value_list],
        "punit": [parameter_unit.strip() if parameter_unit else ""],
        "sweeptype": "sparse",
        "active": True,
    }
    try:
        _set_sweep_state(sweep, planned)
        if _sweep_state(sweep) != planned:
            raise ValueError("Parametric sweep readback mismatch")
    except Exception as exc:
        rollback_errors = []
        try:
            if created:
                _remove_sweep(feature_list, sweep_tag)
                if sweep_tag in {str(tag) for tag in feature_list.tags()}:
                    raise ValueError("created Parametric sweep survived rollback")
            else:
                _set_sweep_state(sweep, before)
                if _sweep_state(sweep) != before:
                    raise ValueError("restored Parametric sweep readback mismatch")
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc)[:300])
        return {
            "success": False,
            "error": f"Failed to configure Parametric sweep: {str(exc)[:300]}",
            "rolled_back": not rollback_errors,
            "rollback_errors": rollback_errors,
        }

    return {
        "success": True,
        "study": study_tag,
        "parameter": parameter_name,
        "values": list(values),
        "parameter_unit": parameter_unit,
        "sweep_tag": sweep_tag,
    }


def register_parameter_tools(mcp: MCPServer) -> None:
    """Register parameter management tools with the MCP server."""
    
    @mcp.tool()
    def param_get(
        name: str,
        model_name: Optional[str] = None,
        evaluate: bool = False
    ) -> dict:
        """
        Get the value of a model parameter.
        
        Args:
            name: Parameter name
            model_name: Model name (default: current model)
            evaluate: If True, return evaluated numerical value; if False, return expression string
        
        Returns:
            Parameter value and description, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            value = model.parameter(name, evaluate=evaluate)
            description = model.description(name)
            
            return {
                "success": True,
                "parameter": name,
                "value": value,
                "description": description,
                "evaluated": evaluate,
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to get parameter: {str(e)}"}
    
    @mcp.tool()
    def param_set(
        name: str,
        value: str,
        model_name: Optional[str] = None,
        description: Optional[str] = None
    ) -> dict:
        """
        Set the value of a model parameter.
        
        Args:
            name: Parameter name
            value: Parameter value (can include units, e.g., "5[V]", "1.5[mm]")
            model_name: Model name (default: current model)
            description: Optional description for the parameter
        
        Returns:
            Confirmation with new value, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            return set_parameter(
                model,
                name,
                value,
                description=description,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to set parameter: {str(e)}"}
    
    @mcp.tool()
    def param_list(
        model_name: Optional[str] = None,
        evaluate: bool = False
    ) -> dict:
        """
        List all parameters in a model.
        
        Args:
            model_name: Model name (default: current model)
            evaluate: If True, return numerical values; if False, return expressions
        
        Returns:
            Dictionary of all parameters with values and descriptions
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            params = model.parameters(evaluate=evaluate)
            descriptions = model.descriptions()
            
            param_list = []
            for name, value in params.items():
                param_list.append({
                    "name": name,
                    "value": value,
                    "description": descriptions.get(name, ""),
                })
            
            return {
                "success": True,
                "parameters": param_list,
                "count": len(param_list),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list parameters: {str(e)}"}
    
    @mcp.tool()
    def param_sweep_setup(
        parameter_name: str,
        values: list[Union[str, float]],
        model_name: Optional[str] = None,
        study_name: Optional[str] = None,
        parameter_unit: Optional[str] = None,
    ) -> dict:
        """
        Set up a parametric sweep for a parameter.
        
        Args:
            parameter_name: Name of the parameter to sweep
            values: List of parameter values to sweep through
            model_name: Model name (default: current model)
            study_name: Study to attach sweep to (default: first study)
            parameter_unit: Optional COMSOL unit for the sweep values.
        
        Returns:
            Sweep configuration confirmation, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            return setup_parametric_sweep(
                model,
                parameter_name,
                values,
                study_name=study_name,
                parameter_unit=parameter_unit,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to set up parametric sweep: {str(e)}"}
    
    @mcp.tool()
    def param_description(
        name: str,
        text: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Get or set the description of a parameter.
        
        Args:
            name: Parameter name
            text: New description text (if None, returns current description)
            model_name: Model name (default: current model)
        
        Returns:
            Parameter description, or confirmation of update
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            if text is not None:
                model.description(name, text)
                return {
                    "success": True,
                    "parameter": name,
                    "description": text,
                }
            else:
                description = model.description(name)
                return {
                    "success": True,
                    "parameter": name,
                    "description": description,
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to get/set description: {str(e)}"}
