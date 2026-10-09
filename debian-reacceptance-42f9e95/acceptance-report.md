# 第二轮 Debian 验收与 CI 分析报告

生成时间：2026-10-09T18:05:35+08:00。**结论：本次两项修复在 Debian 验收通过；整体仍不通过。**

## 固定版本与范围

- 仓库：garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated，远端 alpha7-7。
- 目标：`42f9e95a2f2f33e76a06261b5eaf4801987d70e0`，上轮：`bf9132e6a7b567853c2b89d7e5d1087891a4d4bd`。
- 增加 1 个提交，修改 3 个文件，33 insertions / 2 deletions。
- 全部差异与三文件相关行为已阅读，逐文件记录见 plan.md。
- 使用原 Debian 13 / Python 3.14.7 / Ruff 0.16.10 开发环境。未改产品代码，未安装或启动 COMSOL。
- 原 203 条问题包及上轮报告、原始失败日志保留。本轮覆盖原五项 Linux 复现、上轮八条问题与 CI 阻塞。

## 问题的复验状态

| 编号 | 第二轮结果 |
| --- | --- |
| ACC-001 | 未修复，实际并发丢记录再次复现 |
| ACC-002 | 未修复，独立进程双 owner 再次复现 |
| ACC-003 | 未修复，父链接读取与 release 异常再次复现 |
| ACC-004 | 未修复，祖先链接导致外部测试锁被删再次复现 |
| ACC-005 | 本范围验收通过，短行及时返回，实际 worker start/health/reset 成功 |
| ACC-006 | 未修复，3 schema 测试仍失败，25 工具差异未变 |
| ACC-007 | 未修复，backend 两处和 GUI 一处 format 仍失败 |
| ACC-008 | 本范围验收通过，57 发布契约测试与远端 integration-contracts 成功 |
| ACC-009 | 本轮新记录，watchdog 身份读取间歇失败，上轮版本也可复现 |

ACC-005 的生产读取现在使用 BufferedReader.read1，短 ready 行 flush 后，写者保持存活时 reader 已返回。
独立探针实际 worker start.success=true、health.success=true，reset.absent=true。
上轮版本在同一环境仍返回 startup deadline exceeded，证明修复前后行为改变。
新增正向测试及原超长握手测试进入此次完整 CI 分片；semantic worker 未再出现失败。
此次未运行 hybrid 模型载入或 Windows 原生进程；不能据此声明全部后端和平台验收通过。

ACC-008 仅更新已审阅的精确指纹为 811bb52b…，未扩大白名单或降低检查要求。
发布契约测试为 57 passed，远端对应 integration-contracts 也 succeeded。

六条仍失败问题没有对应代码修改，且已重新执行实际探针或 exact gate。
半行恢复仍能删掉另一进程已确认的 B 行；同一 job 仍能归给两个 owner。
直接父目录链接仍被读取接受并导致 release 异常；祖先链接仍可删除外部测试锁并返回 verified=true。
这些测试只操作本轮创建的临时文件，只有 solver engine 使用确定性替身。
六字段问题和可能解决办法见 [acceptance_buglist.md](acceptance_buglist.md)。

## 原始 Linux 功能与定向检查

原始 BUG-001/002/004/005/041 场景仍已修复。
24 项独立实际 IO 检查：20 通过、4 失败、0 外层超时。
失败场景：BUG-004__concurrent, BUG-005__concurrent, BUG-041__parent_replacement, BUG-041__ancestor_replacement。
FIFO 无写者检查均及时返回；串行 partial-tail 恢复保留完整记录并可新进程重开。
串行 owner 冲突拒绝，不修改原映射，原 owner 保持查询/取消权。
原 BUG-005 旧探针退出 1 是预期 owner conflict；另有真实行为断言，未按该退出码判失败。

| 新进程检查 | 结果 | 实际耗时 |
| --- | --- | --- |
| BUG-001__cold_single | 1 passed in 1.35s | 1.846 秒 |
| BUG-001__cold_platform_posix | 21 passed in 3.54s | 4.059 秒 |
| BUG-001__reverse_order | 21 passed in 3.37s | 3.84 秒 |
| focused_unit_suite | 109 passed in 1.32s | 1.751 秒 |

## CI 本地复验

- Backend quality gate：lint 通过，format 失败。仍为 durable/io.py:365 与 tools/model_identity.py:61 两处。
- GUI exact format：仍为 settings_gui/tests/test_discovery.py:71 一处。
- 发布契约：57 passed，不再出现 planning-code mismatch。
- schema：上轮和目标各 3 failed。181 工具中 25 工具与 current snapshot 不同；差异仍全部为 additionalProperties。
- semantic：独立真实短管道、实际 fake worker 启动/健康/清理通过；完整分片未出现该组失败。

