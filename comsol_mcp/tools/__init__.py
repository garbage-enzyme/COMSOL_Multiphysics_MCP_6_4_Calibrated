"""Lazy MCP tool registration that keeps startup profile gates solver-free."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from importlib import import_module
from typing import Any, Callable

from .catalog import registrars_for_profile

_REGISTRAR_PATHS = registrars_for_profile("full")


def _load_symbol(path: str) -> Any:
    module_name, symbol_name = path.rsplit(".", 1)
    return getattr(import_module(module_name), symbol_name)


class _LazyRegistrarSequence(Sequence[Callable[..., Any]]):
    def __len__(self) -> int:
        return len(_REGISTRAR_PATHS)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(_load_symbol(path) for path in _REGISTRAR_PATHS[index])
        return _load_symbol(_REGISTRAR_PATHS[index])

    def __iter__(self) -> Iterator[Callable[..., Any]]:
        return (_load_symbol(path) for path in _REGISTRAR_PATHS)


TOOL_REGISTRARS: Sequence[Callable[..., Any]] = _LazyRegistrarSequence()


def register_tool_modules(mcp, profile="full") -> None:
    """Import and register only after the static profile gate is accepted."""
    from .profiles import ProfileSelection, resolve_profile, tool_names_for_profile

    selection = (
        profile if isinstance(profile, ProfileSelection) else resolve_profile(profile)
    )
    enabled_names = tool_names_for_profile(selection.name)
    for registrar_path in registrars_for_profile(selection.name):
        register = _load_symbol(registrar_path)
        from .profiles import register_profiled

        register_profiled(mcp, register, enabled_names, selection)


_REGISTER_EXPORTS = {
    path.rsplit(".", 1)[-1]: path for path in _REGISTRAR_PATHS
}


def __getattr__(name: str) -> Any:
    path = _REGISTER_EXPORTS.get(name)
    if path is not None:
        return _load_symbol(path)
    raise AttributeError(name)


__all__ = [*_REGISTER_EXPORTS, "TOOL_REGISTRARS", "register_tool_modules"]
