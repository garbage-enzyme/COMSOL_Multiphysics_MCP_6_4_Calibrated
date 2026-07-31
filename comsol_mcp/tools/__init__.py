"""Lazy MCP tool registration that keeps startup profile gates solver-free."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .catalog import registrars_for_profile


def _load_symbol(path: str) -> Any:
    module_name, symbol_name = path.rsplit(".", 1)
    return getattr(import_module(module_name), symbol_name)


def register_tool_modules(mcp, profile="full") -> None:
    """Import and register only after the static profile gate is accepted."""
    from .profiles import (
        ProfileSelection,
        _is_validated_profile_selection,
        resolve_profile,
        tool_names_for_profile,
    )

    if isinstance(profile, ProfileSelection):
        if not _is_validated_profile_selection(profile):
            raise ValueError("profile selection was not produced by resolve_profile")
        selection = profile
    else:
        selection = resolve_profile(profile)
    enabled_names = tool_names_for_profile(selection.name)
    for registrar_path in registrars_for_profile(selection.name):
        register = _load_symbol(registrar_path)
        from .profiles import register_profiled

        register_profiled(mcp, register, enabled_names, selection)


__all__ = ["register_tool_modules"]
