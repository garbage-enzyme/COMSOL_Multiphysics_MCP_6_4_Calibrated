# Debian 修复验收计划与完成记录

固定目标：bf9132e6a7b567853c2b89d7e5d1087891a4d4bd；基线 da9f168。
用户确认实际分支 alpha7-7，验收基线后的 13 个提交。
本次只读验收；未修复、提交或推送任何产品代码，未启动 COMSOL。

| 工作项 | 状态 | 证据 |
| --- | --- | --- |
| 固定分支/提交、独立 worktree | 已完成 | environment-and-revision.json；13 提交明确，原工作树保留 |
| 原报告和失败日志冻结 | 已完成 | original-evidence/；九个原件 SHA256 与来源一致 |
| 五项基线重放 | 已完成 | baseline-replay-index.json 与原始 stdout/stderr |
| 原始目标重放 | 已完成 | adapted-original-probes/、target-command-results/；只换目标路径 |
| BUG-001 冷进程与顺序 | 已完成 | 1/21/21 passed，实际 guard/callback/native-import 断言 |
| BUG-002 FIFO、链接、身份与恢复 | 已完成 | 七项独立行为检查通过 |
| BUG-004 串行与并发恢复 | 已完成 | 串行新进程重开通过；B 已确认行被 A 的过期 truncate 删除 |
| BUG-005 双 owner 与同 owner retry | 已完成 | 串行隔离通过；独立 B 进程与 A 仍可绑定同一 job |
| BUG-041 全 IO 入口与链接边界 | 已完成 | FIFO/超大/末端链接/发布验证通过；父与祖先路径替换失败 |
| 新增 JobStore 链接拒绝 | 已完成 | 外部字节与目录内容不变 |
| 关联定向测试 | 已完成 | 109 passed；原始异常日志未覆盖 |
| CI GitHub 状态与步骤采集 | 已完成（日志 API 受限） | #509 最终 7 failed/2 succeeded/1 skipped；公开页面原件与 API Forbidden 输出 |
| 精确 quality / GUI format / release contract | 已完成 | 两处 backend format、一处 GUI format、planning 指纹不匹配 |
| CI 两 serial 分片重放 | 已完成 | 292.196 秒；4969 passed、18 failed、57 skipped、14 deselected |
| CI semantic 回归独立对照 | 已完成 | 短管道 flush/EOF 与真实 fake-worker 基线/目标 start、health、cleanup |
| CI schema 回归独立对照 | 已完成 | 基线 3 passed、目标 3 failed；25 工具差异全为 additionalProperties |
| 八个问题六字段记录 | 已完成 | acceptance_buglist.md |
| 报告、原件、源码快照与封包核验 | 已完成 | acceptance-report.md、final-verification.json、SHA256SUMS.txt |

## 涉及文件的阅读记录

基线文件已在上轮 780 项逐文件审查中阅读。本轮先完整读取 13 提交相关 diff，再核对下列实际入口与失败路径。
下表的完成范围是所列函数/契约，不代表重新审查新目标的全部 780 文件。

| 文件 | 状态 | 本轮阅读与核对范围 |
| --- | --- | --- |
| comsol_mcp/durable/io.py | 已检查 | 完整读取；descriptor 类型、非阻塞、身份、quarantine、bounded read、JSONL 分类 |
| comsol_mcp/jobs/tasks_bridge.py | 已检查 | 模块契约、完整差异、append/rows/submit/owner/query/cancel 及相邻调用 |
| comsol_mcp/operation_arbiter.py | 已检查 | 完整读取；inspect/acquire/publication/release、cache 与 guard finally |
| comsol_mcp/server.py | 已检查 | Tasks 工厂、共享 root、环境 owner；证明不同实例可共用日志 |
| comsol_mcp/jobs/store.py | 已检查 | 完整修复差异、JobLock 拒绝与 JobStore constructor/job_dir/read_state 路径 |
| comsol_mcp/jobs/journal.py | 已检查 | 完整读取；locked_journal/recover_jsonl_tail；Tasks append 未使用同一锁 |
| development_kit/tests/test_platform_support.py | 已检查 | 原始完整测试、目标完整修改与冷进程新命令 |
| development_kit/tests/test_posix_platform.py | 已检查 | 完整基线文件、确认无变更；两种顺序与恢复/链接断言 |
| development_kit/tests/test_durable_primitives.py | 已检查 | 完整读取；普通类型、identity/growth、JSONL 分类 |
| development_kit/tests/test_tasks_extension.py | 已检查 | 完整读取；原半行测试仅读、不追加；owner 原测试未覆盖重复提交并发 |
| development_kit/tests/test_operation_arbiter.py | 已检查 | 新增完整 diff、原测试/调用与定向运行；扩展独立探针补足缺口 |
| development_kit/tests/test_durable_job_control_plane.py | 已检查 | 新增链接测试、planning token 原件与目标字符偏移 |
| .github/workflows/ci.yml | 已检查 | 所有 job 命令/needs/失败步骤，目标与基线文件无修改 |
| development_kit/scripts/quality_gate.py | 已检查 | 声明 lint 范围、format 顺序、失败 receipt 和后续未运行阶段 |
| development_kit/scripts/planning_code_gate.py | 已检查 | 完整读取；token regex、start 指纹和精确 allowlist |
| development_kit/release/planning_code_allowlist.json | 已检查 | 旧/新匹配计数和精确 hash；未修改名单 |
| development_kit/tests/test_release_engineering.py | 已检查 | 原完整审查；本轮失败断言/调用/完整异常与独立运行 |
| comsol_mcp/knowledge/semantic_process.py | 已检查 | 全部修改、startup reader/Popen/queue/deadline/cleanup 入口与真实管道 |
| comsol_mcp/knowledge/semantic_worker.py | 已检查 | ready 短行、flush 后 serve_forever；实际 fake-worker 生命周期 |
| development_kit/tests/test_semantic_worker_protocol.py | 已检查 | 新超长测试、全部 14 失败节点相关原函数及 worker 正常路径 |
| comsol_mcp/contracts/structural.py | 已检查 | schema visitor/map 修复及递归差异；原始基线完整审查 |
| development_kit/tests/test_tool_catalog.py | 已检查 | 快照、legacy 兼容断言与实际 schemas 完整逐路径比较 |
| development_kit/tests/test_tool_profiles.py | 已检查 | profile 快照失败节点与原完整审查；独立前后测试 |
| development_kit/scripts/regenerate_tool_snapshots.py | 已检查 | 完整读取；current 快照、deployment/release facts 更新流程；未执行写入 |
| development_kit/scripts/serial_test_shards.py | 已检查 | 完整读取；两进程、coverage 参数、原始 log 和失败尾 |
| comsol_mcp/tools/model_identity.py | 已检查 | 新 generator 表达式、commit blame 与 exact format 输出 |
| settings_gui/tests/test_discovery.py | 已检查 | 新折行、commit blame 与 exact GUI format 输出 |

## 结果边界

原始五项复现已修正。扩展行为仍有四项失败，CI 本地有五组确定阻塞，补充问题共八条。
本机未执行 Windows、Python 3.15、licensed native COMSOL 或完整 paired acceptance。
原始日志保留；探索性调度的非最终证据分别标注，最终结论使用当前独立探针。
