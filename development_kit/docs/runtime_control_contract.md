# Runtime control contract (version 1)

Status: frozen description of accepted COMSOL MCP 0.8.1 behavior.
Source tree: `0ff945cc2248966f27523d75536b2ebcdd0dc77f` (tree `d657d355`).
Plan item: 0.8.2 P07 (Batch C). This document changes no behavior.

A change to any rule in this document needs its own reviewed batch and a
caller decision. Rule: no change may weaken 0.8.1 ownership. Line
references point to the source tree above. No commit on `alpha8_2` changed
the cited files before this version of the document.

## 1. Actors

| Actor | Process | Authority |
| --- | --- | --- |
| MCP host | `comsol-mcp` server | Acquires, refreshes, and releases its own solver lease. |
| Durable job worker | Detached `comsol_mcp.jobs` worker | Holds a job lease. Contains its own process tree. |
| Cancel coordinator | `python -m comsol_mcp.jobs.cancel_worker` | The only actor that terminates processes. |
| Wall watchdog | `comsol_mcp.jobs.native_wall_watchdog` | Requests cancellation only. Never terminates. |
| Rust shim (0.8.2 plan) | Optional launcher | Must not acquire solver leases. |

## 2. Runtime directory

`comsol_mcp/utils/runtime_paths.py:16-56` resolves the runtime directory.
The first match wins:

1. Environment variable `COMSOL_MCP_RUNTIME_DIR`.
2. The parent of environment variable `COMSOL_MCP_JOBS_DIR`.
3. `COMSOL_MCP_RUNTIME_DIR` injected from settings (`settings_environment`).
4. The parent of the settings-injected `COMSOL_MCP_JOBS_DIR`.
5. Windows: `%PROGRAMDATA%\comsol_mcp_runtime`.
6. POSIX: the XDG state directory plus `runtime`.
7. The system temporary directory plus `comsol_runtime`.

The settings document default is `%PROGRAMDATA%/comsol_mcp/runtime`
(`comsol_mcp/settings.py:72-75`). A real environment value always wins
over a settings value. Every runtime path must be absolute ASCII.

Every agent process on the machine shares the Windows default. This is
intentional: one machine has one solver lease domain. A process that
expects isolation but resolves the shared default contends on the same
lease. This is the B081-021 defect class. The test suite isolates each
worker with a synthetic `COMSOL_MCP_RUNTIME_DIR`
(`development_kit/tests/conftest.py`, `_synthetic_runtime_isolation`).
An explicit value is never overridden by the tests.

The runtime directory contains these entries:

| Path | Purpose |
| --- | --- |
| `solver_owner.json` | Solver lease document. |
| `.solver_owner.operation.lock` | Cross-process lease mutation lock. |
| `operation.lock` | Per-call COMSOL-bound operation lock. |
| `jobs/` | Durable job store. Must be the `jobs` child of the runtime directory. |
| `session/startup-state.json` | Session startup state. |

`default_jobs_root` (`runtime_paths.py:59-79`) refuses a jobs directory
whose parent is not the runtime directory.

## 3. Process identity

Every lock and lease identifies a process by three values:

- `pid`: a positive integer.
- `process_create_time`: the finite process creation time.
- `command_signature`: lowercase hex SHA-256 of the argument vector joined
  by NUL bytes (`ownership.py:326-328`).

Identity checks return one of three states:

| State | Meaning |
| --- | --- |
| `active` | PID, creation time, and command line match. |
| `stale` | The PID is gone, another process reuses the PID, or the command line changed. |
| `uncertain` | Some evidence is unavailable. |

Uncertainty is never treated as absence. Only `stale` permits recovery.

Creation-time tolerance differs by document:

| Document | Tolerance | Source |
| --- | --- | --- |
| Solver lease | 0.05 s | `CREATE_TIME_TOLERANCE_SECONDS`, `ownership.py:31` |
| Durable job lock | 0.05 s | `CREATE_TIME_TOLERANCE_SECONDS`, `jobs/store.py:42` |
| Operation lock | 1.0 s | `PROCESS_CREATE_TIME_TOLERANCE_SECONDS`, `operation_arbiter.py:27` |

The job lock does not mark an identity stale for a missing signature alone
(`jobs/store.py:169-171`).

## 4. Solver lease

### 4.1 Document

`solver_owner.json` uses `LEASE_SCHEMA_VERSION = "3"` (`ownership.py:30`).
The writer stamps the version. No reader checks it (see decision D1).
The writer serializes canonical JSON: `indent=2`, `sort_keys=True`, and a
final newline.

