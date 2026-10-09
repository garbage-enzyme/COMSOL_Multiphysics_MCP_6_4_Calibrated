# Ubuntu 实验性支持

0.7.7 增加 Ubuntu 26.04 x64 的无求解器实验通道。
本机验收使用 WSL2。独立托管验收使用 GitHub Ubuntu runner。
这些检查不能证明 Linux COMSOL 支持，也不能证明原生 agent 互操作性。
Windows 仍是已验收的原生求解平台。

## 支持范围

可使用离线模型检查、证据读取、手册关键词检索、工具发现和共用 Tk 设置 GUI。
服务保留相同的 profile、工具名称和设置 schema。
选择操作前，读取 `capabilities` 的 `platform_support` 和 `available_tools`。
目录中可见的原生求解工具不一定可在 Linux 上执行。
原生执行、共享 Server 连接、Model Manager 操作和生产仿真任务会在启动求解器前拒绝。
拒绝结果包含 `unsupported_platform`，并明确没有启动求解器或修改文件系统。

Linux 语义检索验收延期。
可读取冷态 `semantic_status`。预热请求和语义执行不可用。
内部假工作进程用于验证 Tasks 和持久状态。公开客户端不能提交假仿真。
2026-07-28 核心协议使用逐请求元数据，不使用 initialize 握手。
旧协议继续使用 initialize 协商和普通工具调用。
任务 TTL 不会删除运行任务或证据。当前实现也在 TTL 后保留已完成任务的映射。
SDK 符合性不能证明 Claude 或 OpenCode 原生兼容性。

## 安装

使用 Python 3.14，并选择较短的 ASCII 环境路径。
用 `--require-hashes --only-binary=:all:` 应用 `constraints/release_locked_ubuntu_py314.txt`。
用 `--no-deps` 安装经过审核的精确项目 wheel。
在仓库外执行 `python -m pip check`。
使用同一解释器运行已安装包和 stdio 探针。
Ubuntu 锁保留已接受的运行依赖版本，只排除 Windows 专用的 `pywin32`。
`manuals` extra 提供 PDF 关键词索引，不包含 COMSOL 手册。
使用本机有权使用的手册。不要分发 PDF 或生成的私有证据。

## 路径和设置

| 用途 | 默认值 |
| --- | --- |
| 设置 | `$XDG_CONFIG_HOME/comsol-mcp/settings.json`，否则为 `~/.config/comsol-mcp/settings.json` |
| 运行目录 | `$XDG_STATE_HOME/comsol-mcp/runtime`，否则为 `~/.local/state/comsol-mcp/runtime` |
| 模型 | `$XDG_DATA_HOME/comsol-mcp/models`，否则为 `~/.local/share/comsol-mcp/models` |
| 产物 | `$XDG_DATA_HOME/comsol-mcp/artifacts`，否则为 `~/.local/share/comsol-mcp/artifacts` |
| 可选启动器 | `$XDG_DATA_HOME/applications/comsol-mcp-settings.desktop` |

XDG 覆盖值必须是绝对路径。运行和产物目录必须仅含 ASCII 字符。
设置文件和允许的模型路径继续支持 Unicode。
显式配置和环境覆盖保留原有优先级。
随包提供的 Windows 默认占位符在 Linux 上选择原生默认值。
其他已配置 Windows 路径不会被猜测转换为 Linux 路径。
无效根目录会报告配置错误，不会自动改用临时目录。

## GUI 和所有权

图形启动器检查需要 Tk、中文字体、`desktop-file-utils` 和 `gio`。
CI 使用 Xvfb。本机视觉验收使用 WSLg。
GUI 共用视图、控制器、表单和 gettext 翻译目录。
Linux 提示会说明无求解器边界和语义检索验收限制。
可选启动器绑定已安装入口和精确设置 token。
创建和删除操作会保留其他程序创建或后来改动的启动器文件。

POSIX `flock` 只能串行化配合使用锁的编辑器。
它无法阻止不配合的进程替换路径。
基线身份和内容检查会拒绝检测到的变化。
读取固定检查会检测持续存在的文件或上级目录变化，并拒绝成功结果。
它不提供 Windows 的禁止写入句柄，也不保证阻止所有恶意替换竞态。
私有稳定锁文件会保留在磁盘。文件存在不代表锁仍被持有。
清理只删除已证明属于当前操作的文件。未解决的清理冲突仍是错误。
