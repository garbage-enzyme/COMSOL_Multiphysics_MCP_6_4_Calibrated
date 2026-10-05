"""Experimental Linux support and fail-closed native-operation boundaries."""

from __future__ import annotations

import inspect
import os
import sys
from functools import wraps
from typing import Any

_PORTABLE_CONTROL_TOOLS = frozenset(
    {
        "capabilities",
        "catalog",
        "comsol_status",
        "evidence_integrity_status",
        "job_status",
        "job_tail",
        "settings.start",
        "semantic_status",
    }
)
_WINDOWS_ONLY_OFFLINE_TOOLS = frozenset({"standalone_build", "semantic_search", "solver_preflight"})


def native_solver_enabled() -> bool:
    """Return whether this platform has an accepted native solver lane."""
    return os.name == "nt"


def require_native_solver() -> None:
    """Refuse before native imports, ownership admission or worker creation."""
    if not native_solver_enabled():
        raise RuntimeError("linux_solver_not_enabled")


def platform_capabilities() -> dict[str, Any]:
    """Return redacted platform support without probing or constructing a client."""
    enabled = native_solver_enabled()
    return {
        "platform": "windows" if enabled else "linux" if sys.platform == "linux" else "other",
        "support": "supported"
        if enabled
        else "experimental_solver_free"
        if sys.platform == "linux"
        else "unsupported",
        "native_solver_enabled": enabled,
        "linux_semantic_search_accepted": False,
    }


def tool_available(tool_name: str, concurrency_class: str) -> bool:
    if native_solver_enabled():
        return True
    return tool_name in _PORTABLE_CONTROL_TOOLS or (
        concurrency_class == "solver_free" and tool_name not in _WINDOWS_ONLY_OFFLINE_TOOLS
    )


def guard_platform_call(function: Any, *, tool_name: str, concurrency_class: str) -> Any:
    """Keep signatures and refuse unavailable tools before their operation guard."""

    def refusal() -> dict[str, Any]:
        return {
            "success": False,
            "classification": "unsupported_platform",
            "reason_code": "linux_solver_not_enabled"
            if tool_name not in {"semantic_search", "semantic_status"}
            else "linux_semantic_search_not_accepted",
            "solver_started": False,
            "filesystem_modified": False,
        }

    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            if not tool_available(tool_name, concurrency_class) or (
                tool_name == "semantic_status"
                and not native_solver_enabled()
                and inspect.signature(function).bind(*args, **kwargs).arguments.get("warm", False)
            ):
                return refusal()
            return await function(*args, **kwargs)

        return asynchronous

    @wraps(function)
    def synchronous(*args: Any, **kwargs: Any) -> Any:
        if not tool_available(tool_name, concurrency_class) or (
            tool_name == "semantic_status"
            and not native_solver_enabled()
            and inspect.signature(function).bind(*args, **kwargs).arguments.get("warm", False)
        ):
            return refusal()
        return function(*args, **kwargs)

    return synchronous
