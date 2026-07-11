"""Parameter management tools for COMSOL MCP Server."""

from typing import Optional, Sequence, Union

from jpype import JArray, JString
from mcp.server.fastmcp import FastMCP

from .session import session_manager
from .study import _resolve_study_tag


def _java_string_array(values: Sequence[str]):
    """Build the Java ``String[]`` required by clientapi array properties."""
    return JArray(JString)([str(value) for value in values])


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
    sweep = None
    sweep_tag = None
    for raw_tag in list(feature_list.tags()):
        tag = str(raw_tag)
        feature = feature_list.get(raw_tag)
        try:
            label = str(feature.label()).lower()
        except Exception:
            label = ""
        if tag.lower().startswith(("param", "sweep")) or "parametric" in label:
            sweep = feature
            sweep_tag = tag
            break

    if sweep is None:
        existing = {str(tag) for tag in feature_list.tags()}
        index = 1
        sweep_tag = f"param{index}"
        while sweep_tag in existing:
            index += 1
            sweep_tag = f"param{index}"
        sweep = study.create(sweep_tag, "Parametric")

    value_list = " ".join(str(value) for value in values)
    sweep.set("pname", _java_string_array([parameter_name]))
    sweep.set("plistarr", _java_string_array([value_list]))
    if parameter_unit:
        sweep.set("punit", _java_string_array([parameter_unit]))
    sweep.set("sweeptype", "sparse")
    sweep.active(True)

    return {
        "success": True,
        "study": study_tag,
        "parameter": parameter_name,
        "values": list(values),
        "parameter_unit": parameter_unit,
        "sweep_tag": sweep_tag,
    }


def register_parameter_tools(mcp: FastMCP) -> None:
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
            model.parameter(name, value)
            
            if description:
                model.description(name, description)
            
            return {
                "success": True,
                "parameter": name,
                "value": value,
                "description": description,
            }
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
