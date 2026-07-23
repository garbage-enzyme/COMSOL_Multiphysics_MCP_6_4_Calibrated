# COMSOL MCP 部署指南

本指南覆盖 COMSOL MCP 的全新安装，以及 Claude Code、Hermes Agent、Codex CLI
和 opencode 配置。所有示例路径都必须替换为目标机器的实际路径。

Client 验收状态：

- Codex CLI 和 opencode 已完成本机 installed-package 验证。
- Claude Code 和 Hermes Agent 按其公开的 stdio MCP 配置在理论上兼容，但本项目
  尚未对两者进行端到端测试；欢迎提交测试结果和 PR。

## 1. 安装 Server

要求：

- COMSOL Multiphysics 6.4.0.*（licensed reference acceptance 固定于 6.4.0.293；
  第三位数字变化视为新的 release family）；
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

## 2. 配置统一 settings.json

启动任何 client 前，编辑项目根目录的 [`settings.json`](settings.json)。它是 profile、
runtime/jobs、模型读取和 artifact containment、shared-server 开关、证据检查、语义
手册路径、owner label 以及可选 COMSOL Java 路径的唯一设置来源。Codex、opencode、
Claude Code 和 Hermes 都使用同一个文件。每个字段的含义、默认值和可接受值见
[设置指南](docs/setting_guide/README_CN.md)。

模板列出所有设置和默认值。用户删去设置条目时，该条目使用安全默认值；输入非法值时，
仅该条目回退默认值，并在 `settings_errors` 中报错；JSON 整体损坏时回退完整安全默认
文件并报错。启动后调用 `capabilities` 或 `evidence_integrity_status`，检查
`project_settings.configuration_state` 和 `project_settings.settings_errors`。

例如，Wave Optics 可按功能修改这些条目（这是 partial edit，不要用它替换完整模板）：

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

如果 client 不保留项目路径，只传入一个统一的绝对路径定位变量：

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

旧的 `COMSOL_MCP_*`、`COMSOL_SEMANTIC_*` 和 Java 环境变量仍保留一个 release 的
兼容覆盖能力，但正常部署不需要它们，提交的 client 示例也已移除。

## 3. 选择 Profile

| Profile | 用途 |
| --- | --- |
| `core` | 紧凑默认控制面和词法手册检索。 |
| `basic_fem` | 常规 FEM 构建和有界导出。 |
| `wave_optics` | 周期光学、超表面、有界场数据发现/提取、预检和证据审计。 |
| `desktop_shared` | 默认关闭的 shared Desktop/attached-Server 工作流，提供精确进程/listener/model 身份、非拥有式租约、revision lock、持久化 attached job 和 detach preservation。 |
| `semantic_docs` | 隔离的实验性语义手册检索。 |
| `experimental` | 显式选择的通用和 escape-hatch 工具。 |
| `full` | 宽兼容界面；默认不推荐。 |

在 `settings.json` 的 `profile.name` 中设置 profile。删除时使用 `core`。stdio 进程
启动时会冻结 profile，修改后必须重启 client/MCP host。非法 profile 保持 `core`，并在
`settings_errors` 中报告，不会静默选择另一个 profile。

默认的 `core` 和 `wave_optics` profile 不暴露 shared-session 工具。
只有显式选择 `desktop_shared` profile 并在 `settings.json` 设置
`shared_server.enabled=true`，才会启用受保护
的 shared workflow。旧 `comsol_connect` 仍是 experimental 兼容工具，不能替代该生命周期。

### 可选的 shared Desktop/attached-Server 模式

shared profile 不会启动、停止或终止用户的 COMSOL Server。请先手动启动带 persistent
multi-client 选项的 COMSOL Multiphysics Server 6.4，记录本地 endpoint（通常是 2036
端口），再让 Desktop client 连接该 Server。然后编辑 MCP settings：

```json
{
  "profile": { "name": "desktop_shared" },
  "shared_server": { "enabled": true },
  "runtime": { "directory": "D:/comsol_mcp_runtime" }
}
```

