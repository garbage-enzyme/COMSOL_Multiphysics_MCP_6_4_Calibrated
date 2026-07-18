"""Study and solving tools for COMSOL MCP Server."""

from typing import Optional, Sequence
from mcp.server.fastmcp import FastMCP

from .session import session_manager
from ..async_handler.solver import async_solver


def _resolve_study_tag(model, study_name: Optional[str]) -> Optional[str]:
    """Resolve a study identifier (tag OR label) to its tag.

    mph's high-level ``model.solve(name)`` looks studies up by *label*
    (e.g. "研究 1"), but ``study_create`` returns and most callers pass the
    *tag* (e.g. "std1"). This helper accepts either form and returns the
    canonical tag so we can call the Java API ``jm.study(tag).run()``
    directly.

    Returns ``None`` when ``study_name`` is ``None`` (meaning "all studies").
    Raises ``ValueError`` if no matching study is found.
    """
    if study_name is None:
        return None
    jm = model.java
    study_list = jm.study()
    tags = [str(tag) for tag in study_list.tags()]
    if study_name in tags:
        return study_name
    for tag in tags:
        try:
            if study_list.get(tag).label() == study_name:
                return tag
        except Exception:
            pass
    raise ValueError(
        f"Study '{study_name}' not found. Available tags: {tags}"
    )


def list_studies(model) -> dict:
    """List studies and steps through stable clientapi tags and labels."""
    study_list = model.java.study()
    studies = []
    for raw_tag in list(study_list.tags()):
        tag = str(raw_tag)
        study = study_list.get(tag)
        info = {"tag": tag}
        try:
            info["label"] = str(study.label())
        except Exception:
            info["label"] = tag

        steps = []
        try:
            feature_list = study.feature()
            for raw_step_tag in list(feature_list.tags()):
                step_tag = str(raw_step_tag)
                step = feature_list.get(step_tag)
                step_info = {"tag": step_tag}
                try:
                    step_info["label"] = str(step.label())
                except Exception:
                    step_info["label"] = step_tag
                steps.append(step_info)
        except Exception:
            pass
        info["steps"] = steps
        studies.append(info)
    return {"success": True, "studies": studies, "count": len(studies)}


