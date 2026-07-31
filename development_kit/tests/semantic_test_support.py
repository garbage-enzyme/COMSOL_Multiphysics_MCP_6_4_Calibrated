"""Shared subprocess isolation helpers for semantic dependency tests."""

from __future__ import annotations

import os
from typing import Mapping

_REMOVED_PYTHON_ENVIRONMENT = {
    "PYTHONBREAKPOINT",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONOPTIMIZE",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
}


def isolated_semantic_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith("COMSOL_SEMANTIC_") or name in _REMOVED_PYTHON_ENVIRONMENT:
            environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUTF8"] = "1"
    if overrides:
        environment.update(overrides)
    return environment


__all__ = ["isolated_semantic_environment"]
