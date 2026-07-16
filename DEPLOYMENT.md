# COMSOL MCP deployment guide

[中文](DEPLOYMENT_CN.md)

This guide covers a fresh COMSOL MCP installation and client configuration for
Hermes Agent, Codex CLI, and opencode. Replace every example path with the target
machine's actual paths.

## 1. Server installation

Requirements:

- COMSOL Multiphysics 6.4+;
- Python 3.10+ in an ASCII-only environment path;
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

| Profile | Tools | Intended use |
| --- | ---: | --- |
| `core` | 38 | Compact default control plane and lexical manuals. |
| `basic_fem` | 76 | Conventional FEM construction and bounded exports. |
| `wave_optics` | 62 | Periodic optics, metasurfaces, field-dataset discovery, preflight, and evidence audits. |
| `semantic_docs` | 41 | Isolated experimental semantic manual retrieval. |
| `experimental` | 64 | Explicit opt-in generic and escape-hatch tools. |
| `full` | 119 | Broad compatibility surface; not recommended by default. |

Set `COMSOL_MCP_PROFILE` in the client's server environment. Omitting it selects
`core`. The profile is frozen when the stdio process starts; changing it requires
a client/MCP-host restart. An invalid profile fails startup instead of silently
falling back.

## 3. Hermes Agent

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

Hermes' stdio launcher passes `command`, `args`, and `env`, but does not provide a
working directory to the child process. Keep `supports_parallel_tool_calls` false:
COMSOL ownership and mutation must remain serialized. Native Windows is the
documented configuration for a Windows COMSOL installation; this project does
not claim that a WSL-to-Windows COMSOL bridge has been validated.

Complete template: `config/hermes-mcp.example.yaml`.

## 4. Codex CLI

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

## 5. opencode

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

## 6. Restart and verify

Restart Hermes, Codex, or opencode after changing the profile, executable path,
or installed package. Existing stdio hosts do not hot-load these changes.

Call `capabilities` before starting COMSOL. A `wave_optics` deployment should
report:

```text
profile = wave_optics
active_profile = wave_optics
tool_count = 62
```

Then call `solver_status` and `solver_preflight` before constructing a client.
Keep one solver owner. Use durable jobs for long simulations instead of holding a
single synchronous MCP call for the full wall time.

## 7. Updating an installation

After source changes:

```powershell
D:\path\to\python-env\python.exe -m pip install . --no-deps
```

Restart the exact MCP host and use `capabilities.deployment_identity` to verify
that the installed package and profile are the intended revision.