def register_study_tools(mcp: FastMCP) -> None:
    """Register study and solving tools with the MCP server."""
    
    @mcp.tool()
    def study_list(model_name: Optional[str] = None) -> dict:
        """
        List all studies in a model.

        Args:
            model_name: Model name (default: current model)

        Returns:
            List of study names with their types
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            return list_studies(model)
        except Exception as e:
            return {"success": False, "error": f"Failed to list studies: {str(e)}"}

    @mcp.tool()
    def study_create(
        study_type: str = "Stationary",
        study_name: Optional[str] = None,
        time_list: Optional[Sequence] = None,
        time_unit: str = "s",
        model_name: Optional[str] = None
    ) -> dict:
        """
        Create a new study in the model.

        Common study types:
        - "Stationary": Stationary study (most common for electrostatics, structural)
        - "Transient": Time-dependent study (also accepts "TimeDependent"/"time")
        - "Eigenfrequency": Eigenfrequency analysis
        - "FrequencyDomain": Frequency domain study (also accepts "Frequency")
        - "Perturbation": Perturbation study

        Args:
            study_type: Type of study to create
            study_name: Optional name/tag for the study
            time_list: For Transient studies, the list of output times.
                Elements are numbers expressed in ``time_unit`` (default s).
                Typical usage for ns simulation: ``time_list=[0,0.1e-9,1e-9]``
                or ``time_list=[0,1,10]`` with ``time_unit="ns"``.
            time_unit: Unit string for ``time_list`` (default "s")
            model_name: Model name (default: current model)

        Returns:
            Created study info
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }

        try:
            jm = model.java
            existing_studies = jm.study().size()
            study_tag = study_name or f"std{existing_studies + 1}"

            # clientapi (mph 1.3+ standalone) requires the FULL step-type name
            # (e.g. "Stationary", "Transient"), NOT the short tag ("stat").
            # Direct-Model API used the short form, but clientapi rejects it
            # with "Operation_cannot_be_created_in_this_context".  Note that
            # the *real* tag for a time-dependent study step is "Transient"
            # (NOT "TimeDependent"); the historical alias is normalised here.
            SHORT_TO_FULL = {
                "stat": "Stationary",
                "time": "Transient",
                "timedependent": "Transient",
                "transient": "Transient",
                "eig": "Eigenfrequency",
                "freq": "FrequencyDomain",
                "frequency": "FrequencyDomain",
                "pert": "Perturbation",
            }
            step_type = SHORT_TO_FULL.get(study_type.lower(), study_type)

            study = jm.study().create(study_tag)
            study.create("step1", step_type)

            tlist_warning = None
            if step_type == "Transient" and time_list is not None:
                try:
                    step = study.feature("step1")
                    tlist_str = " ".join(
                        f"{float(t)}[{time_unit}]" for t in time_list
                    )
                    step.set("tlist", tlist_str)
                except Exception as exc:
                    tlist_warning = (
                        f"Study created but failed to set tlist: {exc}"
                    )

            result = {
                "success": True,
                "study": study_tag,
                "type": study_type,
                "step_type": step_type,
                "model": model.name(),
            }
            if step_type == "Transient" and time_list is None:
                result["warning"] = (
                    "Transient study created without time_list. Set it via "
                    "study_create(time_list=...) or the COMSOL GUI before "
                    "solving."
                )
            if tlist_warning:
                result["warning"] = tlist_warning
            return result
        except Exception as e:
            return {"success": False, "error": f"Failed to create study: {str(e)}"}
    
    @mcp.tool()
    def study_solve(
        study_name: Optional[str] = None,
        model_name: Optional[str] = None,
        wait: bool = True,
        timeout: Optional[float] = None
    ) -> dict:
        """
        Solve a study (synchronous by default).
        
        Args:
            study_name: Study to solve (None for all studies)
            model_name: Model name (default: current model)
            wait: If True, wait for completion; if False, return immediately
            timeout: Maximum wait time in seconds (only used if wait=True)
        
        Returns:
            Solution status, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        if async_solver.is_running:
            return {
                "success": False,
                "error": "Another solving operation is in progress. Use study_get_progress to check status."
            }
        
        try:
            # Resolve tag OR label to a canonical tag, then use the Java
            # API directly. mph's ``model.solve(name)`` only accepts the
            # study *label* (e.g. "研究 1"), but callers typically pass the
            # *tag* returned by ``study_create`` (e.g. "std1").
            tag = _resolve_study_tag(model, study_name)
            jm = model.java
            if wait:
                if tag is None:
                    for t in jm.study().tags():
                        jm.study(t).run()
                else:
                    jm.study(tag).run()
                return {
                    "success": True,
                    "study": study_name,
                    "resolved_tag": tag,
                    "message": "Solving completed.",
                }
            else:
                started = async_solver.start_solve(model, tag)
                if started:
                    return {
                        "success": True,
                        "study": study_name,
                        "resolved_tag": tag,
                        "message": "Solving started in background. Use study_get_progress to monitor.",
                        "async": True,
                    }
                else:
                    return {
                        "success": False,
                        "error": "Failed to start async solver."
                    }
        except Exception as e:
            return {"success": False, "error": f"Failed to solve: {str(e)}"}
    
    @mcp.tool()
    def study_solve_async(
        study_name: Optional[str] = None,
        model_name: Optional[str] = None
    ) -> dict:
        """
        Start solving a study in the background (asynchronous).
        
        Use study_get_progress to inspect synthetic lifecycle checkpoints.
        study_cancel records a cooperative request but cannot interrupt a
        blocking COMSOL study.run() call.
        
        Args:
            study_name: Study to solve (None for all studies)
            model_name: Model name (default: current model)
        
        Returns:
            Confirmation that solving started, or error message
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        if async_solver.is_running:
            progress = async_solver.get_progress()
            return {
                "success": False,
                "error": "Another solving operation is already in progress.",
                "current_progress": progress,
            }
        
        try:
            tag = _resolve_study_tag(model, study_name)
            started = async_solver.start_solve(model, tag)
            if started:
                return {
                    "success": True,
                    "study": study_name,
                    "resolved_tag": tag,
                    "model": model.name(),
                    "message": "Solving started in background.",
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to start async solver."
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to start solving: {str(e)}"}
    
    @mcp.tool()
    def study_get_progress() -> dict:
        """
        Get synthetic lifecycle status for the current solving operation.

        The numeric progress field is not COMSOL solver percentage. While
        study.run() is blocking, it remains at the pre-solve checkpoint.
        
        Returns:
            Status, synthetic checkpoint value, and elapsed time
        """
        progress = async_solver.get_progress()
        return {
            "success": True,
            "progress": progress,
        }
    
    @mcp.tool()
    def study_cancel() -> dict:
        """
        Request cooperative cancellation of the current solving operation.

        This cannot interrupt a blocking COMSOL study.run() call. It can only
        stop work before the blocking call begins or between separate studies.
        
        Returns:
            Cancellation status
        """
        if async_solver.cancel():
            return {
                "success": True,
                "requested": True,
                "interrupts_solver": False,
                "message": (
                    "Cancellation flag recorded. A blocking COMSOL study.run() "
                    "will continue until it returns."
                ),
            }
        return {
            "success": False,
            "message": "No solving operation in progress.",
        }
    
    @mcp.tool()
    def study_wait(timeout: Optional[float] = None) -> dict:
        """
        Wait for the current solving operation to complete.
        
        Args:
            timeout: Maximum time to wait in seconds (None for indefinite)
        
        Returns:
            Final progress status
        """
        completed = async_solver.wait(timeout=timeout)
        progress = async_solver.get_progress()
        
        return {
            "success": True,
            "completed": completed,
            "progress": progress,
        }
    
    @mcp.tool()
    def solutions_list(model_name: Optional[str] = None) -> dict:
        """
        List all solutions in a model.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of solution configurations
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            solutions = model.solutions()
            return {
                "success": True,
                "solutions": solutions,
                "count": len(solutions),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list solutions: {str(e)}"}
    
    @mcp.tool()
    def datasets_list(model_name: Optional[str] = None) -> dict:
        """
        List all datasets in a model.
        
        Datasets represent solution data that can be evaluated or visualized.
        
        Args:
            model_name: Model name (default: current model)
        
        Returns:
            List of dataset names
        """
        model = session_manager.get_model(model_name)
        if model is None:
            return {
                "success": False,
                "error": f"Model not found: {model_name or 'no current model'}"
            }
        
        try:
            datasets = model.datasets()
            return {
                "success": True,
                "datasets": datasets,
                "count": len(datasets),
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to list datasets: {str(e)}"}
