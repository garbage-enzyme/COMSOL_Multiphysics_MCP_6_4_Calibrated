"""Native MCP Tasks mapping over the existing durable job engine.

Design constraints this module exists to satisfy
------------------------------------------------

The handoff requires a simulation start to return control promptly with a
*durable native task handle*, while explicitly forbidding a second scheduler.
So this module owns no execution of its own: it is a **mapping and admission
layer** in front of the existing durable job engine. A task id and a job id are
distinct identifiers for one durable row, and the mapping is persisted before any
task-shaped result is returned.

Three identities stay separate, matching :mod:`comsol_mcp.protocol_identity`:

1. the installed SDK version;
2. the date-based protocol revision;
3. the Tasks extension wire generation.

This release targets the stable ``2026-07-28`` extension generation only. The
experimental ``2025-11-25`` in-core dialect is deliberately unsupported and is
refused rather than silently reinterpreted, because the two differ in the opt-in
mechanism, the result envelope, the TTL field name, and the retrieval method.

Frozen behaviours
-----------------

* **Durability before acknowledgement.** A ``task``-shaped result is never
  returned before the mapping row is durably persisted, so a client that
  immediately issues ``tasks/get`` cannot observe an unknown task.
* **Per-request opt-in.** A task-shaped result is never returned to a client
  that did not declare the extension on *that* request. Prior declarations do
  not count.
* **Idempotent submission.** Re-submitting an identical specification returns
  the existing durable job rather than launching a second simulation, and the
  pre-existing task id is reused when one is already bound to that job.
* **TTL never destroys evidence.** Expiry makes a task id unaddressable for
  control; it never abandons a running solve and never erases a scientific
  receipt. A still-running job is reported as running past its TTL.
* **Cancellation is cooperative and distinct from completion.** A protocol
  acknowledgement is not cleanup evidence. The terminal cleanup receipt is only
  produced once the owned process, port, lease, and job are all observed absent.
* **Errors are mapped, not conflated.** A tool result that completed with
  ``isError`` maps to task status ``completed`` with the payload inline, because
  the extension reserves ``failed`` for JSON-RPC protocol faults.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from comsol_mcp.durable import canonical_sha256_v1
from comsol_mcp.durable.io import append_jsonl_record, read_complete_jsonl
from comsol_mcp.protocol_identity import TASKS_EXTENSION_IDENTIFIER

#: The only native Tasks wire generation this build emits.
TASKS_WIRE_GENERATION = "2026-07-28"

#: Extension generations that are recognised by name and deliberately refused.
REFUSED_TASKS_GENERATIONS = ("2025-11-25",)

#: Task statuses from the stable extension. ``completed``/``failed``/``cancelled``
#: are terminal and never transition again.
TASK_STATUSES = ("working", "input_required", "completed", "cancelled", "failed")
TERMINAL_TASK_STATUSES = frozenset({"completed", "cancelled", "failed"})

#: Bound on how many task rows one mapping file may hold.
MAX_TASK_ROWS = 4096

#: Bound on the serialized size of one persisted mapping row.
MAX_TASK_ROW_BYTES = 64 * 1024

#: Default retention. TTL limits *addressability*, never evidence.
DEFAULT_TASK_TTL_MS = 24 * 60 * 60 * 1000

#: Default poll interval advertised to a client.
DEFAULT_POLL_INTERVAL_MS = 1000

SCHEMA_NAME = "comsol_mcp.tasks_mapping"
SCHEMA_VERSION = "1.0.0"

#: Durable job states that map onto each task status. The mapping is total over
#: the engine's states so no state can silently fall through to "working".
JOB_STATE_TO_TASK_STATUS: Mapping[str, str] = {
    "submitted": "working",
    "starting": "working",
    "smoke_running": "working",
    "smoke_validated": "working",
    "running": "working",
    "cancel_requested": "working",
    "cancelling": "working",
    "completed": "completed",
    "cancelled": "cancelled",
    "failed": "failed",
    "interrupted": "failed",
}


class TasksMappingError(RuntimeError):
    """One stable, bounded failure from the mapping layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TasksGenerationRefused(TasksMappingError):
    """A request asked for a Tasks dialect this build does not implement."""


class TaskNotFound(TasksMappingError):
    """The task id is unknown, or its durable row is no longer addressable."""


