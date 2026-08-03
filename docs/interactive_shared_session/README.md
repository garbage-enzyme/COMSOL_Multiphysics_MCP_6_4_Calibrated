# COMSOL Desktop/Server interactive collaboration

Thanks to [Ching-Chiang/comsol-mcp](https://github.com/Ching-Chiang/comsol-mcp)
for introducing this interaction idea. This project used that work only to
study the workflow and built its own default-off safety design. It did not copy,
adapt, translate, cherry-pick, or mechanically rewrite source code from that
repository. The two implementations do not necessarily behave the same way.

## What this mode is for

Use this mode when you want to keep a model visible in COMSOL Desktop while an
assistant uses MCP to work with the same COMSOL Multiphysics Server.

The main rule is simple: **the user and the assistant must take turns. They must
not edit the model at the same time.**

MCP does not start, stop, clear, or terminate the user's Server, Desktop,
listener, or model. It connects only to a local Server that the user names and
checks that the connection and model still match before each protected action.

The current release supports:

- one Windows computer;
- one user-started COMSOL Multiphysics Server;
- one COMSOL Desktop window connected to that Server;
- one exactly identified model held by the Server;
- COMSOL `6.4.0.*`, with `6.4.0.293` as the reference build;
- MPh 1.3.1 and this MCP package;
- short inspection, readback, and Save Copy work;
- bounded attached jobs submitted as `staged_sweep`.

This is not a remote desktop and does not support simultaneous co-editing.

## Two collaboration modes

### Inspection

`interactive_inspection` is for short model inspection, readback, revision
checks, and Save Copy snapshots. The assistant must unlock the model before the
user edits again.

### Bounded automation

`automation_exclusive` is for resumable bounded jobs. Desktop may continue to
show the model, but the user must observe only until the job reaches a verified
terminal state.

The independent `shared_server.enabled` feature does not expose an unrestricted
foreground solver. Parameter changes and solves use
`job_submit/status/tail/cancel/resume`. The attached backend currently supports
only `staged_sweep`.

## Before you begin

Confirm all of the following:

- COMSOL Multiphysics and COMSOL Multiphysics Server are on the same computer;
- Desktop and Server are both in the `6.4.0.*` release line;
- the license permits local Client/Server use;
- formal source models are under a configured read root;
- snapshots and job artifacts use an ASCII-only output root;
- you can restart the MCP host after changing its settings.

A change only in the final build number, such as `6.4.0.293` to another
`6.4.0.x`, is admitted with a warning. A different third component, such as
`6.4.1.*`, is rejected. Unreadable or mixed Desktop/Server versions are also
rejected.

## Quick start

### Step 1: enable the shared feature

Before starting the MCP host, open the Settings GUI. Ask a connected agent to
call profile-independent `settings.start` once, or run the installed command:

```powershell
comsol-mcp-settings
```

In the Profile tab, choose any base profile that exposes the tools needed for
your work, then enable interactive shared-server collaboration. Configure the
runtime, model-read, and artifact roots in the other tabs. Save the settings,
close the GUI, and restart the owning MCP client. Do not edit JSON while the GUI
is open.

Direct JSON editing is the advanced equivalent for developers, deployment
automation, and agents acting on an explicit user request. The following is a
partial example; keep the other settings from the project template:

```json
{
  "profile": { "name": "core" },
  "shared_server": { "enabled": true },
  "runtime": { "directory": "D:/comsol_runtime" },
  "paths": {
    "model_read_roots": ["D:/comsol_models"],
    "artifact_write_root": "D:/comsol_runtime/owned_artifacts"
  }
}
```

If the MCP host does not start from the repository directory, set:

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

Restart the MCP host and call `capabilities`. Confirm that:

- `active_profile` is the profile you selected;
- `enabled_features` contains `shared_server`;
- `shared_session.feature_enabled` and `shared_session.gate_open` are `true`;
- the shared-session tools are listed;
- evidence-integrity checks remain enabled by default.

If the old profile is still shown, stop. Restart the actual MCP host process;
changing a terminal variable does not update a server that is already running.

### Step 2: start Server yourself

On Windows, open:

**COMSOL 6.4 > COMSOL Launchers > COMSOL Multiphysics Server 6.4**

The command-line equivalent can be:

```text
comsolmphserver -multi on -port 2036
```

`-multi on` keeps the Server and in-memory models alive after a client
disconnects. `-port 2036` requests a common port; the message in the Server
window is authoritative.

Wait for a message similar to:

```text
COMSOL Multiphysics Server 6.4 ... started listening on port 2036
```

Record the port and keep this window open. Do not give the assistant any
credentials. See COMSOL's official
[Windows command reference](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.31.html)
and [Client/Server startup guide](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.19.html)
for installation-specific details.

### Step 3: connect Desktop

Open one COMSOL Desktop 6.4 window and choose:

**File > COMSOL Multiphysics Server > Connect to Server**

Use `localhost` and the exact port from the Server window. Enter the username
and password only in COMSOL's own connection dialog. Never copy credentials to
chat, logs, screenshots, or receipts.

After connection, Desktop should show `localhost:<port>` in its lower-left
area, for example `localhost:2036`. If that indicator disappears, Desktop is no
longer connected to Server.

If COMSOL asks whether to use the current Desktop model or an existing Server
model, the user must choose explicitly. MCP can adopt only a model held by the
Server and does not guess the user's intent.

### Step 4: let MCP check and adopt the model

Tell the assistant only the local port. The normal order is:

1. call `shared_server_preflight(host="localhost", port=2036)`;
2. inspect its `state`, versions, process/listener evidence, and warnings;
3. after the user confirms that Desktop shows the same endpoint, call
   `shared_server_attach(..., user_confirmed=true)`;
4. call `shared_server_models`;
5. select one model and call `shared_model_adopt` with `model_tag`, plus either
   its exact saved path or `expected_unsaved=true`; the label is optional;
6. call
   `shared_model_lock(collaboration_mode="interactive_inspection", ...)`.

`user_confirmed=true` means the user actually saw the matching Desktop
connection. The assistant must not infer this confirmation from process data.

## Common states

MCP takes two complete, bounded process/listener observations before creating
an MPh Client. The second observation must be strictly later than the first. If
a relevant process appears, disappears, or changes identity between them, MCP
does not connect.

| What the user sees | MCP result | What to do |
| --- | --- | --- |
| Desktop and Server are both absent | `desktop_and_server_absent` | Start Server, wait for listening, then start Desktop |
| Desktop or the listener is still starting, or the owning Server is nonresponsive | `desktop_or_server_starting` | Wait for Desktop and Server to respond, then retry |
| The two observations are not ordered in time | `probe_chronology_invalid` | Collect two fresh observations |
| Desktop is open but no Server listener exists | Connection refused | Start Server and connect Desktop to the exact port |
| More than one Desktop window is present | `ambiguous_gui_clients` | Close or disconnect extra windows |
| Another MPh/COMSOL owner appears | Collision or identity-change state | Stop the unrelated owner or wait for startup to settle |
| Desktop or Server is outside `6.4.0.*` | `unsupported_or_ambiguous_comsol_version` | Use the same accepted release line and retry |
| Server holds no model | Attach may succeed; adoption returns `no_server_models` | Create, open, or transfer a model in connected Desktop |
| Server holds several models | A model list is returned; none is chosen automatically | Select by exact tag, path, and saved state |
| A same-family listener uses a wildcard address | `listener_bind_scope=wildcard` warning | Review firewall and Server settings |

MCP never chooses “the first model” or “the current window.” Use an exact model
tag, path, and saved-state expectation.

## Taking turns

### User turn

1. Confirm that Desktop still shows `localhost:<port>`.
2. Confirm that the MCP model lock has been released.
3. Make one clear, bounded change and wait for COMSOL to finish.
4. Tell the assistant what changed.
5. Let the assistant read the model again and create a new lock.

The chat message is a hint, not proof. If the model no longer matches the old
lock, that lock is invalid.

### Assistant inspection turn

1. Lock the exact model with `interactive_inspection`.
2. Save the returned `lock_sha256` and `revision_sha256`.
3. Call `shared_model_verify` before each identity-sensitive action.
4. Call `shared_model_snapshot` when a separate copy is needed.
5. Verify again, then call `shared_model_unlock`.
6. Tell the user clearly that their turn may resume.

COMSOL 6.4 Save Copy writes by pathname and cannot enforce a byte ceiling while
writing. When the bound cannot be guaranteed, this release returns
`snapshot_write_bound_unavailable`; it does not write a complete oversized file
and then pretend the write was bounded.

### Assistant solve turn

Shared solves use `automation_exclusive` and the durable job tools. A neutral
one-point example is:

```json
{
  "job_type": "staged_sweep",
  "source_model_path": "<configured immutable source .mph>",
  "parameter_name": "gap",
  "parameter_values": [10.0],
  "expressions": ["result_expression"],
  "execution_backend": {
    "kind": "attached_shared_server",
    "expected_lock_sha256": "<lock hash>",
    "expected_revision_sha256": "<revision hash>",
    "user_confirmed_automation_exclusive": true
  }
}
```

Parameters, units, expressions, source files, and scientific policies depend
on the real model. Do not copy this neutral example directly into a production
model.

Use `job_status` for progress and `job_tail` for bounded logs. The job checks
the external model before each point and persists results point by point. A
Desktop edit during the job blocks the next point or resume instead of mixing
old and new revisions.

`job_cancel` requests cancellation; it is not a terminal state. Wait for
`cancelled` and check the recorded worker, port, lease, and external-resource
preservation evidence. Cancellation must not terminate the user's Server,
Desktop, listener, or model.

## COMSOL busy indication

During a longer operation, Desktop may temporarily prevent editing and show an
occupied-model or busy warning. Wait for the assistant turn to finish; do not
force a concurrent edit.

A short read or property change may finish before the warning appears, so no
warning does not mean no call occurred. The native warning proves only that
COMSOL considered the model busy at that moment. MCP identity, evidence, and
cleanup checks must be read from the MCP result.

## Keep three file roles separate

| Role | Owner | May it change? | Rule |
| --- | --- | --- | --- |
| Immutable source | User | Not within one formal run | Keep under a configured read root, record exact SHA-256, and never open-and-overwrite it |
| Open working model | User and COMSOL Server | Only during an explicit turn | Keep it visible in Desktop and verify Server, model, and revision identity |
| Save Copy snapshot/checkpoint | MCP artifact workflow | Create new files only | Use an ASCII root, collision-free names, and recorded size/hash/manifest |

Even if the three files currently contain the same bytes, their roles are not
interchangeable. An unsaved in-memory model has no verified source-file hash.
Save a separate source and create a new run identity before formal work.

## Safe finish

End a collaboration in this order:

1. wait for any attached job to reach a verified terminal state;
2. retain the required raw results and snapshots;
3. call `shared_model_verify` for the current lock;
4. call `shared_model_unlock`;
5. call `shared_server_detach`;
6. confirm that the result says external resources were preserved;
7. confirm that Desktop still shows `localhost:<port>` and the model remains
   visible.

A normal detach does not require a Server restart. If detach returns
`model_lock_active`, unlock first. If detach is uncertain, do not kill COMSOL
by process name; inspect the exact process/listener identity and let the user
decide whether to restart their resource.

## Security and limitations

This release supports local loopback only, not a remote Server. COMSOL Server's
TCP connection is password-protected but is not otherwise encrypted; firewall
and address restrictions remain the user's or administrator's responsibility.

`0.0.0.0` matches only an IPv4 loopback endpoint, and `::` matches only an IPv6
loopback endpoint. Without explicit socket evidence, MCP does not assume that
an IPv6 wildcard socket also serves IPv4. This contract normalizes `localhost`
to IPv4. A matched wildcard listener retains the
`listener_bind_scope=wildcard` warning and is never rewritten as loopback-only.

Other limitations:

- no simultaneous user/assistant editing;
- no automatic choice among multiple Desktop windows, Servers, or models;
- no support outside `6.4.0.*`;
- MCP does not handle the username and password;
- not every short call produces a COMSOL busy warning;
- visible geometry, plots, and results are not by themselves scientific proof;
- attached automation currently supports only `staged_sweep`;
- `shared_server.enabled` remains experimental and default-off.

Matching visible Desktop output is useful collaboration evidence, but a formal
scientific conclusion still needs raw data, declared acceptance rules,
convergence checks, default-on evidence-integrity checks, and the physical
validation required by the model.
