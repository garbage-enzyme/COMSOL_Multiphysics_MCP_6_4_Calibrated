# COMSOL MCP deployment guide

[中文](DEPLOYMENT_CN.md)

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

- COMSOL Multiphysics 6.4 (licensed acceptance is pinned to 6.4.0.293; other
  builds require separate validation);
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

## 2. Select a profile

| Profile | Intended use |
| --- | --- |
| `core` | Compact default control plane and lexical manuals. |
| `basic_fem` | Conventional FEM construction and bounded exports. |
| `wave_optics` | Periodic optics, metasurfaces, bounded field discovery/extraction, preflight, and evidence audits. |
| `semantic_docs` | Isolated experimental semantic manual retrieval. |
| `experimental` | Explicit opt-in generic and escape-hatch tools. |
| `full` | Broad compatibility surface; not recommended by default. |

Set `COMSOL_MCP_PROFILE` in the client's server environment. Omitting it selects
`core`. The profile is frozen when the stdio process starts; changing it requires
a client/MCP-host restart. An invalid profile fails startup instead of silently
falling back.

No current profile provides a protected shared Desktop/attached-Server mode.
The experimental `comsol_connect` compatibility tool is not a non-owning shared-
model lifecycle and must not be used as one.

## 3. Claude Code (theoretical compatibility; not yet tested)

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
  --env COMSOL_MCP_PROFILE=wave_optics `
  --env COMSOL_MCP_RUNTIME_DIR=D:\comsol_mcp_runtime `
  --env JAVA_HOME=D:\COMSOL64\Multiphysics\java\win64\jre `
  --env JDK_HOME=D:\COMSOL64\Multiphysics\java\win64\jre `
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

## 4. Hermes Agent (theoretical compatibility; not yet tested)

Native Windows Hermes stores its default configuration at
`%LOCALAPPDATA%\hermes\config.yaml`. Linux and WSL use
`~/.hermes/config.yaml`.

```yaml
mcp_servers:
  comsol:
    command: "D:/path/to/python-env/Scripts/comsol-mcp.exe"
    args: []
    env:
      COMSOL_MCP_PROFILE: "wave_optics"
      COMSOL_MCP_RUNTIME_DIR: "D:/comsol_mcp_runtime"
      JAVA_HOME: "D:/COMSOL64/Multiphysics/java/win64/jre"
      JDK_HOME: "D:/COMSOL64/Multiphysics/java/win64/jre"
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

## 5. Codex CLI

Windows configuration: `%USERPROFILE%\.codex\config.toml`.
POSIX configuration: `~/.codex/config.toml`.

```toml
[mcp_servers.comsol]
command = 'D:\path\to\python-env\Scripts\comsol-mcp.exe'
args = []

[mcp_servers.comsol.env]
COMSOL_MCP_PROFILE = "wave_optics"
COMSOL_MCP_RUNTIME_DIR = 'D:\comsol_mcp_runtime'
JAVA_HOME = 'D:\COMSOL64\Multiphysics\java\win64\jre'
JDK_HOME = 'D:\COMSOL64\Multiphysics\java\win64\jre'
```

Complete template: `config/codex-mcp.example.toml`.

## 6. opencode

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
        "COMSOL_MCP_PROFILE": "wave_optics",
        "COMSOL_MCP_RUNTIME_DIR": "D:\\comsol_mcp_runtime",
        "JAVA_HOME": "D:\\COMSOL64\\Multiphysics\\java\\win64\\jre",
        "JDK_HOME": "D:\\COMSOL64\\Multiphysics\\java\\win64\\jre"
      }
    }
  }
}
```

Complete template: `config/opencode-mcp.example.json`.

## 7. Restart and verify

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

## 8. Updating an installation

After source changes:

```powershell
D:\path\to\python-env\python.exe -m pip install . --no-deps
```

Restart the exact MCP host and use `capabilities.deployment_identity` to verify
that the installed package and profile are the intended revision.
