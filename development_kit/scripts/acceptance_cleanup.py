"""Solver-free cleanup accounting shared by licensed acceptance scripts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class CleanupRecorder:
    """Run independent cleanup steps and make their outcome part of success."""

    def __init__(self, result: dict[str, Any]):
        self.result = result
        self.steps: dict[str, dict[str, Any]] = {}

    def run(
        self,
        name: str,
        operation: Callable[[], Any],
        *,
        passed: Callable[[Any], bool] = lambda _value: True,
        expose_result: bool = True,
    ) -> Any:
        try:
            value = operation()
            step_passed = bool(passed(value))
            detail = {"passed": step_passed}
        except Exception as exc:  # cleanup must continue through independent steps
            value = {
                "success": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            detail = {
                "passed": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        self.steps[name] = detail
        if expose_result:
            self.result[name] = value
        return value

    def finalize(self) -> int:
        cleanup_passed = all(
            step.get("passed") is True for step in self.steps.values()
        )
        self.result["cleanup"] = {
            "passed": cleanup_passed,
            "steps": dict(self.steps),
        }
        self.result["success"] = (
            self.result.get("success") is True and cleanup_passed
        )
        return 0 if self.result["success"] else 1


def lease_released(value: Any) -> bool:
    """Require an owned lease to report both successful and actual release."""
    return (
        isinstance(value, dict)
        and value.get("success") is True
        and value.get("released") is True
    )


__all__ = ["CleanupRecorder", "lease_released"]
