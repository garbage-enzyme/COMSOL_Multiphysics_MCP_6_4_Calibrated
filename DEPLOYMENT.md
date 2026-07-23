# COMSOL MCP deployment guide

This guide covers a fresh COMSOL MCP installation and client configuration for
Claude Code, Hermes Agent, Codex CLI, and opencode. Replace every example path
with the target machine's actual paths.

Client acceptance status:

- Codex CLI and opencode have completed local installed-package validation.
- Claude Code and Hermes Agent are expected to be compatible from their
  documented stdio MCP configuration, but neither client has been tested
  end-to-end by this project. Testing reports and pull requests are welcome.

## 1. Server installation

Requirements:

- COMSOL Multiphysics 6.4.0.* (licensed reference acceptance is pinned to
  6.4.0.293; a third numeric component change is a separate release family);
- standard GIL-enabled Python 3.14 in an ASCII-only environment path;
- COMSOL's Java runtime for the verified local configuration.

Perform a non-editable install:

```powershell
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
Set-Location .\COMSOL_Multiphysics_MCP_6_4_Calibrated
D:\path\to\python-env\python.exe -m pip install .
Test-Path "D:\path\to\python-env\Scripts\comsol-mcp.exe"
```

Do not depend on a repository-relative `python -m src.server` command for a
portable deployment. Configure the absolute installed console entry point.

## 2. Configure the shared settings file

Edit the checked-in project-root [`settings.json`](settings.json) before starting
any client. It is the single source for profile, runtime/job roots, model and
artifact containment, shared-server enablement, evidence checks, semantic-doc
paths, ownership label, and optional COMSOL Java paths. Keep the same file for
every agent. See the [settings guide](docs/setting_guide/README.md) for every
field's meaning, default, and accepted values.

The template contains every setting and its default. Removing a field restores
that field's safe default. An illegal value restores only that field's default
and is reported in `settings_errors`; malformed JSON restores the complete safe
default document. Call `capabilities` or `evidence_integrity_status` after
startup and inspect `project_settings.configuration_state` and
`project_settings.settings_errors`.

For example, edit only the relevant grouped entries for a Wave Optics deployment:

```json
{
  "profile": { "name": "wave_optics" },
  "runtime": { "directory": "D:/comsol_runtime" },
  "paths": {
    "model_read_roots": ["D:/comsol_models"],
    "artifact_write_root": "D:/comsol_runtime/owned_artifacts"
  },
  "java": {
    "java_home": "D:/COMSOL64/Multiphysics/java/win64/jre",
    "jdk_home": "D:/COMSOL64/Multiphysics/java/win64/jre"
  }
}
```

The snippet is a partial edit, not a replacement document: keep the schema fields
and other required settings from the repository template. If a client does not preserve the
project path, pass one absolute locator variable and no per-setting variables:

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

The old individual `COMSOL_MCP_*`, `COMSOL_SEMANTIC_*`, and Java variables remain
one-release compatibility overrides, but are not needed for a normal deployment
and are omitted from the checked-in examples.

## 3. Select a profile

| Profile | Intended use |
| --- | --- |
| `core` | Compact default control plane and lexical manuals. |
| `basic_fem` | Conventional FEM construction and bounded exports. |
| `wave_optics` | Periodic optics, metasurfaces, bounded field discovery/extraction, preflight, and evidence audits. |
| `desktop_shared` | Default-off shared Desktop/attached-Server workflow with exact process/listener/model identity, non-owning leases, revision locks, durable attached jobs, and detach preservation. |
| `semantic_docs` | Isolated experimental semantic manual retrieval. |
| `experimental` | Explicit opt-in generic and escape-hatch tools. |
| `full` | Broad compatibility surface; not recommended by default. |

Set `profile.name` in `settings.json`. Omitting the entry selects `core`. The
profile is frozen when the stdio process starts; changing it requires a
client/MCP-host restart. An invalid profile keeps `core` and is reported in
`settings_errors` instead of silently selecting another profile.

The default `core` and `wave_optics` profiles do not expose shared-session tools.
Enable the protected workflow only with the explicit `desktop_shared` profile and
`shared_server.enabled=true` in `settings.json`. The legacy `comsol_connect` compatibility tool
remains experimental and is not a substitute for this lifecycle.

### Optional shared Desktop/attached-Server mode

The shared profile never starts, stops, or terminates the user's COMSOL Server.
Start COMSOL Multiphysics Server 6.4 manually with its persistent multi-client
option, record the local endpoint (normally port 2036), and connect the Desktop
client to that Server. Then edit the MCP settings with:

```json
{
  "profile": { "name": "desktop_shared" },
  "shared_server": { "enabled": true },
  "runtime": { "directory": "D:/comsol_mcp_runtime" }
}
```

After restarting the MCP host, call `capabilities` and verify the live
`desktop_shared` profile. Call `shared_server_preflight` before
`shared_server_attach`; pass `user_confirmed=true` only after the endpoint and
Desktop connection are correct. The attach path requires one exact 6.4.0.*
Server identity and one exact server-held model. It rejects starting/unready
servers, multiple GUI clients, ambiguous models, PID reuse, mixed release
families, and unclassified COMSOL/MPh processes without guessing.

The Desktop lower-left `localhost:2036` cue is useful user evidence, but it does
not replace process/listener identity checks. While MCP holds an
`automation_exclusive` lock, COMSOL may display an occupied-model warning and
disable GUI editing; this is expected. Unlock before detach. Detach preserves
the user-owned Server, listener, Desktop, model, and result; MCP does not call
`clear()` or shut down the external Server.

## 4. Claude Code (theoretical compatibility; not yet tested)