| Field | Content |
| --- | --- |
| `schema_version` | `"3"` |
| `owner` | Owner label, for example `comsol-mcp` or `job:<job_id>`. |
| `mode` | `local-client`, `remote-connect`, `durable-job`, or `attached-server`. |
| `pid`, `parent_pid` | Owner process and its parent. |
| `process_create_time` | Owner creation time. |
| `command_line`, `command_signature` | Full argument vector and its SHA-256. |
| `model_path` | Current model path or `null`. |
| `heartbeat_epoch`, `created_at_epoch` | Wall-clock times. |
| `acquisition_id` | 32-character UUID hex. Required for release and recovery. |
| `resource_ownership` | `mcp_owned` or `external_user_owned_server`. |
| `attached_server` | `null`, or the attached server identity. |
| `comsol_server_processes` | Owned servers: `pid`, `process_create_time`, `command_signature`. |
| `comsol_server_pids` | Legacy PID list. Readable but never used to act. |
| `comsol_server_port` | Never written in this tree. Read by the cancel coordinator. |

### 4.2 Mutation lock

Acquire, heartbeat, release, and recovery hold one lock:
`.solver_owner.operation.lock` (`ownership.py:209-248`).

- Windows: `msvcrt.locking` on one byte. The deadline is 1.0 s. The poll
  interval is 0.02 s. The operating system releases the lock when the
  process dies.
- Other platforms: lease mutation raises
  `"solver lease mutation requires supported Windows locking"`.

### 4.3 Operations

1. Acquire (`ownership.py:1145-1250`). The process requires a complete
   fresh process inventory. It reuses its own active lease. It refuses on
   any collision or stale lease. It creates the file with
   `O_CREAT | O_EXCL`, writes, and calls `fsync`. It then checks for
   collisions again and removes its own bytes if a collision appeared.
2. Heartbeat (`ownership.py:1389-1469`). The owner updates
   `heartbeat_epoch`. It writes a temporary file and calls `os.replace`
   only if the lease bytes still equal the bytes it read. The heartbeat is
   event-driven. It has no timer and no expiry.
3. Release (`ownership.py:1471-1521`). The process removes the lease only
   if the lease identity is its own and `acquisition_id` exists. Removal
   requires the exact bytes it read.
4. Recovery (`ownership.py:1523-1575`). The process requires a complete
   inventory, a `stale` lease, and an `acquisition_id`. Removal requires
   the exact bytes. Recovery never terminates a process.

Liveness comes from process identity, never from heartbeat age.

### 4.4 Lease state truth table

| Observed lease | State | Allowed next action | Forbidden action |
| --- | --- | --- | --- |
| File absent | absent | Acquire after a collision-free inventory. | None specific. |
| Unreadable or not a JSON object | uncertain | Report and refuse mutation. | Recover or overwrite. |
| Invalid identity fields | uncertain | Report and refuse mutation. | Recover or overwrite. |
| Identity evidence unavailable | uncertain | Report and refuse mutation. | Treat as stale. |
| PID gone, PID reused, or command changed | stale | Explicit recovery after a complete inventory. | Automatic overwrite. |
| Identity matches another process | active | Report the owner. | Acquire or release. |
| Identity matches this process | active, owned | Reuse, heartbeat, release. | None specific. |
| Unknown `schema_version` with valid identity | judged by identity | As the identity state permits. | None today (decision D1). |
| No `acquisition_id` | legacy | Report only. | Release or recover. |

### 4.5 Attached-server variant

`acquire_attached` (`ownership.py:1252-1387`) records an external server
that the user owns. The lease has `mode="attached-server"` and
`resource_ownership="external_user_owned_server"`. Workers never clear the
server models. Workers never request a server shutdown and never terminate
the server (`jobs/worker.py:301-303`).

## 5. Operation lock

`operation.lock` serializes COMSOL-bound tool calls
(`comsol_mcp/operation_arbiter.py`).

- Schema `comsol_mcp.operation_lock`, version `1.0.0`. The reader checks
  the schema and version and requires the exact field set:
  `schema_name`, `schema_version`, `operation_id`, `tool_name`,
  `side_effect_class`, `pid`, `process_create_time`, `acquired_at_epoch`.
- Publication uses `O_CREAT | O_EXCL`, a short-write loop, `fsync`, and a
  read-back of the exact published bytes.
- The arbiter removes a malformed lock by exact bytes and retries once.
- The arbiter removes a stale owner lock by exact bytes. An uncertain
  owner means busy.
- Release removes the lock only if its bytes and file identity match the
  claim. An unverified release turns a successful tool result into a
  failure.
- `inspect()` is read-only and never recovers.

## 6. Durable job lock

The job store uses `JOB_SCHEMA_VERSION = "2"` (`jobs/store.py:41`).
Version 1 controls stay readable.

- Lock file: `<job_dir>/.state.lock`, created with `O_CREAT | O_EXCL`.
- Guard: an in-process lock plus an OS guard on `.state.lock.guard`
  (`msvcrt.locking` on Windows, `PosixFileLock` elsewhere).
