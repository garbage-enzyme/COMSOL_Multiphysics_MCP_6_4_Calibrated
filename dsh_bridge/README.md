# dsh-comsol-bridge

当前版本化兼容边界见
[`DEEPSEEK_COMPATIBILITY.md`](DEEPSEEK_COMPATIBILITY.md)。

可选的 DeepSeek Harness 兼容层：让 DSH 以**原生插件**方式驱动标准的 comsol-mcp 服务器，
同时**服务器本身零改动**，codex / Claude / opencode 等其他 agent 照常直接使用它。

```
其他 agent (codex/Claude/opencode)          DSH (本 GUI)
        │ 直接连接                              │ 可选开启（patch 条目即开关）
标准 comsol-mcp 服务器 ◄──(MCP stdio)── dsh-comsol-bridge 原生插件
  （零改动）                                   │ 原生注册全部工具（干净名）
        │ worker 进程                          │ ctx.jobs 镜像作业（comsol kind）
        ▼                                      │ 串行队列（skill 强制一次一个请求）
  持久作业（SQLite+fsync，真相层）              ▼
  跨回合/跨宿主/跨崩溃存活               跨回合监督面（完成通知/进度流/取消）
```

## 为什么不是现成的 dsh-mcp-client 桥

1. 桥无法创建 ctx.jobs 原生作业（完成通知 / job_output 进度流 / job_kill 是核心需求）；
2. 工具面与作业监督必须共用同一根连接（单租约、串行纪律），桥不暴露内部通道；
3. 桥无每工具审批，工具名强制 `mcp__comsol__` 前缀。

## 环境要求

- **Node.js ≥ 20**（任何 CLI agent 环境已自带，无需额外安装）
- 无 npm 依赖、无构建步骤、无需 COMSOL（测试用 fake MCP 服务器，solver-free）
- 生产使用时需要生产版服务器：`D:\condaenvs\comsol-mcp-py314\Scripts\comsol-mcp.exe`

## 安装与启用

```powershell
# 1) 把插件链进 DSH web profile（junction，无需管理员）
.\install.ps1

# 2) 在 ~/.dsh/profiles/web/cordis.patch.yml 增加（HMR 热应用，无需重启宿主）：
```

```yaml
- insert:
    - id: comsol-bridge
      name: '@local/dsh-comsol-bridge'
      config:
        command: 'D:\condaenvs\comsol-mcp-py314\Scripts\comsol-mcp.exe'
        args: []
        cwd: 'D:\comsol_runtime'
        env:
          COMSOL_MCP_SETTINGS_PATH: 'D:\comsol_runtime\settings.json'
        pollIntervalMs: 15000
        toolCallTimeoutMs: 600000
        # enabled: false   ← 保持沉睡不卸载
        # 删除整个条目 = 完全停用
```

> ⚠️ **同一个 DSH 会话内不得同时在 dsh-mcp-client 里配置 comsol 条目**——
> 兼容层必须独占与服务器的连接（一个会话只允许一个进程连服务器）。

## 配置

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 条目存在即开启；`false` 保持沉睡 |
| `jobMirrorEnabled` | `true` | job_submit 成功后自动建 ctx.jobs 镜像 |
| `command` / `args` / `cwd` / `env` | 生产版路径 | 服务器进程 |
| `pollIntervalMs` | `15000` | job_status / job_tail 轮询间隔（串行队列内） |
| `tailLines` | `20` | job_tail 每次取的行数 |
| `outputLimitBytes` | `262144` | 镜像进度流缓冲上限 |
| `toolCallTimeoutMs` | `600000` | 单次工具调用超时（solve/审计放宽到 10 分钟） |
| `cancelConfirmTimeoutMs` | `120000` | 取消后未见终态时的诚实结算时限 |
| `reconnect` | 退避 500ms→30s，最多 10 次 | 断线重连预算 |
| `terminalStates` | completed/failed/cancelled/killed/done/terminal | 终态子串匹配 |
| `stateFile` | `<cwd>\.dsh-comsol-bridge-jobs.json` | 镜像 id 持久化（重启 rehydrate） |

