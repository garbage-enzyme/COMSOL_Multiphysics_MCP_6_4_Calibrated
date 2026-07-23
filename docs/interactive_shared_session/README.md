# Interactive COMSOL Desktop/Server collaboration

We thank the original [Ching-Chiang/comsol-mcp](https://github.com/Ching-Chiang/comsol-mcp) repository for the method and UX contribution that informed this interaction concept. That repository was used only as behavioral research: this project independently implemented its own default-off design and did not copy, adapt, translate, cherry-pick, or mechanically rewrite the original repository's source code. This credit recognizes a method contribution; it does not claim that the two implementations or all behaviors are identical.

This mode lets you keep a COMSOL model visible in Desktop while an agent attaches
to the same user-started COMSOL Multiphysics Server. You and the agent take
explicit turns. The MCP does not start, clear, close, own, or terminate your
Server, Desktop, listener, model, or main file.

## What this mode is

The first release supports one local user, one user-owned COMSOL Multiphysics
Server, one connected Desktop client, and one exact server-held model. MCP
preflight identifies the local processes and listener, attaches a separate MPh
client, inventories server models, adopts one exact model, and applies an
optimistic model/revision lock.

There are two collaboration modes:

- `interactive_inspection` is for short, turn-based adoption, readback,
  revision checks, and Save Copy snapshots. Unlock before the user edits.
- `automation_exclusive` is for a bounded durable attached job. Desktop remains
  visible, but the user must not mutate the model until the job reaches a
  verified terminal state.

The public `desktop_shared` profile deliberately does not mix broad generic
`param_set` or foreground `study_solve` calls into a shared session. Controlled
agent mutation/solve work is submitted through the existing durable
`job_submit/status/tail/cancel/resume` path, currently for `staged_sweep`. A
single-point staged sweep is the bounded public path for one controlled
parameter change and solve. This limitation is important: the mode is not
simultaneous co-editing and not an unrestricted remote console.

## Prerequisites and compatibility

- COMSOL Multiphysics and COMSOL Multiphysics Server on the same computer;
- MPh 1.3.1 and this MCP installation;
- one authorized local user and a license that permits the local client/server
  topology;
- COMSOL Desktop and Server in the accepted `6.4.0.*` release line;
- the exact licensed reference build is `6.4.0.293`;
- a configured immutable model-read root for saved formal work and an ASCII
  owned artifact root for snapshots/jobs;
- an MCP host restart after changing `settings.json` profile or shared-server settings.

Only the final build component may differ inside `6.4.0.*`. For example, an
automatic-update change from `6.4.0.293` to another `6.4.0` build is admitted
with a build-difference warning. A third numeric component change, such as
`6.4.1.*`, is a different release family and fails closed. Older releases,
mixed Desktop/Server release families, and unreadable versions are not guessed.

The public MCP endpoint is local-loopback-only. COMSOL itself can bind its
listener more broadly; see [Security and limitations](#security-and-limitations).

## Quick start

### 1. Enable the default-off MCP profile

Edit the shared project-root `settings.json` before starting the MCP host:

```json
{
  "profile": { "name": "desktop_shared" },
  "shared_server": { "enabled": true },
  "runtime": { "directory": "D:/comsol_runtime" },
  "paths": {
    "model_read_roots": ["D:/comsol_models"],
    "artifact_write_root": "D:/comsol_runtime/owned_artifacts"
  }
}
```

These are partial edits; keep all other settings from the project template. See
the [settings guide](../setting_guide/README.md) for every field's meaning,
default, and accepted values. If the host does not preserve the project
path, pass only the one locator variable:

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

Restart the MCP host. Profile changes are static and are not hot-reloaded. Call
`capabilities` and confirm:

- `active_profile` is `desktop_shared`;
- `shared_session.profile_active` and `shared_session.gate_open` are `true`;
- shared-session tools are listed;
- evidence-integrity checks remain independently default-on.

If a setting is deleted it uses its default. If a setting contains an illegal
value, the safe default remains active and `project_settings.settings_errors`
reports the setting path and reason code.

Do not proceed if capabilities still show an old profile. Restart the actual
host process rather than assuming that changing a terminal variable updated an
already-running stdio server.

### 2. Start COMSOL Multiphysics Server manually

On Windows, open **COMSOL 6.4 > COMSOL Launchers > COMSOL Multiphysics Server
6.4**. COMSOL's 6.4 documentation also describes the Windows server command as
`comsolmphserver [options]`. For reliable detach/reconnect preservation, start
the server with repeated-client behavior enabled:

```text
comsolmphserver -multi on -port 2036
```

Use the executable supplied by your own installation; do not ask the agent to
find or handle credentials. `-multi on` keeps the Server and in-memory model
available after a client disconnects. `-port 2036` requests the normal default
port, but another free port may be selected or configured. The official
[Windows command reference](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.31.html)
documents `-multi`, `-port`, login, and password-storage options.

Wait until the console reports that COMSOL Multiphysics Server 6.4 is listening
and note the actual port, for example:

```text
COMSOL Multiphysics Server 6.4 ... started listening on port 2036
```

Leave this console running. In shared mode, MCP never starts or terminates it.
The Windows Start-menu procedure and first-start credential behavior are also
described in the official [client-server startup guide](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.19.html).

### 3. Connect COMSOL Desktop

Open one COMSOL Desktop 6.4 window. Select **File > COMSOL Multiphysics Server >
Connect to Server**. Choose the local server, use `localhost`, select a manual
port if needed, and enter the exact port reported by the Server console.

On the licensed acceptance host, the connection dialog automatically populated
the username and password from the user's local COMSOL setup. This is a useful
UX observation, not a guarantee for every installation. Use only credentials
from your authorized COMSOL installation. Never copy a username, password, or
login-properties file into an agent prompt, log, screenshot, or receipt.

After connection, the lower-left Desktop status area should show
`localhost:<port>`, such as `localhost:2036`. If that indicator disappears,
Desktop is no longer connected to the Server. The official
[Desktop connection guide](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.20.html)
documents the server/port dialog and explains that COMSOL may ask whether to
use the current Desktop model or the model already on the Server.

If the connection dialog asks which model to use, make a deliberate choice.
MCP can adopt only a model held by the Server. It never guesses whether the
standalone Desktop model or an existing Server model is intended.

### 4. Preflight, attach, and adopt exactly one model

Tell the agent the local port, not credentials. The MCP sequence is:

1. `shared_server_preflight(host="localhost", port=2036)`;
2. inspect `state`, exact process/listener evidence, release line, and warnings;
3. only after you confirm Desktop shows the same endpoint, call
   `shared_server_attach(..., user_confirmed=true)`;
4. call `shared_server_models`;
5. select one exact server model and call `shared_model_adopt` with its
   `model_tag` plus available expected label, path, or unsaved state;
6. call `shared_model_lock(collaboration_mode="interactive_inspection", ...)`.

The `user_confirmed=true` value is per-session evidence. It must reflect a real
user observation; the agent must not synthesize it from process data alone.

## How state detection behaves

Preflight makes two bounded process/listener observations before constructing
an MPh client. A visible window title or a process name alone is not enough.
After attach, the MCP also checks clientapi build readback and enumerates the
server-held model inventory.

| Observed state | MCP behavior | Smallest user action |
| --- | --- | --- |
| No COMSOL Desktop and no Server | Reports `desktop_and_server_absent`, retryable; no client or lease | Start Server, wait for listening, then start one Desktop |
| User clicked COMSOL but it is still starting | Reports `desktop_or_server_starting`, retryable | Wait until Desktop responds and the Server listener is stable; rerun preflight |
| Desktop open, Server absent | Refuses attach because no stable listener exists | Start the Server and connect Desktop to its exact port |
| Connected Desktop, no server-held model | Attach can succeed, but model inventory is empty and adoption returns `no_server_models` | While connected, create a model or transfer/open one on the Server, then refresh inventory |
| New blank unsaved model | Inventory marks it unsaved; exact tag plus `expected_unsaved=true` can adopt it | Use only for bounded interactive work, or save a separate immutable source before formal/durable work |
| Existing saved model | Inventory reports tag/label/path identity; an exact selector is required | Confirm path/label and adopt that one model; keep immutable source distinct from working/snapshot files |
| Model exists only in standalone Desktop | MCP cannot see it in Server inventory | Connect Desktop and explicitly transfer the current model, or save/open it while connected |
| Multiple Desktop windows | Preflight reports `ambiguous_gui_clients`; it does not choose a window | Close or disconnect extra windows and keep one intended Desktop client |
| Multiple server-held models | Inventory is returned, but MCP never auto-selects among ambiguous candidates | Identify one exact tag and add expected label/path/unsaved state |
| Older or mixed COMSOL release | Reports `unsupported_or_ambiguous_comsol_version` and refuses attach | Use matching Desktop and Server from accepted `6.4.0.*`, then rerun |
| Version unreadable | Fails closed rather than inferring from a shortcut/title | Repair the installation/process readback; do not override the version gate |
| Same `6.4.0.*` line, different final build | Admits the release line with `same_accepted_release_line_build_difference` warning | Confirm intentional update; retain exact build evidence in the receipt |
| Extra MPh/COMSOL owner or changing PID/listener | Reports collision or identity change; no lease/client | Stop the unrelated owner or wait for startup to stabilize; never kill by process name alone |
| Listener is wildcard-bound | Preserves `listener_bind_scope=wildcard` as a warning | Review firewall/network exposure; MCP does not rewrite it as loopback |

If several windows contain any combination of empty, blank, saved, or older
models, preflight handles the process/window ambiguity first. It cannot inspect
each GUI tab and guess intent. Reduce the topology to one intended Desktop, one
accepted Server, and one exact server-held model.

## Turn-taking collaboration workflow

### User turn

1. Confirm `localhost:<port>` is visible.
2. Ensure the MCP lock is released before editing.
3. Make a bounded Desktop change and wait for COMSOL to finish it.
4. Tell the agent what you changed as a hint, not as proof.
5. The agent re-inventories/relocks and uses readback to establish a new revision.

If you change a parameter from `55` to `30`, for example, the next revision
readback should establish the change. The agent must not simply trust the chat
message. A mismatch invalidates the old revision and requires a new lock.

### Agent inspection/snapshot turn

1. Adopt and lock the exact model in `interactive_inspection` mode.
2. Retain `lock_sha256` and `revision_sha256`.
3. Run `shared_model_verify` immediately before any identity-sensitive action.
4. For a Save Copy, call `shared_model_snapshot` with the expected lock,
   revision, and a caller-declared maximum byte count.
5. Verify again, then call `shared_model_unlock` with a short audit reason.
6. Tell the user that their turn has resumed.

### Controlled solve/agent mutation turn

For the public v3.1 surface, use `automation_exclusive` and the durable job
controls. The agent locks the model with an immutable source, and `job_submit`
performs the verified handoff, unlocks/detaches the interactive MCP client, and
starts an attached worker. A neutral one-point shape is:

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

The exact parameter, units/conventions, expression, source file, and scientific
policy are model-specific and must be declared by the caller. Do not copy this
neutral example into a real model without adapting the formal specification.

Poll with `job_status` and inspect bounded logs with `job_tail`. Do not run a
foreground loop of ordinary shared calls. The worker checks the external
revision before points, persists evidence point by point, and saves contained
checkpoints/Save Copies. A Desktop edit during this interval blocks the next
point or resume rather than silently mixing revisions.

Use `job_cancel` to request cancellation. `cancel requested` is not terminal.
Wait for `cancelled` plus verified owned-worker/descendant, port, lease, and
external-resource-preservation evidence. Cancellation may stop only the
attached MCP worker/client; it must not terminate the user-owned Server,
Desktop, listener, or model.

## Native Desktop busy warnings

COMSOL Server serializes access. During a longer agent mutation or solve,
Desktop may temporarily lock editing and display an occupied-model or busy warning.
Wait for the agent turn to finish. Do not click through the warning and
attempt a concurrent edit.

Short property writes or read-only calls may finish without showing the warning.
On the licensed host, a longer first model construction/solve showed it, while
later short change/readback operations did not. This difference is expected UX
timing. The native warning proves only that COMSOL considered the Server/model
busy; it is not proof that every MCP identity, revision, evidence, or cleanup
guard passed. Use MCP receipts for those claims.

## Saved-model walkthrough

1. Keep an immutable source `.mph` in a configured model-read root. Hash it and
   do not overwrite it during formal work.
2. Open or transfer a separate working model to the connected Server. Desktop
   displays this in-memory server model; it may have a saved path.
3. Preflight, attach, inventory, and adopt by exact tag plus expected path/label.
4. Lock with both immutable source path and SHA-256 for formal snapshot or
   attached durable work.
5. Alternate user and agent turns. Every agent turn begins with a revision
   check; every user edit causes a new lock/revision.
6. Use `shared_model_snapshot` or durable checkpoints for Save Copy artifacts.
   A snapshot never changes the visible main model path.
7. Unlock and detach. Confirm Desktop and Server still hold the model.

Windows/COMSOL may lock the currently open `.mph`. Also, **Save As** normally
switches the working model to the newly saved file; this was observed during
licensed UX acceptance. For formal work, do not assume that a newly saved file
remained an untouched source. Prefer a distinct immutable source and use Save
Copy for snapshots.

## Unsaved-model walkthrough

1. Connect Desktop to the Server first, then create one blank model.
2. Refresh `shared_server_models` and adopt the exact unsaved tag with
   `expected_unsaved=true`.
3. Use short turn-taking inspection/readback. You may create a contained Save
   Copy, but it does not retroactively prove an immutable starting source.
4. Before formal durable work, save a distinct source `.mph`, place it under a
   configured read root, hash it, and establish a new lock/run identity.
5. Never present an unsaved in-memory model as if it had a verified source-file hash.

## File roles

| Role | Owned by | May change? | Safety rule |
| --- | --- | --- | --- |
| Immutable source | User | No, within one formal identity | Existing readable `.mph` under configured model-read root; exact SHA-256; do not open-and-overwrite it |
| Open working model | User/COMSOL Server | Yes, by explicit turns | Visible in Desktop and identified by exact server/model/revision evidence; simultaneous edits unsupported |
| Save Copy snapshot/checkpoint | MCP-owned artifact workflow | New files only | ASCII owned root, collision-free name, size/hash/manifest; never overwrites source or changes main working path |

These roles may not be collapsed merely because three files currently contain
similar bytes. A verified source is not a scratch file, and a snapshot is not a
new source until a new formal identity explicitly adopts it.

## Collaboration etiquette checklist

- Keep one Desktop window, one intended Server, and one exact server model.
- Say whose turn it is before any edit or solve.
- Unlock before the user edits; relock and read back afterward.
- During `automation_exclusive`, observe only; do not mutate the model.
- Treat native busy warnings as a stop signal, not as a verification receipt.
- Use exact tags, paths, hashes, lock IDs, and revisions; never say “the first model.”
- Keep source, working model, and snapshots separate.
- Preserve failed, partial, diagnostic, cancelled, and residual evidence.
- Do not paste credentials into chat or receipts.
- Normally keep the Server running between collaboration steps.

## Safe detach and shutdown

Normal collaboration ends in this order:

1. Wait for any attached job to reach a verified terminal state.
2. Save required raw evidence and snapshots.
3. Verify the current lock/revision.
4. Call `shared_model_unlock`.
5. Call `shared_server_detach`.
6. Confirm detach reports external resources preserved.
7. Confirm Desktop still shows `localhost:<port>` and the model remains visible.

You normally do **not** restart Server between collaboration steps, after a Save
Copy, after reopening a model, or after normal MCP detach. Only the user closes
Desktop or the Server console after evidence is safe. Restart only when a
documented recovery requires it, such as an unrecoverable Server/client state,
and expect to create or re-establish process, model, lock, and revision identity.

If `shared_server_detach` reports `model_lock_active`, unlock first. If detach is
uncertain, do not kill COMSOL by name; inspect exact process/listener identity
and let the user decide whether to restart their resource.

## Security and limitations

COMSOL Multiphysics Server is a single-user server that permits multiple
connections by the same user. Official COMSOL 6.4 documentation notes that the
TCP connection is password protected but otherwise not encrypted, and that
firewall/address restrictions remain the administrator's responsibility. This
MCP release supports only a local loopback endpoint; it does not turn remote or
wildcard exposure into a supported topology.

Preflight preserves the actual listener bind evidence. If COMSOL listens on
`0.0.0.0` or `::`, it reports `listener_bind_scope=wildcard` even when the MCP
connects through `localhost`. Review the host firewall and COMSOL configuration.
MCP never rewrites the listener or claims it is loopback-only.

Limitations:

- no remote-host support in the first release;
- no simultaneous user/agent editing;
- no automatic choice among multiple windows, servers, or models;
- no support outside `6.4.0.*` without new release acceptance;
- no credential handling through MCP;
- no promise that every short call will trigger a native busy dialog;
- no claim that visible 3D geometry, plots, or GUI output are scientifically verified;
- only `staged_sweep` currently has the attached durable execution backend;
- `desktop_shared` is experimental and default-off.

Visible GUI agreement is useful collaboration evidence, but scientific claims
still require the independent default-on evidence-integrity workflow, raw data,
declared policy, convergence, and physical validation appropriate to the model.
