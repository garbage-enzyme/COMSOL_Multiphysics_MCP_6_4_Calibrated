# COMSOL Desktop/Server 交互协作模式

感谢原始 [Ching-Chiang/comsol-mcp](https://github.com/Ching-Chiang/comsol-mcp) 仓库对这一交互概念作出的方法和 UX 贡献。该仓库在本项目中仅用作 behavioral research；本项目独立实现了自己的默认关闭设计，没有复制、改写、翻译、cherry-pick 或机械重写原仓库的源代码。这里是对方法贡献的致谢，不表示两个实现或其全部行为相同。

该模式让 COMSOL model 在 Desktop 中保持可见，同时 agent 连接同一个由用户手动启动
的 COMSOL Multiphysics Server。用户和 agent 必须明确轮流操作。MCP 不会启动、清空、
关闭、拥有或终止用户的 Server、Desktop、listener、model 或 main file。

## 这个模式是什么

首个 release 支持一个本地用户、一个用户拥有的 COMSOL Multiphysics Server、一个
连接的 Desktop client，以及一个精确的 server-held model。MCP preflight 识别本地
process/listener，使用另一个 MPh client attach，列出 Server 模型，采用一个精确模型，
并建立 optimistic model/revision lock。

有两种 collaboration mode：

- `interactive_inspection` 用于短时、轮流执行的 adoption、readback、revision check
  和 Save Copy snapshot。用户编辑前要先 unlock。
- `automation_exclusive` 用于有界 durable attached job。Desktop 仍可观察，但在 job
  达到 verified terminal state 前，用户不得修改模型。

公共 `desktop_shared` profile 不会把广泛的 generic `param_set` 或前台
`study_solve` 混入 shared session。受控的 agent mutation/solve 通过现有 durable
`job_submit/status/tail/cancel/resume` 路径提交，目前 attached backend 支持
`staged_sweep`。单点 staged sweep 是做一次受控 parameter change 和 solve 的公共
有界路径。这个限制很重要：它不是 simultaneous co-editing，也不是不受限制的远程控制台。

## 前提与兼容性

- COMSOL Multiphysics 和 COMSOL Multiphysics Server 位于同一台计算机；
- MPh 1.3.1 和本 MCP installation；
- 一个获得授权的本地用户，以及允许本地 client/server topology 的 license；
- COMSOL Desktop 和 Server 均属于已接受的 `6.4.0.*` release line；
- exact licensed reference build 为 `6.4.0.293`；
- saved formal work 配置 immutable model-read root，snapshot/job 配置仅 ASCII 的
  owned artifact root；
- 改变 profile 或 feature flag 后重启 MCP host。

`6.4.0.*` 内只能是最后一个 build component 不同。例如自动更新把 `6.4.0.293`
改为另一个 `6.4.0` build 时，可以在携带 build-difference warning 的情况下继续。
第三位数字改变（例如 `6.4.1.*`）属于不同 release family，必须 fail closed。老版本、
Desktop/Server 混合 release family 以及无法读取的版本都不会靠猜测放行。

公共 MCP endpoint 只支持 local loopback。COMSOL 自己的 listener 仍可能绑定更大范围；
见[安全与限制](#安全与限制)。

## 快速开始

### 1. 开启默认关闭的 MCP profile

启动 MCP host 前编辑项目根目录统一的 `settings.json`：

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

这是 partial edit；请保留项目模板中的其他设置。每个字段的含义、默认值和可接受值见
[设置指南](../setting_guide/README_CN.md)。如果 host 不保留项目路径，只传入一个统一的定位变量：

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

重启 MCP host。profile change 是 static 的，不会 hot reload。调用 `capabilities` 并确认：

- `active_profile` 为 `desktop_shared`；
- `shared_session.profile_active` 和 `shared_session.gate_open` 为 `true`；
- shared-session tools 已列出；
- evidence-integrity checks 仍然独立保持 default-on。

删除设置条目会使用默认值；输入非法值时该条目保持安全默认值，并在
`project_settings.settings_errors` 中报告设置路径和 reason code。

如果 capabilities 仍显示旧 profile，不要继续。应重启真正的 host process，而不是认为
在 terminal 中改变变量会自动更新已经运行的 stdio server。

### 2. 手动启动 COMSOL Multiphysics Server

在 Windows 打开 **COMSOL 6.4 > COMSOL Launchers > COMSOL Multiphysics Server
6.4**。COMSOL 6.4 文档把 Windows server 命令写为
`comsolmphserver [options]`。为了让 detach/reconnect 后仍保留 Server 与 model，
应开启 repeated-client behavior：

```text
comsolmphserver -multi on -port 2036
```

使用自己 installation 提供的 executable；不要让 agent 寻找或处理凭据。`-multi on`
使 client disconnect 后 Server 与内存模型继续存在。`-port 2036` 请求常用默认端口，
但实际也可能使用其他空闲或 configured port。官方
[Windows command reference](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.31.html)
说明了 `-multi`、`-port`、login 和 password-storage options。

等待 console 报告 COMSOL Multiphysics Server 6.4 正在监听，并记录实际端口，例如：

```text
COMSOL Multiphysics Server 6.4 ... started listening on port 2036
```

不要关闭这个 console。shared mode 中 MCP 永远不会启动或终止它。Windows Start-menu
步骤和首次启动的 credential behavior 也可参考官方
[client-server startup guide](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.19.html)。

### 3. 连接 COMSOL Desktop

只打开一个 COMSOL Desktop 6.4 window。选择 **File > COMSOL Multiphysics Server >
Connect to Server**，server 使用 `localhost`；如有需要选择 manual port，并填写
Server console 报告的精确端口。

在 licensed acceptance host 上，连接对话框会从用户本地 COMSOL 设置中自动填入
username 和 password。这是一项有用的 UX 观察，不保证每台机器都相同。只能使用
自己获得授权的 COMSOL installation 中的凭据；绝不能把 username、password 或
login-properties 文件复制到 agent prompt、log、screenshot 或 receipt 中。

连接后，Desktop 左下角应显示 `localhost:<port>`，例如 `localhost:2036`。如果该
indicator 消失，Desktop 就不再连接 Server。官方
[Desktop connection guide](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_running.38.20.html)
说明了 server/port dialog，也说明连接时 COMSOL 可能询问使用当前 Desktop model
还是 Server 上已有的 model。

如果 dialog 询问使用哪个模型，必须明确选择。MCP 只能采用 Server 持有的模型，
不会猜测 standalone Desktop model 或 existing Server model 哪一个才是目标。

### 4. Preflight、attach 并采用一个精确模型

只告诉 agent 本地端口，不提供凭据。MCP 顺序如下：

1. `shared_server_preflight(host="localhost", port=2036)`；
2. 查看 `state`、精确 process/listener evidence、release line 和 warning；
3. 只有用户确认 Desktop 显示相同 endpoint 后，才调用
   `shared_server_attach(..., user_confirmed=true)`；
4. 调用 `shared_server_models`；
5. 选择一个精确 server model，并调用 `shared_model_adopt`，提供 `model_tag`，
   再加可用的 expected label、path 或 unsaved state；
6. 调用 `shared_model_lock(collaboration_mode="interactive_inspection", ...)`。

`user_confirmed=true` 是每个 session 的用户证据，必须对应真实 GUI 观察；agent 不能
只根据 process data 自己生成该确认。

## 状态检测如何处理常见情况

Preflight 会在创建 MPh client 前做两次 bounded process/listener observation。仅凭
window title 或 process name 不足以 attach。连接后，MCP 还会检查 clientapi build
readback，并列出 server-held model inventory。

| 观察状态 | MCP 处理 | 用户最小操作 |
| --- | --- | --- |
| 没有 COMSOL Desktop，也没有 Server | 返回 retryable 的 `desktop_and_server_absent`；不创建 client/lease | 启动 Server，等到 listening，再启动一个 Desktop |
| 用户点击 COMSOL，但仍在启动 | 返回 retryable 的 `desktop_or_server_starting` | 等 Desktop 可响应且 Server listener 稳定，再跑 preflight |
| Desktop 已打开，Server 不存在 | 因没有 stable listener 而拒绝 attach | 启动 Server，并把 Desktop 连接到精确端口 |
| Desktop 已连接，但 Server 没有模型 | attach 可以成功，但 inventory 为空；adoption 返回 `no_server_models` | 在 connected Desktop 中新建模型，或 transfer/open 一个模型，再刷新 inventory |
| 新建的空白 unsaved model | inventory 标记 unsaved；可用 exact tag 与 `expected_unsaved=true` 采用 | 只做有界 interactive work；formal/durable 前先保存独立 immutable source |
| Existing saved model | inventory 给出 tag/label/path identity；必须 exact selector | 确认 path/label 并采用；source、working、snapshot 三者分离 |
| Model 只在 standalone Desktop 中 | MCP 在 Server inventory 中看不到 | 连接 Desktop 后明确 transfer current model，或在 connected 状态打开/保存 |
| 多个 Desktop window | preflight 返回 `ambiguous_gui_clients`，不选择 window | 关闭或断开额外 window，只保留目标 Desktop client |
| Server 中有多个模型 | 返回 inventory，但绝不自动选 ambiguous candidate | 确定一个 exact tag，并添加 expected label/path/unsaved state |
| 老版本或混合 COMSOL release | 返回 `unsupported_or_ambiguous_comsol_version` 并拒绝 attach | 使用同一 accepted `6.4.0.*` 的 Desktop/Server，再重试 |
| Version 无法读取 | fail closed，不从 shortcut/title 推测 | 修复 installation/process readback，不绕过 version gate |
| 同属 `6.4.0.*`，最后 build 不同 | 携带 `same_accepted_release_line_build_difference` warning 放行 | 确认是预期更新，并在 receipt 中保留 exact build evidence |
| 额外 MPh/COMSOL owner 或 PID/listener 变化 | 返回 collision/identity change；不获取 lease/client | 关闭无关 owner 或等启动稳定；绝不能只按 process name kill |
| Listener 绑定 wildcard | 保留 `listener_bind_scope=wildcard` warning | 检查 firewall/network exposure；MCP 不会改写成 loopback |

如果多个 window 包含 empty、blank、saved 或 older model 的任意组合，preflight 先解决
process/window ambiguity。它无法检查所有 GUI tab 并猜目标。应把 topology 简化成一个
目标 Desktop、一个 accepted Server 和一个精确 server-held model。

## 轮流协作流程

### 用户回合

1. 确认 `localhost:<port>` 仍可见。
2. 编辑前确认 MCP lock 已释放。
3. 在 Desktop 做一个有界修改，并等待 COMSOL 完成。
4. 把修改内容告诉 agent，作为提示而不是证据。
5. Agent 重新 inventory/relock，用 readback 建立新的 revision。

例如用户把参数从 `55` 改成 `30`，下一次 revision readback 应建立这个变化。Agent
不能只信任聊天消息。mismatch 会使旧 revision 失效，需要新的 lock。

### Agent inspection/snapshot 回合

1. 采用精确模型，以 `interactive_inspection` lock。
2. 保存 `lock_sha256` 和 `revision_sha256`。
3. 每个 identity-sensitive action 前立即调用 `shared_model_verify`。
4. 需要 Save Copy 时，调用 `shared_model_snapshot`，传入 expected lock、revision
   和 caller-declared maximum byte count。
5. 再次 verify，然后用简短 audit reason 调用 `shared_model_unlock`。
6. 明确告诉用户可以继续其回合。

### 受控 solve/agent mutation 回合

公共 v3.1 surface 使用 `automation_exclusive` 和 durable job controls。Agent 用
immutable source lock 模型，`job_submit` 执行 verified handoff，释放 lock 并 detach
interactive MCP client，然后启动 attached worker。中性单点 spec 形状如下：

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

parameter、unit/convention、expression、source file 和 scientific policy 都依赖
具体模型，必须由 caller 声明。不要在没有形成正式 specification 时把这个中性示例
直接复制到真实模型中。

使用 `job_status` polling，用 `job_tail` 查看 bounded log。不要用普通 shared call
做前台 loop。worker 在 point 前检查 external revision，逐点持久化证据，并保存 contained
checkpoint/Save Copy。用户此时修改 Desktop，会阻止下一 point 或 resume，而不是混入旧 revision。

使用 `job_cancel` 请求取消。`cancel requested` 不是终态；要等待 `cancelled`，并确认
owned worker/descendant、port、lease 和 external-resource-preservation evidence。
取消只能停止 attached MCP worker/client，不得终止用户的 Server、Desktop、listener 或 model。

## Desktop 原生 busy warning

COMSOL Server 会串行化 access。较长的 agent mutation 或 solve 期间，Desktop 可能
暂时锁定编辑，并显示 occupied-model 或 busy warning。此时等待 agent 回合完成，不要
越过 warning 尝试 concurrent edit。

短 property write 或 read-only call 可能在 warning 出现前就结束。在 licensed host 上，
第一次较长模型构建/solve 出现了 warning，之后的短 change/readback 没有出现。这是正常
UX timing 差异。原生 warning 只证明 COMSOL 当时认为 Server/model busy，不能证明所有
MCP identity、revision、evidence 或 cleanup guard 都已通过；这些结论要看 MCP receipt。

## Saved-model walkthrough

1. 在 configured model-read root 中保留 immutable source `.mph`，记录 hash，并在该
   formal identity 内禁止覆盖。
2. 在 connected Server 中打开或 transfer 一个独立 working model。Desktop 显示的是
   server in-memory model，它可以有 saved path。
3. Preflight、attach、inventory，并按 exact tag 加 expected path/label 采用。
4. formal snapshot 或 attached durable work 时，同时提供 immutable source path 和
   SHA-256 建立 lock。
5. 用户与 agent 轮流操作。每个 agent 回合从 revision check 开始；每次用户 edit 后建立
   新 lock/revision。
6. 用 `shared_model_snapshot` 或 durable checkpoint 创建 Save Copy。snapshot 不改变
   visible main model path。
7. Unlock 并 detach，确认 Desktop/Server 仍保留模型。

Windows/COMSOL 可能锁定当前打开的 `.mph`。另外，**Save As** 通常会把 working model
切换到刚保存的文件；licensed UX acceptance 也观察到了这一点。formal work 不应假定
新保存文件仍是 untouched source。应保留不同的 immutable source，并用 Save Copy 创建 snapshot。

## Unsaved-model walkthrough

1. 先把 Desktop 连接到 Server，再创建一个 blank model。
2. 刷新 `shared_server_models`，以 exact unsaved tag 和 `expected_unsaved=true` 采用。
3. 进行短时 turn-taking inspection/readback。可以建立 contained Save Copy，但它不能
   追溯证明一个 immutable starting source。
4. formal durable work 前，保存一个独立 source `.mph`，放入 configured read root，
   记录 hash，并建立新的 lock/run identity。
5. 不能把 unsaved in-memory model 说成拥有 verified source-file hash。

## 三种文件角色

| 角色 | 所有者 | 是否允许变化 | 安全规则 |
| --- | --- | --- | --- |
| Immutable source | 用户 | 一个 formal identity 内不允许 | configured model-read root 下可读的 existing `.mph`；exact SHA-256；不能 open-and-overwrite |
| Open working model | 用户/COMSOL Server | 只在明确回合中允许 | Desktop 可见，使用 exact server/model/revision evidence；不支持 simultaneous edit |
| Save Copy snapshot/checkpoint | MCP-owned artifact workflow | 只能新建文件 | ASCII owned root、collision-free name、size/hash/manifest；不覆盖 source，也不改变 main working path |

即使三个文件当前字节相似，也不能合并角色。verified source 不是 scratch file；snapshot
只有在新的 formal identity 明确采用后，才可能成为新的 source。

## 协作礼仪 checklist

- 保持一个 Desktop window、一个目标 Server 和一个 exact server model。
- edit/solve 前明确当前是谁的回合。
- 用户编辑前 unlock；编辑后 relock/readback。
- `automation_exclusive` 期间只观察，不修改模型。
- 把原生 busy warning 当作停止信号，不当作 verification receipt。
- 使用 exact tag、path、hash、lock ID 和 revision；不要说“第一个模型”。
- source、working model、snapshot 必须分开。
- 保留 failed、partial、diagnostic、cancelled 和 residual evidence。
- 不把 credential 粘贴到聊天或 receipt。
- 正常协作步骤之间保持 Server 运行。

## 安全 detach 与 shutdown

正常协作按以下顺序结束：

1. 等 attached job 达到 verified terminal state。
2. 保存所需 raw evidence 和 snapshot。
3. Verify 当前 lock/revision。
4. 调用 `shared_model_unlock`。
5. 调用 `shared_server_detach`。
6. 确认 detach 报告 external resources preserved。
7. 确认 Desktop 仍显示 `localhost:<port>`，model 仍可见。

正常情况下，在协作步骤之间、Save Copy 后、重新打开模型后或正常 MCP detach 后都**不需要**
重启 Server。只有用户在证据保存后才能关闭 Desktop 或 Server console。只有 documented
recovery 明确要求时才重启，例如不可恢复的 Server/client state；重启后要重新建立
process、model、lock 和 revision identity。

若 `shared_server_detach` 返回 `model_lock_active`，先 unlock。若 detach 结果 uncertain，
不要按进程名 kill COMSOL；检查 exact process/listener identity，由用户决定是否重启其资源。

## 安全与限制

COMSOL Multiphysics Server 是允许同一用户 multiple connection 的 single-user server。
COMSOL 6.4 官方文档说明 TCP connection 有 password protection，但除此之外并不加密；
firewall/address restriction 属于 administrator 责任。本 MCP release 只支持 local
loopback endpoint，不会把 remote 或 wildcard exposure 变成受支持 topology。

Preflight 保留实际 listener bind evidence。如果 COMSOL 监听 `0.0.0.0` 或 `::`，即使
MCP 通过 `localhost` 连接，也会报告 `listener_bind_scope=wildcard`。应检查 host firewall
和 COMSOL configuration。MCP 不会改写 listener，也不会声称它只绑定 loopback。

限制如下：

- 首个 release 不支持 remote host；
- 不支持用户与 agent 同时编辑；
- 不自动选择多个 window、Server 或 model；
- `6.4.0.*` 以外在新 release acceptance 前不支持；
- MCP 不处理 credential；
- 不保证每个短 call 都会触发原生 busy dialog；
- 可见 3D geometry、plot 或 GUI output 不等于 scientifically verified evidence；
- attached durable execution backend 目前只支持 `staged_sweep`；
- `desktop_shared` 是 experimental 且 default-off。

GUI 可见的一致结果是有价值的 collaboration evidence，但科学结论仍需要独立的 default-on
evidence-integrity workflow、raw data、declared policy、convergence 和该模型所需的
physical validation。