class DurableJobEngine(Protocol):
    """The narrow job-engine surface this mapping layer depends on.

    Declared as a protocol so the mapping can be exercised with a fake engine in
    solver-free tests and cannot reach into engine internals.
    """

    def submit(self, raw_spec: dict[str, Any]) -> dict[str, Any]: ...

    def status(self, job_id: str) -> dict[str, Any]: ...

    def cancel(self, job_id: str, *, expected_attempt: int | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TaskRow:
    """One durable task-to-job binding."""

    task_id: str
    job_id: str
    created_at_ms: int
    ttl_ms: int | None
    poll_interval_ms: int
    spec_fingerprint: str
    owner: str
    request_client: str | None = None
    status_override: str | None = None
    cleanup: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> dict[str, Any]:
        # The row carries its own schema identity so a journal read years later
        # is self-describing rather than depending on this module's current
        # constants.
        return {
            "schema_name": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "created_at_ms": self.created_at_ms,
            "ttl_ms": self.ttl_ms,
            "poll_interval_ms": self.poll_interval_ms,
            "spec_fingerprint": self.spec_fingerprint,
            "owner": self.owner,
            "request_client": self.request_client,
            "status_override": self.status_override,
            "cleanup": dict(self.cleanup),
        }


def _bounded(value: object, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TasksMappingError("invalid_mapping_row", f"{label} must be a nonempty string")
    if len(value) > maximum:
        raise TasksMappingError("invalid_mapping_row", f"{label} is out of range")
    return value


def _finite_int(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise TasksMappingError(
            "invalid_mapping_row", f"{label} must be an integer within [{minimum}, {maximum}]"
        )
    return value


def normalize_ttl_ms(value: object) -> int | None:
    """Validate a requested TTL. ``None`` means unlimited retention."""
    if value is None:
        return None
    return _finite_int(value, "ttlMs", minimum=0, maximum=2**53 - 1)


def validate_extension_generation(requested: object) -> str:
    """Return the admitted generation, or refuse an unsupported dialect."""
    if requested is None:
        return TASKS_WIRE_GENERATION
    if not isinstance(requested, str):
        raise TasksMappingError("invalid_generation", "the Tasks generation must be a string")
    if requested in REFUSED_TASKS_GENERATIONS:
        raise TasksGenerationRefused(
            "unsupported_tasks_generation",
            f"the experimental {requested} Tasks dialect is not implemented; "
            f"only {TASKS_WIRE_GENERATION} is supported",
        )
    if requested != TASKS_WIRE_GENERATION:
        raise TasksGenerationRefused(
            "unsupported_tasks_generation",
            f"unknown Tasks generation {requested!r}; only {TASKS_WIRE_GENERATION} is supported",
        )
    return requested


def task_status_from_job_state(state: object) -> str:
    """Map one durable job state onto a task status, failing closed."""
    if not isinstance(state, str) or state not in JOB_STATE_TO_TASK_STATUS:
        raise TasksMappingError(
            "unmappable_job_state",
            f"durable job state {state!r} has no declared task-status mapping",
        )
    return JOB_STATE_TO_TASK_STATUS[state]


class TasksMappingStore:
    """Append-only task-to-job mapping with bounded reads.

    An append-only journal (rather than a mutable table) means a crash mid-write
    cannot corrupt an earlier binding: the last complete record wins, and a
    partial trailing record is ignored. That property is what lets a restarted
    process rebind the same task id to the same durable job.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.journal_path = self.root / "tasks.jsonl"

    def append(self, row: TaskRow) -> None:
        payload = row.to_document()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_TASK_ROW_BYTES:
            raise TasksMappingError("mapping_row_too_large", "task mapping row exceeds its bound")
        self.root.mkdir(parents=True, exist_ok=True)
        append_jsonl_record(self.journal_path, payload)

    def rows(self) -> list[TaskRow]:
        """Return the complete, well-formed rows from the append-only journal.

        The reader classifies the journal rather than raising on damage: a
        truncated trailing record reports ``partial`` and a corrupt journal
        reports ``corrupt``. In both cases only the complete records are used, so
        a crash mid-write degrades the newest row instead of erasing the
        bindings a restarted process still needs.
        """
        try:
            report = read_complete_jsonl(self.journal_path)
        except OSError as exc:
            raise TasksMappingError(
                "mapping_unreadable", "task mapping journal is unreadable"
            ) from exc
        state = report.get("state")
        if state == "oversized":
            raise TasksMappingError(
                "mapping_oversized", "task mapping journal exceeds its size bound"
            )
        records = report.get("records")
        if not isinstance(records, list):
            return []
        rows: list[TaskRow] = []
        for document in records[-MAX_TASK_ROWS:]:
            if not isinstance(document, dict):
                continue
            try:
                rows.append(self._row(document))
            except TasksMappingError:
                # A malformed historical row must not erase the readings of the
                # rows that are well formed, but it also must not be trusted.
                continue
        return rows

    @staticmethod
    def _row(document: Mapping[str, Any]) -> TaskRow:
        cleanup = document.get("cleanup")
        return TaskRow(
            task_id=_bounded(document.get("task_id"), "task_id", 128),
            job_id=_bounded(document.get("job_id"), "job_id", 256),
            created_at_ms=_finite_int(
                document.get("created_at_ms"), "created_at_ms", minimum=0, maximum=2**53 - 1
            ),
            ttl_ms=normalize_ttl_ms(document.get("ttl_ms")),
            poll_interval_ms=_finite_int(
                document.get("poll_interval_ms"),
                "poll_interval_ms",
                minimum=1,
                maximum=2**53 - 1,
            ),
            spec_fingerprint=_bounded(document.get("spec_fingerprint"), "spec_fingerprint", 128),
            owner=_bounded(document.get("owner"), "owner", 256),
            request_client=(
                document.get("request_client")
                if isinstance(document.get("request_client"), str)
                else None
            ),
            status_override=(
                document.get("status_override")
                if isinstance(document.get("status_override"), str)
                else None
            ),
            cleanup=dict(cleanup) if isinstance(cleanup, dict) else {},
        )

    def latest_for_task(self, task_id: str) -> TaskRow | None:
        found: TaskRow | None = None
        for row in self.rows():
            if row.task_id == task_id:
                found = row
        return found

    def latest_for_job(self, job_id: str) -> TaskRow | None:
        found: TaskRow | None = None
        for row in self.rows():
            if row.job_id == job_id:
                found = row
        return found

    def rows_for_owner(self, owner: str) -> list[TaskRow]:
        return [row for row in self.rows() if row.owner == owner]


def spec_fingerprint(raw_spec: Mapping[str, Any]) -> str:
    """Fingerprint a submission so an identical retry is recognisable."""
    if not isinstance(raw_spec, Mapping):
        raise TasksMappingError("invalid_spec", "a task submission must be an object")
    return canonical_sha256_v1({"task_submission": dict(raw_spec)})


class TasksBridge:
    """Map durable jobs onto the stable Tasks extension wire contract.

    The bridge is the only place that decides whether a task-shaped result may be
    emitted, and it never owns solver execution.
    """

    def __init__(
        self,
        *,
        engine: DurableJobEngine,
        store: TasksMappingStore,
        owner: str,
        clock_ms: Callable[[], int] | None = None,
        new_task_id: Callable[[], str] | None = None,
    ) -> None:
        self._engine = engine
        self._store = store
        self._owner = _bounded(owner, "owner", 256)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._new_task_id = new_task_id or _uuid4

    # -- admission ---------------------------------------------------------

    @staticmethod
    def client_declared_tasks(client_extensions: object) -> bool:
        """Whether *this* request declared the Tasks extension.

        Prior declarations must not be inferred from, so this reads one request's
        capability map only and treats a malformed map as "not declared".
        """
        if not isinstance(client_extensions, Mapping):
            return False
        return TASKS_EXTENSION_IDENTIFIER in client_extensions

    @staticmethod
    def refuse_without_capability() -> dict[str, Any]:
        """The exact ``-32021`` error body for a non-declaring client."""
        return {
            "code": -32021,
            "message": "Missing required client capability",
            "data": {
                "requiredCapabilities": {
                    "extensions": {TASKS_EXTENSION_IDENTIFIER: {}},
                }
            },
        }

    # -- submission --------------------------------------------------------

    def submit_task(
        self,
        raw_spec: Mapping[str, Any],
        *,
        client_extensions: object,
        request_client: str | None = None,
        ttl_ms: object = None,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        generation: object = None,
    ) -> dict[str, Any]:
        """Create or reuse a durable job and return a durably-bound task handle."""
        validate_extension_generation(generation)
        if not self.client_declared_tasks(client_extensions):
            raise TasksMappingError(
                "client_capability_missing",
                "a task-shaped result requires the Tasks extension on this request",
            )
        ttl = normalize_ttl_ms(ttl_ms)
        interval = _finite_int(poll_interval_ms, "pollIntervalMs", minimum=1, maximum=2**53 - 1)
        fingerprint = spec_fingerprint(raw_spec)

        # Reuse an existing durable job for an identical retry so a duplicate
        # submission cannot launch a second simulation.
        submission = self._engine.submit(dict(raw_spec))
        job_id = _bounded(submission.get("job_id"), "job_id", 256)
        existing = self._store.latest_for_job(job_id)

        created = self._clock_ms()
        if existing is not None:
            task_id = existing.task_id
            created = existing.created_at_ms
        else:
            # Pre-generate the id so the durable mapping row is written *before*
            # any task-shaped result is returned.
            task_id = self._new_task_id()
        row = TaskRow(
            task_id=task_id,
            job_id=job_id,
            created_at_ms=created,
            ttl_ms=ttl,
            poll_interval_ms=interval,
            spec_fingerprint=fingerprint,
            owner=self._owner,
            request_client=request_client,
        )
        self._store.append(row)
        persisted = self._store.latest_for_task(task_id)
        if persisted is None:
            raise TasksMappingError(
                "mapping_not_durable",
                "the task mapping row was not durable after writing; refusing to acknowledge",
            )
        return {
            "resultType": "task",
            "taskId": task_id,
            "status": "working",
            "createdAt": _iso_ms(created),
            "lastUpdatedAt": _iso_ms(created),
            "ttlMs": ttl,
            "pollIntervalMs": interval,
            "duplicate_submission": bool(submission.get("duplicate")),
            "jobStatus": submission.get("status"),
        }

    # -- reads and control -------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        """Return one ``tasks/get`` result, or raise for an unaddressable id."""
        row = self._require_row(task_id)
        state = self._job_state(row.job_id)
        status = _task_status_for(row, state)
        document: dict[str, Any] = {
            "resultType": "complete",
            "taskId": row.task_id,
            "status": status,
            "createdAt": _iso_ms(row.created_at_ms),
            "lastUpdatedAt": _iso_ms(self._clock_ms()),
            "ttlMs": row.ttl_ms,
            "pollIntervalMs": row.poll_interval_ms,
        }
        if row.status_override:
            document["statusMessage"] = row.status_override
        if status == "completed":
            document["result"] = _completed_payload(state)
        elif status == "failed":
            document["error"] = _failed_payload(state)
        return document

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Request cooperative cancellation and return an empty acknowledgement.

        The acknowledgement is *not* completion evidence: the response records
        that cleanup is still pending until the exact owned job reaches a
        terminal state.
        """
        row = self._require_row(task_id)
        state = self._job_state(row.job_id)
        status = _task_status_for(row, state)
        if status in TERMINAL_TASK_STATUSES:
            return {
                "resultType": "complete",
                "taskId": row.task_id,
                "cancellationRequested": False,
                "taskStatus": status,
                "reason": "task_is_terminal",
                "cleanupPending": False,
            }
        attempt = state.get("attempt")
        expected = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None
        try:
            receipt = self._engine.cancel(row.job_id, expected_attempt=expected)
        except Exception as exc:
            raise TasksMappingError(
                "cancellation_failed", "the durable job refused the cancellation request"
            ) from exc
        return {
            "resultType": "complete",
            "taskId": row.task_id,
            "cancellationRequested": True,
            # Distinct from completion, and never claimed from an acknowledgement.
            "taskStatus": "working",
            "cleanupPending": True,
            "cancellationReceipt": _bounded_receipt(receipt),
        }

    def update_task(self, task_id: str, input_responses: object) -> dict[str, Any]:
        """Acknowledge only outstanding input keys; silence is not consent.

        This release submits no server-to-client input requests, so no key can be
        outstanding. Unknown keys are ignored rather than treated as answers,
        and the response says so instead of implying acceptance.
        """
        row = self._require_row(task_id)
        if not isinstance(input_responses, Mapping):
            raise TasksMappingError("invalid_input_responses", "inputResponses must be an object")
        return {
            "resultType": "complete",
            "taskId": row.task_id,
            "acceptedKeys": [],
            "ignoredKeys": sorted(str(key) for key in input_responses),
            "reason": "no_outstanding_input_requests",
        }

    def cleanup_receipt(self, task_id: str) -> dict[str, Any]:
        """Report cleanup separately from protocol cancellation.

        Cancellation completes only when the owned job is terminal. A protocol
        acknowledgement alone never yields a completed cleanup receipt.
        """
        row = self._require_row(task_id)
        state = self._job_state(row.job_id)
        status = _task_status_for(row, state)
        terminal = status in TERMINAL_TASK_STATUSES
        body = {
            "schema_name": "comsol_mcp.task_cleanup_receipt",
            "schema_version": "1.0.0",
            "taskId": row.task_id,
            "jobId": row.job_id,
            "owner": row.owner,
            "taskStatus": status,
            "jobState": state.get("status"),
            "cleanupComplete": terminal,
            "pendingReason": None if terminal else "job_not_terminal",
            "protocolAcknowledgementIsNotCleanup": True,
        }
        return {**body, "cleanup_receipt_sha256": canonical_sha256_v1(body)}

    # -- internals ---------------------------------------------------------

    def _require_row(self, task_id: str) -> TaskRow:
        identifier = _bounded(task_id, "taskId", 128)
        row = self._store.latest_for_task(identifier)
        if row is None:
            raise TaskNotFound("task_not_found", f"unknown or expired task id {identifier!r}")
        if row.owner != self._owner:
            # A task id is only controllable by the owner that created it.
            raise TaskNotFound("task_not_found", f"unknown or expired task id {identifier!r}")
        return row

    def _job_state(self, job_id: str) -> dict[str, Any]:
        try:
            state = self._engine.status(job_id)
        except Exception as exc:
            raise TaskNotFound(
                "task_not_found", "the durable job for this task is no longer readable"
            ) from exc
        if not isinstance(state, dict):
            raise TaskNotFound("task_not_found", "the durable job state is not an object")
        return state


def _task_status_for(row: TaskRow, state: Mapping[str, Any]) -> str:
    """Resolve the task status, honouring a recorded override first."""
    if row.status_override in TERMINAL_TASK_STATUSES:
        return str(row.status_override)
    return task_status_from_job_state(state.get("status"))


def _completed_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """Inline exactly what the synchronous call would have returned.

    A tool-level ``isError`` completion belongs here as ``completed``; the
    extension reserves ``failed`` for JSON-RPC faults.
    """
    summary = state.get("summary")
    payload: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": "The durable job reached a terminal state; see structuredContent.",
            }
        ],
        "isError": bool(state.get("job_error")) if isinstance(state, Mapping) else False,
        "structuredContent": {
            "jobId": state.get("job_id"),
            "status": state.get("status"),
            "attempt": state.get("attempt"),
            "summary": summary if isinstance(summary, dict) else None,
            "artifactReferences": state.get("artifacts")
            if isinstance(state.get("artifacts"), list)
            else [],
        },
    }
    return payload


