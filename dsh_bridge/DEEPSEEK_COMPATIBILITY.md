# DeepSeek Harness compatibility contract

This is the versioned compatibility note for the optional
`@local/dsh-comsol-bridge` component in the COMSOL MCP repository. The
bridge is a repository-only Node.js client adapter; it is not a feature of the
Python `comsol-mcp` server.

## Version and support boundary

| Component | Current identity | Evidence |
| --- | --- | --- |
| Server package baseline | `0.7.3` | Python server unchanged by bridge repair |
| DSH acceptance host | `0.1.6-alpha.2` | real Web host, native controller and registered Agents |
| DSH bridge package | `@local/dsh-comsol-bridge` `0.1.0` | `package.json` |
| Node runtime | Node.js `>=20` | package engine declaration |
| Bridge dependencies | none | package metadata and CI |
| Bridge regression | 69/69 Node tests + smoke | solver-free fake-server lane |

Historically, the bridge was exercised against the installed `0.7.1` server for capabilities,
tool registration, durable-job mirroring, progress streaming, and completion
notification. This is transport/client evidence, not licensed COMSOL solve
acceptance for `0.7.2`.

On DSH `0.1.6-alpha.2`, running-job and offline-completion recovery passed
with two original sessions, delayed owner registration, isolated notices and
no resubmission, using an independent persistent fake MCP server. The harness
explicitly called the public `sessions.flush` before the final restart.
This verifies recovery after session persistence, not crash-proof delivery.
Native non-COMSOL background jobs also lose an unflushed notice on abrupt
restart, while a flushed notice survives. The accepted notification boundary
follows native session persistence; crash-proof acknowledgement is outside it.
Real-host negative checks also cover unavailable owners/controllers, legacy
rows, controller delay/loss, foreign progress/cancellation access, four terminal
outcomes, transport loss, state-write failure, stale attempts and owner disposal.

## Architecture contract

```text
DSH Cordis plugin -> one serialized MCP stdio connection
                   -> installed comsol-mcp executable
                   -> standard durable worker and evidence store
```

The bridge owns one connection and registers discovered tools with clean DSH
names. `job_submit` creates a `ctx.jobs` mirror; status and tail use the same
single-flight queue. The server's durable journal, lease, process identity,
cleanup, and evidence rules remain authoritative.

Do not configure a second COMSOL entry through `dsh-mcp-client` in the same DSH
session. Two clients would violate one-solver-owner and serialized-call rules.

## Installation and configuration

From the repository root:

```powershell
.\dsh_bridge\install.ps1
npm test --prefix dsh_bridge
npm run smoke --prefix dsh_bridge
```

Add one `comsol-bridge` entry to the DSH web profile. Use the absolute installed
executable, an ASCII runtime directory, and the shared settings locator:

```yaml
- insert:
    - id: comsol-bridge
      name: '@local/dsh-comsol-bridge'
      config:
        enabled: true
        command: 'D:\\condaenvs\\comsol-mcp-py314\\Scripts\\comsol-mcp.exe'
        args: []
        cwd: 'D:\\comsol_runtime'
        env:
          COMSOL_MCP_SETTINGS_PATH: 'D:\\comsol_runtime\\settings.json'
```

`config.enabled` is a DSH client-plugin switch. It must not be added to the
COMSOL Settings GUI or Python server settings schema. Profile or executable
changes require the DSH host lifecycle to reload/restart; do not assume HMR is
a server restart or a capabilities re-verification.

## Verification sequence

1. Run the solver-free Node tests and smoke command above.
2. Start DSH with only the bridge COMSOL entry.
3. Verify `capabilities` and discovered tool names before any solver call.
4. Verify `solver_status`/preflight and the configured ASCII runtime path.
5. For durable jobs, compare bridge notifications and `job_tail` with durable
   server state; never infer completion from CPU or disk activity.
6. After a profile/settings change, restart the owning DSH host and repeat
   capabilities and ownership checks. The user has independently accepted this
   settings-change path for the current deployment.

The fake-server suite proves framing, reconnect, serialization, bounded output,
cancellation ambiguity, and mirror recovery. It does not prove a COMSOL solve,
production cancellation, or a licensed COMSOL solve. Those require a separate
authorized acceptance receipt.

## Failure and recovery rules

- Missing executable, malformed discovery, unsupported MCP version, and
  exhausted reconnect budget fail closed.
- A lost bridge connection does not prove a server job stopped. Reconnect,
  inspect `job_status`, and use the server's `job_resume` contract.
- A cancel request is terminal only after a terminal server state and cleanup/
  lease evidence are available.
- Never replace the installed server while a solver lease, durable job, COMSOL
  process, or Java owner remains.
- Keep server evidence and bridge state; do not rewrite receipts to match a
  DSH notification.
- A server-terminal row found at startup still needs delivery to its original
  live Agent/controller. Missing owners or failed mirror starts retain the row
  for bounded retry or a later restart; never bind an arbitrary current session.
- Native job completion and session persistence are separate boundaries. A
  notification in memory is not proof that it survives an immediate crash.
- A known attempt that differs from current server status blocks recovery and
  retains its original identity. A mirror observing an attempt change settles
  unconfirmed and must not report the replacement execution as its own result.
- DSH `owner disposed` and `jobs service disposed` hook reasons detach the
  supervisor without sending server cancellation. Loss of the native controller
  also retains tracking. Explicit job cancellation remains a separate operation.

## Packaging and attribution

`dsh_bridge/` is excluded from the Python wheel and sdist. It does not alter
the standard server, public schemas, Settings GUI, production configuration,
solver ownership, or COMSOL version support matrix. The bridge is released and
tested independently from the Python package.

The maintained fork retains the upstream acknowledgement for
[commit `99172f8`](https://github.com/wjc9011/COMSOL_Multiphysics_MCP/commit/99172f8f43c6753c2442c406cd5c6055ea8c5bef).