CI 原命令两 serial 分片重放，实际耗时 234.945 秒，返回 1，外层超时 false。
合计 **4984 passed、4 failed、57 skipped、14 deselected**。

- shard0.log：3 failed, 2544 passed, 26 skipped, 1 deselected in 173.12s (0:02:53)
- shard1.log：1 failed, 2440 passed, 31 skipped, 13 deselected in 233.56s (0:03:53)

本轮四个失败节点：

- development_kit/tests/test_adjoint_optimization.py::test_native_adjoint_worker_fails_closed_without_armed_watchdog
- development_kit/tests/test_tool_catalog.py::test_full_tool_schema_snapshot_is_stable
- development_kit/tests/test_tool_catalog.py::test_pre_h3_compatibility_snapshot_is_preserved
- development_kit/tests/test_tool_profiles.py::test_profile_name_and_schema_snapshots_are_exact

上轮 14 个 semantic worker 和 1 个白名单失败已消失，3 个 schema 失败仍在。
本轮另出现 1 个 watchdog 身份读取失败，完整分片为 4 个失败，不能写成 3 个。
定向复验目标 adjoint 文件 16 passed；上轮文件 15 passed/1 failed，出现同类 OSError。
相关三文件在本次提交无变化；将其记为已有间歇故障 ACC-009，未归为本次新增回归。
watchdog Popen 后只读取一次身份，空 cmdline 被直接拒绝；具体内核时序未做跟踪。
需要有界身份确认和负向验收，不应放宽身份要求或丢弃原失败日志。
本次增加 1 个短握手测试；因此总测试数量也变化。
剩余 schema 快照/兼容期望应经过契约审阅迁移，不能直接回退动态 map 的功能修复来消除失败。
完整原始 shard0.log/shard1.log 和全部命令 stdout/stderr 已保存。

## 远端 CI

对应 [solver-free-ci #510](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/runs/37912552977)，head SHA 与目标相同。
最终 Failure：6 failed、3 succeeded、1 skipped，总时长 12m13s。

| Job | 公开状态 | 公开失败步骤 |
| --- | --- | --- |
| dependency compatibility (minimum-supported, Python 3.14) | failed | Run dependency and process regression suite |
| Settings GUI, package, and installed entry | failed | Check GUI formatting and lint |
| locked runtime vulnerability policy | succeeded | 无失败 annotation |
| Ubuntu 26.04 x64 shared Tk GUI and owned launcher (solver-free) | succeeded | 无失败 annotation |
| unit-and-package (Python 3.14, default production lane) | failed | Run ratcheted quality, coverage, license, and budget gates |
| Ubuntu 26.04 x64 backend and installed wheel (solver-free) | failed | Run the declared quality gate without a solver |
| Python 3.15 preview compatibility (not production) | failed | Run preview solver-free regression and GUI tests |
| integration-contracts | succeeded | 无失败 annotation |
| dependency compatibility (current-compatible, Python 3.14) | failed | Run dependency and process regression suite |
| Required Windows and Ubuntu branch coverage | skipped | 无失败 annotation |

本地证明仍有 backend/GUI 格式和 schema 阻塞；公开 annotation 确认对应 gate/suite 失败。
Actions API / gh --log-failed 仍被代理返回 Forbidden，原输出保存在 ci-api-access-failure.log。
公开 HTML 与 annotation 不等于远端完整 stdout；未取得 Windows dependency lanes 和 Python 3.15 的全部原始失败。
本地 Debian 4 个失败不能用于推断远端全部额外失败。
Node.js 20 迁移文字为 warning，不能作为此次失败根因。
paired coverage 因 prerequisite gate 失败而 skipped，未验收通过。

## 交接与后续验收

工作 agent 仍需修复 ACC-001/002 的跨进程临界区与 ACC-003/004 的路径、文件身份固定。
工作 agent 需审阅并同步 schema 快照、兼容允许变更、deployment/release facts，修正三处格式。
工作 agent 需处理 ACC-009 的 watchdog 有界身份确认，保留持续不可读/身份不符的拒绝行为。
修复后重跑四个失败探针、当前三项 schema 测试、exact format/quality、原始 Linux 场景与平台 CI。
Windows、Python 3.15、licensed COMSOL 和完整 paired coverage 仍需各自证据。
证据按 BUG/ACC 名称组织，包内 source 是固定提交的源码快照，SHA256SUMS 用于完整性核对。
此前的原始失败日志独立保存，未被本轮成功输出覆盖。
