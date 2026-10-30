"""Solver-free reference-power control plane for durable job submission and reconciliation."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Protocol

import psutil

from .adjoint_optimization import expand_adjoint_optimization_manifest
from .attached_backend import normalize_attached_execution_backend
from .process_control import inspect_identity
from .resource_admission import normalize_resource_policy, resource_journal_capacity_verdict
from .store import (
    ACTIVE_STATES,
    JOB_SCHEMA_VERSION,
    TERMINAL_STATES,
    TRANSITIONS,
    JobLock,
    JobStore,
    atomic_write_json,
    cancel_request_targets_attempt,
    process_identity,
    process_identity_state,
    read_json,
)
from .surrogate_training import normalize_surrogate_training_spec
from .thermo_optomechanical_replay import normalize_thermo_optomechanical_replay_spec
from .validation_matrix import normalize_validation_matrix_spec


class _PollableProcess(Protocol):
    def poll(self) -> int | None: ...


def _uses_windows_venv_redirector(command: list[str]) -> bool:
    executable = Path(command[0])
    return (
        sys.platform == "win32"
        and executable.parent.name.casefold() == "scripts"
        and (executable.parent.parent / "pyvenv.cfg").is_file()
    )


def _launched_process_identity(process: subprocess.Popen, command: list[str]) -> dict[str, Any]:
    """Identify the executing child when a Windows venv launcher redirects."""
    if not _uses_windows_venv_redirector(command):
        return process_identity(process.pid)
    expected = hashlib.sha256("\0".join(command).encode("utf-8", errors="replace")).hexdigest()
    launcher = process_identity(process.pid)
    if launcher["command_signature"] != expected:
        raise OSError("Python venv launcher command does not match")
    matches = []
    for child in psutil.Process(process.pid).children():
        with child.oneshot():
            if child.ppid() != process.pid:
                continue
            created = child.create_time()
            observed = process_identity(child.pid)
        if (
            observed["pid"] == child.pid
            and observed["process_create_time"] == created
            and created >= launcher["process_create_time"]
            and observed["command_signature"] == expected
        ):
            matches.append(observed)
    if len(matches) != 1:
        raise OSError("Python venv executing child identity is not uniquely verified")
    # Recheck the launcher after enumeration so PID reuse cannot supply children.
    if process_identity(process.pid) != launcher:
        raise OSError("Python venv launcher identity changed")
    return matches[0]


def _wait_for_launched_identity(process: subprocess.Popen, command: list[str]) -> dict[str, Any]:
    expected = hashlib.sha256("\0".join(command).encode("utf-8", errors="replace")).hexdigest()
    deadline = time.monotonic() + 2.0
    while process.poll() is None:
        try:
            observed = _launched_process_identity(process, command)
            if observed["command_signature"] == expected:
                return observed
        except psutil.NoSuchProcess, psutil.ZombieProcess, OSError:
            pass
        if time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    raise RuntimeError("Launched watchdog identity could not be verified before the deadline")


logger = logging.getLogger(__name__)

REMOVED_RESEARCH_JOB_TYPES = frozenset(
    {
        "spectral_characterization",
        "convergence_campaign",
        "branch_continuation_campaign",
        "robust_shape_optimization",
    }
)

_DUPLICATE_INDEX_NAME = "duplicate-index.json"
_DUPLICATE_INDEX_JOB_TYPES = frozenset(
    {
        "validation_matrix",
        "thermo_optomechanical_replay",
        "adjoint_optimization",
        "surrogate_training",
    }
)


def _rebuild_duplicate_index(root: Path) -> dict[str, str]:
    """Rebuild the duplicate index from retained job evidence.

    Retained job directories are immutable evidence, so the rebuild reads
    each spec and never deletes a job. It runs once when the index is absent
    or corrupt; every subsequent submission is a bounded index lookup instead
    of a directory scan whose cost grew with retained history. A job
    directory whose spec evidence is missing or unreadable is uncertainty,
    not proof of absence: the rebuild fails closed naming that job, because
    silently skipping it would reopen duplicate admission after a storage
    fault. Repair or remove the named job before submitting again.
    """
    entries: dict[str, str] = {}
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not path.name.startswith("job-"):
            continue
        spec_path = path / "spec.json"
        if not spec_path.is_file():
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{path.name} has no spec evidence; repair or remove that job "
                "before new duplicate-type submissions"
            )
        try:
            existing = read_json(spec_path)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{path.name} has an unreadable spec ({type(exc).__name__}); "
                "repair or remove that job before new duplicate-type submissions"
            ) from exc
        if not isinstance(existing, dict):
            # A retained spec that is valid JSON but not an object loses the
            # job-type discriminator: this job could be ANY duplicate family,
            # so skipping it would reopen duplicate admission for it.
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{path.name} spec is not an object; repair or remove that "
                "job before new duplicate-type submissions"
            )
        job_type = existing.get("job_type")
        if not isinstance(job_type, str) or not job_type:
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{path.name} spec has no job_type; repair or remove that "
                "job before new duplicate-type submissions"
            )
        if job_type not in _DUPLICATE_INDEX_JOB_TYPES:
            continue
        # For duplicate families the fingerprint is a mandatory, internally
        # consistent discriminator: admission wrote it, so a missing or
        # placeholder fingerprint on a retained duplicate-family spec is
        # damaged evidence, not absence of duplication.
        fingerprint = existing.get("spec_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{path.name} spec has no spec_fingerprint; repair or remove "
                "that job before new duplicate-type submissions"
            )
        # The stored fingerprint is retained DATA, not authority: admission
        # stamped it from this same content, so the rebuild re-derives it and
        # refuses a mismatch. Trusting a stored-but-wrong value would re-key
        # the index so the next legitimate retry misses its own entry and
        # launches a duplicate worker.
        recomputed = _fingerprint(
            {key: value for key, value in existing.items() if key != "spec_fingerprint"}
        )
        if fingerprint != recomputed:
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{path.name} spec_fingerprint disagrees with its retained "
                "content; repair or remove that job before new duplicate-type "
                "submissions"
            )
        entries[fingerprint] = path.name
    atomic_write_json(root / _DUPLICATE_INDEX_NAME, {"entries": entries})
    return entries


def _has_retained_job_evidence(root: Path) -> bool:
    """Whether any retained job directory carries spec evidence at all."""
    if not root.is_dir():
        return False
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("job-") and (path / "spec.json").is_file():
            return True
    return False


def _load_duplicate_index(root: Path) -> dict[str, str]:
    """Load the duplicate index, rebuilding it from evidence when unusable.

    A readable but empty index over retained job evidence is index loss, not
    proof that nothing was admitted: reconcile from the evidence itself
    rather than letting a second launch through.
    """
    try:
        payload = read_json(root / _DUPLICATE_INDEX_NAME)
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if isinstance(entries, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in entries.items()
        ):
            if not entries and _has_retained_job_evidence(root):
                return _rebuild_duplicate_index(root)
            return dict(entries)
    except OSError, ValueError, RuntimeError:
        pass
    return _rebuild_duplicate_index(root)


def _append_duplicate_index_entry(root: Path, fingerprint: str, job_id: str) -> None:
    """Record one admitted job; callers hold the submit lock."""
    entries = _load_duplicate_index(root)
    entries[fingerprint] = job_id
    atomic_write_json(root / _DUPLICATE_INDEX_NAME, {"entries": entries})


def public_request_fingerprint(spec: Mapping[str, Any]) -> str:
    """Fingerprint the FULL public request, including attached backend fields.

    Shared by the attached submission path and the replay resolvers so a
    caller-side identity computed before the session handoff matches the
    binding the submission writes.
    """
    import json as _json

    return hashlib.sha256(
        _json.dumps(dict(spec), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


_REQUEST_BINDING_DIR = "request-bindings"


#: Job families whose engine admission deduplicates identical specs into one
#: durable job. The Tasks bridge uses this to know whether a matching spec
#: fingerprint guarantees job-identity reuse (and thus mapping reuse) or
#: whether a new submission always creates a new job that needs reserved
#: mapping capacity.
ENGINE_DUPLICATE_JOB_TYPES = _DUPLICATE_INDEX_JOB_TYPES
_REQUEST_BINDING_SCHEMA = "comsol_mcp.request_binding"
_PENDING_ATTACHED_FINGERPRINT = "pending-attached"


def _validate_request_id(value: object) -> str:
    """Accept a caller request identity: bounded, ASCII, never a path."""
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or not value.isascii():
        raise ValueError("request_id must be a non-empty ASCII string of at most 128 characters")
    return value


def _request_binding_path(root: Path, request_id: str) -> Path:
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return root / _REQUEST_BINDING_DIR / f"{digest}.json"


_LAUNCH_MARKER_NAME = "launch-in-progress.json"


def _write_launch_marker(store: Any, job_id: str) -> None:
    """Durably record that this process owns an in-flight worker launch.

    A job published without a bound worker identity is ambiguous evidence:
    either the launcher crashed, or the launcher is alive and still inside
    the launch call. The marker carries the owning process identity so a
    concurrent retry or resume can tell those apart instead of treating a
    live dispatch as an interruptible orphan.
    """
    import os as _os

    marker = {
        "schema_name": "comsol_mcp.launch_marker",
        "schema_version": "1.0.0",
        "pid": _os.getpid(),
        "process_create_time": None,
        "started_at_epoch": time.time(),
    }
    try:
        import psutil

        marker["process_create_time"] = psutil.Process(_os.getpid()).create_time()
    except Exception:  # noqa: S110 - identity degrades to pid-only liveness
        pass
    atomic_write_json(store.job_dir(job_id) / _LAUNCH_MARKER_NAME, marker)


def _clear_launch_marker(store: Any, job_id: str) -> None:
    try:
        (store.job_dir(job_id) / _LAUNCH_MARKER_NAME).unlink()
    except OSError:
        pass


def _authoritative_in_flight(root: Path, store: Any, job_id: str) -> bool:
    """Re-check in-flight status under the submit lock.

    The submitter writes the launch marker while still holding the submit
    lock and releases only after the marker is durable, so a resolver that
    acquires the lock and still sees no marker holds authoritative proof
    the dispatcher cannot be mid-publication: the job is a true orphan.
    """
    try:
        with JobLock(root / ".submit.lock", timeout=30.0):
            return _launch_marker_alive(store, job_id)
    except Exception:  # noqa: S110 - lock contention is uncertainty, not liveness
        # Contention means a submitter is active for this root: treat the
        # dispatch as in flight rather than risking a duplicate launch.
        return True


def _launch_marker_alive(store: Any, job_id: str) -> bool:
    """Whether a live process still owns the in-flight launch for this job."""
    try:
        marker = read_json(store.job_dir(job_id) / _LAUNCH_MARKER_NAME)
    except OSError, ValueError, RuntimeError:
        return False
    if not isinstance(marker, dict):
        return False
    pid = marker.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import psutil

        process = psutil.Process(pid)
        created = marker.get("process_create_time")
        if isinstance(created, (int, float)) and not isinstance(created, bool):
            if abs(process.create_time() - float(created)) > 1.0:
                return False  # the pid was recycled by a different process
        return True
    except psutil.Error:
        return False
    except Exception:  # noqa: S110 - no psutil evidence means not provable alive
        return False


def _refuse_removed_job(job_type: object) -> None:
    if isinstance(job_type, str) and job_type in REMOVED_RESEARCH_JOB_TYPES:
        raise ValueError("Research job type removed in 0.8.0; use 0.7.8 for legacy execution")


def _verify_source_identities(spec: dict[str, Any]) -> None:
    if spec["job_type"] in {"test_sequence", "surrogate_training"}:
        return
    _refuse_removed_job(spec.get("job_type"))
    sources = [spec]
    for source in sources:
        digest = hashlib.sha256()
        with Path(source["source_model_path"]).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != source["source_model_sha256"]:
            raise ValueError("Refusing resume because immutable source SHA-256 changed")


_DETACHED_PROCESS_LOCK = threading.Lock()
_DETACHED_PROCESS_WAKE = threading.Event()
_DETACHED_PROCESSES: set[_PollableProcess] = set()


class JobLaunchError(RuntimeError):
    """Bind a failed worker launch to the durable job that records its state."""

    def __init__(
        self,
        job_id: str,
        cause: Exception,
        *,
        state_record_error: Exception | None = None,
    ):
        super().__init__(str(cause))
        self.job_id = job_id
        self.cause_type = type(cause).__name__
        self.state_record_error = (
            None
            if state_record_error is None
            else f"{type(state_record_error).__name__}: {state_record_error}"
        )


_DETACHED_REAPER_STARTED = False


def _reap_detached_processes_once() -> int:
    """Reap completed detached children without waiting for active jobs."""
    with _DETACHED_PROCESS_LOCK:
        completed = []
        for process in _DETACHED_PROCESSES:
            try:
                if process.poll() is not None:
                    completed.append(process)
            except Exception:
                completed.append(process)
        _DETACHED_PROCESSES.difference_update(completed)
        return len(completed)


def _detached_process_reaper() -> None:
    while True:
        _DETACHED_PROCESS_WAKE.wait()
        _reap_detached_processes_once()
        with _DETACHED_PROCESS_LOCK:
            active = bool(_DETACHED_PROCESSES)
            if not active:
                _DETACHED_PROCESS_WAKE.clear()
        if active:
            time.sleep(0.05)


def _track_detached_process(process: _PollableProcess) -> None:
    """Retain and asynchronously reap a detached child's parent-side handle."""
    global _DETACHED_REAPER_STARTED
    with _DETACHED_PROCESS_LOCK:
        _DETACHED_PROCESSES.add(process)
        if not _DETACHED_REAPER_STARTED:
            threading.Thread(
                target=_detached_process_reaper,
                name="comsol-detached-process-reaper",
                daemon=True,
            ).start()
            _DETACHED_REAPER_STARTED = True
    _DETACHED_PROCESS_WAKE.set()


