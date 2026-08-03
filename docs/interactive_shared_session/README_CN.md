# COMSOL Desktop/Server 交互协作模式

感谢原始 [Ching-Chiang/comsol-mcp](https://github.com/Ching-Chiang/comsol-mcp)
仓库提出这种交互思路。本项目只参考了操作方法，独立完成了默认关闭的安全实现；没有复制、
改写、翻译、挑选提交或机械重写原仓库代码。两个项目的具体行为不一定相同。

## 先看结论

这种模式适合希望一边在 COMSOL Desktop 中观察模型，一边让智能助手通过 MCP 操作同一个
COMSOL Multiphysics Server 的用户。

最重要的规则只有一条：**用户和智能助手必须轮流操作，不能同时修改模型。**

MCP 不会启动、关闭、清空或终止用户拥有的 Server、Desktop、监听器和模型。它只会连接
用户明确指定的本机 Server，并且在每次操作前检查当前连接和模型是否仍与上次一致。

目前支持：

- 一台 Windows 计算机；
- 一个用户手动启动的 COMSOL Multiphysics Server；
- 一个连接到该 Server 的 COMSOL Desktop 窗口；
- 一个由 Server 持有、且能被精确识别的模型；
- COMSOL `6.4.0.*`，正式参考构建号为 `6.4.0.293`；
- MPh 1.3.1 和本 MCP；
- 短时查看、读取和保存副本；
- 通过 `staged_sweep` 提交的有界自动任务。

这不是远程桌面，也不支持用户与智能助手同时编辑。

## 两种协作方式

### 查看模式

`interactive_inspection` 用于短时间查看模型、读取参数、核对模型变化和创建 Save Copy
快照。智能助手完成后必须解除模型锁，用户才能继续编辑。

### 自动任务模式

`automation_exclusive` 用于可恢复的有界任务。Desktop 可以继续显示模型，但任务达到
已确认的终止状态前，用户只能观察，不能修改。

独立的 `shared_server.enabled` 功能不会开放不受约束的前台求解。需要改变参数或求解时，智能
助手应使用 `job_submit/status/tail/cancel/resume`。目前共享模式只支持
`staged_sweep`。

## 使用前准备

请先确认：

- COMSOL Multiphysics 和 COMSOL Multiphysics Server 安装在同一台计算机；
- Desktop 和 Server 都属于 `6.4.0.*` 版本系列；
- 许可证允许本机 Client/Server 连接；
- 正式任务的源模型位于已配置的只读目录；
- 快照和任务产物写入只含 ASCII 字符的目录；
- 更改 MCP 配置后会重启 MCP 宿主进程。

`6.4.0.293` 与另一个 `6.4.0.x` 只差最后构建号时，MCP 会给出警告但可以继续。若第三段
版本号不同，例如 `6.4.1.*`，MCP 会拒绝连接。Desktop、Server 版本不一致或版本无法
读取时也会拒绝连接。

## 快速开始

### 第一步：开启共享功能

在启动 MCP 前，编辑项目根目录的 `settings.json`。下面只是局部示例，请保留模板中的
其他设置：

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

如果 MCP 宿主进程的当前目录不是项目目录，请设置：

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

重启 MCP，然后调用 `capabilities`，确认：

- `active_profile` 是用户选择的 profile；
- `enabled_features` 包含 `shared_server`；
- `shared_session.feature_enabled` 和 `shared_session.gate_open` 都是 `true`；
- 返回结果中列出了共享模式工具；
- 证据完整性检查仍保持默认开启。

如果仍显示旧配置，不要继续。必须重启真正运行 MCP 的进程；仅在命令行中修改环境变量
不会改变已经运行的服务。

### 第二步：手动启动 Server

在 Windows 开始菜单中打开：

**COMSOL 6.4 > COMSOL Launchers > COMSOL Multiphysics Server 6.4**

若使用命令行，可采用：

```text
comsolmphserver -multi on -port 2036
```

`-multi on` 让 MCP 断开后 Server 和内存模型继续保留。`-port 2036` 请求使用常见端口，
实际端口仍以 Server 窗口显示的信息为准。

等待窗口出现类似内容：

```text
COMSOL Multiphysics Server 6.4 ... started listening on port 2036
```

记录端口并保持这个窗口开启。不要把用户名、密码或登录文件发给智能助手。可参考 COMSOL
官方的 [Windows 命令说明](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.31.html)
和 [Client/Server 启动说明](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.19.html)。

### 第三步：连接 Desktop

只打开一个 COMSOL Desktop 6.4 窗口，然后选择：

**File > COMSOL Multiphysics Server > Connect to Server**

Server 地址填写 `localhost`，端口填写上一步记录的精确端口。用户名和密码只在 COMSOL
自己的连接窗口中填写，不要复制到聊天、日志、截图或测试报告中。

连接成功后，Desktop 左下角应显示 `localhost:<port>`，例如 `localhost:2036`。如果
这个标记消失，说明 Desktop 已不再连接 Server。

若 COMSOL 询问使用 Desktop 当前模型还是 Server 已有模型，请由用户明确选择。MCP 只会
采用 Server 当前持有的模型，不会替用户猜测。

### 第四步：让 MCP 检查并采用模型

只需告诉智能助手本机端口，不要提供凭据。标准顺序如下：

1. 调用 `shared_server_preflight(host="localhost", port=2036)`；
2. 检查返回的 `state`、版本、进程、监听器和警告；
3. 用户确认 Desktop 显示同一地址后，调用
   `shared_server_attach(..., user_confirmed=true)`；
4. 调用 `shared_server_models` 查看 Server 中的模型；
5. 选定一个模型后调用 `shared_model_adopt`。除了 `model_tag`，已保存的模型必须提供
   完整保存路径；未保存的模型必须提供 `expected_unsaved=true`。标签可以不填；
6. 调用 `shared_model_lock(collaboration_mode="interactive_inspection", ...)`。

`user_confirmed=true` 表示用户确实看到了相同的 Desktop 连接。智能助手不能仅根据进程
信息自行填写这个确认。

## 常见状态怎么处理

MCP 会在创建 MPh Client 前连续观察两次本机进程和监听器。第二次观察时间必须严格晚于
第一次。两次之间只要有相关进程出现、消失或身份改变，MCP 就不会连接。

| 用户看到的情况 | MCP 返回 | 用户应该做什么 |
| --- | --- | --- |
| Desktop 和 Server 都没有启动 | `desktop_and_server_absent` | 先启动 Server，等待监听，再启动 Desktop |
| Desktop 或监听端口仍在启动，或 Server 无响应 | `desktop_or_server_starting` | 等待 Desktop 和 Server 都恢复响应，然后重试 |
| 两次观察的时间没有前后顺序 | `probe_chronology_invalid` | 重新收集两次新的状态 |
| Desktop 已打开，但没有 Server 监听端口 | 拒绝连接 | 启动 Server，并让 Desktop 连接精确端口 |
| Desktop 窗口多于一个 | `ambiguous_gui_clients` | 关闭或断开额外窗口，只保留目标窗口 |
| 发现额外的 MPh 或 COMSOL 操作者 | 冲突或身份变化状态 | 停止无关操作者，或等待启动过程稳定 |
| Desktop 或 Server 版本不属于 `6.4.0.*` | `unsupported_or_ambiguous_comsol_version` | 使用同一版本系列后重试 |
| Server 中没有模型 | 连接可成功，但采用模型时返回 `no_server_models` | 在已连接的 Desktop 中新建、打开或传入模型 |
| Server 中有多个模型 | 返回模型清单，不自动选择 | 根据标签、路径和是否已保存精确选择 |
| 监听器绑定到同地址族的通配地址 | `listener_bind_scope=wildcard` 警告 | 检查防火墙和 Server 设置 |

MCP 不会根据“第一个模型”或“当前窗口”猜测目标。请始终使用精确的模型标签、路径和状态。

## 轮流操作

### 用户操作时

1. 确认 Desktop 仍显示 `localhost:<port>`；
2. 确认智能助手已经解除模型锁；
3. 只做一个清楚、有限的修改，并等待 COMSOL 完成；
4. 告诉智能助手改了什么；
5. 让智能助手重新读取模型并建立新的锁。

聊天中的说明只是提示，不能替代模型读取结果。若模型实际状态与旧锁不一致，旧锁会失效。

### 智能助手查看时

1. 用 `interactive_inspection` 锁定精确模型；
2. 保存返回的 `lock_sha256` 和 `revision_sha256`；
3. 每次关键操作前调用 `shared_model_verify`；
4. 需要副本时调用 `shared_model_snapshot`；
5. 再次核对模型，然后调用 `shared_model_unlock`；
6. 明确告诉用户现在可以继续操作。

COMSOL 6.4 的 Save Copy 只能按路径写文件，无法在写入过程中强制最大字节数。因此当前
版本在无法保证写入上限时会返回 `snapshot_write_bound_unavailable`，不会先写完整文件再
假装满足上限。

### 智能助手求解时

共享模式的求解必须使用 `automation_exclusive` 和任务工具。一个中性的单点示例如下：

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

参数、单位、表达式、源文件和科学判定规则都必须按具体模型填写，不能直接照抄示例。

用 `job_status` 查看进度，用 `job_tail` 查看有界日志。任务会在每个求解点前重新检查
模型状态，并逐点保存结果。用户在此期间修改 Desktop，会使下一求解点或恢复操作停止，
不会把新旧模型状态混在一起。

调用 `job_cancel` 只是提出取消请求。必须等到状态成为 `cancelled`，并确认工作进程、
端口、租约和外部资源保护结果均已记录。取消任务不得终止用户的 Server、Desktop、
监听器或模型。

## COMSOL 的模型占用提示

较长操作期间，Desktop 可能暂时不能编辑，并显示模型占用或忙碌提示。此时应等待智能
助手完成，不要强行同时编辑。

很短的读取或属性修改可能在提示出现前已经结束，因此没有提示不等于没有执行。这个提示
只能说明 COMSOL 当时认为模型被占用，不能证明 MCP 的身份、证据和清理检查已经通过；
这些结论应以 MCP 返回结果为准。

## 三种文件不要混用

| 文件角色 | 所有者 | 是否允许变化 | 规则 |
| --- | --- | --- | --- |
| 不可变源模型 | 用户 | 同一正式任务中不允许 | 位于已配置的读取目录，记录精确 SHA-256，不得打开后覆盖 |
| 当前工作模型 | 用户和 COMSOL Server | 只在明确轮次中允许 | Desktop 可见，必须核对 Server、模型和修订状态 |
| Save Copy 快照或检查点 | MCP 产物流程 | 只能新建 | 写入 ASCII 目录，名称不得碰撞，记录大小、散列和清单 |

即使三个文件当前内容相同，也不能把它们当成同一个角色。未保存的内存模型没有可验证的
源文件散列；需要正式任务时，应先保存一个独立源模型，再建立新的任务标识。

## 安全结束

按以下顺序结束一次协作：

1. 等任务达到已确认的终止状态；
2. 保存需要的原始结果和快照；
3. 调用 `shared_model_verify` 核对当前锁；
4. 调用 `shared_model_unlock`；
5. 调用 `shared_server_detach`；
6. 确认返回结果说明外部资源仍被保留；
7. 确认 Desktop 仍显示 `localhost:<port>`，模型仍可见。

正常解除连接后不需要重启 Server。若返回 `model_lock_active`，应先解除模型锁。若解除
连接结果不确定，不要按进程名强制结束 COMSOL；应检查精确进程和监听器身份，由用户决定
是否重启自己的资源。

## 安全限制

本版本只支持本机回环地址，不支持远程 Server。COMSOL Server 的 TCP 连接有密码保护，
但不提供额外加密，防火墙和地址限制仍由用户或管理员负责。

`0.0.0.0` 只与 IPv4 回环地址匹配，`::` 只与 IPv6 回环地址匹配。没有明确的套接字证据
时，MCP 不会假定 IPv6 通配监听器同时服务 IPv4。当前规则会把 `localhost` 规范化为
IPv4。任何通配监听都会保留 `listener_bind_scope=wildcard` 警告，MCP 不会把它改写成
仅本机监听。

其他限制：

- 不支持用户和智能助手同时编辑；
- 不自动选择多个 Desktop、Server 或模型；
- 不支持 `6.4.0.*` 以外的版本；
- MCP 不处理用户名和密码；
- 不保证每个短操作都会触发 COMSOL 的占用提示；
- Desktop 中看见的几何、图和结果不等于科学结论已经验证；
- 共享自动任务目前只支持 `staged_sweep`；
- `shared_server.enabled` 仍是默认关闭的试验功能。

Desktop 中可见的一致结果是有价值的协作证据，但正式科学结论还需要原始数据、明确的判定
规则、收敛性检查、默认开启的证据完整性检查，以及具体模型所需的物理验证。
