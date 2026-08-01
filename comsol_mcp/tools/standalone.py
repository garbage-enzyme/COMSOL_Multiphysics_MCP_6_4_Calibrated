"""MCP adapters for Python-free standalone COMSOL 6.4 campaigns."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from comsol_mcp.standalone.builder import build_standalone_executable
from comsol_mcp.standalone.control import (
    launch_standalone_campaign,
    read_standalone_results,
    request_standalone_pause,
    tail_standalone_log,
)
from comsol_mcp.standalone.control import (
    standalone_status as read_standalone_status,
)
from comsol_mcp.utils.public_errors import public_error


def _call(callback) -> dict[str, Any]:
    try:
        return callback()
    except ValueError as exc:
        return public_error("standalone_input_rejected", str(exc))
    except FileNotFoundError, FileExistsError:
        return public_error(
            "standalone_artifact_unavailable",
            "A required standalone artifact is absent or the target is not empty.",
        )
    except TimeoutError:
        return public_error(
            "standalone_control_timeout",
            "The standalone control command exceeded its bounded deadline.",
        )
    except OSError, RuntimeError:
        return public_error(
            "standalone_operation_failed",
            "The standalone operation failed; inspect its bounded status and logs.",
        )


def register_standalone_tools(mcp: FastMCP) -> None:
    """Register reviewed build, lifecycle, and solver-free inspection tools."""

    @mcp.tool()
    def standalone_build(output_directory: str) -> dict[str, Any]:
        """Build one reviewed Windows x64 launcher under the owned artifact root."""
        return _call(
            lambda: {
                "success": True,
                **build_standalone_executable(Path(output_directory)),
            }
        )

    @mcp.tool()
    def standalone_start(deployment_directory: str, comsol_root: str) -> dict[str, Any]:
        """Start one detached campaign using only the supplied licensed COMSOL 6.4 root."""
        return _call(
            lambda: launch_standalone_campaign(deployment_directory, comsol_root, resume=False)
        )

    @mcp.tool()
    def standalone_status(deployment_directory: str) -> dict[str, Any]:
        """Read bounded live ownership and durable campaign status without starting COMSOL."""
        return _call(lambda: read_standalone_status(deployment_directory))

    @mcp.tool()
    def standalone_pause(deployment_directory: str) -> dict[str, Any]:
        """Request an attempt-bound pause at the next durable point boundary."""
        return _call(lambda: request_standalone_pause(deployment_directory))

    @mcp.tool()
    def standalone_resume(deployment_directory: str, comsol_root: str) -> dict[str, Any]:
        """Resume exact verified rows under a new detached attempt."""
        return _call(
            lambda: launch_standalone_campaign(deployment_directory, comsol_root, resume=True)
        )

    @mcp.tool()
    def standalone_tail(
        deployment_directory: str,
        log_name: Literal["launcher.log", "compile.log", "current-point.log"] = ("launcher.log"),
        lines: int = 100,
    ) -> dict[str, Any]:
        """Return at most 500 lines from one allowlisted bounded campaign log."""
        return _call(
            lambda: tail_standalone_log(deployment_directory, log_name=log_name, lines=lines)
        )

    @mcp.tool()
    def standalone_results(deployment_directory: str, limit: int = 32) -> dict[str, Any]:
        """Read complete hash-bound point rows and the terminal receipt after process exit."""
        return _call(lambda: read_standalone_results(deployment_directory, limit=limit))


__all__ = ["register_standalone_tools"]
