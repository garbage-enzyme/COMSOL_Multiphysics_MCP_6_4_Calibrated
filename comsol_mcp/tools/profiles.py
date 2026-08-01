"""Static MCP tool-profile selection and registration filtering."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from comsol_mcp.contracts import bounded_public_schema, structurally_guarded
from comsol_mcp.operation_arbiter import guard_tool_call
from comsol_mcp.settings import PROFILE_ENV, SETTINGS_PATH_ENV, settings_environment
from comsol_mcp.shared_session.contracts import (
    SHARED_SERVER_PROFILE,
    normalize_shared_server_feature_gate,
)

from .catalog import PROFILE_NAMES, TOOL_METADATA

PROFILE_ENV_VAR = PROFILE_ENV
DEFAULT_PROFILE = "core"
_PROFILE_SELECTION_TOKEN = object()

PROFILE_DESCRIPTIONS = {
    "core": (
        "Default mature ownership, job, session, inspection, one-point solve, and manual surface."
    ),
    "basic_fem": (
        "Core plus typed conventional FEM construction, bounded exports, and standalone execution."
    ),
    "wave_optics": (
        "Recommended metasurface profile: core plus material preview, field-dataset "
        "discovery, visual-review contracts, Wave Optics preflight, point audit, and "
        "staged workflows."
    ),
    "semantic_docs": (
        "Core plus isolated immutable BM25/vector manual retrieval and worker controls."
    ),
    "desktop_shared": (
        "Default-off non-owning local COMSOL Server and shared Desktop collaboration surface."
    ),
    "experimental": "Core plus explicitly risky, generic, asynchronous, and project helpers.",
    "full": (
        "Backward-compatible discovery surface with legacy broad-path behavior and "
        "weaker containment guarantees."
    ),
}

PROFILE_MATURITY = {
    "core": "verified",
    "basic_fem": "verified",
    "wave_optics": "experimental",
    "semantic_docs": "experimental",
    "desktop_shared": "experimental",
    "experimental": "experimental",
    "full": "compatibility",
}


@dataclass(frozen=True)
class ProfileSelection:
    """One startup-time profile decision and its provenance."""

    name: str
    environment_variable: str | None
    default_used: bool
    source: str
    _registration_token: object | None = field(default=None, repr=False, compare=False)


def _is_validated_profile_selection(selection: ProfileSelection) -> bool:
    return selection._registration_token is _PROFILE_SELECTION_TOKEN


def resolve_profile(
    requested: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProfileSelection:
    """Resolve one explicit or environment-selected static profile."""
    environment = settings_environment(environ)
    original_environment = os.environ if environ is None else environ
    if requested is not None:
        raw_name = requested
        source = "explicit_argument"
        default_used = False
        environment_variable = None
    elif PROFILE_ENV_VAR in original_environment:
        raw_name = environment.get(PROFILE_ENV_VAR, original_environment[PROFILE_ENV_VAR])
        source = "environment"
        default_used = False
        environment_variable = PROFILE_ENV_VAR
    else:
        raw_name = environment.get(PROFILE_ENV_VAR, DEFAULT_PROFILE)
        source = "settings"
        default_used = SETTINGS_PATH_ENV not in original_environment
        environment_variable = (
            SETTINGS_PATH_ENV if SETTINGS_PATH_ENV in original_environment else None
        )

    if not isinstance(raw_name, str):
        raise ValueError(f"Invalid {PROFILE_ENV_VAR} profile type")
    name = raw_name.strip().lower()
    if name not in PROFILE_NAMES:
        available = ", ".join(PROFILE_NAMES)
        raise ValueError(
            f"Invalid {PROFILE_ENV_VAR} profile {raw_name!r}; expected one of: {available}"
        )
    if name == SHARED_SERVER_PROFILE:
        gate = normalize_shared_server_feature_gate(name, environ=environment)
        if not gate.feature_enabled:
            raise ValueError(
                f"Profile {SHARED_SERVER_PROFILE!r} requires "
                f"{gate.environment_variable}=true and an MCP host restart"
            )
    return ProfileSelection(
        name=name,
        environment_variable=environment_variable,
        default_used=default_used,
        source=source,
        _registration_token=_PROFILE_SELECTION_TOKEN,
    )


def tool_names_for_profile(profile: str) -> frozenset[str]:
    """Return the exact canonical tool-name set for a validated profile."""
    if not isinstance(profile, str):
        raise ValueError("profile name must be a string")
    name = profile.strip().lower()
    if name not in PROFILE_NAMES:
        available = ", ".join(PROFILE_NAMES)
        raise ValueError(f"Invalid profile {profile!r}; expected one of: {available}")
    return frozenset(
        tool_name
        for tool_name, metadata in TOOL_METADATA.items()
        if name in metadata.intended_profiles
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
                    structurally_guarded(function),
                    tool_name=name,
                    side_effect_class=metadata.side_effect_class,
                    concurrency_class=metadata.concurrency_class,
                    profile_name=self.profile_selection.name,
                    requires_model_revision=metadata.requires_model_revision,
                    advances_model_revision=metadata.advances_model_revision,
                )
                registered = real_decorator(guarded)
                tool = self._server._tool_manager._tools[name]
                tool.parameters = bounded_public_schema(tool.parameters)
                argument_model = tool.fn_metadata.arg_model
                argument_model.model_config["extra"] = "forbid"
                argument_model.model_rebuild(force=True)
                return registered
            return function

        return decorator


def register_profiled(
    server: Any,
    registrar: Callable[[Any], None],
    enabled_names: frozenset[str],
    profile_selection: ProfileSelection,
) -> None:
    """Run one existing registrar through a static name filter."""
    if not _is_validated_profile_selection(profile_selection):
        raise ValueError("profile selection was not produced by resolve_profile")
    if not enabled_names <= tool_names_for_profile(profile_selection.name):
        raise ValueError("enabled tool names exceed the validated profile")
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