重启 MCP host 后调用 `capabilities`，确认 live profile 是 `desktop_shared`。在
`shared_server_attach` 前调用 `shared_server_preflight`；只有确认 endpoint 和 Desktop
连接正确后，才传入 `user_confirmed=true`。attach 要求一个精确的 6.4.0.* Server 身份
和一个精确的 Server-held model；对于启动中/未就绪 Server、多个 GUI client、歧义模型、
PID reuse、混合 release family 和未分类 COMSOL/MPh 进程，都会拒绝并 fail closed，不会猜测。

Desktop 左下角的 `localhost:2036` 提示可作为用户观察证据，但不能替代进程/listener
身份检查。MCP 持有 `automation_exclusive` lock 时，COMSOL 可能显示占用模型警告并禁用
GUI 编辑，这是预期行为。detach 前先 unlock；detach 会保留用户的 Server、listener、
Desktop、model 和 result，MCP 不会调用 `clear()` 或关闭外部 Server。

## 4. Claude Code（理论兼容，尚未测试）

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
  --env COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json `
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

## 5. Hermes Agent（理论兼容，尚未测试）

Hermes native Windows 默认配置文件：
`%LOCALAPPDATA%\hermes\config.yaml`。Linux 和 WSL 使用
`~/.hermes/config.yaml`。

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

Hermes 文档中的 stdio launcher 会传递 `command`、`args` 和 `env`，但不会给
子进程提供工作目录。保持 `supports_parallel_tool_calls: false`：COMSOL ownership
和模型修改必须串行。Windows COMSOL 理论上应搭配 Hermes native Windows；本项目
既未完成 Hermes 端到端测试，也未验证 WSL 到 Windows COMSOL bridge。欢迎提交带
有脱敏 discovery 和 cleanup receipt 的测试结果及 PR。

完整模板：`config/hermes-mcp.example.yaml`。

## 6. Codex CLI

Windows 配置文件：`%USERPROFILE%\.codex\config.toml`。
POSIX 配置文件：`~/.codex/config.toml`。

```toml
[mcp_servers.comsol]
command = 'D:\path\to\python-env\Scripts\comsol-mcp.exe'
args = []

[mcp_servers.comsol.env]
COMSOL_MCP_SETTINGS_PATH = 'D:\path\to\COMSOL_Multiphysics_MCP\settings.json'
```

完整模板：`config/codex-mcp.example.toml`。

## 7. opencode

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
        "COMSOL_MCP_SETTINGS_PATH": "D:\\path\\to\\COMSOL_Multiphysics_MCP\\settings.json"
      }
    }
  }
}
```

完整模板：`config/opencode-mcp.example.json`。

## 8. 重启与验证

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

对于本地 stand-alone session，`comsol_start` 会先返回 accepted 响应，再执行 solver
preflight、MPh import 和 JPype JVM 初始化。随后轮询 `comsol_status`；它会返回有界启动
阶段，同一状态也持久化在配置的 runtime root 下。JVM 可能嵌入 MCP Python 进程，
因此没有单独的 COMSOL child process 本身不代表启动失败。

MPh 每个 Python 进程只允许一个 client wrapper。因此 `comsol_disconnect` 会清除
模型并释放 solver lease，但保留同一个 stand-alone wrapper，供同一 host 后续
`comsol_start` 复用；绝不创建第二个 client。启动超过 180 秒后，对调用方进入终态。
若 native constructor 仍被阻塞，状态会报告 `cleanup_pending=true`，并继续持有 owned
lease，直到该调用返回且清理得到验证。cleanup pending 时不要重试 start，也不要重启
MCP host。

如果使用 `desktop_shared`，还要确认 `capabilities` 报告 shared profile，并且只有在
feature flag 开启后才出现 shared-session 工具。先启动并连接 Desktop/Server，再调用
`shared_server_preflight` 和带显式用户确认的 `shared_server_attach`。此模式不要调用
`comsol_start`，也不要把成功 attach 理解为可以并行执行模型修改。

## 9. 更新安装

源码变化后：

```powershell
D:\path\to\python-env\python.exe -m pip install . --no-deps
```

重启准确的 MCP host，并使用 `capabilities.deployment_identity` 验证安装包和
profile 确实是目标修订。
