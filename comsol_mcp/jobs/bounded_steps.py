"""Bounded-step review receipts and checkpoint usability decisions.

``comsol_mcp.bounded_step_receipt`` binds one workflow step to its pre/post
state hashes, checkpoint identity, numerical acceptance checks, and five
explicitly separated outcomes: transport success, execution success, evidence
completeness, cleanup proof, and scientific disposition. Rows form a
hash-chained JSONL journal under the durable job directory. A resume claim is
possible only through a checkpoint whose bytes, source identity, model
identity, and revision all match the receipt; every other crash/cancel path
decides restart.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.durable import canonical_sha256_v1, read_complete_jsonl

BOUNDED_STEP_RECEIPT_SCHEMA_NAME = "comsol_mcp.bounded_step_receipt"
BOUNDED_STEP_SCHEMA_VERSION = "1.0.0"
BOUNDED_STEPS_FILENAME = "bounded_steps.jsonl"

_MAX_TEXT = 512
_MAX_REASON_CODES = 16
_MAX_NUMERICAL_CHECKS = 32
_MAX_CHECK_NAME = 128

_TRANSPORT_STATUSES = frozenset({"ok", "timeout", "disconnect", "unknown"})
_EXECUTION_STATUSES = frozenset(
    {"not_started", "running", "completed", "failed", "crashed", "cancelled"}
)
_EVIDENCE_STATUSES = frozenset({"complete", "incomplete", "unverified"})
_CLEANUP_OUTCOMES = frozenset({"proven", "pending", "failed", "not_applicable"})
_SCIENTIFIC_DISPOSITIONS = frozenset({"pass", "fail", "not_evaluated"})
_FAILURE_DISPOSITIONS = frozenset(
    {"none", "retryable", "pause_and_repair", "restart_from_checkpoint", "abandoned"}
)
_CHECKPOINT_POLICIES = frozenset({"none", "before_step", "after_step", "required"})

_HEX_DIGITS = frozenset("0123456789abcdef")


class BoundedStepError(ValueError):
    """Raised when a step record or journal violates the closed contract."""


def _hex64(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character.lower() not in _HEX_DIGITS for character in value)
    ):
        raise BoundedStepError(f"{label} must be 64 hexadecimal characters or null")
    return value.lower()


def _text(value: Any, label: str, *, maximum: int = _MAX_TEXT, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise BoundedStepError(f"{label} must be a non-empty string")
    if len(value) > maximum:
        raise BoundedStepError(f"{label} exceeds {maximum} characters")
    return value


def _vocab(value: Any, allowed: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise BoundedStepError(f"{label} must be one of {sorted(allowed)}")
    return value


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BoundedStepError(f"{label} must be an object")
    return value


def _normalize_numerical_checks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise BoundedStepError("numerical_checks must be a list")
    if len(value) > _MAX_NUMERICAL_CHECKS:
        raise BoundedStepError(f"numerical_checks exceeds {_MAX_NUMERICAL_CHECKS} rows")
    checks: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item = _mapping(raw, f"numerical_check {index}")
        expected_fields = {
            "name",
            "expected",
            "observed",
            "tolerance",
            "unit",
            "passed",
            "reason_code",
        }
        if set(item) != expected_fields:
            raise BoundedStepError(f"numerical_check {index} has missing or unsupported fields")
        name = _text(item.get("name"), f"numerical_check {index}.name", maximum=_MAX_CHECK_NAME)
        observed = item.get("observed")
        expected = item.get("expected")
        tolerance = item.get("tolerance")
        for label, number in (
            ("observed", observed),
            ("expected", expected),
            ("tolerance", tolerance),
        ):
            if number is not None:
                if isinstance(number, bool) or not isinstance(number, (int, float)):
                    raise BoundedStepError(
                        f"numerical_check {index}.{label} must be numeric or null"
                    )
                if not math.isfinite(float(number)):
                    raise BoundedStepError(f"numerical_check {index}.{label} must be finite")
        passed = item.get("passed")
        if not isinstance(passed, bool):
            raise BoundedStepError(f"numerical_check {index}.passed must be boolean")
        unit = item.get("unit")
        if unit is not None:
            unit = _text(unit, f"numerical_check {index}.unit", maximum=32)
        reason_code = item.get("reason_code")
        if reason_code is not None:
            reason_code = _text(reason_code, f"numerical_check {index}.reason_code", maximum=64)
        if observed is None and passed:
            # A missing observation can never pass: fail closed here so a
            # partially collected check cannot masquerade as acceptance.
            raise BoundedStepError(
                f"numerical_check {index} declares passed=true without an observation"
            )
        checks.append(
            {
                "name": name,
                "expected": expected,
                "observed": observed,
                "tolerance": tolerance,
                "unit": unit,
                "passed": passed,
                "reason_code": reason_code,
            }
        )
    return checks


def build_bounded_step_receipt(record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and fingerprint one bounded-step receipt without IO."""
    raw = _mapping(record, "bounded step record")
    required_fields = {
        "schema_name",
        "schema_version",
        "job_id",
        "attempt",
        "step_id",
        "created_at_epoch",
        "input_fingerprint",
        "pre_state_hash",
        "post_state_hash",
        "checkpoint_policy",
        "checkpoint_path_redacted",
        "checkpoint_sha256",
        "checkpoint_byte_size",
        "numerical_checks",
        "transport_status",
        "execution_status",
        "evidence_status",
        "cleanup",
        "scientific_disposition",
        "failure_disposition",
        "failure_reason_codes",
        "source_identity",
        "previous_step_sha256",
    }
    if set(raw) != required_fields:
        unknown = sorted(set(raw) - required_fields)
        missing = sorted(required_fields - set(raw))
        raise BoundedStepError(
            f"bounded step record field mismatch (missing={missing}, unsupported={unknown})"
        )
    if raw["schema_name"] != BOUNDED_STEP_RECEIPT_SCHEMA_NAME:
        raise BoundedStepError("bounded step schema_name mismatch")
    if raw["schema_version"] != BOUNDED_STEP_SCHEMA_VERSION:
        raise BoundedStepError("unsupported bounded step schema_version")

    job_id = _text(raw["job_id"], "job_id", maximum=128)
    attempt = raw["attempt"]
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
        raise BoundedStepError("attempt must be a positive integer")
    step_id = _text(raw["step_id"], "step_id", maximum=_MAX_CHECK_NAME)
    created_at_epoch = raw["created_at_epoch"]
    if (
        isinstance(created_at_epoch, bool)
        or not isinstance(created_at_epoch, (int, float))
        or not math.isfinite(float(created_at_epoch))
    ):
        raise BoundedStepError("created_at_epoch must be finite")

    checkpoint_policy = _vocab(raw["checkpoint_policy"], _CHECKPOINT_POLICIES, "checkpoint_policy")
    checkpoint_redacted = raw["checkpoint_path_redacted"]
    checkpoint_sha = _hex64(raw["checkpoint_sha256"], "checkpoint_sha256")
    checkpoint_size = raw["checkpoint_byte_size"]
    if checkpoint_policy == "none":
        if checkpoint_redacted is not None or checkpoint_sha is not None:
            raise BoundedStepError("checkpoint_policy none forbids checkpoint identity")
        if checkpoint_size is not None:
            raise BoundedStepError("checkpoint_policy none forbids checkpoint_byte_size")
    else:
        checkpoint_redacted = _text(checkpoint_redacted, "checkpoint_path_redacted")
        if checkpoint_sha is None:
            raise BoundedStepError("a declared checkpoint requires checkpoint_sha256")
        if (
            isinstance(checkpoint_size, bool)
            or not isinstance(checkpoint_size, int)
            or checkpoint_size <= 0
        ):
            raise BoundedStepError("checkpoint_byte_size must be a positive integer")

    failure_reason_codes_raw = raw["failure_reason_codes"]
    if not isinstance(failure_reason_codes_raw, list) or len(failure_reason_codes_raw) > (
        _MAX_REASON_CODES
    ):
        raise BoundedStepError(f"failure_reason_codes must hold at most {_MAX_REASON_CODES} rows")
    failure_reason_codes = [
        _text(code, f"failure_reason_codes[{index}]", maximum=64)
        for index, code in enumerate(failure_reason_codes_raw)
    ]

    source_raw = _mapping(raw["source_identity"], "source_identity")
    if set(source_raw) != {"model_path_redacted", "file_sha256", "model_fingerprint", "revision"}:
        raise BoundedStepError("source_identity has missing or unsupported fields")
    source_identity = {
        "model_path_redacted": _text(
            source_raw["model_path_redacted"],
            "source_identity.model_path_redacted",
            allow_empty=False,
        ),
        "file_sha256": _hex64(source_raw["file_sha256"], "source_identity.file_sha256"),
        "model_fingerprint": _hex64(
            source_raw["model_fingerprint"], "source_identity.model_fingerprint"
        ),
        "revision": source_raw["revision"],
    }
    if source_identity["revision"] is not None:
        source_identity["revision"] = _text(
            source_identity["revision"], "source_identity.revision", maximum=128
        )

    transport_status = _vocab(raw["transport_status"], _TRANSPORT_STATUSES, "transport_status")
    execution_status = _vocab(raw["execution_status"], _EXECUTION_STATUSES, "execution_status")
    evidence_status = _vocab(raw["evidence_status"], _EVIDENCE_STATUSES, "evidence_status")
    cleanup = _vocab(raw["cleanup"], _CLEANUP_OUTCOMES, "cleanup")
    scientific = _vocab(
        raw["scientific_disposition"], _SCIENTIFIC_DISPOSITIONS, "scientific_disposition"
    )
    failure_disposition = _vocab(
        raw["failure_disposition"], _FAILURE_DISPOSITIONS, "failure_disposition"
    )
    if failure_disposition == "restart_from_checkpoint":
        if checkpoint_policy == "none":
            raise BoundedStepError("restart_from_checkpoint requires a declared checkpoint policy")
    if failure_disposition == "none":
        if execution_status != "completed":
            raise BoundedStepError("failure_disposition none requires completed execution")
        if failure_reason_codes:
            raise BoundedStepError("failure_disposition none forbids failure reason codes")

    numerical_checks = _normalize_numerical_checks(raw["numerical_checks"])

    body = {
        "schema_name": BOUNDED_STEP_RECEIPT_SCHEMA_NAME,
        "schema_version": BOUNDED_STEP_SCHEMA_VERSION,
        "job_id": job_id,
        "attempt": attempt,
        "step_id": step_id,
        "created_at_epoch": float(created_at_epoch),
        "input_fingerprint": _text(raw["input_fingerprint"], "input_fingerprint"),
        "pre_state_hash": _hex64(raw["pre_state_hash"], "pre_state_hash"),
        "post_state_hash": _hex64(raw["post_state_hash"], "post_state_hash"),
        "checkpoint_policy": checkpoint_policy,
        "checkpoint_path_redacted": checkpoint_redacted,
        "checkpoint_sha256": checkpoint_sha,
        "checkpoint_byte_size": checkpoint_size,
        "numerical_checks": numerical_checks,
        "transport_status": transport_status,
        "execution_status": execution_status,
        "evidence_status": evidence_status,
        "cleanup": cleanup,
        "scientific_disposition": scientific,
        "failure_disposition": failure_disposition,
        "failure_reason_codes": failure_reason_codes,
        "source_identity": source_identity,
        "previous_step_sha256": _hex64(raw["previous_step_sha256"], "previous_step_sha256"),
    }
    body["step_sha256"] = canonical_sha256_v1(body)
    return body


