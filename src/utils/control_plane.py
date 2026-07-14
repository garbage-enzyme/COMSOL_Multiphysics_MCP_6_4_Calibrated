"""Bounded in-process latency and outcome evidence for MCP control calls."""

from __future__ import annotations

from collections import Counter, deque
import math
import threading
import time
from typing import Any, Callable


CONTROL_PLANE_SCHEMA_VERSION = "1.0.0"
CONTROL_PLANE_WINDOW_SIZE = 256


def _outcome(result: dict[str, Any]) -> str:
    if result.get("success", True):
        return "success"
    error = result.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    text = " ".join(
        str(value)
        for value in (result.get("error_type"), error_code, error)
        if value is not None
    ).casefold()
    if error_code == "busy" or "queue is full" in text or " busy" in f" {text}":
        return "busy"
    if any(token in text for token in ("timeout", "timed out", "deadline", "exceeded")):
        return "timeout"
    return "error"


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, math.ceil(float(fraction) * len(ordered)) - 1),
    )
    return ordered[index]


class ControlPlaneMetrics:
    """Thread-safe rolling metrics with fixed memory per named operation."""

    def __init__(self, window_size: int = CONTROL_PLANE_WINDOW_SIZE):
        if not 1 <= int(window_size) <= 4096:
            raise ValueError("control-plane window_size must be between 1 and 4096")
        self.window_size = int(window_size)
        self._lock = threading.Lock()
        self._samples: dict[str, deque[tuple[float, str]]] = {}
        self._totals: Counter[str] = Counter()

    def record(self, operation: str, latency_seconds: float, result: dict[str, Any]) -> dict[str, Any]:
        operation = str(operation)
        latency = float(latency_seconds)
        if not operation or len(operation) > 80:
            raise ValueError("control-plane operation must be a short non-empty string")
        if not math.isfinite(latency) or latency < 0:
            raise ValueError("control-plane latency must be finite and nonnegative")
        outcome = _outcome(result)
        with self._lock:
            samples = self._samples.setdefault(
                operation,
                deque(maxlen=self.window_size),
            )
            samples.append((latency, outcome))
            self._totals[operation] += 1
            return self._summary_unlocked(operation)

    def _summary_unlocked(self, operation: str) -> dict[str, Any]:
        samples = list(self._samples.get(operation, ()))
        latencies = [item[0] for item in samples]
        counts = Counter(item[1] for item in samples)
        latency = (
            {
                "p50_seconds": round(_percentile(latencies, 0.50), 6),
                "p95_seconds": round(_percentile(latencies, 0.95), 6),
                "max_seconds": round(max(latencies), 6),
            }
            if latencies
            else {
                "p50_seconds": None,
                "p95_seconds": None,
                "max_seconds": None,
            }
        )
        return {
            "schema_version": CONTROL_PLANE_SCHEMA_VERSION,
            "operation": operation,
            "window_capacity": self.window_size,
            "window_samples": len(samples),
            "total_recorded": int(self._totals.get(operation, 0)),
            "outcomes": {
                name: int(counts.get(name, 0))
                for name in ("success", "busy", "timeout", "error")
            },
            "latency": latency,
        }

    def summary(self, operation: str) -> dict[str, Any]:
        with self._lock:
            return self._summary_unlocked(str(operation))

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._totals.clear()


control_plane_metrics = ControlPlaneMetrics()


def attach_control_plane_evidence(
    operation: str,
    started: float,
    result: dict[str, Any],
    *,
    metrics: ControlPlaneMetrics = control_plane_metrics,
) -> dict[str, Any]:
    """Attach current-call timing and a bounded rolling summary to one result."""
    latency = time.perf_counter() - float(started)
    enriched = dict(result)
    summary = metrics.record(operation, latency, enriched)
    enriched["control_plane"] = {
        "operation": operation,
        "latency_seconds": round(latency, 6),
        "outcome": _outcome(enriched),
        "summary": summary,
    }
    return enriched


def measured_call(operation: str, callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    return attach_control_plane_evidence(operation, started, callback())


__all__ = [
    "CONTROL_PLANE_SCHEMA_VERSION",
    "CONTROL_PLANE_WINDOW_SIZE",
    "ControlPlaneMetrics",
    "attach_control_plane_evidence",
    "control_plane_metrics",
    "measured_call",
]
