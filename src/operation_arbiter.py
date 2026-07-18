"""Cross-thread and cross-process arbitration for COMSOL-bound tool calls."""

from __future__ import annotations

from dataclasses import dataclass
import functools
import inspect
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, get_type_hints
import uuid

import psutil

from src.utils.runtime_paths import default_runtime_dir


OPERATION_LOCK_SCHEMA = "comsol_mcp.operation_lock"
OPERATION_LOCK_VERSION = "1.0.0"
PROCESS_CREATE_TIME_TOLERANCE_SECONDS = 1.0
RETRY_AFTER_MS = 250


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _process_create_time(pid: int) -> float:
    return float(psutil.Process(pid).create_time())


@dataclass(frozen=True)
class OperationClaim:
    """One exact operation-lock claim owned by this process."""

    operation_id: str
    tool_name: str
    lock_bytes: bytes


class OperationArbiter:
    """Fail-fast durable mutex for calls that may touch COMSOL state."""

    def __init__(
        self,
        runtime_root: str | Path | None = None,
        *,
        pid: int | None = None,
        process_create_time: float | None = None,
        process_probe: Callable[[int], float] = _process_create_time,
        clock: Callable[[], float] = time.time,
    ):
        self.runtime_root = Path(runtime_root or default_runtime_dir()).resolve()
        self.lock_path = self.runtime_root / "operation.lock"
        self.pid = int(os.getpid() if pid is None else pid)
        self.process_create_time = float(
            process_probe(self.pid)
            if process_create_time is None
            else process_create_time
        )
        self._process_probe = process_probe
        self._clock = clock
        self._thread_lock = threading.Lock()

    def _read_lock(self) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
        try:
            payload = self.lock_path.read_bytes()
        except FileNotFoundError:
            return None, None, None
        except OSError as exc:
            return None, None, f"operation lock cannot be read: {type(exc).__name__}"
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, payload, "operation lock is malformed"
        expected = {
            "schema_name", "schema_version", "operation_id", "tool_name",
            "side_effect_class", "pid", "process_create_time",
            "acquired_at_epoch",
        }
        if not isinstance(value, dict) or set(value) != expected:
            return None, payload, "operation lock fields are invalid"
        if (
            value["schema_name"] != OPERATION_LOCK_SCHEMA
            or value["schema_version"] != OPERATION_LOCK_VERSION
            or not isinstance(value["operation_id"], str)
            or not value["operation_id"]
            or not isinstance(value["tool_name"], str)
            or not value["tool_name"]
            or isinstance(value["pid"], bool)
            or not isinstance(value["pid"], int)
            or isinstance(value["process_create_time"], bool)
            or not isinstance(value["process_create_time"], (int, float))
        ):
            return None, payload, "operation lock values are invalid"
        return value, payload, None

    def _owner_state(self, lock: dict[str, Any]) -> tuple[str, str]:
        pid = int(lock["pid"])
        expected_create_time = float(lock["process_create_time"])
        try:
            observed_create_time = float(self._process_probe(pid))
        except psutil.NoSuchProcess:
            return "stale", "recorded process is absent"
        except (psutil.AccessDenied, OSError) as exc:
            return "uncertain", f"process identity unavailable: {type(exc).__name__}"
        if abs(observed_create_time - expected_create_time) > PROCESS_CREATE_TIME_TOLERANCE_SECONDS:
            return "stale", "PID was reused by a different process"
        return "active", "recorded process identity is active"

    def _remove_stale(self, expected_bytes: bytes) -> bool:
        try:
            if self.lock_path.read_bytes() != expected_bytes:
                return False
            self.lock_path.unlink()
            return True
        except (FileNotFoundError, OSError):
            return False

    def inspect(self) -> dict[str, Any]:
        """Inspect the operation lock without acquiring or recovering it."""
        lock, _original, error = self._read_lock()
        if error:
            return {
                "state": "uncertain",
                "retryable": False,
                "retry_after_ms": None,
                "error": error,
                "active_operation": None,
            }
        if lock is None:
            return {
                "state": "idle",
                "retryable": True,
                "retry_after_ms": 0,
                "active_operation": None,
            }
        state, reason = self._owner_state(lock)
        return {
            "state": state,
            "retryable": state in {"active", "stale"},
            "retry_after_ms": RETRY_AFTER_MS if state == "active" else 0 if state == "stale" else None,
            "reason": reason,
            "active_operation": {
                "operation_id": lock["operation_id"],
                "tool_name": lock["tool_name"],
                "side_effect_class": lock["side_effect_class"],
                "pid": lock["pid"],
                "process_create_time": lock["process_create_time"],
                "acquired_at_epoch": lock["acquired_at_epoch"],
            },
        }

    def try_acquire(
        self, *, tool_name: str, side_effect_class: str
    ) -> tuple[OperationClaim | None, dict[str, Any]]:
        """Acquire immediately or return bounded busy/uncertain evidence."""
        with self._thread_lock:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            recovered_stale = False
            for _attempt in range(2):
                operation_id = uuid.uuid4().hex
                body = {
                    "schema_name": OPERATION_LOCK_SCHEMA,
                    "schema_version": OPERATION_LOCK_VERSION,
                    "operation_id": operation_id,
                    "tool_name": tool_name,
                    "side_effect_class": side_effect_class,
                    "pid": self.pid,
                    "process_create_time": self.process_create_time,
                    "acquired_at_epoch": float(self._clock()),
                }
                payload = _canonical_bytes(body)
                try:
                    descriptor = os.open(
                        self.lock_path,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    )
                except FileExistsError:
                    lock, original, error = self._read_lock()
                    if error or lock is None or original is None:
                        return None, {
                            "state": "uncertain",
                            "retryable": False,
                            "retry_after_ms": None,
                            "error": error or "operation lock identity is unavailable",
                        }
                    state, reason = self._owner_state(lock)
                    if state == "stale" and self._remove_stale(original):
                        recovered_stale = True
                        continue
                    return None, {
                        "state": state if state != "stale" else "busy",
                        "retryable": state == "active",
                        "retry_after_ms": RETRY_AFTER_MS if state == "active" else None,
                        "reason": reason,
                        "active_operation": {
                            "operation_id": lock["operation_id"],
                            "tool_name": lock["tool_name"],
                            "side_effect_class": lock["side_effect_class"],
                            "pid": lock["pid"],
                            "acquired_at_epoch": lock["acquired_at_epoch"],
                        },
                    }
                except OSError as exc:
                    return None, {
                        "state": "uncertain",
                        "retryable": False,
                        "retry_after_ms": None,
                        "error": f"operation lock cannot be created: {type(exc).__name__}",
                    }
                else:
                    try:
                        os.write(descriptor, payload)
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    claim = OperationClaim(operation_id, tool_name, payload)
                    return claim, {
                        "state": "acquired",
                        "operation_id": operation_id,
                        "recovered_stale_lock": recovered_stale,
                    }
            return None, {
                "state": "uncertain",
                "retryable": False,
                "retry_after_ms": None,
                "error": "stale operation lock changed during recovery",
            }

    def release(self, claim: OperationClaim) -> dict[str, Any]:
        """Release only the exact bytes written by this claim."""
        with self._thread_lock:
            try:
                current = self.lock_path.read_bytes()
            except FileNotFoundError:
                return {"released": False, "verified": False, "reason": "lock_missing"}
            except OSError as exc:
                return {
                    "released": False,
                    "verified": False,
                    "reason": f"lock_unreadable:{type(exc).__name__}",
                }
            if current != claim.lock_bytes:
                return {"released": False, "verified": False, "reason": "lock_changed"}
            try:
                self.lock_path.unlink()
            except OSError as exc:
                return {
                    "released": False,
                    "verified": False,
                    "reason": f"unlink_failed:{type(exc).__name__}",
                }
            return {"released": True, "verified": True}


