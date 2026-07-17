"""Static MCP tool-profile selection and registration filtering."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable, Mapping

from .catalog import PROFILE_NAMES, TOOL_METADATA
from src.operation_arbiter import guard_tool_call


PROFILE_ENV_VAR = "COMSOL_MCP_PROFILE"
DEFAULT_PROFILE = "core"

PROFILE_DESCRIPTIONS = {
    "core": "Default mature ownership, job, session, inspection, one-point solve, and manual surface.",
    "basic_fem": "Core plus typed conventional FEM construction and bounded exports.",
    "wave_optics": "Recommended metasurface profile: core plus material preview, field-dataset discovery, visual-review contracts, Wave Optics preflight, point audit, and staged workflows.",
    "semantic_docs": "Core plus isolated immutable BM25/vector manual retrieval and worker controls.",
    "experimental": "Core plus explicitly risky, generic, asynchronous, and project helpers.",
    "full": "Backward-compatible discovery surface with legacy broad-path behavior and weaker containment guarantees.",
}

PROFILE_MATURITY = {
    "core": "verified",
    "basic_fem": "verified",
    "wave_optics": "experimental",
    "semantic_docs": "experimental",
    "experimental": "experimental",
    "full": "compatibility",
}


@dataclass(frozen=True)
class ProfileSelection:
    """One startup-time profile decision and its provenance."""

    name: str
    environment_variable: str
    default_used: bool
    source: str


def resolve_profile(
    requested: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProfileSelection:
    """Resolve one explicit or environment-selected static profile."""
    environment = os.environ if environ is None else environ
    if requested is not None:
        raw_name = requested
        source = "explicit_argument"
        default_used = False
    elif PROFILE_ENV_VAR in environment:
        raw_name = environment[PROFILE_ENV_VAR]
        source = "environment"
        default_used = False
    else:
        raw_name = DEFAULT_PROFILE
        source = "default"
        default_used = True

    name = raw_name.strip().lower()
    if name not in PROFILE_NAMES:
        available = ", ".join(PROFILE_NAMES)
        raise ValueError(
            f"Invalid {PROFILE_ENV_VAR} profile {raw_name!r}; expected one of: {available}"
        )
    return ProfileSelection(
        name=name,
        environment_variable=PROFILE_ENV_VAR,
        default_used=default_used,
        source=source,
    )


def tool_names_for_profile(profile: str) -> frozenset[str]:
    """Return the exact canonical tool-name set for a validated profile."""
    selection = resolve_profile(profile, environ={})
    return frozenset(
        name
        for name, metadata in TOOL_METADATA.items()
        if selection.name in metadata.intended_profiles
    )


class ProfiledRegistrar:
    """Filter ``@mcp.tool`` registration without mutating manager internals."""

    def __init__(
        self,
        server: Any,
        enabled_names: frozenset[str],
        profile_selection: ProfileSelection,
    ):
        self._server = server
        self._enabled_names = enabled_names
        self.profile_selection = profile_selection

    def tool(self, *args: Any, **kwargs: Any) -> Callable:
        real_decorator = self._server.tool(*args, **kwargs)

        def decorator(function: Callable) -> Callable:
            name = kwargs.get("name") or function.__name__
            if name in self._enabled_names:
                metadata = TOOL_METADATA[name]
                guarded = guard_tool_call(
                    function,
                    tool_name=name,
                    side_effect_class=metadata.side_effect_class,
                    concurrency_class=metadata.concurrency_class,
                    profile_name=self.profile_selection.name,
                    requires_model_revision=metadata.requires_model_revision,
                    advances_model_revision=metadata.advances_model_revision,
                )
                return real_decorator(guarded)
            return function

        return decorator


def register_profiled(
    server: Any,
    registrar: Callable[[Any], None],
    enabled_names: frozenset[str],
    profile_selection: ProfileSelection,
) -> None:
    """Run one existing registrar through a static name filter."""
    registrar(ProfiledRegistrar(server, enabled_names, profile_selection))


__all__ = [
    "DEFAULT_PROFILE",
    "PROFILE_DESCRIPTIONS",
    "PROFILE_ENV_VAR",
    "PROFILE_MATURITY",
    "ProfileSelection",
    "ProfiledRegistrar",
    "register_profiled",
    "resolve_profile",
    "tool_names_for_profile",
]
