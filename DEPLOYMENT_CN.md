# COMSOL MCP 部署指南

[English](DEPLOYMENT.md)

本指南覆盖 COMSOL MCP 的全新安装，以及 Claude Code、Hermes Agent、Codex CLI
和 opencode 配置。所有示例路径都必须替换为目标机器的实际路径。

Client 验收状态：

- Codex CLI 和 opencode 已完成本机 installed-package 验证。
- Claude Code 和 Hermes Agent 按其公开的 stdio MCP 配置在理论上兼容，但本项目
  尚未对两者进行端到端测试；欢迎提交测试结果和 PR。

## 1. 安装 Server

要求：

- COMSOL Multiphysics 6.4（licensed acceptance 固定于 6.4.0.293；其他 build
  需要单独验证）；
- 标准 GIL 版本的 Python 3.14，环境路径只使用 ASCII 字符；
- 已验证本机配置所需的 COMSOL Java runtime。

执行非 editable 安装：

```powershell
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
Set-Location .\COMSOL_Multiphysics_MCP_6_4_Calibrated
D:\path\to\python-env\python.exe -m pip install .
Test-Path "D:\path\to\python-env\Scripts\comsol-mcp.exe"
```

可移植部署不要依赖仓库 cwd 下的 `python -m src.server`，而应配置安装后的
console entry point 绝对路径。

## 2. 选择 Profile

| Profile | 用途 |
| --- | --- |
| `core` | 紧凑默认控制面和词法手册检索。 |
| `basic_fem` | 常规 FEM 构建和有界导出。 |
| `wave_optics` | 周期光学、超表面、有界场数据发现/提取、预检和证据审计。 |
| `semantic_docs` | 隔离的实验性语义手册检索。 |
| `experimental` | 显式选择的通用和 escape-hatch 工具。 |
| `full` | 宽兼容界面；默认不推荐。 |

在 client 的 server environment 中设置 `COMSOL_MCP_PROFILE`。省略时使用
`core`。stdio 进程启动时会冻结 profile，修改后必须重启 client/MCP host。非法
profile 会使启动失败，不会静默回退。

当前没有任何 profile 提供受保护的共享 Desktop/attached-Server 模式。
experimental 中的旧 `comsol_connect` 兼容工具不具备 non-owning 共享模型生命周期，
不得按共享模式使用。

## 3. Claude Code（理论兼容，尚未测试）

Claude Code 官方支持通过 `claude mcp add` 添加本地 stdio MCP server，也支持
`~/.claude.json` 中的 user/local 配置和项目根目录 `.mcp.json`。仓库中的模板默认
不生效，复制并修改后才会被使用：

```powershell
Copy-Item .\config\claude-code-mcp.example.json .\.mcp.json
# 将 .mcp.json 中的所有示例路径替换为本机绝对路径。
claude mcp list
claude mcp get comsol
```

Claude Code 首次使用项目级 `.mcp.json` 时会要求用户批准。不要把本机 executable、
Java、runtime、凭据或模型路径提交到共享项目。

如果不使用 `.mcp.json`，可添加私有的 user-scope 配置：

```powershell
claude mcp add --transport stdio --scope user `
  --env COMSOL_MCP_PROFILE=wave_optics `
  --env COMSOL_MCP_RUNTIME_DIR=D:\comsol_mcp_runtime `
  --env JAVA_HOME=D:\COMSOL64\Multiphysics\java\win64\jre `
  --env JDK_HOME=D:\COMSOL64\Multiphysics\java\win64\jre `
  comsol -- 'D:\path\to\python-env\Scripts\comsol-mcp.exe'
```

所有 Claude 选项必须放在 server 名称前，`--` 用来分隔 server 名称和 executable/
arguments。会话内使用 `/mcp` 查看连接状态。该配置有意使用已安装 executable 的
绝对路径和 ASCII runtime 根目录，不依赖 Claude Code 的启动目录。

Claude Code 官方 `.mcp.json` 格式没有本项目可用的“禁止并行调用”字段。因此应在
项目 `CLAUDE.md` 或配套 skill 中要求 Claude 先调用 `capabilities`、
`solver_status` 和 `solver_preflight`，且绝不并行执行 COMSOL 修改或求解操作。

完整模板：`config/claude-code-mcp.example.json`。它依据 Claude Code 官方
[MCP 文档](https://code.claude.com/docs/en/mcp)编写，但尚未经过真实 Claude Code
client 测试。如果验证成功，欢迎提交包含脱敏 `initialize`、`list_tools`、
`capabilities`、status 和 cleanup receipt 的 PR。

## 4. Hermes Agent（理论兼容，尚未测试）

Hermes native Windows 默认配置文件：
`%LOCALAPPDATA%\hermes\config.yaml`。Linux 和 WSL 使用
`~/.hermes/config.yaml`。

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

Hermes 文档中的 stdio launcher 会传递 `command`、`args` 和 `env`，但不会给
子进程提供工作目录。保持 `supports_parallel_tool_calls: false`：COMSOL ownership
和模型修改必须串行。Windows COMSOL 理论上应搭配 Hermes native Windows；本项目
既未完成 Hermes 端到端测试，也未验证 WSL 到 Windows COMSOL bridge。欢迎提交带
有脱敏 discovery 和 cleanup receipt 的测试结果及 PR。

完整模板：`config/hermes-mcp.example.yaml`。

## 5. Codex CLI

Windows 配置文件：`%USERPROFILE%\.codex\config.toml`。
POSIX 配置文件：`~/.codex/config.toml`。

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

完整模板：`config/codex-mcp.example.toml`。

## 6. opencode

使用项目级 `opencode.json`，或合并到
`~/.config/opencode/opencode.json`。

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

完整模板：`config/opencode-mcp.example.json`。

## 7. 重启与验证

修改 profile、executable 路径或安装包后，重启 Claude Code、Hermes、Codex 或
opencode。已有 stdio host 不会热加载这些变化。

在启动 COMSOL 前调用 `capabilities`。`wave_optics` 部署应返回：

```text
profile = wave_optics
active_profile = wave_optics
```

以返回的注册工具列表和部署哈希为准，不要与本指南中复制的工具数量比较。

然后在构造 client 前调用 `solver_status` 和 `solver_preflight`。保持单一 solver
owner。长仿真使用 durable jobs，不要让单个同步 MCP call 持续占用全部 wall time。

## 8. 更新安装

源码变化后：

```powershell
D:\path\to\python-env\python.exe -m pip install . --no-deps
```

重启准确的 MCP host，并使用 `capabilities.deployment_identity` 验证安装包和
profile 确实是目标修订。
