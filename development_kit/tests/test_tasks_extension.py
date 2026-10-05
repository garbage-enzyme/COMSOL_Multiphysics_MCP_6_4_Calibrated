"""Fake-worker Tasks conformance and negative wire tests.

These tests exercise the stable ``2026-07-28`` Tasks extension against a fake
durable-job engine, so the whole mapping and wire contract is verified without
starting COMSOL. A licensed end-to-end check is a separate, serial,
caller-authorized run.

The negative cases matter as much as the positive ones: a task-shaped result
reaching a client that did not opt in, a duplicate submission launching a second
simulation, or an acknowledgement being reported as cleanup would each be a real
defect that a happy-path test cannot see.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from comsol_mcp.jobs.tasks_bridge import (
    DEFAULT_POLL_INTERVAL_MS,
    JOB_STATE_TO_TASK_STATUS,
    TASKS_WIRE_GENERATION,
    TERMINAL_TASK_STATUSES,
    TaskNotFound,
    TasksBridge,
    TasksGenerationRefused,
    TasksMappingError,
    TasksMappingStore,
    normalize_ttl_ms,
    spec_fingerprint,
    task_status_from_job_state,
    validate_extension_generation,
)
from comsol_mcp.jobs.tasks_extension import (
    REMOVED_TASKS_METHODS,
    SUPPORTED_PROTOCOL_VERSIONS,
    TASK_CAPABLE_TOOLS,
    TASKS_METHOD_CANCEL,
    TASKS_METHOD_GET,
    TASKS_METHOD_UPDATE,
    advertise_tasks_capability,
    build_tasks_extension,
    missing_capability_error,
    request_declared_tasks,
)
from comsol_mcp.protocol_identity import TASKS_EXTENSION_IDENTIFIER

OPT_IN = {TASKS_EXTENSION_IDENTIFIER: {}}


class FakeEngine:
    """A deterministic stand-in for the durable job engine.

    Records every call so a test can prove that a retry did not reach the engine
    a second time, and that cancellation is requested with the attempt the caller
    believed was current.
    """

    def __init__(self) -> None:
        self.submit_calls: list[dict[str, Any]] = []
        self.cancel_calls: list[tuple[str, int | None]] = []
        self._jobs: dict[str, dict[str, Any]] = {}
        self._next = 0
        self.duplicate_fingerprints: set[str] = set()
        self.fail_submit = False

    def submit(self, raw_spec: dict[str, Any]) -> dict[str, Any]:
        self.submit_calls.append(dict(raw_spec))
        if self.fail_submit:
            raise RuntimeError("engine refused the submission")
        fingerprint = spec_fingerprint(raw_spec)
        if fingerprint in self.duplicate_fingerprints:
            existing = next(
                job_id
                for job_id, job in self._jobs.items()
                if job["spec_fingerprint"] == fingerprint
            )
            return {
                "success": True,
                "job_id": existing,
                "status": self._jobs[existing]["status"],
                "duplicate": True,
            }
        self.duplicate_fingerprints.add(fingerprint)
        self._next += 1
        job_id = f"job-{self._next:04d}"
        self._jobs[job_id] = {
            "job_id": job_id,
            "status": "submitted",
            "attempt": 1,
            "spec_fingerprint": fingerprint,
        }
        return {"success": True, "job_id": job_id, "status": "submitted"}

    def status(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            raise KeyError(job_id)
        return dict(self._jobs[job_id])

    def cancel(self, job_id: str, *, expected_attempt: int | None = None) -> dict[str, Any]:
        self.cancel_calls.append((job_id, expected_attempt))
        if job_id not in self._jobs:
            raise KeyError(job_id)
        self._jobs[job_id]["status"] = "cancel_requested"
        return {"job_id": job_id, "status": "cancel_requested", "attempt": expected_attempt}

    # test controls -------------------------------------------------------

    def set_state(self, job_id: str, status: str, **extra: Any) -> None:
        self._jobs[job_id].update({"status": status, **extra})


@pytest.fixture()
def bridge(tmp_path: Path) -> TasksBridge:
    return TasksBridge(
        engine=FakeEngine(),
        store=TasksMappingStore(tmp_path / "tasks"),
        owner="owner-a",
    )


def _engine(bridge: TasksBridge) -> FakeEngine:
    engine = bridge._engine
    assert isinstance(engine, FakeEngine)
    return engine


def _submit(bridge: TasksBridge, spec: dict[str, Any] | None = None, **kwargs: Any) -> dict:
    return bridge.submit_task(
        spec or {"job_type": "staged_sweep", "parameter_name": "wl", "parameter_values": [1.0]},
        client_extensions=OPT_IN,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Conformance: the result envelope and its required fields
# ---------------------------------------------------------------------------


def test_a_task_handle_carries_every_required_wire_field(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    assert handle["resultType"] == "task"
    assert isinstance(handle["taskId"], str) and handle["taskId"]
    assert handle["status"] == "working"
    # Required by the extension schema.
    for field in ("taskId", "status", "createdAt", "lastUpdatedAt", "ttlMs"):
        assert field in handle, field
    assert handle["pollIntervalMs"] == DEFAULT_POLL_INTERVAL_MS
    # ISO-8601 timestamps, not epoch numbers.
    assert handle["createdAt"].endswith("Z") and "T" in handle["createdAt"]
    assert handle["lastUpdatedAt"].endswith("Z")


def test_tasks_get_answers_with_the_complete_result_type(bridge: TasksBridge) -> None:
    """`tasks/get` is a Detail edTask with resultType "complete", not "task"."""
    handle = _submit(bridge)
    detail = bridge.get_task(handle["taskId"])
    assert detail["resultType"] == "complete"
    assert detail["taskId"] == handle["taskId"]
    assert detail["status"] == "working"
    assert "result" not in detail and "error" not in detail


def test_the_ttl_field_name_is_the_stable_one(bridge: TasksBridge) -> None:
    """The stable dialect uses `ttlMs`; `ttl` belongs to the refused dialect."""
    handle = _submit(bridge, ttl_ms=60_000)
    assert handle["ttlMs"] == 60_000
    assert "ttl" not in handle
    assert "pollInterval" not in handle


def test_terminal_statuses_carry_the_right_payload(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    engine = _engine(bridge)
    job_id = handle["taskId"]
    row = bridge._store.latest_for_task(job_id)
    assert row is not None

    engine.set_state(row.job_id, "completed", summary={"points": 3})
    completed = bridge.get_task(job_id)
    assert completed["status"] == "completed"
    # Terminal payload is inline, and a completed tool error is NOT `failed`.
    assert completed["result"]["structuredContent"]["jobId"] == row.job_id
    assert completed["result"]["isError"] is False

    engine.set_state(row.job_id, "failed", error="solver blew up")
    failed = bridge.get_task(job_id)
    assert failed["status"] == "failed"
    assert failed["error"]["message"] == "solver blew up"


def test_a_completed_tool_error_maps_to_completed_not_failed(bridge: TasksBridge) -> None:
    """Tool-level isError belongs in `result`; `failed` is for JSON-RPC faults."""
    handle = _submit(bridge)
    row = bridge._store.latest_for_task(handle["taskId"])
    assert row is not None
    _engine(bridge).set_state(row.job_id, "completed", job_error=True)
    detail = bridge.get_task(handle["taskId"])
    assert detail["status"] == "completed"
    assert detail["result"]["isError"] is True
    assert "error" not in detail


@pytest.mark.parametrize("state", sorted(JOB_STATE_TO_TASK_STATUS))
def test_every_engine_state_has_a_declared_task_status(state: str) -> None:
    assert task_status_from_job_state(state) in {
        "working",
        "completed",
        "cancelled",
        "failed",
    }


def test_an_unknown_engine_state_fails_closed() -> None:
    with pytest.raises(TasksMappingError) as excinfo:
        task_status_from_job_state("teleported")
    assert excinfo.value.code == "unmappable_job_state"


# ---------------------------------------------------------------------------
# Durability: acknowledgement implies a readable task
# ---------------------------------------------------------------------------


def test_the_mapping_is_durable_before_the_handle_is_returned(bridge: TasksBridge) -> None:
    """A client may issue tasks/get immediately; it must not see an unknown id."""
    handle = _submit(bridge)
    # A fresh store object over the same journal proves durability, not caching.
    reopened = TasksMappingStore(bridge._store.root)
    row = reopened.latest_for_task(handle["taskId"])
    assert row is not None
    assert row.job_id == handle["taskId"] or row.job_id


def test_a_restart_rebinds_the_same_task_id(tmp_path: Path) -> None:
    """Reconnect retrieves the same durable job rather than a new one."""
    engine = FakeEngine()
    store = TasksMappingStore(tmp_path / "tasks")
    first = TasksBridge(engine=engine, store=store, owner="owner-a")
    handle = _submit(first)

    restarted = TasksBridge(
        engine=engine, store=TasksMappingStore(tmp_path / "tasks"), owner="owner-a"
    )
    detail = restarted.get_task(handle["taskId"])
    assert detail["taskId"] == handle["taskId"]
    assert detail["status"] == "working"


def test_a_partial_trailing_record_does_not_erase_earlier_bindings(tmp_path: Path) -> None:
    """A crash mid-write must not corrupt the last complete binding."""
    engine = FakeEngine()
    store = TasksMappingStore(tmp_path / "tasks")
    bridge = TasksBridge(engine=engine, store=store, owner="owner-a")
    handle = _submit(bridge)

    with store.journal_path.open("ab") as handle_file:
        handle_file.write(b'{"task_id": "truncated"')

    reopened = TasksBridge(engine=engine, store=TasksMappingStore(store.root), owner="owner-a")
    assert reopened.get_task(handle["taskId"])["taskId"] == handle["taskId"]


# ---------------------------------------------------------------------------
# Idempotency: a retry must not launch a second simulation
# ---------------------------------------------------------------------------


def test_an_identical_retry_reuses_the_durable_job_and_task(bridge: TasksBridge) -> None:
    spec = {"job_type": "staged_sweep", "parameter_name": "wl", "parameter_values": [1.0]}
    first = _submit(bridge, spec)
    second = _submit(bridge, spec)
    assert second["taskId"] == first["taskId"]
    assert second["duplicate_submission"] is True
    # The engine saw two submissions, but the mapping bound one task to one job.
    assert len(_engine(bridge).submit_calls) == 2
    assert len({row.job_id for row in bridge._store.rows()}) == 1
    assert len({row.task_id for row in bridge._store.rows()}) == 1


def test_a_different_specification_gets_its_own_task(bridge: TasksBridge) -> None:
    first = _submit(bridge, {"job_type": "staged_sweep", "parameter_values": [1.0]})
    second = _submit(bridge, {"job_type": "staged_sweep", "parameter_values": [2.0]})
    assert second["taskId"] != first["taskId"]


# ---------------------------------------------------------------------------
# Negative: opt-in is per request, and absent capability is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "capabilities",
    [
        {},
        {"other/extension": {}},
        None,
        "not-a-map",
        [],
    ],
)
def test_task_submission_requires_this_requests_capability(
    bridge: TasksBridge, capabilities: object
) -> None:
    """Only the declared form is admitted; every other shape is refused."""
    with pytest.raises(TasksMappingError) as excinfo:
        bridge.submit_task({"job_type": "staged_sweep"}, client_extensions=capabilities)
    assert excinfo.value.code == "client_capability_missing"


@pytest.mark.parametrize(
    "capabilities",
    [{TASKS_EXTENSION_IDENTIFIER: {}}, {TASKS_EXTENSION_IDENTIFIER: {"ttlMs": 1000}}],
)
def test_a_declared_capability_map_is_accepted(
    bridge: TasksBridge, capabilities: dict[str, Any]
) -> None:
    handle = bridge.submit_task(
        {"job_type": "staged_sweep", "parameter_values": [1.0]},
        client_extensions=capabilities,
    )
    assert handle["resultType"] == "task"


def test_a_prior_declaration_is_not_inferred_from() -> None:
    """Capability is read from one request envelope, never from history."""
    assert request_declared_tasks(None) is False
    assert request_declared_tasks({}) is False
    assert request_declared_tasks({"io.modelcontextprotocol/clientCapabilities": None}) is False
    assert (
        request_declared_tasks(
            {"io.modelcontextprotocol/clientCapabilities": {"extensions": {"other/extension": {}}}}
        )
        is False
    )
    assert (
        request_declared_tasks(
            {"io.modelcontextprotocol/clientCapabilities": {"extensions": OPT_IN}}
        )
        is True
    )


def test_the_missing_capability_error_is_the_required_minus_32021() -> None:
    body = missing_capability_error()
    assert body["code"] == -32021
    assert body["data"]["requiredCapabilities"]["extensions"] == {TASKS_EXTENSION_IDENTIFIER: {}}


# ---------------------------------------------------------------------------
# Negative: unknown, expired, and cross-owner task ids
# ---------------------------------------------------------------------------


def test_an_unknown_task_id_is_reported_as_not_found(bridge: TasksBridge) -> None:
    with pytest.raises(TaskNotFound):
        bridge.get_task("no-such-task")


def test_another_owners_task_is_indistinguishable_from_unknown(tmp_path: Path) -> None:
    """Ownership is enforced, and must not leak the existence of the task."""
    engine = FakeEngine()
    store = TasksMappingStore(tmp_path / "tasks")
    owner_a = TasksBridge(engine=engine, store=store, owner="owner-a")
    handle = _submit(owner_a)

    owner_b = TasksBridge(engine=engine, store=TasksMappingStore(store.root), owner="owner-b")
    with pytest.raises(TaskNotFound):
        owner_b.get_task(handle["taskId"])
    with pytest.raises(TaskNotFound):
        owner_b.cancel_task(handle["taskId"])


def test_a_job_that_is_no_longer_readable_is_reported_as_not_found(tmp_path: Path) -> None:
    engine = FakeEngine()
    store = TasksMappingStore(tmp_path / "tasks")
    bridge = TasksBridge(engine=engine, store=store, owner="owner-a")
    handle = _submit(bridge)
    engine._jobs.clear()
    with pytest.raises(TaskNotFound):
        bridge.get_task(handle["taskId"])


# ---------------------------------------------------------------------------
# Cancellation: cooperative, and distinct from completion
# ---------------------------------------------------------------------------


def test_cancellation_acknowledges_without_claiming_cleanup(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    receipt = bridge.cancel_task(handle["taskId"])
    assert receipt["resultType"] == "complete"
    assert receipt["cancellationRequested"] is True
    # An acknowledgement is never reported as terminal.
    assert receipt["taskStatus"] == "working"
    assert receipt["cleanupPending"] is True


def test_cancellation_passes_the_attempt_the_caller_believed_current(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    bridge.cancel_task(handle["taskId"])
    _job_id, expected_attempt = _engine(bridge).cancel_calls[-1]
    assert expected_attempt == 1


def test_a_terminal_task_refuses_a_new_cancellation(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    row = bridge._store.latest_for_task(handle["taskId"])
    assert row is not None
    _engine(bridge).set_state(row.job_id, "completed")
    receipt = bridge.cancel_task(handle["taskId"])
    assert receipt["cancellationRequested"] is False
    assert receipt["reason"] == "task_is_terminal"
    assert _engine(bridge).cancel_calls == []


def test_cleanup_is_incomplete_until_the_job_is_terminal(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    pending = bridge.cleanup_receipt(handle["taskId"])
    assert pending["cleanupComplete"] is False
    assert pending["pendingReason"] == "job_not_terminal"
    # The receipt states the distinction explicitly rather than implying it.
    assert pending["protocolAcknowledgementIsNotCleanup"] is True

    row = bridge._store.latest_for_task(handle["taskId"])
    assert row is not None
    _engine(bridge).set_state(row.job_id, "completed")
    complete = bridge.cleanup_receipt(handle["taskId"])
    assert complete["cleanupComplete"] is True
    assert complete["pendingReason"] is None
    assert complete["cleanup_receipt_sha256"] != pending["cleanup_receipt_sha256"]


# ---------------------------------------------------------------------------
# Input requests: silence is not consent
# ---------------------------------------------------------------------------


def test_update_ignores_unknown_input_keys_and_says_so(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    result = bridge.update_task(handle["taskId"], {"k1": {"ok": True}})
    assert result["acceptedKeys"] == []
    assert result["ignoredKeys"] == ["k1"]
    assert result["reason"] == "no_outstanding_input_requests"


def test_update_rejects_a_non_object_payload(bridge: TasksBridge) -> None:
    handle = _submit(bridge)
    with pytest.raises(TasksMappingError) as excinfo:
        bridge.update_task(handle["taskId"], ["not", "an", "object"])
    assert excinfo.value.code == "invalid_input_responses"


# ---------------------------------------------------------------------------
# TTL: expiry never destroys evidence and never abandons a running solve
# ---------------------------------------------------------------------------


def test_ttl_is_nullable_meaning_unlimited() -> None:
    assert normalize_ttl_ms(None) is None
    assert normalize_ttl_ms(0) == 0


@pytest.mark.parametrize("value", [-1, 2**53, True, "60000", 1.5])
def test_an_invalid_ttl_is_refused(value: object) -> None:
    with pytest.raises(TasksMappingError):
        normalize_ttl_ms(value)


def test_a_running_job_is_reported_running_past_its_ttl(tmp_path: Path) -> None:
    """TTL limits addressability; it must not erase a running solve or evidence."""
    now = {"ms": 1_000_000}
    engine = FakeEngine()
    bridge = TasksBridge(
        engine=engine,
        store=TasksMappingStore(tmp_path / "tasks"),
        owner="owner-a",
        clock_ms=lambda: now["ms"],
    )
    handle = _submit(bridge, ttl_ms=1000)
    now["ms"] += 60_000  # far past the TTL
    detail = bridge.get_task(handle["taskId"])
    assert detail["status"] == "working"
    # The durable row and its evidence are still present.
    assert TasksMappingStore(bridge._store.root).latest_for_task(handle["taskId"]) is not None


# ---------------------------------------------------------------------------
# Negative: the refused experimental dialect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("generation", ["2025-11-25", "2024-01-01", "draft", ""])
def test_a_refused_tasks_generation_is_rejected_not_reinterpreted(
    bridge: TasksBridge, generation: str
) -> None:
    with pytest.raises(TasksGenerationRefused):
        _submit(bridge, generation=generation)


def test_only_the_stable_generation_is_admitted() -> None:
    assert validate_extension_generation(None) == TASKS_WIRE_GENERATION
    assert validate_extension_generation(TASKS_WIRE_GENERATION) == TASKS_WIRE_GENERATION
    assert SUPPORTED_PROTOCOL_VERSIONS == frozenset({TASKS_WIRE_GENERATION})


def test_the_removed_methods_are_declared_but_never_registered() -> None:
    """`tasks/result` and `tasks/list` were deleted by the redesign."""
    assert REMOVED_TASKS_METHODS == ("tasks/result", "tasks/list")
    bridge = TasksBridge(
        engine=FakeEngine(), store=TasksMappingStore(Path("unused-root")), owner="owner-a"
    )
    registered = {binding.method for binding in build_tasks_extension(bridge).methods()}
    assert registered == {TASKS_METHOD_GET, TASKS_METHOD_CANCEL, TASKS_METHOD_UPDATE}
    assert not registered & set(REMOVED_TASKS_METHODS)


# ---------------------------------------------------------------------------
# Extension advertisement and opt-in gating
# ---------------------------------------------------------------------------


def test_the_extension_advertises_its_identifier_with_no_settings() -> None:
    assert advertise_tasks_capability() == {TASKS_EXTENSION_IDENTIFIER: {}}


def test_the_extension_binds_only_the_three_stable_methods() -> None:
    bridge = TasksBridge(
        engine=FakeEngine(), store=TasksMappingStore(Path("unused-root")), owner="owner-a"
    )
    extension = build_tasks_extension(bridge)
    assert extension.identifier == TASKS_EXTENSION_IDENTIFIER
    assert extension.settings() == {}
    methods = {binding.method: binding for binding in extension.methods()}
    assert set(methods) == {TASKS_METHOD_GET, TASKS_METHOD_CANCEL, TASKS_METHOD_UPDATE}
    for binding in methods.values():
        # Every method is restricted to the stable revision.
        assert binding.protocol_versions == SUPPORTED_PROTOCOL_VERSIONS


def test_only_job_submit_is_task_capable_in_this_release() -> None:
    """Advertising the extension is not a claim that every tool is task-capable."""
    assert TASK_CAPABLE_TOOLS == ("job_submit",)


@pytest.mark.parametrize(
    "tool_name", ["job_submit", "job_status", "solver_preflight", "capabilities"]
)
def test_the_interceptor_passes_through_without_opt_in(tmp_path: Path, tool_name: str) -> None:
    """A non-declaring client must never receive a task-shaped result."""

    class Params:
        def __init__(self, name: str) -> None:
            self.name = name
            self.arguments: dict[str, Any] = {"job_type": "staged_sweep"}

    class Context:
        meta = None

    ordinary = {"content": [{"type": "text", "text": "ordinary"}], "resultType": "complete"}
    calls: list[str] = []

    async def call_next(ctx: Any) -> dict[str, Any]:
        calls.append("called")
        return ordinary

    bridge = TasksBridge(
        engine=FakeEngine(),
        store=TasksMappingStore(tmp_path / "tasks"),
        owner="owner-a",
    )
    extension = build_tasks_extension(bridge)
    result = asyncio.run(extension.intercept_tool_call(Params(tool_name), Context(), call_next))
    assert result == ordinary
    assert result["resultType"] != "task"
    assert calls == ["called"]


def test_the_interceptor_returns_a_task_only_for_an_opted_in_capable_call(
    tmp_path: Path,
) -> None:
    class Params:
        name = "job_submit"
        arguments = {"job_type": "staged_sweep", "parameter_values": [1.0]}

    class Context:
        meta = {
            "io.modelcontextprotocol/clientCapabilities": {"extensions": OPT_IN},
            "io.modelcontextprotocol/clientInfo": {"name": "tasks-client", "version": "9.9"},
        }

    async def call_next(ctx: Any) -> dict[str, Any]:  # pragma: no cover - must not run
        raise AssertionError("an opted-in task-capable call must not run the ordinary handler")

    bridge = TasksBridge(
        engine=FakeEngine(),
        store=TasksMappingStore(tmp_path / "tasks"),
        owner="owner-a",
    )
    extension = build_tasks_extension(bridge)
    result = asyncio.run(extension.intercept_tool_call(Params(), Context(), call_next))
    assert result["resultType"] == "task"
    assert result["status"] == "working"
    row = bridge._store.latest_for_task(result["taskId"])
    assert row is not None
    # The self-reported client identity is recorded for the receipt.
    assert row.request_client == "tasks-client/9.9"


def test_an_opted_in_call_to_another_tool_is_not_task_shaped(tmp_path: Path) -> None:
    class Params:
        name = "job_status"
        arguments = {"job_id": "job-0001"}

    class Context:
        meta = {"io.modelcontextprotocol/clientCapabilities": {"extensions": OPT_IN}}

    ordinary = {"resultType": "complete", "content": []}

    async def call_next(ctx: Any) -> dict[str, Any]:
        return ordinary

    bridge = TasksBridge(
        engine=FakeEngine(),
        store=TasksMappingStore(tmp_path / "tasks"),
        owner="owner-a",
    )
    extension = build_tasks_extension(bridge)
    result = asyncio.run(extension.intercept_tool_call(Params(), Context(), call_next))
    assert result == ordinary


# ---------------------------------------------------------------------------
# Bounded persistence
# ---------------------------------------------------------------------------


def test_the_mapping_layer_never_writes_outside_its_owned_root(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    bridge = TasksBridge(engine=FakeEngine(), store=TasksMappingStore(root), owner="owner-a")
    for index in range(5):
        _submit(bridge, {"job_type": "staged_sweep", "parameter_values": [float(index)]})
    written = {path.name for path in root.iterdir()}
    assert written <= {"tasks.jsonl"}
    assert (root / "tasks.jsonl").is_file()


def test_the_journal_is_append_only_jsonl(bridge: TasksBridge) -> None:
    _submit(bridge)
    _submit(bridge, {"job_type": "staged_sweep", "parameter_values": [7.0]})
    lines = bridge._store.journal_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        document = json.loads(line)
        assert document["task_id"] and document["job_id"]


def test_a_terminal_task_keeps_its_durable_row(bridge: TasksBridge) -> None:
    """Expiry or completion must not erase the durable scientific receipt."""
    handle = _submit(bridge)
    row = bridge._store.latest_for_task(handle["taskId"])
    assert row is not None
    _engine(bridge).set_state(row.job_id, "completed")
    assert bridge.get_task(handle["taskId"])["status"] in TERMINAL_TASK_STATUSES
    assert bridge._store.latest_for_task(handle["taskId"]) is not None