def _fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_staged_sweep_spec(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Job specification must be an object")
    spec = dict(raw)
    allowed = {
        "job_type",
        "source_model_path",
        "parameter_name",
        "parameter_values",
        "expressions",
        "parameter_unit",
        "study_name",
        "study_step_tag",
        "study_step_property",
        "study_step_unit",
        "study_step_unit_property",
        "physical_bounds",
        "max_retries",
        "continue_on_error",
        "checkpoint_every",
        "cores",
        "version",
        "smoke_points",
        "record_wavelength_controls",
        "strict_wavelength_policy",
        "resource_policy",
        "execution_backend",
    }
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise ValueError(f"Unsupported staged_sweep fields: {unknown}")
    if spec.get("job_type") != "staged_sweep":
        raise ValueError("Production jobs require job_type='staged_sweep'")
    if spec.get("strict_wavelength_policy") is not None:
        from comsol_mcp.strict_wavelength import normalize_policy, requested_metres

        spec["strict_wavelength_policy"] = normalize_policy(spec["strict_wavelength_policy"])
        if spec.get("parameter_name") not in {"wl", "wavelength"}:
            raise ValueError("strict wavelength mode requires a wavelength parameter")
        if not spec.get("study_name") or not spec.get("study_step_tag"):
            raise ValueError("strict wavelength mode requires study and step tags")
        if spec.get("record_wavelength_controls") is False:
            raise ValueError("strict wavelength mode requires recorded controls")
        if (
            spec.get("study_step_property", "plist") != "plist"
            or spec.get("study_step_unit_property", "punit") != "punit"
        ):
            raise ValueError("strict wavelength mode requires plist and punit controls")
        for value in spec.get("parameter_values", []):
            requested_metres(value, spec.get("parameter_unit"))
    required_strings = ("source_model_path", "parameter_name")
    for key in required_strings:
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    source = Path(spec["source_model_path"]).expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() != ".mph":
        raise ValueError("source_model_path must name an existing MPH file")
    values = spec.get("parameter_values")
    if not isinstance(values, list) or not values:
        raise ValueError("parameter_values must be a nonempty list")
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("parameter_values must contain only finite numbers")
    if len({str(value) for value in values}) != len(values):
        raise ValueError("parameter_values must not contain duplicates")
    expressions = spec.get("expressions")
    if (
        not isinstance(expressions, list)
        or not expressions
        or not all(isinstance(item, str) and item.strip() for item in expressions)
    ):
        raise ValueError("expressions must be a nonempty string list")
    if len(set(expressions)) != len(expressions):
        raise ValueError("expressions must not contain duplicates")
    smoke_points = spec.get("smoke_points", 1)
    if isinstance(smoke_points, bool) or not isinstance(smoke_points, int):
        raise ValueError("smoke_points must be an integer")
    if smoke_points not in (1, 2) or smoke_points > len(values):
        raise ValueError("smoke_points must be 1 or 2 and no larger than the sweep")
    for key in ("continue_on_error", "record_wavelength_controls"):
        if key in spec and not isinstance(spec[key], bool):
            raise ValueError(f"{key} must be a boolean")
    for key in ("max_retries",):
        value = spec.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
    checkpoint_every = spec.get("checkpoint_every", 1)
    if (
        isinstance(checkpoint_every, bool)
        or not isinstance(checkpoint_every, int)
        or checkpoint_every < 1
    ):
        raise ValueError("checkpoint_every must be a positive integer")
    cores = spec.get("cores")
    if cores is not None and (isinstance(cores, bool) or not isinstance(cores, int) or cores < 1):
        raise ValueError("cores must be a positive integer")
    for key in (
        "parameter_unit",
        "study_name",
        "study_step_tag",
        "study_step_property",
        "study_step_unit",
        "study_step_unit_property",
        "version",
    ):
        if key in spec and spec[key] is not None and not isinstance(spec[key], str):
            raise ValueError(f"{key} must be a string when provided")
    bounds = spec.get("physical_bounds")
    if bounds is not None:
        if not isinstance(bounds, dict):
            raise ValueError("physical_bounds must be an expression-to-[minimum, maximum] object")
        for expression, limits in bounds.items():
            if (
                expression not in expressions
                or not isinstance(limits, (list, tuple))
                or len(limits) != 2
            ):
                raise ValueError(
                    "physical_bounds keys must be requested expressions with two limits"
                )
            if any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in limits
            ):
                raise ValueError("physical_bounds limits must be finite numbers")
            if float(limits[0]) > float(limits[1]):
                raise ValueError("physical_bounds minimum must not exceed maximum")
    if "resource_policy" in spec:
        normalized_policy = normalize_resource_policy(spec["resource_policy"])
        if normalized_policy is None:
            spec.pop("resource_policy")
        else:
            spec["resource_policy"] = normalized_policy
    spec["source_model_path"] = str(source)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    spec["source_model_sha256"] = digest.hexdigest()
    if "execution_backend" in spec:
        spec["execution_backend"] = normalize_attached_execution_backend(spec["execution_backend"])
    spec["smoke_points"] = smoke_points
    spec["schema_version"] = JOB_SCHEMA_VERSION
    spec["spec_fingerprint"] = _fingerprint(
        {k: v for k, v in spec.items() if k != "spec_fingerprint"}
    )
    return spec


