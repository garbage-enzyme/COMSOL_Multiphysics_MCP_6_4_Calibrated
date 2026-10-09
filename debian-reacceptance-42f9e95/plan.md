# 第二轮 Debian 验收计划

固定版本：42f9e95a2f2f33e76a06261b5eaf4801987d70e0。对照上轮 bf9132e。
只读验收，不改产品代码，不启动 COMSOL。原始证据保持不变。

| 文件 | 检查状态 | 核对内容 |
| --- | --- | --- |
| comsol_mcp/knowledge/semantic_process.py | 已检查 | 完整差异、startup reader 与生命周期相邻入口；read1 修复 |
| development_kit/release/planning_code_allowlist.json | 已检查 | 全文件、唯一变动 hash 与上轮计算值相同 |
| development_kit/tests/test_semantic_worker_protocol.py | 已检查 | 完整新增测试、子进程清理与 reader join |

| 工作 | 状态 |
| --- | --- |
| 固定版本与检查全部三文件 diff | 完成 |
| 24 个真实 IO 行为探针 | 完成：20 通过、4 失败、无超时 |
| 原始复现、冷进程和定向测试 | 完成：1/21/21/109 通过，原始五项仍已修复 |
| semantic/schema 对照与发布契约、格式门禁 | 完成：semantic 修复、57 契约测试通过；schema 3 失败，格式仍失败 |
| CI 两分片重放 | 完成：4984 通过、4 失败；16/15 adjoint 对照已完成 |
| 报告、问题状态与证据包 | 已生成并执行完整性核验；GitHub 交付见独立 delivery receipt |


## 新失败路径核对

| 文件 | 状态 | 内容 |
| --- | --- | --- |
| comsol_mcp/jobs/manager.py | 已检查 | watchdog 完整启动函数、相邻 worker identity 有界确认、submit 失败回执；本次无修改 |
| comsol_mcp/jobs/store.py | 已检查 | process_identity 的空命令行与 zombie 分类，完整相邻入口；本次无修改 |
| development_kit/tests/test_adjoint_optimization.py | 已检查 | 两个失败函数、真实 watchdog 进程与 fake worker、前后全部 16 项测试；本次无修改 |

## 新失败路径核对

| 文件 | 状态 | 内容 |
| --- | --- | --- |
| comsol_mcp/jobs/manager.py | 已检查 | watchdog 完整启动函数、相邻 worker identity 有界确认、submit 失败回执；本次无修改 |
| comsol_mcp/jobs/store.py | 已检查 | process_identity 的空命令行与 zombie 分类，完整相邻入口；本次无修改 |
| development_kit/tests/test_adjoint_optimization.py | 已检查 | 两个失败函数、真实 watchdog 进程与 fake worker、前后全部 16 项测试；本次无修改 |
