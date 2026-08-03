"""Static MCP tool-profile selection and registration filtering."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from comsol_mcp.contracts import bounded_public_schema, structurally_guarded
from comsol_mcp.operation_arbiter import guard_tool_call
from comsol_mcp.settings import (
    PROFILE_ENV,
    SEMANTIC_ENABLED_ENV,
    SETTINGS_PATH_ENV,
    SHARED_SERVER_ENV,
    settings_environment,
)

from .catalog import FEATURE_NAMES, PROFILE_NAMES, TOOL_METADATA

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
    enabled_features: tuple[str, ...] = ()
    feature_sources: tuple[tuple[str, str], ...] = ()
    fallback_used: bool = False
    requested_name: str | None = None
    _registration_token: object | None = field(default=None, repr=False, compare=False)

    def feature_enabled(self, name: str) -> bool:
        return name in self.enabled_features


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
    requested_name = name
    fallback_used = False
    legacy_feature: str | None = None
    if source == "environment" and name in {"semantic_docs", "desktop_shared"}:
        legacy_feature = "semantic_docs" if name == "semantic_docs" else "shared_server"
        name = DEFAULT_PROFILE
        source = "environment_legacy_feature_alias"
        fallback_used = True
    if name not in PROFILE_NAMES:
        name = DEFAULT_PROFILE
        source = f"{source}_invalid_profile_fallback"
        fallback_used = True
    feature_environment = {
        "semantic_docs": SEMANTIC_ENABLED_ENV,
        "shared_server": SHARED_SERVER_ENV,
    }
    enabled_features: list[str] = []
    feature_sources: list[tuple[str, str]] = []
    for feature in FEATURE_NAMES:
        variable = feature_environment[feature]
        raw_flag = environment.get(variable, "false")
        if not isinstance(raw_flag, str) or raw_flag.strip().casefold() not in {"true", "false"}:
            raise ValueError(f"{variable} must be exactly true or false")
        enabled = raw_flag.strip().casefold() == "true" or legacy_feature == feature
        if enabled:
            enabled_features.append(feature)
        feature_sources.append(
            (
                feature,
                "legacy_profile_alias"
                if legacy_feature == feature
                else "environment"
                if variable in original_environment
                else "settings",
            )
        )
    return ProfileSelection(
        name=name,
        environment_variable=environment_variable,
        default_used=default_used,
        source=source,
        enabled_features=tuple(enabled_features),
        feature_sources=tuple(feature_sources),
        fallback_used=fallback_used,
        requested_name=requested_name,
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
        if name in metadata.intended_profiles and metadata.feature_gate is None
    )


def tool_names_for_selection(selection: ProfileSelection) -> frozenset[str]:
    """Return deterministic base-profile tools plus enabled feature overlays."""
    if selection.name not in PROFILE_NAMES:
        raise ValueError("profile selection contains an unknown base profile")
    enabled_features = frozenset(selection.enabled_features)
    if not enabled_features <= set(FEATURE_NAMES):
        raise ValueError("profile selection contains an unknown feature gate")
    return frozenset(
        tool_name
        for tool_name, metadata in TOOL_METADATA.items()
        if selection.name in metadata.intended_profiles
        and (metadata.feature_gate is None or metadata.feature_gate in enabled_features)
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
    if not enabled_names <= tool_names_for_selection(profile_selection):
        raise ValueError("enabled tool names exceed the validated startup selection")
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
    "tool_names_for_selection",
]