- The acquirer reclaims a stale lock identity with unchanged bytes.
- The acquirer reclaims an empty or unparsable lock older than
  `LOCK_RECOVERY_GRACE_SECONDS` (2.0 s) with unchanged bytes.
- Release removes only the exact bytes of the holder.
- A worker binds its identity in `state.json` before any side effect.

## 7. Lock order

1. In-process job guard.
2. OS job guard file.
3. Exclusive `.state.lock`.
4. State changes through atomic writes (`durable/io.py`, `os.replace`).

Lease changes hold `.solver_owner.operation.lock` first. They then use an
exclusive create, or a replace or unlink gated on exact bytes. A
COMSOL-bound tool call holds `operation.lock` outside any job lock.

## 8. Termination and cleanup authority

- Containment: each durable worker assigns itself to a Windows Job Object
  with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`
  (`jobs/process_control.py:63-194`). A host restart never kills a
  worker. Worker exit kills only its own descendants.
- Termination: only the cancel coordinator terminates processes. It opens
  a pinned handle and checks the exact identity first. It never acts on an
  `uncertain` identity (`jobs/process_control.py:385-465`).
- Cancellation phases: `requested`, `native_grace`, `terminate`,
  `force_kill`, `verifying`, then `verified` or `terminal_commit`. The
  coordinator records each phase before its side effect
  (`jobs/cancel_worker.py`).
- Wall watchdog: at the deadline it calls `manager.cancel` with the exact
  attempt. It never terminates a process
  (`jobs/native_wall_watchdog.py:118-258`).
- Lease reclamation during cancellation requires: owner `job:<job_id>`,
  every recorded server proven absent, the recorded port closed, and a
  `stale` lease (`jobs/cancel_worker.py:347-492`).
- Worker exit: a worker publishes completion only after verified cleanup.
  Otherwise the job fails with `OwnedCleanupUnverified` or
  `AttachedCleanupUnverified` (`jobs/worker.py:773-919`).

## 9. Frozen schema identities

| Document | Name | Version | Read validation |
| --- | --- | --- | --- |
| Solver lease | `solver_owner.json` | `"3"` | None (identity only) |
| Job store | `control.json`, `state.json` | `"2"` | Version 1 controls stay readable |
| Operation lock | `comsol_mcp.operation_lock` | `1.0.0` | The reader checks the schema and version |
| Shared model lock | `comsol_mcp.shared_model_lock` | `1.0.0` | Strict, whole-document hash |
| Solver owner registry | `comsol_mcp.solver_owner_registry` | `1.0.0` | Strict |
| Cleanup outcome | `comsol_mcp.cleanup_outcome` | `1.0.0` | Strict |
| Wall watchdog | `comsol_mcp.robust_wall_watchdog` | `1.0.0` | Strict |
| Settings | `comsol_mcp.settings` | `1.3.0` | Reads 1.0.0 to 1.3.0 |

## 10. Rules for 0.8.2 and later work

1. The Rust shim must not acquire, refresh, release, or recover a lease.
2. No component may reclaim a lease or lock because a heartbeat is old or
   a parent process died while an owned solver may survive.
3. Cross-version tests (P08) use one shared test ownership domain under an
   isolated root. They never touch production runtime files.
4. A change to any table in this document needs a version bump in that
   document schema and a reviewed reader rule for the old version.

## 11. Open decisions

These items describe current behavior. This batch does not decide them.

- D1: No reader checks `LEASE_SCHEMA_VERSION`. Identity decides the
  lease state. A future rule could refuse unknown versions. That change
  would make some leases `uncertain` that are `active` today.
- D2: `comsol_server_port` is never written, but the cancel coordinator
  reads it. The port check is therefore inert for leases from this tree.
- D3: Non-Windows platforms refuse lease mutation. Any Linux
  licensed-solver claim needs a reviewed Linux lock design first.
- D4: The Windows default runtime directory is machine-shared by design.
  Changing it would split the solver lease domain.

## 12. Tests that pin this contract

| Area | Tests |
| --- | --- |
| Solver lease | `development_kit/tests/test_ownership.py` |
| Operation lock | `development_kit/tests/test_operation_arbiter.py` |
| Runtime paths | `test_runtime_paths.py`, `test_worker_runtime_isolation.py` |
| POSIX lock | `test_posix_platform.py`, `test_platform_support.py` |
| Termination | `test_process_control.py`, `test_cancel_state_machine.py` |
| Durable jobs | `test_durable_job_control_plane.py`, `test_validation_worker.py` |
| Wall watchdog | `test_native_wall_watchdog.py` |
| Shared session | `test_shared_model_locking.py`, `test_shared_cleanup_contracts.py`, `test_solver_owners.py`, `test_shared_session_lifecycle.py` |
| Atomic writes | `test_durable_primitives.py` |
