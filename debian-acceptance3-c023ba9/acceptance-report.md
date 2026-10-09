# 第三轮 Debian 修复验收报告

生成时间：2026-10-09T19:55:46+08:00。**结论：原 ACC-001 至 ACC-009 在本范围通过；BUG-052 进程树清理仍有扩展失败，不能无条件验收通过。**

## 版本与检查范围

- 仓库：garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated，远端 alpha7-7。
- 固定目标：`c023ba9a470f0c1cd60b6692713d26c27e2f4a6a`，对照 `42f9e95a2f2f33e76a06261b5eaf4801987d70e0`。
- 新增 2 提交：78c0e74、c023ba9；37 修改文件，1133 insertions / 223 deletions。
- 已逐个核对全部 37 文件的完整差异、相关契约与入口；每个文件更新 plan.md。不是重审全部 780 文件。
- 独立本地 checkout：/workspace/COMSOL_MCP_debian_acceptance_c023ba9；未改产品代码，未安装或启动 COMSOL。
- 环境：Debian 13、Python 3.14.7、Ruff 0.16.10、pytest 9.1.1；Node 工具位于已有 runtime。

## 历史问题状态

| 编号 | 结论与证据 |
| --- | --- |
| ACC-001 | 本范围通过：恢复事务内 B 阻塞，释放后两写者均确认，first/A/B 全部保留；同一探针上轮仍丢 B |
| ACC-002 | 本范围通过：跨进程 owner admission 串行，B 返回 task_owner_conflict；上轮仍双 owner |
| ACC-003 | 本范围通过：父目录链接 inspect/acquire/release 拒绝，无未处理异常，外部字节保留 |
| ACC-004 | 本范围通过：逐级无链接目录打开；祖先链接不读/删外部文件，claim inode 参与清理 |
| ACC-005 | 保持通过：短握手在 writer 存活时返回，实际 worker start/health/reset 成功 |
| ACC-006 | 本范围通过：current schema 与实际广告一致，3 项 schema 回归测试通过，deployment hash 已更新 |
| ACC-007 | 本范围通过：backend lint/format 与 GUI format 均通过 |
| ACC-008 | 保持通过：57 个发布契约测试通过，远端 integration-contracts succeeded |
| ACC-009 | 本范围通过：watchdog 暂时空命令行有界重试；持续为空、错签名、退出拒绝；真实 adjoint 测试通过 |

24 个独立实际 IO 场景全部通过，无外层超时。原 BUG-001/002/004/005/041 场景保持修复。
Tasks 的新 transaction 会阻塞第二写者；旧探针在持锁回调内等待 B 完成，会人为死锁。
本轮使用独立 B 进程先进入、检查尚未确认，再让 A 返回并释放事务，最后验证 B 确认及真实 journal 字节。
同一新探针对照上轮版本，半行恢复仍丢 B，owner 仍可双绑定，证明探针保留了失败识别能力。
只有 solver engine 是替身；生产 journal、锁、truncate、append、fsync 和独立进程均真实执行。
原始 BUG-005 旧脚本退出 1 是预期 owner conflict；另有访问、取消与不改映射的行为断言。

## 新增 Linux 修复

| 原问题 | 本范围验收 |
| --- | --- |
| BUG-052 semantic worker 后代清理 | 同组与 ready 前已记录的 detached 子进程回收通过；ready 后派生、脱离 session、父先退出仍失败，记录 ACC-010 |
| BUG-061 非有限 solver owner 时间 | NaN、正负 Infinity、超大整数均拒绝，registry 保持空 |
| BUG-062 不完整 listener 清单 | 注入 PID 未知或缺失地址的合法观察记录，complete=false，不宣称完整 |
| BUG-167 Unicode XDG 默认值 | 明确 ASCII runtime/artifact 加 Unicode model roots 时，normalize/serialize 通过，XDG_DATA_HOME 和 XDG_STATE_HOME 均检查 |
| BUG-176 DSH bridge Linux 测试路径 | 修改文件 19 tests passed；整个 bridge 69 tests passed，不需要 DeepSeek key |
| BUG-190/191 semantic feature 验收 | 真实 Debian stdio initialize/list_tools/capabilities 五 profile 通过：56/118/85/110/166；关闭功能不注入 Windows 模型路径 |

实际 stdio discovery 不加载 hybrid 模型，不等同于完整 semantic retrieval 集成验收。
DSH 检查使用真实 Node 子进程与 bridge fake server，未重复证明当前真实 DSH host 的全部任务行为。
额外 surrogate/thermal 修改也已核对：原生 opaque checksum 保留、科学身份漂移、field-schema 次序与值域、sealed 文档版本/语义拒绝、材料端点与数值溢出、Kirchhoff 请求绑定和有符号 Stokes。
相关实际公开工具及 helper 测试合计 421 passed、5 skipped，用时约 38 秒。
这些为 solver-free 契约与计算验收，不声明 surrogate 训练或 licensed native 求解已执行。

## ACC-010 实际失败

