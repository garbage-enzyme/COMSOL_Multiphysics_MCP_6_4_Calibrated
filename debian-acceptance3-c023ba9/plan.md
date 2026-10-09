# 第三轮 Debian 验收计划

固定目标 c023ba9a470f0c1cd60b6692713d26c27e2f4a6a，对照 42f9e95；新增 2 个提交、37 个文件。
只读验收，产品源码不改动，不安装或启动 COMSOL。每检查完一个文件即更新此记录。

| 文件 | 状态 | 核对范围 |
| --- | --- | --- |
| README.md | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| README_CN.md | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/contracts/thermal_radiation.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/deployment_manifest.json | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/durable/io.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/evidence/surrogate_evidence.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/evidence/thermal_material.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/evidence/thermal_radiation.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/jobs/manager.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/jobs/surrogate_training.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/jobs/tasks_bridge.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/knowledge/semantic_process.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/operation_arbiter.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/settings.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/shared_session/process_probe.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/shared_session/solver_owners.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| comsol_mcp/surrogate/export.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/surrogate/registry.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/tools/model_identity.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| comsol_mcp/tools/surrogate.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/integration/semantic_feature_acceptance.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| development_kit/tests/snapshots/full_tool_schemas.json | 已检查 | 完整 JSON 逐路径比较：89 个 map False→True，5 个 thermal 契约变化，无增删工具；实际注册快照复验 |
| development_kit/tests/test_adjoint_optimization.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| development_kit/tests/test_integration_boundaries.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/test_operation_arbiter.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/test_semantic_worker_protocol.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| development_kit/tests/test_settings.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| development_kit/tests/test_shared_session_preflight.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| development_kit/tests/test_solver_owners.py | 已检查 | 完整修改差异及原始契约核对；实际行为/测试正在复验 |
| development_kit/tests/test_surrogate_public_tools.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/test_surrogate_registry.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/test_surrogate_training_job.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/test_tasks_extension.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/test_thermal_material.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| development_kit/tests/test_thermal_radiation.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| dsh_bridge/tests/plugin.test.mjs | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |
| settings_gui/tests/test_discovery.py | 已检查 | 完整修改差异核对；新增测试与对应实现、拒绝契约已阅读 |

## 执行计划

1. 阅读全部 37 个修改文件的差异与相关入口，核对 Linux 修复及其他声明。
2. 重跑原始五项复现、上轮问题与新增修复正负向行为。
3. 修复改变同步策略时，调整探针调度以验证锁内串行；保留旧探针的兼容性说明。
4. 运行关联现有测试、完整 CI 两分片和适用门禁，保存 stdout/stderr。
5. 对失败定位并立即写独立六字段 buglist，不改产品代码。
6. 生成报告、问题状态和按编号命名的证据包，校验后通过 buglist 分支交付。

## 完成回执

全部 37 文件检查完成；24 IO 通过；5047 两分片通过；421 关联测试通过、5 跳过；69 DSH bridge 通过；五 profile 实际 stdio discovery 通过。
原 ACC-001..009 本范围通过，新增 ACC-010 保留六字段及真实证据。
本地 quality 首次为只读默认目录环境错误；配置重跑状态 failed。
报告/包生成并核验，GitHub 交付见独立 delivery receipt。