def _validate_test_spec(raw: dict[str, Any]) -> dict[str, Any]:
    spec = dict(raw)
    if spec.get("job_type") != "test_sequence":
        raise ValueError("Injected test manager accepts only job_type='test_sequence'")
    delays = spec.get("delays", [0.05])
    if (
        not isinstance(delays, list)
        or not delays
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            or float(value) > 30
            for value in delays
        )
    ):
        raise ValueError("test_sequence delays must be finite values between 0 and 30 seconds")
    spec = {"job_type": "test_sequence", "delays": [float(value) for value in delays]}
    spec["schema_version"] = JOB_SCHEMA_VERSION
    spec["spec_fingerprint"] = _fingerprint(spec)
    return spec


def _worker_module(job_type: str) -> str:
    modules = {
        "test_sequence": "comsol_mcp.jobs.sequence_worker",
        "staged_sweep": "comsol_mcp.jobs.worker",
        "validation_matrix": "comsol_mcp.jobs.validation_worker",
        "thermo_optomechanical_replay": "comsol_mcp.jobs.thermo_optomechanical_replay_worker",
        "adjoint_optimization": "comsol_mcp.jobs.adjoint_optimization_worker",
        "surrogate_training": "comsol_mcp.jobs.surrogate_training_worker",
    }
    try:
        return modules[job_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported durable job type: {job_type}") from exc


def _point_count(spec: dict[str, Any]) -> int:
    if spec["job_type"] == "test_sequence":
        return len(spec["delays"])
    if spec["job_type"] == "validation_matrix":
        return len(spec["points"])
    if spec["job_type"] == "thermo_optomechanical_replay":
        return int(spec["declared_stage_count"])
    if spec["job_type"] == "adjoint_optimization":
        return int(spec["optimizer"]["budget"]["max_iterations"])
    if spec["job_type"] == "surrogate_training":
        return int(spec["maximum_epochs"])
    return len(spec["parameter_values"])


class JobManager:
    """Persist and reconcile jobs without importing or starting COMSOL."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        allow_test_jobs: bool = False,
        preflight: Callable[..., dict[str, Any]] | None = None,
        cancel_grace_seconds: float = 10.0,
        cancel_terminate_seconds: float = 5.0,
        reconcile_on_start: bool = True,
    ):
        self.store = JobStore(root)
        self.allow_test_jobs = bool(allow_test_jobs)
        self._preflight = preflight
        self.cancel_grace_seconds = float(cancel_grace_seconds)
        self.cancel_terminate_seconds = float(cancel_terminate_seconds)
        if reconcile_on_start:
            self.reconcile_cancellations()

    def submit(
        self,
        raw_spec: dict[str, Any],
        *,
        request_id: str | None = None,
        source_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        job_type = raw_spec.get("job_type") if isinstance(raw_spec, dict) else None
        _refuse_removed_job(job_type)
        if request_id is not None:
            request_id = _validate_request_id(request_id)
        if job_type == "test_sequence":
            if not self.allow_test_jobs:
                raise ValueError("test_sequence jobs are disabled")
            spec = _validate_test_spec(raw_spec)
        elif job_type == "validation_matrix":
            spec = normalize_validation_matrix_spec(raw_spec)
        elif job_type == "thermo_optomechanical_replay":
            spec = normalize_thermo_optomechanical_replay_spec(raw_spec)
        elif job_type == "adjoint_optimization":
            spec = expand_adjoint_optimization_manifest(raw_spec)
        elif job_type == "surrogate_training":
            spec = normalize_surrogate_training_spec(raw_spec)
        else:
            spec = validate_staged_sweep_spec(raw_spec)
        worker_module = _worker_module(spec["job_type"])
        duplicate_job_types = {
            "validation_matrix",
            "thermo_optomechanical_replay",
            "adjoint_optimization",
            "surrogate_training",
        }
        # A response-loss retry that carries the same caller request id
        # replays the one bound attempt instead of launching a second job.
        early_binding = self._resolve_request_binding(request_id, spec)
        if isinstance(early_binding, dict):
            return early_binding
        if spec["job_type"] in duplicate_job_types:
            with JobLock(self.store.root / ".submit.lock"):
                duplicate = self._find_exact_duplicate(spec["job_type"], spec["spec_fingerprint"])
                if duplicate is not None:
                    existing_state = self.store.read_state(duplicate)
                    if request_id is not None:
                        # Revalidate the binding under the lock: a concurrent
                        # request with the same id may have bound a different
                        # spec or job already, and that conflict must refuse
                        # here rather than silently overwrite the binding.
                        binding = self._bind_request_under_lock(request_id, spec)
                        if isinstance(binding, dict):
                            return binding
                        self._write_request_record(
                            request_id,
                            spec["spec_fingerprint"],
                            duplicate,
                            "dispatched",
                            source_fingerprint=source_fingerprint,
                        )
                    return {
                        "success": True,
                        "job_id": duplicate,
                        "status": existing_state["status"],
                        "duplicate": True,
                        "action": (
                            "resume_existing"
                            if existing_state["status"] in {"failed", "interrupted", "cancelled"}
                            else "observe_existing"
                        ),
                    }
        if spec["job_type"] != "test_sequence":
            preflight = self._run_preflight(spec)
            if not preflight.get("ready", preflight.get("success", False)):
                raise RuntimeError(
                    f"Job preflight failed: {preflight.get('blockers') or preflight}"
                )
        now = time.time()
        total_points = _point_count(spec)
        capacity = resource_journal_capacity_verdict(total_points)
        if not capacity["fits"]:
            # Refuse before job creation or launch: even the single-attempt
            # lower bound cannot fit the durable resource journal, so the
            # sweep could only die mid-run against the entry limit.
            raise ValueError(
                "resource journal cannot hold the admitted sweep: "
                f"{capacity['required_entries_lower_bound']} entries needed "
                f"for {total_points} points against {capacity['max_entries']}"
            )
        state = {
            "schema_version": JOB_SCHEMA_VERSION,
            "status": "submitted",
            "attempt": 1,
            "created_at_epoch": now,
            "updated_at_epoch": now,
            "worker_pid": None,
            "worker_process_create_time": None,
            "worker_command_signature": None,
            "progress": {"completed": 0, "total": total_points},
            "last_error": None,
        }
        if spec["job_type"] in duplicate_job_types:
            with JobLock(self.store.root / ".submit.lock"):
                duplicate = self._find_exact_duplicate(spec["job_type"], spec["spec_fingerprint"])
                if duplicate is not None:
                    existing_state = self.store.read_state(duplicate)
                    if request_id is not None:
                        # Revalidate the binding under the lock: a concurrent
                        # request with the same id may have bound a different
                        # spec or job already, and that conflict must refuse
                        # here rather than silently overwrite the binding.
                        binding = self._bind_request_under_lock(request_id, spec)
                        if isinstance(binding, dict):
                            return binding
                        self._write_request_record(
                            request_id,
                            spec["spec_fingerprint"],
                            duplicate,
                            "dispatched",
                            source_fingerprint=source_fingerprint,
                        )
                    return {
                        "success": True,
                        "job_id": duplicate,
                        "status": existing_state["status"],
                        "duplicate": True,
                        "action": (
                            "resume_existing"
                            if existing_state["status"] in {"failed", "interrupted", "cancelled"}
                            else "observe_existing"
                        ),
                    }
                bound_job_id = None
                if request_id is not None:
                    binding = self._bind_request_under_lock(request_id, spec)
                    if isinstance(binding, dict):
                        return binding
                    bound_job_id = binding
                job_id = self.store.create(spec, state, job_id=bound_job_id)
                # Arm the launch marker while the submit lock is still held:
                # once the dispatched record is visible, any resolver that
                # re-checks under this lock will see the marker and treat the
                # dispatch as in flight instead of an interruptible orphan.
                _write_launch_marker(self.store, job_id)
                if request_id is not None:
                    self._write_request_record(
                        request_id,
                        spec["spec_fingerprint"],
                        job_id,
                        "dispatched",
                        source_fingerprint=source_fingerprint,
                    )
                _append_duplicate_index_entry(self.store.root, spec["spec_fingerprint"], job_id)
        elif request_id is not None:
            # Bind the caller request durably before the first side effect:
            # an interrupted submit leaves an UNKNOWN record whose retry
            # reuses the same pre-allocated job id, so a lost response can
            # never materialize as a second launched job.
            with JobLock(self.store.root / ".submit.lock"):
                binding = self._bind_request_under_lock(request_id, spec)
                if isinstance(binding, dict):
                    return binding
                job_id = self.store.create(spec, state, job_id=binding)
                _write_launch_marker(self.store, job_id)
                self._write_request_record(
                    request_id,
                    spec["spec_fingerprint"],
                    job_id,
                    "dispatched",
                    source_fingerprint=source_fingerprint,
                )
        else:
            with JobLock(self.store.root / ".submit.lock"):
                job_id = self.store.create(spec, state)
                _write_launch_marker(self.store, job_id)
        try:
            self.store.append_event(
                job_id,
                "submitted",
                {"spec_fingerprint": spec["spec_fingerprint"]},
            )
            try:
                identity = self._launch_worker(job_id, worker_module)
                self.store.bind_worker_identity(job_id, identity)
            finally:
                _clear_launch_marker(self.store, job_id)
        except Exception as exc:
            state_record_error = None
            try:
                self.store.update_state(
                    job_id,
                    "failed",
                    patch={
                        "last_error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    },
                    event="launch_failed",
                )
            except Exception as record_exc:
                state_record_error = record_exc
            raise JobLaunchError(
                job_id,
                exc,
                state_record_error=state_record_error,
            ) from exc
        try:
            self._arm_native_wall_watchdog_if_required(
                job_id,
                spec,
                attempt=1,
                worker_identity=identity,
            )
        except Exception as exc:
            cancellation = self.cancel(job_id, expected_attempt=1)
            raise JobLaunchError(
                job_id,
                RuntimeError(
                    "Licensed robust wall watchdog could not be armed; "
                    f"exact-attempt cancellation result: {cancellation}"
                ),
            ) from exc
        return {"success": True, "job_id": job_id, "status": "submitted"}

    def _read_request_record(self, request_id: str) -> dict[str, Any] | None:
        path = _request_binding_path(self.store.root, request_id)
        if not path.is_file():
            return None
        try:
            payload = read_json(path)
        except (OSError, ValueError, RuntimeError) as exc:
            # A damaged record is uncertainty, not absence: treating it as
            # absent would let one uncertain retry launch a second job.
            raise ValueError(
                f"request binding record is unreadable ({type(exc).__name__}); "
                "repair or remove it before resubmitting this request_id"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                "request binding record is malformed; repair or remove it "
                "before resubmitting this request_id"
            )
        return payload

    def _write_request_record(
        self,
        request_id: str,
        fingerprint: str,
        job_id: str,
        state: str,
        *,
        source_fingerprint: str | None = None,
    ) -> None:
        path = _request_binding_path(self.store.root, request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_name": _REQUEST_BINDING_SCHEMA,
            "schema_version": "1.0.0",
            "job_id": job_id,
            "spec_fingerprint": fingerprint,
            "state": state,
            "updated_at_epoch": time.time(),
        }
        # Attached submissions bind a caller-side (pre-handoff) fingerprint
        # so a lost-response retry can resolve its authority BEFORE the
        # session handoff repeats its side effects.
        if source_fingerprint is not None:
            record["source_fingerprint"] = source_fingerprint
        atomic_write_json(path, record)

    def _resolve_request_binding(
        self, request_id: str | None, spec: dict[str, Any]
    ) -> dict[str, Any] | str | None:
        """Fast-path replay for a bound request, outside the submit lock.

        Returns a replay result dict, a pre-allocated job id for a record
        whose dispatch never published a job (UNKNOWN), or None when the
        request is unbound. The authoritative re-check runs under the submit
        lock at creation time, so an unlocked race only falls through.
        """
        if request_id is None:
            return None
        record = self._read_request_record(request_id)
        if record is None:
            return None
        return self._request_record_outcome(record, spec)

    def _request_job_replay(self, job_id: str) -> dict[str, Any] | str:
        """Replay outcome for a bound job id, honoring dispatch uncertainty."""
        if self.store.job_dir(job_id).exists():
            existing_state = self.store.read_state(job_id)
            if (
                existing_state.get("status") == "submitted"
                and existing_state.get("worker_pid") is None
                and _launch_marker_alive(self.store, job_id)
            ):
                # The publishing launcher still owns an in-flight dispatch:
                # this is a live attempt, not an orphan. Replay the truthful
                # current state; never mark it interrupted and never permit
                # a second launch through resume.
                return {
                    "success": True,
                    "job_id": job_id,
                    "status": existing_state["status"],
                    "duplicate": True,
                    "action": "request_replay_in_flight",
                }
            if (
                existing_state.get("status") == "submitted"
                and existing_state.get("worker_pid") is None
                and not _launch_marker_alive(self.store, job_id)
            ):
                # Ambiguous: reread the CURRENT state and marker under the
                # submit lock before any transition. The launcher binds the
                # worker identity only after arming the marker under this
                # same lock, so by the time the lock is free the state is
                # authoritative: a bound worker means a live completed
                # dispatch (replay it), an armed marker means in flight, and
                # only a state still submitted with no worker identity and
                # no marker is a true orphan whose transition happens here,
                # inside the lock.
                try:
                    with JobLock(self.store.root / ".submit.lock", timeout=30.0):
                        locked_state = self.store.read_state(job_id)
                        if (
                            locked_state.get("status") != "submitted"
                            or locked_state.get("worker_pid") is not None
                        ):
                            existing_state = locked_state
                        elif _launch_marker_alive(self.store, job_id):
                            return {
                                "success": True,
                                "job_id": job_id,
                                "status": locked_state["status"],
                                "duplicate": True,
                                "action": "request_replay_in_flight",
                            }
                        else:
                            return self._transition_orphan_replay(job_id)
                except Exception:  # noqa: S110 - contention is uncertainty
                    # A submitter is active for this root: treat the dispatch
                    # as in flight rather than risking a duplicate launch.
                    return {
                        "success": True,
                        "job_id": job_id,
                        "status": existing_state["status"],
                        "duplicate": True,
                        "action": "request_replay_in_flight",
                    }
            if (
                existing_state.get("status") == "submitted"
                and existing_state.get("worker_pid") is None
                and not _launch_marker_alive(self.store, job_id)
            ):
                # Publication happened but no worker identity was ever bound:
                # the attempt died between publication and launch.
                return self._transition_orphan_replay(job_id)
            return {
                "success": True,
                "job_id": job_id,
                "status": existing_state["status"],
                "duplicate": True,
                "action": "request_replay",
            }
        # A job directory that does not exist leaves the binding's own
        # state to decide: UNKNOWN keeps the pre-allocated id (reusing it
        # holds the retry on one attempt); anything else is uncertainty.
        return job_id

    def _request_record_outcome(
        self, record: dict[str, Any], spec: dict[str, Any]
    ) -> dict[str, Any] | str:
        if record.get("spec_fingerprint") == _PENDING_ATTACHED_FINGERPRINT:
            # A pre-handoff reservation: the caller identity lives in
            # source_fingerprint (compared by the resolvers), and this
            # admission completes the binding under the reserved id.
            job_id = record.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                raise ValueError(
                    "request binding record is incomplete; repair or remove it before resubmitting"
                )
            return job_id
        if record.get("spec_fingerprint") != spec["spec_fingerprint"]:
            raise ValueError(
                "request_id is already durably bound to a different submission; "
                "use a new request_id for an independent submission"
            )
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError(
                "request binding record is incomplete; repair or remove it before resubmitting"
            )
        return self._request_replay_with_record(record, job_id)

    def _transition_orphan_replay(self, job_id: str) -> dict[str, Any]:
        """Mark a proven-orphan dispatch interrupted and return its outcome."""
        self.store.update_state(
            job_id,
            "interrupted",
            patch={
                "last_error": {
                    "type": "PublishedWithoutLaunch",
                    "message": (
                        "The job was published but its worker launch "
                        "never completed; reconcile with job_status or resume."
                    ),
                }
            },
            event="request_replay_orphan_reconciled",
        )
        return {
            "success": False,
            "job_id": job_id,
            "status": "interrupted",
            "duplicate": True,
            "action": "reconcile_orphan_then_resume",
            "error": (
                "the bound job was published but never launched; it is "
                "marked interrupted - resume it or inspect job_status "
                "before retrying this request_id"
            ),
        }

    def _request_replay_with_record(
        self, record: dict[str, Any], job_id: str
    ) -> dict[str, Any] | str:
        outcome = self._request_job_replay(job_id)
        if isinstance(outcome, str) and record.get("state") == "dispatched":
            raise ValueError(
                f"request_id was dispatched to job {job_id!r} whose evidence is "
                "missing; reconcile that job before resubmitting"
            )
        return outcome

    def _attached_guard_path(self, request_id: str) -> Path:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self.store.root / "attached-in-flight" / f"{digest}.json"

    def arm_attached_guard(self, request_id: str) -> None:
        """Mark this process as actively running the attached flow."""
        import psutil

        path = self._attached_guard_path(request_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {
                "pid": os.getpid(),
                "process_create_time": psutil.Process(os.getpid()).create_time(),
                "armed_at_epoch": time.time(),
            },
        )

    def disarm_attached_guard(self, request_id: str) -> None:
        try:
            self._attached_guard_path(request_id).unlink()
        except OSError:
            pass

    def _attached_flow_in_flight(self, request_id: str) -> bool:
        """Whether a live process is mid attached-flow for this request."""
        try:
            guard = read_json(self._attached_guard_path(request_id))
        except OSError, ValueError, RuntimeError:
            return False
        if not isinstance(guard, dict):
            return False
        pid = guard.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            import psutil

            process = psutil.Process(pid)
            created = guard.get("process_create_time")
            if isinstance(created, (int, float)) and not isinstance(created, bool):
                if abs(process.create_time() - float(created)) > 1.0:
                    return False
            return True
        except Exception:  # noqa: S110 - no provable liveness means recoverable
            return False

    def reserve_attached_request(
        self, request_id: str, source_fingerprint: str
    ) -> dict[str, Any] | None:
        """Durably reserve a request before attached handoff side effects.

        Written BEFORE the session handoff runs: an in-flight identical
        retry observes the reservation instead of re-entering the
        lifecycle, and a changed identity refuses. The reservation is
        completed by the attached admission's dispatched binding (same
        record, same fingerprints), so an interruption between handoff and
        publication leaves an UNKNOWN record whose retry reconciles
        exactly like every other UNKNOWN dispatch.

        The attached-flow guard is armed INSIDE this lock on both paths
        that continue into the handoff (fresh reservation and recovery of
        a dead reserver), so no reserve-to-guard window exists in which a
        concurrent retry could mistake a live reservation for a dead one.
        """
        request_id = _validate_request_id(request_id)
        with JobLock(self.store.root / ".submit.lock"):
            record = self._read_request_record(request_id)
            if record is not None:
                if record.get("source_fingerprint") != source_fingerprint:
                    raise ValueError(
                        "request_id is already durably bound to a different "
                        "submission; use a new request_id for an independent "
                        "submission"
                    )
                job_id = str(record.get("job_id") or "")
                if job_id and self.store.job_dir(job_id).exists():
                    return self._request_replay_with_record(record, job_id)
                if self._attached_flow_in_flight(request_id):
                    return self._reservation_in_flight(job_id)
                # The reserving flow is no longer active (it failed or died
                # between handoff and publication): recovery proceeds, and
                # the admission completes the reservation under its id.
                # Arming under the lock atomically claims that recovery.
                self.arm_attached_guard(request_id)
                return None
            job_id = f"job-{uuid.uuid4().hex}"
            self._write_request_record(
                request_id,
                "pending-attached",
                job_id,
                "unknown",
                source_fingerprint=source_fingerprint,
            )
            self.arm_attached_guard(request_id)
            return None

    @staticmethod
    def _reservation_in_flight(job_id: str) -> dict[str, Any]:
        """Truthful outcome for a reserved-but-unpublished attached request."""
        return {
            "success": False,
            "job_id": job_id,
            "status": "reserved",
            "duplicate": True,
            "action": "request_reservation_in_flight",
            "error": (
                "this request_id holds a durable pre-handoff reservation whose "
                "submission has not published yet; retry after job_status "
                "shows the job, or reconcile if it never appears"
            ),
        }

    def resolve_attached_retry(
        self, request_id: str, source_fingerprint: str
    ) -> dict[str, Any] | None:
        """Pre-handoff replay resolution for an attached retry.

        The caller-side fingerprint is computable before the session handoff;
        a binding whose source fingerprint matches resolves to its replay
        outcome without repeating the handoff side effects. A bound request
        with a different source fingerprint is a conflict and refuses.
        """
        request_id = _validate_request_id(request_id)
        record = self._read_request_record(request_id)
        if record is None:
            return None
        bound_source = record.get("source_fingerprint")
        if bound_source != source_fingerprint:
            raise ValueError(
                "request_id is already durably bound to a different submission; "
                "use a new request_id for an independent submission"
            )
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError(
                "request binding record is incomplete; repair or remove it before resubmitting"
            )
        if self.store.job_dir(job_id).exists():
            outcome = self._request_job_replay(job_id)
            return outcome if isinstance(outcome, dict) else None
        return self._reservation_in_flight(job_id)

    def record_attached_handoff(self, request_id: str, handoff: Mapping[str, Any]) -> None:
        """Durably preserve a successful attached handoff receipt (C081F-02).

        Written after the lifecycle handoff succeeds but BEFORE admission
        publishes the job: if this flow dies in that window, the retry's
        recovery completes the interrupted admission under the reserved id
        without repeating the handoff (the detached lifecycle would refuse
        with model_not_locked). No-op once the bound job is published.
        """
        request_id = _validate_request_id(request_id)
        if not isinstance(handoff, Mapping):
            raise ValueError("attached handoff receipt must be a mapping")
        with JobLock(self.store.root / ".submit.lock"):
            record = self._read_request_record(request_id)
            if record is None:
                raise ValueError("cannot record an attached handoff for an unreserved request_id")
            job_id = record.get("job_id")
            if isinstance(job_id, str) and job_id and self.store.job_dir(job_id).exists():
                return
            updated = dict(record)
            updated["attached_handoff"] = dict(handoff)
            updated["updated_at_epoch"] = time.time()
            atomic_write_json(_request_binding_path(self.store.root, request_id), updated)

    def attached_handoff_receipt(self, request_id: str) -> dict[str, Any] | None:
        """Read the durably recorded successful handoff receipt, if any.

        Only meaningful while the bound job is unpublished: once the job
        directory exists the binding replays through its dispatch outcome
        instead of reconciling a handoff.
        """
        record = self._read_request_record(_validate_request_id(request_id))
        if record is None:
            return None
        job_id = record.get("job_id")
        if isinstance(job_id, str) and job_id and self.store.job_dir(job_id).exists():
            return None
        receipt = record.get("attached_handoff")
        return dict(receipt) if isinstance(receipt, dict) else None

    def resolve_request_replay(
        self, raw_spec: object, request_id: str | None
    ) -> dict[str, Any] | None:
        """Resolve a durably bound request WITHOUT dispatching (C081D-04).

        Accepts either a flat engine spec or the public {spec, request_id}
        envelope. Normalizes the spec to its fingerprint, reads the binding,
        and returns the replay outcome (dict) when bound, None when the
        request is unbound; conflicting bindings and damaged records refuse
        exactly like a dispatch would.
        """
        if request_id is None:
            return None
        request_id = _validate_request_id(request_id)
        spec = raw_spec
        if isinstance(spec, Mapping) and "job_type" not in spec:
            inner = spec.get("spec")
            if isinstance(inner, Mapping):
                spec = inner
        if not isinstance(spec, Mapping):
            return None
        job_type = spec.get("job_type")
        _refuse_removed_job(job_type)
        backend = spec.get("execution_backend")
        if job_type == "staged_sweep" and isinstance(backend, Mapping) and "spec" not in spec:
            if "expected_lock_sha256" in backend or "expected_revision_sha256" in backend:
                # A schema-valid PUBLIC attached request: its identity is the
                # full public request, resolved through the pre-handoff
                # contract - never through the expanded internal backend the
                # submission itself would normalize.
                return self.resolve_attached_retry(request_id, public_request_fingerprint(spec))
        if job_type == "validation_matrix":
            normalized = normalize_validation_matrix_spec(dict(spec))
        elif job_type == "thermo_optomechanical_replay":
            normalized = normalize_thermo_optomechanical_replay_spec(dict(spec))
        elif job_type == "adjoint_optimization":
            normalized = expand_adjoint_optimization_manifest(dict(spec))
        elif job_type == "surrogate_training":
            normalized = normalize_surrogate_training_spec(dict(spec))
        else:
            normalized = validate_staged_sweep_spec(dict(spec))
        record = self._read_request_record(request_id)
        if record is None:
            return None
        if record.get("spec_fingerprint") != normalized["spec_fingerprint"]:
            raise ValueError(
                "request_id is already durably bound to a different submission; "
                "use a new request_id for an independent submission"
            )
        job_id = record.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError(
                "request binding record is incomplete; repair or remove it before resubmitting"
            )
        outcome = self._request_replay_with_record(record, job_id)
        return outcome if isinstance(outcome, dict) else None

    def _bind_request_under_lock(
        self, request_id: str, spec: dict[str, Any]
    ) -> dict[str, Any] | str:
        """Idempotently bind a request under the submit lock.

        Returns the replay result when the bound job exists, otherwise the
        job id to create under (freshly pre-allocated and recorded as
        UNKNOWN for an unbound request, or the recorded id of an UNKNOWN
        attempt).
        """
        record = self._read_request_record(request_id)
        if record is not None:
            return self._request_record_outcome(record, spec)
        job_id = f"job-{uuid.uuid4().hex}"
        self._write_request_record(request_id, spec["spec_fingerprint"], job_id, "unknown")
        return job_id

    def _find_exact_duplicate(self, job_type: str, spec_fingerprint: str) -> str | None:
        entries = _load_duplicate_index(self.store.root)
        job_id = entries.get(spec_fingerprint)
        if job_id is None:
            # A miss is not proof of absence: the disposable index may have
            # lost individual entries. Rebuild from retained evidence (which
            # fails closed on unreadable specs) and let the evidence decide
            # before a second launch can be admitted.
            rebuilt = _rebuild_duplicate_index(self.store.root)
            job_id = rebuilt.get(spec_fingerprint)
            if job_id is None:
                return None
        if not (self.store.root / job_id).is_dir():
            # The whole job directory is gone: retention legitimately removes
            # jobs, and the index entry is dead history. Drop it.
            entries.pop(spec_fingerprint, None)
            atomic_write_json(self.store.root / _DUPLICATE_INDEX_NAME, {"entries": entries})
            return None
        try:
            existing = read_json(self.store.root / job_id / "spec.json")
        except (OSError, ValueError, RuntimeError) as exc:
            # The directory survives but its evidence is unreadable: that is
            # uncertain retained evidence, not proof the entry is dead
            # history.
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{job_id} spec is unreadable ({type(exc).__name__}); repair "
                "or remove that job before new duplicate-type submissions"
            ) from exc
        if not isinstance(existing, dict) or not isinstance(existing.get("job_type"), str):
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{job_id} spec lost its object shape or job_type; repair or "
                "remove that job before new duplicate-type submissions"
            )
        if existing.get("job_type") != job_type:
            # The entry maps a fingerprint to a job of a different family:
            # dead history, drop it.
            entries.pop(spec_fingerprint, None)
            atomic_write_json(self.store.root / _DUPLICATE_INDEX_NAME, {"entries": entries})
            return None
        stored = existing.get("spec_fingerprint")
        if stored == spec_fingerprint:
            return job_id
        if not isinstance(stored, str) or not stored:
            raise ValueError(
                f"duplicate index cannot be reconciled: retained job "
                f"{job_id} spec has no spec_fingerprint; repair or remove "
                "that job before new duplicate-type submissions"
            )
        # Same family, but the retained fingerprint disagrees with the index:
        # uncertain evidence on BOTH mappings - refuse instead of silently
        # dropping a possibly-live duplicate.
        raise ValueError(
            f"duplicate index cannot be reconciled: retained job "
            f"{job_id} spec fingerprint disagrees with the index; repair or "
            "remove that job before new duplicate-type submissions"
        )

    def _run_preflight(self, spec: dict[str, Any]) -> dict[str, Any]:
        if spec.get("job_type") == "surrogate_training":
            # A surrogate-training submission is bound to dataset and split
            # identities rather than a single MPH source, so it must not be
            # forced through the model-path preflight.  A caller-supplied
            # preflight still takes precedence when one is installed.
            if self._preflight is not None:
                return self._preflight(
                    model_path=None,
                    output_path=str(self.store.root / "probe"),
                    requested_version=None,
                    execution_backend=None,
                )
            return self._preflight_surrogate_training(spec)
        _refuse_removed_job(spec.get("job_type"))
        model_path = spec["source_model_path"]
        requested_version = spec.get("version")
        execution_backend = spec.get("execution_backend")
        if self._preflight is not None:
            return self._preflight(
                model_path=model_path,
                output_path=str(self.store.root / "probe"),
                requested_version=requested_version,
                execution_backend=execution_backend,
            )
        if execution_backend is not None:
            from comsol_mcp.jobs.attached_runtime import normalize_attached_execution_target
            from comsol_mcp.shared_session.lifecycle import SharedSessionManager
            from comsol_mcp.shared_session.preflight import classify_shared_server_preflight
            from comsol_mcp.shared_session.process_probe import (
                collect_shared_preflight_snapshot,
            )

            target = normalize_attached_execution_target(execution_backend)
            endpoint = {
                "host": target.server.endpoint.host,
                "port": target.server.endpoint.port,
            }
            first = collect_shared_preflight_snapshot()
            second = collect_shared_preflight_snapshot()
            preflight = classify_shared_server_preflight(
                endpoint=endpoint,
                first_probe=first,
                second_probe=second,
            )
            if not preflight.get("success"):
                return {
                    **preflight,
                    "ready": False,
                    "execution_backend": "attached_shared_server",
                }
            try:
                observed = SharedSessionManager._server_identity_from_snapshot(
                    target.server.endpoint, second
                )
            except Exception as exc:
                return {
                    "success": False,
                    "ready": False,
                    "state": "attached_server_identity_unavailable",
                    "blockers": [f"{type(exc).__name__}: {exc}"],
                    "execution_backend": "attached_shared_server",
                }
            if observed.identity_sha256 != target.server.identity_sha256:
                return {
                    "success": False,
                    "ready": False,
                    "state": "attached_server_identity_changed",
                    "blockers": ["attached_server_identity_changed"],
                    "expected_server_identity_sha256": (target.server.identity_sha256),
                    "observed_server_identity_sha256": observed.identity_sha256,
                    "execution_backend": "attached_shared_server",
                }
            return {
                "success": True,
                "ready": True,
                "state": "ready_for_attached_worker",
                "execution_backend": "attached_shared_server",
                "server_identity_sha256": observed.identity_sha256,
                "preflight": preflight,
                "mph_imported": False,
                "client_constructed": False,
            }
        from comsol_mcp.tools.ownership import SolverOwnership
        from comsol_mcp.tools.session import session_manager

        return SolverOwnership(self.store.root.parent).preflight(
            session_state=session_manager.get_status(),
            model_path=model_path,
            output_path=str(self.store.root / "probe"),
            requested_version=requested_version,
        )

    def _preflight_surrogate_training(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Prove a surrogate-training submission is self-consistent and solver-free.

        A surrogate submission is bound to dataset, split, schema, transform, and
        architecture identities rather than a single MPH source.  This preflight
        therefore checks identity binding and the declared budgets, and records
        explicitly that no COMSOL client, lease, or solver is involved.
        """
        blockers: list[str] = []
        dataset = spec.get("dataset_manifest") or {}
        split_plan = spec.get("split_plan") or {}
        declared = {
            "dataset_manifest_sha256": dataset.get("manifest_sha256"),
            "split_manifest_sha256": split_plan.get("manifest_sha256"),
            "field_schema_sha256": (spec.get("field_schema") or {}).get("manifest_sha256"),
            "transforms_sha256": (spec.get("transforms") or {}).get("manifest_sha256"),
            "architecture_sha256": spec.get("architecture_sha256"),
        }
        for name, value in declared.items():
            if spec.get(name) != value:
                blockers.append(f"{name}_not_bound")

        # Bind the running package's driver identity at preflight so a resume
        # cannot silently execute against a different training implementation
        # than the one whose identity the spec froze.
        driver_blockers: list[str] = []
        try:
            from comsol_mcp.jobs.surrogate_training import (
                validate_surrogate_training_driver_identity,
            )

            expected_driver = validate_surrogate_training_driver_identity(spec)
        except ValueError:
            driver_blockers.append("driver_identity_not_bound")
            expected_driver = None

        ineligible = {item["row_id"] for item in (dataset.get("ineligible_rows") or [])}
        assignments = split_plan.get("assignments") or []
        holdout = [item for item in assignments if item.get("split") == "scientific_holdout"]
        if not assignments:
            blockers.append("split_plan_has_no_assignments")
        if not holdout:
            blockers.append("scientific_holdout_is_empty")
        if spec.get("maximum_epochs", 0) < 1:
            blockers.append("maximum_epochs_is_not_positive")
        if not spec.get("seeds"):
            blockers.append("seeds_are_empty")
        del ineligible  # retained for future eligibility enforcement
        blockers.extend(driver_blockers)

        return {
            "success": not blockers,
            "ready": not blockers,
            "state": "ready_for_solver_free_surrogate_worker" if not blockers else "blocked",
            "blockers": blockers,
            "solver_free": True,
            "mph_imported": False,
            "client_constructed": False,
            "acquires_solver_lease": False,
            "bound_identities": {**declared, "driver_identity": expected_driver},
        }

    def _launch_worker(self, job_id: str, module: str) -> dict[str, Any]:
        directory = self.store.job_dir(job_id)
        command = [sys.executable, "-m", module, str(self.store.root), job_id]
        flags = 0
        if os.name == "nt":
            flags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        with (directory / "worker.log").open("ab", buffering=0) as log:
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
        _track_detached_process(process)
        expected_signature = hashlib.sha256(
            "\0".join(command).encode("utf-8", errors="replace")
        ).hexdigest()
        deadline = time.monotonic() + 2.0
        while True:
            try:
                observed = _launched_process_identity(process, command)
                if os.name == "nt" or observed["command_signature"] == expected_signature:
                    return observed
            except psutil.NoSuchProcess, psutil.ZombieProcess, OSError:
                pass
            # A fast child can bind and exit before the parent samples /proc.
            # Use only its persisted, exact launched-command identity.
            state = self.store.read_state(job_id)
            if (
                state.get("worker_pid") == process.pid
                and state.get("worker_command_signature") == expected_signature
            ):
                created = state.get("worker_process_create_time")
                if (
                    isinstance(created, (int, float))
                    and not isinstance(created, bool)
                    and math.isfinite(created)
                ):
                    return {
                        "pid": process.pid,
                        "process_create_time": created,
                        "command_signature": expected_signature,
                    }
            if time.monotonic() >= deadline or process.poll() is not None:
                raise RuntimeError(
                    "Detached worker identity could not be verified before launch acknowledgement"
                )
            time.sleep(0.01)

    def _arm_native_wall_watchdog_if_required(
        self,
        job_id: str,
        spec: dict[str, Any],
        *,
        attempt: int,
        worker_identity: dict[str, Any],
    ) -> dict[str, Any] | None:
        if spec.get("synthetic_mode") is not False:
            return None
        from .native_wall_watchdog import native_wall_budget

        budget = native_wall_budget(spec)
        if budget is None:
            return None
        budget_source, budget_seconds = budget
        started_at = float(worker_identity["process_create_time"])
        deadline = started_at + budget_seconds
        directory = self.store.job_dir(job_id)
        artifact = directory / "wall-watchdog.json"
        launching = {
            "schema_name": "comsol_mcp.robust_wall_watchdog",
            "schema_version": "1.0.0",
            "job_id": job_id,
            "attempt": int(attempt),
            "budget_source": budget_source,
            "budget_seconds": budget_seconds,
            "worker_started_at_epoch": started_at,
            "deadline_epoch": deadline,
            "target_worker": worker_identity,
            "status": "launching",
            "updated_at_epoch": time.time(),
        }
        atomic_write_json(artifact, launching)
        command = [
            sys.executable,
            "-m",
            "comsol_mcp.jobs.native_wall_watchdog",
            str(self.store.root),
            job_id,
            str(attempt),
            repr(deadline),
        ]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
        try:
            with (directory / "worker.log").open("ab", buffering=0) as log:
                process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    close_fds=True,
                    creationflags=flags,
                    start_new_session=(os.name != "nt"),
                )
            _track_detached_process(process)
            watchdog_identity = _wait_for_launched_identity(process, command)
        except Exception as exc:
            atomic_write_json(
                artifact,
                {
                    **launching,
                    "status": "launch_failed",
                    "launch_error": {
                        "type": type(exc).__name__,
                        "message": str(exc)[:500],
                    },
                    "updated_at_epoch": time.time(),
                },
            )
            raise
        armed = {
            **launching,
            "status": "armed",
            "watchdog_identity": watchdog_identity,
            "armed_at_epoch": time.time(),
            "updated_at_epoch": time.time(),
        }
        atomic_write_json(artifact, armed)
        self.store.append_event(
            job_id,
            "robust_wall_watchdog_armed",
            {
                "attempt": int(attempt),
                "budget_seconds": budget_seconds,
                "deadline_epoch": deadline,
                "watchdog_pid": watchdog_identity["pid"],
            },
        )
        return armed

    def cancel(self, job_id: str, *, expected_attempt: int | None = None) -> dict[str, Any]:
        _refuse_removed_job(self.store.read_spec(job_id).get("job_type"))
        request = self.store.request_cancel(
            job_id,
            requester_identity=process_identity(os.getpid()),
            expected_attempt=expected_attempt,
        )
        state = request["state"]
        control = request["control"]
        if not request["accepted"]:
            if request["reason"] == "terminal":
                error = "Job was terminal before the cancellation request acquired the job lock"
            elif request["reason"] == "stale_control_attempt":
                error = "Existing cancellation request belongs to a different attempt"
            elif request["reason"] == "attempt_mismatch":
                error = "Job attempt changed before the cancellation request acquired the lock"
            else:
                error = f"Cancellation request refused: {request['reason']}"
            return {
                "success": False,
                "job_id": job_id,
                "status": state["status"],
                "error": error,
            }
        prior_launch_failed = bool(
            isinstance(state.get("cancel"), dict)
            and state["cancel"].get("coordinator_launch_error")
        )
        if not request["idempotent"] or prior_launch_failed:
            try:
                self._launch_cancel_coordinator(job_id, control["request_id"])
            except Exception as exc:
                self.store.update_state(
                    job_id,
                    patch={
                        "cancel": {
                            **state.get("cancel", {}),
                            "coordinator_launch_error": f"{type(exc).__name__}: {exc}",
                        }
                    },
                    event="cancel_coordinator_launch_failed",
                )
                return {
                    "success": False,
                    "job_id": job_id,
                    "status": state["status"],
                    "request_id": control["request_id"],
                    "target_attempt": control["target_attempt"],
                    "idempotent": bool(request["idempotent"]),
                    "error": "Cancellation coordinator could not be started",
                }
            if prior_launch_failed:
                self.store.update_state(
                    job_id,
                    patch={
                        "cancel": {
                            **state["cancel"],
                            "coordinator_launch_error": None,
                        }
                    },
                    event="cancel_coordinator_relaunched",
                )
        return {
            "success": True,
            "job_id": job_id,
            "status": state["status"],
            "request_id": control["request_id"],
            "target_attempt": control["target_attempt"],
            "idempotent": bool(request["idempotent"]),
        }

    def _launch_cancel_coordinator(self, job_id: str, request_id: str) -> None:
        directory = self.store.job_dir(job_id)
        command = [
            sys.executable,
            "-m",
            "comsol_mcp.jobs.cancel_worker",
            str(self.store.root),
            job_id,
            request_id,
            str(self.cancel_grace_seconds),
            str(self.cancel_terminate_seconds),
        ]
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
        with (directory / "worker.log").open("ab", buffering=0) as log:
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter/module argv
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
        _track_detached_process(process)

    def reconcile_cancellations(self, *, limit: int | None = None) -> int:
        """Reconcile orphaned cancellation attempts without weakening cleanup proof."""
        reconciled = 0
        if not self.store.root.is_dir():
            return 0
        count = None if limit is None else max(1, min(int(limit), 100))
        directories = sorted(
            (
                path
                for path in self.store.root.iterdir()
                if path.is_dir()
                and path.name.startswith("job-")
                and (path / "state.json").is_file()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if count is not None:
            directories = directories[:count]
        for directory in directories:
            job_id = directory.name
            try:
                if self.store.read_spec(job_id).get("job_type") in REMOVED_RESEARCH_JOB_TYPES:
                    continue
                action: tuple[str, dict[str, Any]] | None = None
                with self.store.lock(job_id, timeout=0.1):
                    if self.store.read_spec(job_id).get("job_type") in REMOVED_RESEARCH_JOB_TYPES:
                        continue
                    state = self.store.read_state(job_id)
                    control = self.store.read_control(job_id)
                    if state.get("status") not in {"cancel_requested", "cancelling"}:
                        continue
                    if control.get("request") != "cancel_requested" or not control.get(
                        "request_id"
                    ):
                        continue
                    attempt = int(state.get("attempt", 1))
                    if not cancel_request_targets_attempt(control, attempt):
                        continue
                    cancel = dict(state.get("cancel") or {})
                    if cancel.get("request_id") not in (None, control.get("request_id")):
                        continue
                    coordinator = cancel.get("coordinator")
                    coordinator_verdict = (
                        inspect_identity(coordinator)
                        if isinstance(coordinator, dict)
                        else {
                            "identity": coordinator,
                            "state": "missing",
                            "reason": "coordinator identity is missing",
                        }
                    )
                    if coordinator_verdict["state"] == "active":
                        continue
                    request_id = str(control["request_id"])

                    worker = control.get("target_worker")
                    worker_verdict = (
                        inspect_identity(worker)
                        if isinstance(worker, dict)
                        else {
                            "identity": worker,
                            "state": "missing",
                            "reason": "worker identity is missing",
                        }
                    )
                    capture = cancel.get("descendant_capture")
                    capture_proved = (
                        isinstance(capture, dict)
                        and isinstance(cancel.get("descendants"), list)
                        and (
                            (
                                isinstance(capture.get("worker"), dict)
                                and capture["worker"].get("state") == "active"
                                and capture.get("capture_method", "live_enumeration")
                                == "live_enumeration"
                            )
                            or (
                                capture.get("capture_method") == "contained_worker_exit"
                                and capture.get("process_tree_contained") is True
                                and state.get("process_tree_contained") is True
                            )
                        )
                    )
                    if (
                        isinstance(capture, dict)
                        and capture.get("capture_method") == "controlled_leaf_worker"
                    ):
                        from .process_control import controlled_leaf_worker_proved

                        capture_proved = (
                            isinstance(worker, dict)
                            and cancel.get("descendants") == []
                            and controlled_leaf_worker_proved(
                                self.store.read_spec(job_id), state, worker
                            )
                        )

                    if (
                        state.get("status") == "cancelling"
                        and coordinator_verdict["state"] == "stale"
                        and worker_verdict["state"] == "stale"
                        and capture_proved
                    ):
                        action = ("relaunch", {"request_id": request_id})
                    elif "uncertain" in {coordinator_verdict["state"], worker_verdict["state"]}:
                        now = time.time()
                        cancel["reconciliation"] = {
                            "observed_at_epoch": now,
                            "outcome": "blocked_uncertain_identity",
                            "coordinator": coordinator_verdict,
                            "worker": worker_verdict,
                        }
                        state["cancel"] = cancel
                        state["updated_at_epoch"] = now
                        atomic_write_json(self.store.job_dir(job_id) / "state.json", state)
                        self.store._append_event_unlocked(
                            job_id,
                            "cancel_reconciliation_blocked",
                            {"request_id": request_id, "reason": "uncertain_identity"},
                            str(state["status"]),
                        )
                    elif (
                        state.get("status") == "cancelling"
                        and coordinator_verdict["state"] == "stale"
                        and worker_verdict["state"] == "stale"
                        and not capture_proved
                    ):
                        now = time.time()
                        cancel["reconciliation"] = {
                            "observed_at_epoch": now,
                            "outcome": "blocked_missing_descendant_capture",
                            "coordinator": coordinator_verdict,
                            "worker": worker_verdict,
                        }
                        state["cancel"] = cancel
                        state["updated_at_epoch"] = now
                        atomic_write_json(self.store.job_dir(job_id) / "state.json", state)
                        self.store._append_event_unlocked(
                            job_id,
                            "cancel_reconciliation_blocked",
                            {"request_id": request_id, "reason": "missing_descendant_capture"},
                            str(state["status"]),
                        )
                    else:
                        action = ("relaunch", {"request_id": request_id})

                if action is None:
                    continue
                _kind, payload = action
                self._launch_cancel_coordinator(job_id, payload["request_id"])
                reconciled += 1
            except FileNotFoundError, TimeoutError, ValueError, OSError:
                continue
        return reconciled

    def _retained_resource_journal_entries(self, job_id: str) -> int:
        """Count retained resource-journal entries with the authoritative reader.

        The store's bounded reader is the same validator the next append
        goes through: unreadable or malformed retained evidence surfaces as
        a refusal here instead of being silently counted as zero entries
        and letting the relaunch die at its first append.
        """
        entries = self.store.read_resource_journal(job_id)
        return len(entries)

    def resume(self, job_id: str) -> dict[str, Any]:
        _refuse_removed_job(self.store.read_spec(job_id).get("job_type"))
        if _launch_marker_alive(self.store, job_id):
            # A live launcher still owns this job's dispatch; a concurrent
            # resume here would race its pending worker into a second
            # launch for one job.
            raise ValueError(
                "this job has an in-flight worker launch owned by a live "
                "process; wait for it to finish and reconcile with "
                "job_status before resuming"
            )
        with self.store.lock(job_id):
            state = self.store.read_state(job_id)
            if state["status"] not in {"failed", "interrupted", "cancelled"}:
                raise ValueError("Only failed, interrupted, or cancelled jobs may be resumed")
            spec = self.store.read_spec(job_id)
            _refuse_removed_job(spec.get("job_type"))
            if spec["job_type"] == "test_sequence" and not self.allow_test_jobs:
                raise ValueError("test_sequence jobs are disabled")
            expected = spec.get("spec_fingerprint")
            actual = _fingerprint(
                {key: value for key, value in spec.items() if key != "spec_fingerprint"}
            )
            if expected != actual:
                raise ValueError("Refusing resume because immutable spec fingerprint changed")

        # Ownership status merges durable-job summaries, so preflight must run
        # outside this job's lock.  The second locked validation below prevents
        # two concurrent resume callers from both transitioning and launching.
        _verify_source_identities(spec)

        # A resume inherits every retained journal entry from earlier
        # attempts; relaunching without headroom for the new attempt's
        # entries would only die against the hard limit mid-run.
        retained_entries = self._retained_resource_journal_entries(job_id)
        projected = resource_journal_capacity_verdict(int(_point_count(spec)))
        if retained_entries + projected["required_entries_lower_bound"] > projected["max_entries"]:
            raise ValueError(
                "resource journal cannot hold the resumed sweep: "
                f"{retained_entries} retained entries plus "
                f"{projected['required_entries_lower_bound']} projected entries "
                f"exceed the {projected['max_entries']}-entry limit; export the "
                "retained evidence and submit a fresh job instead"
            )

        if spec["job_type"] != "test_sequence":
            preflight = self._run_preflight(spec)
            if not preflight.get("ready", preflight.get("success", False)):
                raise ValueError(f"Job preflight failed: {preflight.get('blockers') or preflight}")

        with self.store.lock(job_id):
            state = self.store.read_state(job_id)
            if state["status"] not in {"failed", "interrupted", "cancelled"}:
                raise ValueError("Job state changed while resume preflight was running")
            current_spec = self.store.read_spec(job_id)
            current_expected = current_spec.get("spec_fingerprint")
            current_actual = _fingerprint(
                {key: value for key, value in current_spec.items() if key != "spec_fingerprint"}
            )
            if current_expected != expected or current_actual != expected:
                raise ValueError("Refusing resume because immutable spec changed during preflight")
            _verify_source_identities(current_spec)
            state["status"] = "starting"
            state["attempt"] = int(state.get("attempt", 1)) + 1
            state["worker_pid"] = None
            state["worker_process_create_time"] = None
            state["worker_command_signature"] = None
            state["last_error"] = None
            if isinstance(state.get("cancel"), dict):
                state["cancel"] = {
                    **state["cancel"],
                    "superseded_by_attempt": state["attempt"],
                }
            state["updated_at_epoch"] = time.time()
            atomic_write_json(self.store.job_dir(job_id) / "state.json", state)
            self.store.write_control(
                job_id,
                None,
                fields={"cleared_for_attempt": state["attempt"]},
            )
            self.store._append_event_unlocked(
                job_id, "resume_requested", {"attempt": state["attempt"]}, "starting"
            )
        module = _worker_module(current_spec["job_type"])
        try:
            identity = self._launch_worker(job_id, module)
            self.store.bind_worker_identity(job_id, identity)
        except Exception as exc:
            self.store.update_state(
                job_id,
                "failed",
                patch={"last_error": {"type": type(exc).__name__, "message": str(exc)}},
                event="resume_launch_failed",
            )
            raise
        try:
            self._arm_native_wall_watchdog_if_required(
                job_id,
                current_spec,
                attempt=int(state["attempt"]),
                worker_identity=identity,
            )
        except Exception as exc:
            cancellation = self.cancel(job_id, expected_attempt=int(state["attempt"]))
            raise JobLaunchError(
                job_id,
                RuntimeError(
                    "Licensed robust wall watchdog could not be armed on resume; "
                    f"exact-attempt cancellation result: {cancellation}"
                ),
            ) from exc
        return {
            "success": True,
            "job_id": job_id,
            "status": "starting",
            "attempt": state["attempt"],
        }

    def status(self, job_id: str, *, refresh_probe: bool = False) -> dict[str, Any]:
        legacy_spec = self.store.read_spec(job_id)
        if legacy_spec.get("job_type") in REMOVED_RESEARCH_JOB_TYPES:
            state = self.store.read_state(job_id)
            return {
                **state,
                "success": True,
                "job_id": job_id,
                "legacy_execution": {
                    "supported": False,
                    "reason_code": "research_job_removed",
                    "legacy_version": "0.7.8",
                    "analysis_replayed": False,
                },
            }
        # A cancellation coordinator must be able to acquire the durable lock
        # promptly.  Atomic state replacement makes this terminal observation
        # safe without taking the polling lock, and it avoids status callers
        # starving the coordinator during the durable cancellation grace/verification window.
        try:
            observed = self.store.read_state(job_id)
        except RuntimeError:
            observed = None
        if isinstance(observed, dict) and observed.get("status") == "cancelling":
            return {"success": True, "job_id": job_id, **observed}
        with self.store.lock(job_id):
            state = self.store.read_state(job_id)
            if state.get("status") in ACTIVE_STATES and state.get("worker_pid") is not None:
                identity = {
                    "pid": state["worker_pid"],
                    "process_create_time": state.get("worker_process_create_time"),
                    "command_signature": state.get("worker_command_signature"),
                }
                process_state, reason = process_identity_state(identity)
                current = str(state["status"])
                if process_state == "stale" and current != "cancelling":
                    control = self.store.read_control(job_id)
                    attempt = int(state.get("attempt", 1))
                    if current == "cancel_requested" and cancel_request_targets_attempt(
                        control, attempt
                    ):
                        # A matching cancellation owns this attempt's terminal
                        # outcome.  Preserve the nonterminal state so the
                        # coordinator can prove process/port/lease cleanup.
                        state["worker_process_state"] = process_state
                        state["worker_process_reason"] = reason
                    else:
                        if "interrupted" not in TRANSITIONS[current]:
                            raise RuntimeError(f"Cannot reconcile state {current} as interrupted")
                        state["status"] = "interrupted"
                        state["last_error"] = {"type": "WorkerInterrupted", "message": reason}
                        state["updated_at_epoch"] = time.time()
                        atomic_write_json(self.store.job_dir(job_id) / "state.json", state)
                        self.store._append_event_unlocked(
                            job_id, "worker_interrupted", {"reason": reason}, "interrupted"
                        )
                else:
                    state["worker_process_state"] = process_state
                    state["worker_process_reason"] = reason
            spec = self.store.read_spec(job_id)
            if spec.get("job_type") == "validation_matrix":
                from .validation_rows import read_validation_rows

                rows = read_validation_rows(self.store.job_dir(job_id) / "matrix_rows.jsonl", spec)
                ok = [row for row in rows if row["status"] == "ok"]
                errors = [row for row in rows if row["status"] == "error"]
                state["matrix_summary"] = {
                    "total_declared": len(spec["points"]),
                    "rows": len(rows),
                    "complete": len(ok),
                    "errors": len(errors),
                    "pending": len(spec["points"]) - len({row["point_fingerprint"] for row in ok}),
                    "last_row_sha256": rows[-1]["row_sha256"] if rows else None,
                    "last_error_type": errors[-1]["error"]["type"] if errors else None,
                }
            elif spec.get("job_type") == "thermo_optomechanical_replay":
                from .thermo_optomechanical_replay_rows import (
                    read_thermo_optomechanical_stage_rows,
                )

                directory = self.store.job_dir(job_id)
                rows = read_thermo_optomechanical_stage_rows(
                    directory / "thermo_optomechanical_stages.jsonl",
                    spec,
                    artifact_root=directory,
                )
                state["thermo_optomechanical_progress"] = {
                    "declared_stages": len(spec["declared_stages"]),
                    "completed_stages": len(rows),
                    "pending_stages": len(spec["declared_stages"]) - len(rows),
                    "completed_stage_ids": [row["stage_id"] for row in rows],
                    "last_stage_row_sha256": rows[-1]["row_sha256"] if rows else None,
                    "declared_optical_points": spec["declared_optical_point_count"],
                }
            if state.get("status") in TERMINAL_STATES:
                # B13 warning-only post-run probe: cached after first terminal
                # scan; refresh_probe forces a re-hash. A failed `.mph` parse
                # is recorded here and never changes the job disposition.
                from ..evidence.inspection.probe import probe_mph_artifacts

                state["mph_artifact_probe"] = probe_mph_artifacts(
                    self.store.job_dir(job_id),
                    refresh=refresh_probe,
                )
            if (self.store.job_dir(job_id) / "bounded_steps.jsonl").is_file():
                # Warning-only bounded-step review surface: a failed journal
                # parse is recorded here and never changes the disposition.
                try:
                    from .bounded_steps import summarize_bounded_steps

                    state["bounded_steps"] = summarize_bounded_steps(self.store.job_dir(job_id))
                except Exception:
                    logger.warning("bounded step summary was unreadable", exc_info=True)
                    state["bounded_steps"] = {
                        "available": False,
                        "reason_code": "bounded_steps_unreadable",
                    }
            if (self.store.job_dir(job_id) / "observation.json").is_file():
                # Warning-only observation surface: a failed receipt read is
                # recorded here and never changes the job disposition.
                try:
                    from .observation import summarize_observation

                    state["observation"] = summarize_observation(self.store.job_dir(job_id))
                except Exception:
                    logger.warning("observation receipt was unreadable", exc_info=True)
                    state["observation"] = {
                        "available": False,
                        "reason_code": "observation_unreadable",
                    }
            return {"success": True, "job_id": job_id, **state}

    def tail(self, job_id: str, n: int = 20) -> dict[str, Any]:
        return {"success": True, **self.store.tail(job_id, n)}

    def summaries(self, limit: int = 20) -> dict[str, Any]:
        count = max(1, min(int(limit), 100))
        directories = sorted(
            (
                path
                for path in self.store.root.iterdir()
                if path.is_dir()
                and path.name.startswith("job-")
                and (path / "state.json").is_file()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )[:count]
        jobs = []
        for directory in directories:
            try:
                state = self.status(directory.name)
                jobs.append(
                    {
                        "job_id": directory.name,
                        "status": state["status"],
                        "attempt": state.get("attempt"),
                        "progress": state.get("progress"),
                        "worker_pid": state.get("worker_pid"),
                        "updated_at_epoch": state.get("updated_at_epoch"),
                        "last_error": state.get("last_error"),
                    }
                )
            except Exception as exc:
                jobs.append(
                    {
                        "job_id": directory.name,
                        "status": "unreadable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        active = [job for job in jobs if job["status"] in ACTIVE_STATES]
        return {
            "available": True,
            "root": str(self.store.root),
            "count_returned": len(jobs),
            "active_count": len(active),
            "active": active,
            "recent": jobs,
        }