fake worker 先输出有效 ready 握手。manager 记录的 descendants 为空。
验收进程随后发送文件信号；worker 才创建 start_new_session=True 的有界 Python 等待子进程，然后退出。
reset 返回 success=true、absent=true、cleanup_errors=[]，实际后代仍处于运行状态。
_terminate_posix_group 在父已退出后不再查询 children，原组扫描也不能发现脱离组的进程。
ready/reset 的瞬时后代快照不能覆盖该时序。
探针只改 worker 命令，未修改 manager 身份字段；最终以 PID/create_time 核对并终止测试子进程。
BUG-052__late_detached_child.json 的 passed=false；exit=0 仅表示记录证据完成。
六字段记录与可能解决办法见 [acceptance_buglist.md](acceptance_buglist.md)。

## 测试与 CI

| 新进程检查 | 结果 | 耗时 |
| --- | --- | --- |
| BUG-001__cold_single | 1 passed in 1.48s | 1.986 秒 |
| BUG-001__cold_platform_posix | 21 passed in 4.60s | 5.375 秒 |
| BUG-001__reverse_order | 21 passed in 4.63s | 5.161 秒 |
| focused_unit_suite | 114 passed in 5.00s | 5.576 秒 |

CI 原命令两个 serial 分片：**5047 passed、0 failed、57 skipped、14 deselected**，304.025 秒。

- shard0：2575 passed, 26 skipped, 1 deselected in 241.01s (0:04:01)
- shard1：2472 passed, 31 skipped, 13 deselected in 302.24s (0:05:02)

schema 新进程对照：上轮 3 failed，目标 3 passed。
实际广告 181 工具与 current snapshot 的差异为 0；完整 snapshot 有 191 条，无工具增删。
逐路径审阅有 89 个 map additionalProperties false→true 和 5 个 thermal 契约变更；collection/string 上界仍保留。
deployment_manifest 更新了快照 hash；发布契约测试 57 passed。
GUI exact format 通过。Backend lint/format 通过，并执行完整本地质量门禁。

### 本地质量门禁与环境纠正

首次未指定 test ASCII root，默认 /home/agent/mcp_tests 位于只读文件系统。
首次结果 10 failed、4423 passed、57 skipped、614 errors，receipt stage=parallel_tests。
底层 OSError/Read-only filesystem 已保留；这是本次调用遗漏配置，不记为产品缺陷。
重跑只通过 COMSOL_MCP_TEST_ASCII_ROOT 与 cache 路径选择可写目录，没有修改测试或阈值。
重跑耗时 288.23 秒，exit=1，receipt status=`failed`。
receipt 的 failures：["command"]。
重跑主测试为 5047 passed、57 skipped；serial startup tail 为 2 failed、1 passed。
失败都是 0.75 秒预算：首样本 create_seconds=0.9278778100，七样本中位数=0.7676671080。
无 heavy native module 导入、无进程启动、56 工具等前置断言通过。
不能把本次完整本地 quality gate 写成通过；coverage/license/后续阶段未形成完整成功回执。
独立 startup suite 对照：上轮 3 passed，目标 3 passed，用时分别 17.721 / 17.146 秒。
完整命令和回执见 startup-budget-recheck.json，stdout/stderr 各自保留。
本轮未建立新提交造成性能回归的证据；原 gate 超时预算失败仍按实际结果记账。
该定向复验与首次 gate 结果分别保存，不能覆盖 gate 原始失败；单次时间偏差不被直接归为本次代码回归。
完整本地门禁采用四 worker main 与 serial startup tail；与 hosted 两 serial 分片分别记账。
未用首次环境失败或远端绿色状态替代本轮真实进程清理判断。

### 远端 CI 状态

[solver-free-ci #511](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/runs/37919936572) 的 head SHA 为本轮目标。
公开最终状态 Success，10 个 jobs 均 succeeded，总时长 14m04s，包括 paired coverage。
Windows、Ubuntu、两个 dependency lane、Python 3.15 preview、GUI、integration contracts 均有成功的公开 job 页面。
Actions API / gh --log 仍由代理返回 Forbidden；保存了原输出和全部公开 job HTML。
这些页面可证明远端公开结果，但未独立取得 raw stdout、覆盖和 license 等 artifact 详细回执。
Node.js 迁移文字为 warning；本轮没有 CI 失败根因可归给它。

## 后续动作与边界

工作 agent 需补足 BUG-052 生命周期内的后代跟踪或 Linux containment，并验证父先退出、ready 后派生和 detached 子进程。
无法完整确认时应返回未验证清理，不能给 absent=true。
修复后重跑 ACC-010，同时保留短握手、超长握手、同组/脱离组与 startup-failure 清理的正负向检查。
原 203 条审查没有被整体关闭。本报告只验收上述新提交和声明范围，不代表其余历史问题已修复。
报告、plan、问题状态、源码快照、原始失败/成功日志、JSON 回执按 BUG/ACC 命名保存。
未启动 COMSOL，未执行 licensed solver、hybrid 模型加载或完整真实 DSH host 验收。
