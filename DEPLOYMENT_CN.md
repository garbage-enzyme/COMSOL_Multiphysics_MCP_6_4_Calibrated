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
Test-Path "D:\path\to\python-env\Scripts\comsol-mcp-settings.exe"
```

wheel 公开 canonical `comsol_mcp` runtime 和 solver-free `settings_gui` 应用；仓库源码中的
`src` compatibility namespace 不会安装。可移植部署应配置安装后的 server console entry
point 绝对路径。

## 2. 使用设置界面配置（推荐）

普通用户应在启动 MCP client 前打开设置界面；若 agent 已连接，也可以让它只调用一次
`settings.start`：

```powershell
comsol-mcp-settings
# 需要时，把界面绑定到 MCP client 使用的同一份明确设置文件：
comsol-mcp-settings --settings-path "D:\settings\settings.json"
```

在界面中选择 profile 与功能开关，检查自动发现的 COMSOL/Java 路径，并设置 runtime、
模型读取和 artifact 目录。保存并关闭界面后，重启真正拥有 MCP 的 client。界面和所有
agent 修改的是同一份 `settings.json`，不存在每个 agent 各自的设置文件。每个字段的
含义、默认值和可接受值见[设置指南](docs/setting_guide/README_CN.md)。

直接编辑 JSON 是面向开发者、自动部署和获得用户明确授权的 agent 的高级路径。普通用户
不需要手工修改仓库模板。

### 开发者和 agent 的 JSON 配置（高级）

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

安装包尚无持久设置文件时，`capabilities` 会报告：

```json
{
  "setup_required": true,
  "configuration_source": "bundled_template",
  "setup_methods": ["settings.start", "agent_edit"],
  "restart_required_after_change": true
}
```

对普通用户，只调用一次 `settings.start`，说明启动设置需要重启 Codex 或所属 MCP client，
然后停止继续输出并等待用户下一条消息；GUI 打开期间不要直接修改设置。只有用户明确要求
时才由 agent 编辑 JSON；只修改解析出的可写文件，验证后请求重启。GUI 默认写入
`%LOCALAPPDATA%/comsol_mcp/settings.json`，包内文件保持只读
模板。`comsol-mcp-settings` 是直接命令行备用入口。MCP 响应会要求 agent 暂停，但无法从
技术上强制任意第三方 agent 遵守。

安装版应把独立 GUI 可执行文件绑定到实际共享设置文件；即使所有 MCP stdio host 都已停止，
它也能工作：

```powershell
comsol-mcp-settings --settings-path "D:\settings\settings.json" --validate-only
comsol-mcp-settings --settings-path "D:\settings\settings.json"
comsol-mcp-settings --settings-path "D:\settings\settings.json" --create-desktop-shortcut
comsol-mcp-settings --settings-path "D:\settings\settings.json" --remove-desktop-shortcut
```

每用户快捷方式名为 `COMSOL MCP Settings.lnk`，只能由用户明确创建。安装、部署、启动、首次
打开、“保存”或“应用”都不会自动创建。外来同名项目会保留；只有用户在 GUI 中确认，或在
创建命令中明确追加 `--replace-existing-shortcut` 后才会替换。

用户确认首次设置后，支持 Unicode 的模型读取目录创建在 `%LOCALAPPDATA%/comsol_mcp`；
必须仅含 ASCII 字符的 runtime 和自有 artifact 目录创建在 `%PROGRAMDATA%/comsol_mcp`。
可选资产保持未设置。

选择 profile 前，先确定仿真怎样运行。独立的
[五种运行方式指南](docs/simulation_execution_modes/README_CN.md)区分 `interactive`、
`inline`、`launcher`、`standalone` 和 `mphonly`。跨设备或云端运行前，必须先确认
目标操作系统、COMSOL 版本与模块、许可证、调度器、存储和输出要求。

## 3. 选择 Profile

| Profile | 用途 |
| --- | --- |
| `core` | 紧凑默认控制面和词法手册检索。 |
| `basic_fem` | 常规 FEM 构建、有界导出，以及无需 Python 的独立启动器工具。 |
| `wave_optics` | 周期光学、超表面、有界场数据发现/提取、预检和证据审计。 |
| `experimental` | 显式选择的通用和 escape-hatch 工具。 |
| `full` | 宽泛的非 feature 兼容界面；默认不推荐。 |

普通用户在设置界面选择 profile。开发者和 agent 可以在 JSON 的 `profile.name` 中设置
等价值；删除时使用 `core`。stdio 进程启动时会冻结 profile，修改后必须重启 client/MCP
host。非法 profile 保持 `core`，并在 `settings_errors` 中报告，不会静默选择另一个 profile。

Profile 只控制 COMSOL 自动化仿真及未来自主探索工具的可见性。在设置界面中，独立功能
开关可与任意 profile 组合，也可彼此组合。高级 JSON 等价值是用于受保护 shared workflow
的 `shared_server.enabled=true`，以及用于隔离语义检索的
`semantic_docs.enabled=true`；两个开关默认均为 false。旧 `comsol_connect` 仍是
experimental 兼容工具，不能替代该生命周期。

`basic_fem` 中的独立启动器工具仍运行在普通 Python MCP host 中；它们负责构建和控制另一个原生
EXE。目标机只需 Windows 10/11 x64 与已安装并授权的 COMSOL 6.4。EXE 不打包 COMSOL，
也不要求目标机安装 Python、Conda、MPh、JPype 或外部 Java。
构建步骤使用 Windows 随系统提供的 `.NET Framework 4.x` 64 位编译器
`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`；不需要另装现代
`.NET Runtime`、`.NET SDK` 或 Visual Studio，也不需要下载或联网。系统内置编译器缺失时
会直接 fail closed。

### 可选的 shared Desktop/attached-Server 模式

shared-server 功能不会启动、停止或终止用户的 COMSOL Server。请先手动启动带 persistent
multi-client 选项的 COMSOL Multiphysics Server 6.4，记录本地 endpoint（通常是 2036
端口），再让 Desktop client 连接该 Server。在设置界面的“工具配置”页开启交互式共享
Server 协作。等价的高级 JSON 配置如下：

```json
{
  "profile": { "name": "core" },
  "shared_server": { "enabled": true },
  "runtime": { "directory": "D:/comsol_mcp_runtime" }
}
```

重启 MCP host 后调用 `capabilities`，确认所选 live profile 未改变、`enabled_features`
包含 `shared_server`，且 `shared_session.feature_enabled` 与 `shared_session.gate_open` 都是 true。在
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

如果使用 shared Desktop，还要确认 `capabilities` 报告的所选 profile 未改变，并且只有在
独立 feature flag 开启后才出现 shared-session 工具。先启动并连接 Desktop/Server，再调用
`shared_server_preflight` 和带显式用户确认的 `shared_server_attach`。此模式不要调用
`comsol_start`，也不要把成功 attach 理解为可以并行执行模型修改。

## 9. 更新安装

源码变化后：

```powershell
D:\path\to\python-env\python.exe -m pip install . --no-deps
```

重启准确的 MCP host，并使用 `capabilities.deployment_identity` 验证安装包和
profile 确实是目标修订。