Claude Code officially supports local stdio MCP servers through `claude mcp
add`, user/local configuration in `~/.claude.json`, or a project-scoped
`.mcp.json`. The checked-in template is inactive until copied and edited:

```powershell
Copy-Item .\config\claude-code-mcp.example.json .\.mcp.json
# Replace every example path in .mcp.json with an absolute local path.
claude mcp list
claude mcp get comsol
```

Claude Code asks for approval before using a project-scoped `.mcp.json`. Do not
commit machine-specific executable, Java, runtime, credential, or model paths to
a shared project.

For a private user-scoped configuration instead of `.mcp.json`:

```powershell
claude mcp add --transport stdio --scope user `
  --env COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json `
  comsol -- 'D:\path\to\python-env\Scripts\comsol-mcp.exe'
```

All Claude options must precede the server name, and `--` separates the server
name from its executable and arguments. Use the in-session `/mcp` panel to
inspect connection status. The configuration deliberately uses an absolute
installed executable and an ASCII runtime root; it does not depend on Claude
Code's launch directory.

Claude Code's documented `.mcp.json` format has no project-specific field used
here to disable parallel calls. In the project `CLAUDE.md` or companion skill,
instruct Claude to call `capabilities`, `solver_status`, and `solver_preflight`
first and never issue COMSOL mutation or solver operations in parallel.

Complete template: `config/claude-code-mcp.example.json`. This configuration is
derived from the official [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp),
but remains untested with the real Claude Code client. Please submit a PR with a
sanitized `initialize`, `list_tools`, `capabilities`, status, and cleanup receipt
if you validate it.

## 5. Hermes Agent (theoretical compatibility; not yet tested)

Native Windows Hermes stores its default configuration at
`%LOCALAPPDATA%\hermes\config.yaml`. Linux and WSL use
`~/.hermes/config.yaml`.

```yaml
mcp_servers:
  comsol:
    command: "D:/path/to/python-env/Scripts/comsol-mcp.exe"
    args: []
    env:
      COMSOL_MCP_SETTINGS_PATH: "D:/path/to/COMSOL_Multiphysics_MCP/settings.json"
    connect_timeout: 120
    timeout: 3600
    supports_parallel_tool_calls: false
    idle_timeout_seconds: 0
    max_lifetime_seconds: 0
```

Hermes' documented stdio launcher passes `command`, `args`, and `env`, but does
not provide a working directory to the child process. Keep
`supports_parallel_tool_calls` false: COMSOL ownership and mutation must remain
serialized. Native Windows is the expected configuration for a Windows COMSOL
installation; neither Hermes end-to-end operation nor a WSL-to-Windows COMSOL
bridge has been tested by this project. Test reports and pull requests with
sanitized discovery and cleanup receipts are welcome.

Complete template: `config/hermes-mcp.example.yaml`.

## 6. Codex CLI

Windows configuration: `%USERPROFILE%\.codex\config.toml`.
POSIX configuration: `~/.codex/config.toml`.

```toml
[mcp_servers.comsol]
command = 'D:\path\to\python-env\Scripts\comsol-mcp.exe'
args = []

[mcp_servers.comsol.env]
COMSOL_MCP_SETTINGS_PATH = 'D:\path\to\COMSOL_Multiphysics_MCP\settings.json'
```

Complete template: `config/codex-mcp.example.toml`.

## 7. opencode

Use a project `opencode.json`, or merge the entry into
`~/.config/opencode/opencode.json`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["D:\\path\\to\\python-env\\Scripts\\comsol-mcp.exe"],
      "environment": {
        "COMSOL_MCP_SETTINGS_PATH": "D:\\path\\to\\COMSOL_Multiphysics_MCP\\settings.json"
      }
    }
  }
}
```

Complete template: `config/opencode-mcp.example.json`.

## 8. Restart and verify

Restart Claude Code, Hermes, Codex, or opencode after changing the profile,
executable path, or installed package. Existing stdio hosts do not hot-load
these changes.

Call `capabilities` before starting COMSOL. A `wave_optics` deployment should
report:

```text
profile = wave_optics
active_profile = wave_optics
```

Use the returned registered-tool list and deployment hashes as authority; do
not compare against a tool count copied from this guide.

Then call `solver_status` and `solver_preflight` before constructing a client.
Keep one solver owner. Use durable jobs for long simulations instead of holding a
single synchronous MCP call for the full wall time.

For a local stand-alone session, `comsol_start` returns an accepted response
before solver preflight, MPh import, or JPype JVM initialization. Poll
`comsol_status`; it exposes the bounded startup phases also persisted under the
configured runtime root.
The JVM may remain embedded in the MCP Python process, so the absence of a
separate COMSOL child process is not by itself a startup failure.

MPh permits only one client wrapper per Python process. Therefore
`comsol_disconnect` clears models and releases the solver lease but retains the
exact stand-alone wrapper for a later same-host `comsol_start`; it never creates
a second client. A 180-second startup timeout is terminal to callers. If the
native constructor is still blocked, status reports `cleanup_pending=true` and
the owned lease remains held until that call returns and cleanup is verified.
Do not retry start or restart the MCP host while cleanup is pending.

For `desktop_shared`, verify that `capabilities` reports the shared profile and
that shared-session tools are present only after the feature flag is enabled.
Start and connect Desktop/Server first, then call `shared_server_preflight` and
`shared_server_attach` with explicit user confirmation. Do not call
`comsol_start` in this mode, and do not treat a successful attach as permission
to run parallel model mutations.

## 9. Updating an installation

After source changes:

```powershell
D:\path\to\python-env\python.exe -m pip install . --no-deps
```

Restart the exact MCP host and use `capabilities.deployment_identity` to verify
that the installed package and profile are the intended revision.