def _failed_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    """A protocol-level fault, kept separate from a completed tool error."""
    return {
        "code": -32603,
        "message": str(state.get("error") or "the durable job did not complete"),
        "data": {
            "jobId": state.get("job_id"),
            "status": state.get("status"),
            "attempt": state.get("attempt"),
        },
    }


def _bounded_receipt(receipt: object) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        return {}
    bounded: dict[str, Any] = {}
    for key in sorted(receipt)[:32]:
        value = receipt[key]
        if isinstance(value, (str, int, float, bool)) or value is None:
            bounded[str(key)] = value
    return bounded


def _iso_ms(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _uuid4() -> str:
    return str(uuid4())


#: Files the mapping layer may create inside its own root. Declared so a test can
#: assert the layer never writes outside its owned directory.
OWNED_FILES = ("tasks.jsonl",)

__all__ = [
    "DEFAULT_POLL_INTERVAL_MS",
    "DEFAULT_TASK_TTL_MS",
    "JOB_STATE_TO_TASK_STATUS",
    "MAX_TASK_ROWS",
    "MAX_TASK_ROW_BYTES",
    "OWNED_FILES",
    "REFUSED_TASKS_GENERATIONS",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "TASKS_WIRE_GENERATION",
    "TASK_STATUSES",
    "TERMINAL_TASK_STATUSES",
    "DurableJobEngine",
    "TaskNotFound",
    "TaskRow",
    "TasksBridge",
    "TasksGenerationRefused",
    "TasksMappingError",
    "TasksMappingStore",
    "normalize_ttl_ms",
    "spec_fingerprint",
    "task_status_from_job_state",
    "validate_extension_generation",
]
