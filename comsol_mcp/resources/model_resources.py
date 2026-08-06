"""MCP Resources for COMSOL model information."""

import html
import logging
import re

from mcp.server.mcpserver import MCPServer

from ..tools.session import session_manager

logger = logging.getLogger(__name__)

_BACKTICK_RUN = re.compile(r"`+")


def _markdown_text(value: object) -> str:
    """Render one untrusted value as inert single-line Markdown text."""
    text = html.escape(str(value).replace("\r", " ").replace("\n", " "), quote=False)
    text = text.replace("\\", "\\\\")
    for character in ("`", "*", "_", "[", "]", "|", "~"):
        text = text.replace(character, f"\\{character}")
    return text


def _markdown_code(value: object) -> str:
    """Render one untrusted value in a code span with a non-conflicting fence."""
    text = (
        str(value).replace("\r", " ").replace("\n", " ").replace("\\", "\\\\").replace("|", "\\|")
    )
    longest = max((len(match.group(0)) for match in _BACKTICK_RUN.finditer(text)), default=0)
    fence = "`" * (longest + 1)
    if text.startswith("`") or text.endswith("`"):
        return f"{fence} {text} {fence}"
    return f"{fence}{text}{fence}"


def register_model_resources(mcp: MCPServer) -> None:
    """Register model resources with the MCP server."""

    @mcp.resource("comsol://session/info")
    def get_session_info() -> str:
        """
        Get current COMSOL session information as a resource.

        Returns formatted session status including connection state and loaded models.
        """
        status = session_manager.get_status()

        if not status.get("connected"):
            return "# COMSOL Session Status\n\nNo active COMSOL session.\n\nUse `comsol_start` to start a new session."

        lines = [
            "# COMSOL Session Status",
            "",
            f"**Version:** {_markdown_text(status.get('version', 'unknown'))}",
            f"**Cores:** {_markdown_text(status.get('cores', 'unknown'))}",
            f"**Mode:** {'Standalone' if status.get('standalone') else 'Client-Server'}",
            "",
            "## Loaded Models",
            "",
        ]

        models = status.get("models", [])
        current = status.get("current_model")

        if not models:
            lines.append("No models loaded.")
        else:
            for model in models:
                name = model.get("name", "unnamed")
                marker = " (current)" if name == current else ""
                lines.append(f"- **{_markdown_text(name)}**{marker}")
                if model.get("file"):
                    lines.append(f"  - File: {_markdown_text(model['file'])}")

        return "\n".join(lines)

    @mcp.resource("comsol://model/{name}/tree")
    def get_model_tree(name: str) -> str:
        """
        Get the model tree structure as a resource.

        Args:
            name: Model name

        Returns formatted model tree showing all features.
        """
        model = session_manager.get_model(name)
        if model is None:
            return (
                f"# Model Not Found\n\nModel '{_markdown_text(name)}' not found in current session."
            )

        try:
            lines = [
                f"# Model Tree: {_markdown_text(model.name())}",
                "",
                f"**File:** {_markdown_text(model.file() or 'Not saved')}",
                f"**COMSOL Version:** {_markdown_text(model.version())}",
                "",
            ]

            sections = [
                ("Parameters", "parameters"),
                ("Functions", "functions"),
                ("Components", "components"),
                ("Geometries", "geometries"),
                ("Selections", "selections"),
                ("Physics", "physics"),
                ("Multiphysics", "multiphysics"),
                ("Materials", "materials"),
                ("Meshes", "meshes"),
                ("Studies", "studies"),
                ("Solutions", "solutions"),
                ("Datasets", "datasets"),
                ("Plots", "plots"),
                ("Exports", "exports"),
            ]

            for title, attr in sections:
                items = getattr(model, attr)()
                if items:
                    lines.append(f"## {title}")
                    for item in items:
                        lines.append(f"- {_markdown_text(item)}")
                    lines.append("")

            problems = model.problems()
            if problems:
                lines.append("## Problems")
                for problem in problems:
                    lines.append(
                        f"- **{_markdown_text(problem.get('node', 'unknown'))}**: "
                        f"{_markdown_text(problem.get('message', ''))}"
                    )
                lines.append("")

            return "\n".join(lines)
        except Exception:
            logger.exception("Model-tree resource failed")
            return "# Error\n\nModel tree is unavailable."

    @mcp.resource("comsol://model/{name}/parameters")
    def get_model_parameters(name: str) -> str:
        """
        Get model parameters as a resource.

        Args:
            name: Model name

        Returns formatted parameter list with values and descriptions.
        """
        model = session_manager.get_model(name)
        if model is None:
            return (
                f"# Model Not Found\n\nModel '{_markdown_text(name)}' not found in current session."
            )

        try:
            params = model.parameters()
            descriptions = model.descriptions()

            lines = [
                f"# Parameters: {_markdown_text(model.name())}",
                "",
                "| Name | Value | Description |",
                "|------|-------|-------------|",
            ]

            for param_name, value in params.items():
                desc = descriptions.get(param_name, "")
                lines.append(
                    f"| {_markdown_text(param_name)} | {_markdown_code(value)} | "
                    f"{_markdown_text(desc)} |"
                )

            return "\n".join(lines)
        except Exception:
            logger.exception("Model-parameters resource failed")
            return "# Error\n\nModel parameters are unavailable."

    @mcp.resource("comsol://model/{name}/physics")
    def get_model_physics(name: str) -> str:
        """
        Get model physics interfaces as a resource.

        Args:
            name: Model name

        Returns formatted physics interface list.
        """
        model = session_manager.get_model(name)
        if model is None:
            return (
                f"# Model Not Found\n\nModel '{_markdown_text(name)}' not found in current session."
            )

        try:
            physics_list = model.physics()
            multiphysics_list = model.multiphysics()

            lines = [
                f"# Physics: {_markdown_text(model.name())}",
                "",
            ]

            if physics_list:
                lines.append("## Physics Interfaces")
                for p in physics_list:
                    lines.append(f"- {_markdown_text(p)}")
                lines.append("")

            if multiphysics_list:
                lines.append("## Multiphysics Couplings")
                for m in multiphysics_list:
                    lines.append(f"- {_markdown_text(m)}")
                lines.append("")

            if not physics_list and not multiphysics_list:
                lines.append("No physics interfaces defined.")

            return "\n".join(lines)
        except Exception:
            logger.exception("Model-physics resource failed")
            return "# Error\n\nModel physics is unavailable."