def _normalize_row_payload(
    value: Any, *, sequence: int, previous_step_sha256: str | None
) -> dict[str, Any]:
    raw = dict(_mapping(value, f"bounded step row {sequence}"))
    declared = raw.pop("step_sha256", None)
    row = build_bounded_step_receipt(raw)
    if _hex64(declared, f"bounded step row {sequence}.step_sha256") != row["step_sha256"]:
        raise BoundedStepError(f"bounded step row {sequence} hash does not match its content")
    if row["previous_step_sha256"] != previous_step_sha256:
        raise BoundedStepError(f"bounded step row {sequence} breaks the hash chain")
    return row


def _rebuild(row: Mapping[str, Any]) -> dict[str, Any]:
    """Re-validate a stored row through the closed builder."""
    return build_bounded_step_receipt(
        {key: value for key, value in row.items() if key != "step_sha256"}
    )


def append_bounded_step(job_dir: str | Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Durably append one validated bounded-step row to the job journal.

    The caller serializes concurrent appends for one job (the durable worker
    protocol already holds the job lock); this function owns chain integrity,
    duplicate rejection, and fsync durability.
    """
    directory = Path(job_dir)
    path = directory / BOUNDED_STEPS_FILENAME
    existing = read_bounded_steps(path)
    if existing["state"] not in {"absent", "current_valid"} or existing["warnings"]:
        raise BoundedStepError(
            f"bounded step journal is {existing['state']!r} with warnings "
            f"{existing['warnings']}; refusing to append"
        )
    rows = existing["rows"]
    seen = {(row["attempt"], row["step_id"]) for row in rows}
    previous = rows[-1] if rows else None
    candidate = build_bounded_step_receipt(
        {
            **record,
            "previous_step_sha256": previous["step_sha256"] if previous else None,
        }
    )
    key = (candidate["attempt"], candidate["step_id"])
    if key in seen:
        raise BoundedStepError(f"duplicate bounded step (attempt={key[0]}, step_id={key[1]!r})")
    line = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    directory.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return candidate


def read_bounded_steps(path: str | Path, *, max_bytes: int = 8_388_608) -> dict[str, Any]:
    """Read complete bounded-step rows without rewriting the journal."""
    outcome = read_complete_jsonl(Path(path), max_bytes=max_bytes)
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    previous: str | None = None
    if outcome["state"] in {"corrupt", "oversized"}:
        return {
            "state": outcome["state"],
            "rows": [],
            "warnings": [f"bounded_step_journal_{outcome['state']}"],
        }
    if outcome["state"] == "incomplete":
        warnings.append("bounded_step_torn_tail")
    for index, payload in enumerate(outcome["records"], start=1):
        try:
            row = _normalize_row_payload(payload, sequence=index, previous_step_sha256=previous)
        except BoundedStepError, ValueError:
            # Classified, bounded diagnostics only; raw errors never surface.
            warnings.append(f"bounded_step_row_invalid:{index}")
            continue
        rows.append(row)
        previous = row["step_sha256"]
    if len(warnings) > _MAX_REASON_CODES:
        del warnings[_MAX_REASON_CODES:]
    return {"state": outcome["state"], "rows": rows, "warnings": warnings}


def verify_checkpoint_usability(
    receipt: Mapping[str, Any],
    checkpoint_path: str | Path,
    *,
    current_source_file_sha256: str | None,
    current_model_fingerprint: str | None,
    current_revision: str | None,
) -> dict[str, Any]:
    """Prove one checkpoint is usable against its own receipt.

    Usable requires every declared expectation: intact bytes matching
    ``checkpoint_sha256``/``checkpoint_byte_size``, plus current source
    identity, model fingerprint, and revision equal to the receipt. Missing
    expectations fail closed as undeclared rather than being guessed.
    """
    row = _rebuild(receipt)
    reason_codes: list[str] = []
    if row["checkpoint_sha256"] is None:
        return {
            "usable": False,
            "reason_codes": ["no_declared_checkpoint"],
        }
    expectations = {
        "source_identity_undeclared": current_source_file_sha256,
        "model_identity_undeclared": current_model_fingerprint,
        "revision_undeclared": current_revision,
    }
    for label, expectation in expectations.items():
        if expectation is None:
            reason_codes.append(label)

    candidate = Path(checkpoint_path)
    try:
        payload = candidate.read_bytes()
    except OSError:
        reason_codes.append("checkpoint_unreadable")
    else:
        if row["checkpoint_byte_size"] is not None and len(payload) != row["checkpoint_byte_size"]:
            reason_codes.append("checkpoint_byte_size_mismatch")
        digest = hashlib.sha256(payload).hexdigest()
        if digest != row["checkpoint_sha256"]:
            reason_codes.append("checkpoint_hash_mismatch")
        source_sha = row["source_identity"]["file_sha256"]
        if current_source_file_sha256 is not None and source_sha is not None:
            if current_source_file_sha256.lower() != source_sha:
                reason_codes.append("source_identity_mismatch")
        if current_model_fingerprint is not None:
            fingerprint = row["source_identity"]["model_fingerprint"]
            if fingerprint is None or current_model_fingerprint.lower() != fingerprint:
                reason_codes.append("model_identity_mismatch")
        if current_revision is not None:
            revision = row["source_identity"]["revision"]
            if revision is None or current_revision != revision:
                reason_codes.append("revision_mismatch")
    unique = list(dict.fromkeys(reason_codes))
    return {"usable": not unique, "reason_codes": unique}


def decide_resume(
    rows: list[Mapping[str, Any]], *, checkpoint_verified: bool | None
) -> dict[str, Any]:
    """Classify replay intent from completed rows and a verification verdict."""
    if not rows:
        return {"decision": "restart", "reason_codes": ["no_completed_steps"]}
    last = _rebuild(rows[-1])
    if last["execution_status"] == "completed" and last["cleanup"] == "proven":
        return {"decision": "none_needed", "reason_codes": []}
    if checkpoint_verified is None:
        return {"decision": "restart", "reason_codes": ["checkpoint_unverified"]}
    if checkpoint_verified is True:
        if last["failure_disposition"] == "abandoned":
            return {"decision": "restart", "reason_codes": ["step_abandoned"]}
        return {"decision": "resume_from_checkpoint", "reason_codes": []}
    return {"decision": "restart", "reason_codes": ["checkpoint_not_usable"]}


def summarize_bounded_steps(job_dir: str | Path) -> dict[str, Any]:
    """Return the bounded status view used by ``job_status``; warning-only."""
    path = Path(job_dir) / BOUNDED_STEPS_FILENAME
    outcome = read_bounded_steps(path)
    rows = outcome["rows"]
    summary: dict[str, Any] = {
        "available": True,
        "journal_state": outcome["state"],
        "warnings": outcome["warnings"],
        "step_count": len(rows),
        "chain_intact": outcome["state"] == "current_valid"
        and not any(
            warning.startswith("bounded_step_row_invalid") for warning in outcome["warnings"]
        ),
        "last_step": None,
        "resume_decision": None,
    }
    if rows:
        last = rows[-1]
        summary["last_step"] = {
            "attempt": last["attempt"],
            "step_id": last["step_id"],
            "transport_status": last["transport_status"],
            "execution_status": last["execution_status"],
            "evidence_status": last["evidence_status"],
            "cleanup": last["cleanup"],
            "scientific_disposition": last["scientific_disposition"],
            "failure_disposition": last["failure_disposition"],
            "step_sha256": last["step_sha256"],
        }
        summary["resume_decision"] = decide_resume(rows, checkpoint_verified=None)
    summary["generated_at_epoch"] = time.time()
    return summary


__all__ = [
    "BOUNDED_STEP_RECEIPT_SCHEMA_NAME",
    "BOUNDED_STEP_SCHEMA_VERSION",
    "BOUNDED_STEPS_FILENAME",
    "BoundedStepError",
    "append_bounded_step",
    "build_bounded_step_receipt",
    "decide_resume",
    "read_bounded_steps",
    "summarize_bounded_steps",
    "verify_checkpoint_usability",
]
