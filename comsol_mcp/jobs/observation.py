"""Exact long-task observation receipts and recovery dispositions.

``comsol_mcp.observation_receipt`` separates what an observer can honestly
know about one long-running owned task: the exact owned process identity
(PID, creation time, executable, command-line signature, target files),
bounded log tail with output timestamps, the declared solver terminal state,
and an observer outcome drawn from ``solver_terminal``,
``transport_disconnect``, ``worker_failure``, and ``observer_timeout`` as
mutually distinct reason codes. Termination planning here never matches by
process name: any drift between the expected and live identity (PID reuse,
changed command line, changed executable, absent PID) refuses action. Resume
claims require a usable checkpoint; every other failure decides restart.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.durable import canonical_sha256_v1

OBSERVATION_RECEIPT_SCHEMA_NAME = "comsol_mcp.observation_receipt"
OBSERVATION_SCHEMA_VERSION = "1.0.0"
OBSERVATION_RECEIPT_FILENAME = "observation.json"

_MAX_TEXT = 512
_MAX_TAIL_ROWS = 64
_MAX_TARGET_FILES = 16

_CLEANUP_OUTCOMES = frozenset({"proven", "pending", "failed", "not_applicable"})
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class ObservationError(ValueError):
    """Raised when observation evidence violates the closed contract."""


def _hex64(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX_DIGITS for character in value)
    ):
        raise ObservationError(f"{label} must be exactly 64 hexadecimal characters")
    return value.lower()


def _positive_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ObservationError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ObservationError(f"{label} must be positive and finite")
    return number


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ObservationError(f"{label} must be a non-empty string")
    if len(value) > _MAX_TEXT:
        raise ObservationError(f"{label} exceeds {_MAX_TEXT} characters")
    return value


def normalize_process_identity(identity: Any) -> dict[str, Any]:
    """Validate the exact owned-identity binding used for every observation."""
    required = {
        "pid",
        "process_create_time",
        "executable",
        "command_signature",
        "target_files",
    }
    if not isinstance(identity, Mapping):
        raise ObservationError("process identity must be an object")
    if not required <= set(identity):
        missing = sorted(required - set(identity))
        raise ObservationError(f"process identity is incomplete: {missing}")
    pid = identity["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ObservationError("pid must be a positive integer")
    create_time = _positive_float(identity["process_create_time"], "process_create_time")
    executable = _text(identity["executable"], "executable")
    signature = _hex64(identity["command_signature"], "command_signature")
    target_files_raw = identity["target_files"]
    if not isinstance(target_files_raw, list) or len(target_files_raw) > _MAX_TARGET_FILES:
        raise ObservationError(f"target_files must hold at most {_MAX_TARGET_FILES} entries")
    target_files = [_text(item, "target_files entry") for item in target_files_raw]
    return {
        "pid": pid,
        "process_create_time": create_time,
        "executable": executable,
        "command_signature": signature,
        "target_files": target_files,
    }


def verify_exact_ownership(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    create_time_tolerance_seconds: float = 1.0e-3,
) -> dict[str, Any]:
    """Compare a live snapshot against the expected owned identity exactly."""
    expected_normalized = normalize_process_identity(expected)
    reasons: list[str] = []
    observed_pid = observed.get("pid")
    if isinstance(observed_pid, bool) or not isinstance(observed_pid, int):
        reasons.append("pid_absent_or_changed")
        observed_pid = None
    elif observed_pid != expected_normalized["pid"]:
        reasons.append("pid_absent_or_changed")
    else:
        observed_time = observed.get("process_create_time")
        if (
            isinstance(observed_time, bool)
            or not isinstance(observed_time, (int, float))
            or not math.isfinite(float(observed_time))
        ):
            reasons.append("process_create_time_unreadable")
        else:
            time_delta = abs(float(observed_time) - expected_normalized["process_create_time"])
            if time_delta > create_time_tolerance_seconds:
                reasons.append("pid_reuse")
    observed_signature = observed.get("command_signature")
    if (
        isinstance(observed_signature, str)
        and observed_signature.lower() != expected_normalized["command_signature"]
    ):
        reasons.append("command_line_drift")
    observed_executable = observed.get("executable")
    if (
        isinstance(observed_executable, str)
        and observed_executable != expected_normalized["executable"]
    ):
        reasons.append("executable_mismatch")
    return {
        "owned": not reasons,
        "reason_codes": reasons,
    }


def classify_observer_outcome(
    *,
    terminal_state: str | None,
    terminal_identity_verified: bool,
    transport_alive: bool | None,
    worker_exit_observed: bool | None,
    deadline_exceeded: bool,
) -> dict[str, Any]:
    """Pick exactly one observer outcome with documented precedence.

    Precedence: a verified solver terminal state wins; then a lost transport
    with unverifiable process state; then an observed exit without a declared
    terminal row (worker failure); finally an expired deadline while nothing
    terminal was seen (observer timeout).
    """
    if terminal_state is not None and terminal_identity_verified:
        return {"outcome": "solver_terminal", "reason_codes": ["solver_terminal"]}
    if transport_alive is False and worker_exit_observed is None:
        return {"outcome": "transport_disconnect", "reason_codes": ["transport_disconnect"]}
    if worker_exit_observed:
        return {"outcome": "worker_failure", "reason_codes": ["worker_failure"]}
    if deadline_exceeded:
        return {"outcome": "observer_timeout", "reason_codes": ["observer_timeout"]}
    return {"outcome": "solver_terminal", "reason_codes": ["solver_terminal"]}


def build_observation_receipt(
    *,
    job_id: str,
    attempt: int,
    process_identity: Mapping[str, Any],
    observer_started_epoch: float,
    deadline_epoch: float,
    terminal_state: str | None,
    terminal_identity_verified: bool,
    transport_alive: bool | None,
    worker_exit_observed: bool | None,
    observer_deadline_exceeded: bool,
    log_tail: list[str],
    log_truncated: bool,
    last_output_epoch: float | None,
    cleanup_outcome: str,
    checkpoint_usable: bool | None,
    execution_status: str | None = None,
) -> dict[str, Any]:
    """Assemble one bounded observation receipt without touching processes."""
    normalized_identity = normalize_process_identity(process_identity)
    job_text = _text(job_id, "job_id")
    attempt_value = attempt
    if isinstance(attempt_value, bool) or not isinstance(attempt_value, int) or attempt_value <= 0:
        raise ObservationError("attempt must be a positive integer")
    started = _positive_float(observer_started_epoch, "observer_started_epoch")
    deadline = _positive_float(deadline_epoch, "deadline_epoch")

    if not isinstance(log_tail, list) or len(log_tail) > _MAX_TAIL_ROWS * 4:
        raise ObservationError(f"log_tail must hold at most {_MAX_TAIL_ROWS * 4} rows")
    tail_rows = [
        item if isinstance(item, str) else str(item) for item in log_tail[-_MAX_TAIL_ROWS:]
    ]
    tail_rows = [row[:_MAX_TEXT] for row in tail_rows]
    truncated = bool(log_truncated)
    last_output = (
        _positive_float(last_output_epoch, "last_output_epoch")
        if last_output_epoch is not None
        else None
    )

    if cleanup_outcome not in _CLEANUP_OUTCOMES:
        raise ObservationError(f"cleanup_outcome must be one of {sorted(_CLEANUP_OUTCOMES)}")

    # The observer owns the clock: timeout is an explicitly supplied fact so
    # this builder stays deterministic and free of hidden wall-clock reads.
    outcome = classify_observer_outcome(
        terminal_state=terminal_state,
        terminal_identity_verified=terminal_identity_verified,
        transport_alive=transport_alive,
        worker_exit_observed=worker_exit_observed,
        deadline_exceeded=bool(observer_deadline_exceeded),
    )

    if terminal_state is not None:
        terminal_text = _text(terminal_state, "terminal_state")
    else:
        terminal_text = None
    if execution_status is not None:
        execution_status = _text(execution_status, "execution_status")

    if outcome["outcome"] == "solver_terminal":
        if cleanup_outcome == "proven":
            resume_disposition, resume_reasons = "none_needed", []
        elif checkpoint_usable is None:
            resume_disposition, resume_reasons = "restart", ["checkpoint_unverified"]
        elif checkpoint_usable is True:
            resume_disposition, resume_reasons = "resume_from_checkpoint", []
        else:
            resume_disposition, resume_reasons = "restart", ["checkpoint_not_usable"]
    else:
        if checkpoint_usable is True:
            resume_disposition, resume_reasons = "resume_from_checkpoint", []
        elif checkpoint_usable is None:
            resume_disposition, resume_reasons = "restart", ["checkpoint_unverified"]
        else:
            resume_disposition, resume_reasons = "restart", ["checkpoint_not_usable"]

    receipt_body: dict[str, Any] = {
        "schema_name": OBSERVATION_RECEIPT_SCHEMA_NAME,
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "job_id": job_text,
        "attempt": attempt_value,
        "process_identity": normalized_identity,
        "observer_started_epoch": started,
        "deadline_epoch": deadline,
        "log_tail": tail_rows,
        "log_truncated": truncated,
        "last_output_epoch": last_output,
        "terminal_state": terminal_text,
        "terminal_identity_verified": bool(terminal_identity_verified),
        "transport_alive": transport_alive,
        "worker_exit_observed": worker_exit_observed,
        "observer_outcome": outcome["outcome"],
        "reason_codes": outcome["reason_codes"],
        "cleanup_outcome": cleanup_outcome,
        "resume_disposition": resume_disposition,
        "resume_reason_codes": resume_reasons,
    }
    if execution_status is not None:
        receipt_body["execution_status"] = execution_status
    receipt_body["receipt_sha256"] = canonical_sha256_v1(receipt_body)
    return receipt_body


def summarize_observation(job_dir: str | Path) -> dict[str, Any]:
    """Return the warning-only status view attached to durable job status."""
    path = Path(job_dir) / OBSERVATION_RECEIPT_FILENAME
    raw = path.read_bytes()
    if len(raw) > 262_144:
        return {"available": False, "reason_code": "observation_receipt_oversized"}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return {"available": False, "reason_code": "observation_receipt_unreadable"}
    required = {
        "job_id",
        "attempt",
        "observer_outcome",
        "reason_codes",
        "terminal_state",
        "cleanup_outcome",
        "resume_disposition",
        "receipt_sha256",
    }
    if not isinstance(payload, Mapping) or not required <= set(payload):
        return {"available": False, "reason_code": "observation_receipt_shape_invalid"}
    return {
        "available": True,
        "job_id": payload["job_id"],
        "attempt": payload.get("attempt"),
        "observer_outcome": payload["observer_outcome"],
        "reason_codes": payload["reason_codes"],
        "terminal_state": payload["terminal_state"],
        "cleanup_outcome": payload["cleanup_outcome"],
        "resume_disposition": payload["resume_disposition"],
        "receipt_sha256": payload["receipt_sha256"],
    }


__all__ = [
    "OBSERVATION_RECEIPT_FILENAME",
    "OBSERVATION_RECEIPT_SCHEMA_NAME",
    "OBSERVATION_SCHEMA_VERSION",
    "ObservationError",
    "build_observation_receipt",
    "classify_observer_outcome",
    "normalize_process_identity",
    "summarize_observation",
    "verify_exact_ownership",
]