_ARBITERS: dict[str, OperationArbiter] = {}
_ARBITERS_LOCK = threading.Lock()


def get_operation_arbiter() -> OperationArbiter:
    """Return one process-local facade for the configured durable lock root."""
    root = str(default_runtime_dir().resolve()).casefold()
    with _ARBITERS_LOCK:
        arbiter = _ARBITERS.get(root)
        if arbiter is None:
            arbiter = OperationArbiter(default_runtime_dir())
            _ARBITERS[root] = arbiter
        return arbiter


def get_operation_status() -> dict[str, Any]:
    """Return bounded solver-free operation status for control-plane tools."""
    return get_operation_arbiter().inspect()


def guard_tool_call(
    function: Callable[..., Any],
    *,
    tool_name: str,
    side_effect_class: str,
    concurrency_class: str,
    profile_name: str = "full",
    requires_model_revision: bool = False,
    advances_model_revision: bool = False,
) -> Callable[..., Any]:
    """Wrap one registered tool with fail-fast COMSOL operation arbitration."""
    @functools.wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        from src.path_policy import validate_tool_paths

        expected_model_revision = kwargs.pop("expected_model_revision", None)

        try:
            normalized_args, normalized_kwargs, path_evidence = validate_tool_paths(
                function,
                args,
                kwargs,
                tool_name=tool_name,
                profile_name=profile_name,
            )
        except (OSError, TypeError, ValueError) as exc:
            return {
                "success": False,
                "error": str(exc),
                "path_policy": {
                    "schema_name": "comsol_mcp.path_policy",
                    "schema_version": "1.0.0",
                    "enforced": profile_name != "full",
                    "accepted": False,
                    "error_type": type(exc).__name__,
                },
            }
        if concurrency_class != "comsol_bound":
            result = function(*normalized_args, **normalized_kwargs)
            if isinstance(result, dict):
                result = dict(result)
                result["path_policy"] = {**path_evidence, "accepted": True}
            return result
        arbiter = get_operation_arbiter()
        claim, acquisition = arbiter.try_acquire(
            tool_name=tool_name,
            side_effect_class=side_effect_class,
        )
        if claim is None:
            return {
                "success": False,
                "error": "Another COMSOL-bound operation owns the runtime.",
                "operation_gate": acquisition,
                "path_policy": {**path_evidence, "accepted": True},
            }
        result: Any
        try:
            revision_evidence = None
            revision_model_name = None
            if requires_model_revision:
                from src.tools.session import session_manager

                signature = inspect.signature(function)
                bound = signature.bind(*normalized_args, **normalized_kwargs)
                revision_model_name = (
                    bound.arguments.get("model_name")
                    or bound.arguments.get("source_model_name")
                    or session_manager.current_model
                )
                current_revision = session_manager.get_model_revision(
                    revision_model_name
                )
                if current_revision is None and profile_name == "full":
                    result = function(*normalized_args, **normalized_kwargs)
                elif current_revision is None:
                    result = {
                        "success": False,
                        "error": "A tracked model revision is required for this operation.",
                    }
                elif (
                    profile_name != "full"
                    and expected_model_revision
                    != current_revision["revision_sha256"]
                ):
                    result = {
                        "success": False,
                        "error": "expected_model_revision does not match current model state.",
                        "model_revision": current_revision,
                    }
                else:
                    result = function(*normalized_args, **normalized_kwargs)
                    revision_evidence = current_revision
                    if (
                        isinstance(result, dict)
                        and result.get("success") is True
                        and advances_model_revision
                    ):
                        revision_evidence = session_manager.advance_model_revision(
                            revision_model_name, tool_name
                        )
            else:
                result = function(*normalized_args, **normalized_kwargs)
        finally:
            release = arbiter.release(claim)
        if isinstance(result, dict):
            result = dict(result)
            result["operation_gate"] = {
                **acquisition,
                "release": release,
            }
            result["path_policy"] = {**path_evidence, "accepted": True}
            if revision_evidence is not None:
                result["model_revision"] = revision_evidence
            if not release["verified"]:
                result["success"] = False
                result["error"] = "Operation completed but lock release could not be verified."
        return result

    if requires_model_revision:
        signature = inspect.signature(function)
        hints = get_type_hints(function)
        parameters = [
            parameter.replace(
                annotation=hints.get(parameter.name, parameter.annotation)
            )
            for parameter in signature.parameters.values()
        ]
        parameters.append(inspect.Parameter(
            "expected_model_revision",
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=str | None,
        ))
        guarded.__signature__ = signature.replace(
            parameters=parameters,
            return_annotation=hints.get("return", signature.return_annotation),
        )
    return guarded


__all__ = [
    "OPERATION_LOCK_SCHEMA",
    "OPERATION_LOCK_VERSION",
    "OperationArbiter",
    "OperationClaim",
    "get_operation_arbiter",
    "guard_tool_call",
]