## 行为

- **串行纪律**：所有请求（工具调用 + 镜像轮询）走 single-flight 队列，服务器永远只看到一个
  在途请求（comsol skill 强制规则）。
- **作业镜像**：`job_submit` 成功返回 `job_id` 后自动 `ctx.jobs.start({kind:"comsol", ...})`，
  模型获得完成通知；`job_output` 输出 job_tail 进度流；`job_kill` 走 job_cancel。
- **中途读点**：模型随时调 `job_status` / `job_tail`（原生工具名，跨回合可用），
  判定以 append-only 日志为准，不以 CPU/磁盘活动推断。
- **两层持久化**：DSH 镜像跨回合存活、跨宿主消亡（进程内注册表）；comsol worker + SQLite
  才是真相层，跨宿主存活，`job_resume` 精确身份恢复。
- **重启恢复**：镜像 job_id 持久化到 stateFile；插件启动时逐个 `job_status` 探测，
  活跃的以 **unowned** 镜像重建（job_list 对调用者开放可见）。
- **断线**：指数退避重启子进程；预算耗尽后注销工具并明确报错；镜像诚实结算
  "bridge lost contact; job may still be running (use job_resume after recovery)"。
- **fail-closed**：服务器未安装 / 崩溃 / 协议版本不支持 / 工具列表畸形 → 明确失败，
  不静默降级；stdout 垃圾行与分块写入被容忍（行帧协议）。

## 测试与 CI

```bash
node --test            # 回归测试（node:test，solver-free，fake MCP 服务器）
node scripts/smoke.mjs # 端到端冒烟演示
```

- `tests/core.test.mjs`（8）：连接/发现/串行性(maxInFlight=1)/中止/超时/断线重连/分页
- `tests/failures.test.mjs`（13）：MCP 未安装(缺 exe/缺脚本)、启动即崩溃、stdout 垃圾容错、
  分块行帧、协议版本不支持、init 超时、畸形工具列表、空工具列表、isError、重连预算耗尽、
  list_changed 重同步、取消不可确认
- `tests/mirror.test.mjs`（10）：终态判定/完成/取消/失联/进度流增量/缓冲上限/多镜像独立/state store
- `tests/plugin.test.mjs`（6）：工具命名规范化、配置合并、文本投影
- CI（`.github/workflows/ci.yml`）：Windows + Ubuntu 双矩阵（对齐主仓库：concurrency 组、
  fail-fast:false、timeout、workflow_dispatch），外加 Windows 上 install.ps1 junction 冒烟。

## 目录

```text
dsh-comsol-bridge/
├── lib/
│   ├── mcp-client-core.mjs   # ctx 无关：MCP stdio 客户端（串行队列/重连/发现）
│   ├── job-mirror.mjs        # ctx.jobs 镜像 + state store（注入式，可单测）
│   └── index.js              # cordis 插件入口（工具注册 + 镜像接线 + rehydrate）
├── fixtures/fake-comsol-server.mjs  # solver-free MCP 服务器（10 种 FAKE_MODE）
├── tests/                    # node:test 回归测试（37 用例）
├── scripts/smoke.mjs         # 冒烟演示
├── install.ps1               # junction 安装脚本
└── .github/workflows/ci.yml  # 云端 CI
```

## 当前状态与边界

本目录已作为主仓库中的 **repo-only 可选组件** 集成，并由自身的 Node CI
执行 solver-free 回归；它不进入 Python wheel/sdist，也不改变标准
`comsol-mcp` server、工具 schema、生产设置或 COMSOL ownership 逻辑。
启用它必须由用户显式安装 junction 并在 DSH web profile 中加入插件条目。

真实生产服务器联调已覆盖 `capabilities`、工具发现、job mirror、进度流和
完成通知；settings-change 后重启路径已由用户独立验收。仍不能把 fake-server
37/37 或设置变更验收当作 COMSOL solve 或生产取消路径验收。

如果未来上游 `dsh-mcp-client` 提供等价的 post-call 扩展点，本兼容层可以退役；
在此之前应保持它作为独立、默认关闭、单连接的桥接组件。
