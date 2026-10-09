# Debian 独立验收与 CI 失败分析报告

生成时间：2026-10-09 17:05:55 +08:00。本次结论：**原始五项复现已修复；扩展验收有四项失败，本批不能作为完整验收通过。CI 也未通过。**

## 版本与范围

- 仓库：garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated。
- 基线：`da9f168baedfcca28d04d93369ef711240011d73`；原 203 条 buglist 保持原样。
- 用户确认目标：`bf9132e6a7b567853c2b89d7e5d1087891a4d4bd`，实际远端分支 `alpha7-7`。
- 用户最初称 alpha7-8；仓库无该分支，已确认验收上述 13 个提交。
- 两提交间有 13 个提交、51 个修改文件。验收范围是 BUG-001/002、004/005、041 及当前 CI 阻塞项。
- 独立 worktree：`/workspace/COMSOL_MCP_debian_fix_acceptance`。未修复代码，未启动 COMSOL。
- 宿主 Debian 13 x86_64，Python 3.14.7、Ruff 0.16.10、pytest 9.1.1；完整版本见 environment-and-revision.json。
- Python 验收显式导入目标 worktree，未将旧安装包当作修复版本。

## 每项验收结论

| 原 BUG | 原始场景 | 扩展验收 | 结论 |
| --- | --- | --- | --- |
| BUG-001 | 原始 AttributeError 已消失 | 冷进程单测、平台/POSIX 两种顺序、无 native 导入/回调的现有测试通过 | 本范围通过 |
| BUG-002 | 无写端 FIFO 清理不再阻塞 | identity/content、打开前换成 FIFO、末端/直接父级链接拒绝、正常身份删除、替换恢复均通过 | 本范围通过 |
| BUG-004 | 完整行+半行+正常追加后保留 first/second；新进程重开仍可读 | 另一进程已确认 B 后，A 用过期边界恢复会删除 B | 串行通过；并发耐久性不通过（ACC-001） |
| BUG-005 | 跨 owner 串行重复提交返回 task_owner_conflict，原 owner 保持查询/取消权 | A 查询为空后，独立进程 B 确认 task-b，A 仍可确认 task-a；同一 job 由两 owner 控制 | 串行通过；并发隔离不通过（ACC-002） |
| BUG-041 | inspect/acquire/release 的 FIFO、大文件与末端链接拒绝通过；发布验证遇 FIFO 有界拒绝 | 父目录链接被读取接受；release 可能抛异常。祖先链接替换可删除外部锁并返回 verified=true | 原始 IO 范围通过；路径扩展不通过（ACC-003/004） |

原始脚本 BUG-005 在目标版本退出 1，原因是它把 owner-b 成功复用作为后续动作；新版本按预期抛出 owner conflict。
新独立探针另行证明冲突不写 journal、owner-a 仍可访问、owner-b 查询/取消被拒绝。
不能将这个退出码单独当作修复失败。

原始 BUG-041 的 8 MiB 输入测量：基线峰值 16,778,916 字节；目标峰值 1,967 字节。
新增探针对异常文件验证 buffered read 句柄数为 0，确认在读取之前拒绝。
FIFO 场景没有写者；探针使用独立有界进程，全部未触发 12 秒上界。
所有替换、链接、FIFO 和日志文件均在验收临时目录；外部目标是本次创建的有限测试文件。

## 四项扩展失败证据

1. **ACC-001 / BUG-004**：B 的独立进程返回 acknowledged=true；随后 journal 从 first/B 变成 first/A。
   真实前后字节见 fixtures/BUG-004__concurrent。A 的调度 hook 只在真实读取之后暂停；B 使用真实存储。
2. **ACC-002 / BUG-005**：B 的独立进程持久化 task-b 后，A 根据旧快照持久化 task-a。
   同一 job-fixed 两条真实映射分别属于 owner-a/b；两个 owner 的取消都到达确定性引擎替身。
3. **ACC-003 / BUG-041**：直接父目录替换后，inspect 返回 active；release 抛 NotADirectoryError。
   首次未捕获异常与随后结构化捕获的输出均保留；外部文件未被删除。
4. **ACC-004 / BUG-041**：祖先目录被替换后，inspect 接受外部锁；release 返回 released=true/verified=true，外部锁被删。
   最终探针从构造时就使用嵌套 runtime 路径，只修改文件系统，未改 arbiter 的路径字段。

复现入口：`python independent_checks.py --case <BUG-NNN__case>`。
完整四项六字段报告及可能解决办法见 [acceptance_buglist.md](acceptance_buglist.md)。
每个实际结果都有 checks、回执、耗时、stdout/stderr；见 independent-results.json 和 independent-results/。

## 定向测试记录

| 新进程命令组 | 实际结果 | 包含启动开销的耗时 |
| --- | --- | --- |
| BUG-001__cold_single | 1 passed in 1.33s | 1.872 秒 |
| BUG-001__cold_platform_posix | 21 passed in 3.20s | 3.681 秒 |
| BUG-001__reverse_order | 21 passed in 3.13s | 3.584 秒 |
| focused_unit_suite | 109 passed in 1.40s | 1.839 秒 |

独立行为检查：24 项，20 项通过，4 项失败，0 外层超时。
额外 JobStore 路径链接检查通过：job_dir/read_state 均拒绝链接目录，外部 state.json 字节和文件列表未变。
原始失败日志、此次基线重放、目标重放各自保留，未用成功日志覆盖原失败。

## CI 状态与确定阻塞项

对应 [solver-free-ci #509](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/runs/37904487424)，head SHA 与验收目标一致。
最终公开状态为 Failure：7 jobs failed，2 succeeded，1 skipped；总时长 13m41s。
GitHub 页面和 annotation 原件保存在 ci-*.html；文本提取与结构化摘要分别保存。

| Job | 公开失败步骤 | 本地证据 / 判断 |
| --- | --- | --- |
| Windows unit-and-package | ratcheted quality gate | 同一 gate 在 format 阶段失败，2 文件不合格式 |
| Ubuntu backend | declared quality gate | 同一 gate 在 format 阶段失败；未进入 coverage/license/budget/installed wheel |
| Windows Settings GUI | GUI formatting and lint | workflow 原样格式范围有 1 文件不合格式 |
| integration-contracts | test_release_engineering.py | 56 passed / 1 failed：planning-code fingerprint mismatch |
| current-compatible dependencies | dependency/process suite | 同一静态 planning-code 断言在该套件中；Debian 两分片复现见下文 |
| minimum-supported dependencies | dependency/process suite | Debian 复现 planning-code、schema、semantic worker 三组失败；未重建 Windows minimum lane |
| Python 3.15 preview | preview regression/GUI suite | 上述三组代码同样进入该套件；本机未运行 Python 3.15，远端失败对应关系需完整日志 |
| Ubuntu Tk GUI | succeeded | 公开页面通过；该 job 没有运行 Windows GUI 的格式门禁 |
| locked runtime vulnerability policy | succeeded | 公开页面通过；本次未重复该平台 gate |
| paired coverage | skipped | 两个 prerequisite backend job 失败，未产出成功的 paired coverage 验收 |

### CI 阻塞 1：quality format

`comsol_mcp/durable/io.py:365` 的 read_flags 多行表达式需由格式器折叠。
`comsol_mcp/tools/model_identity.py:61` 的 next/generator 表达式需重新排版。
对应引入提交为 1ce4cce、131eef8。精确 quality gate 的 lint 步骤先通过，随后 format 失败。
保存的 quality-receipt.json 明确记载 command_failure.stage=format、coverage=null、solver_started=false。
修复方向：按当前 Ruff 格式修正这两处，再重跑原 gate。

### CI 阻塞 2：GUI format

`settings_gui/tests/test_discovery.py:71` 的 write_text 参数折行不符合格式器输出。
对应引入提交 1ce4cce。workflow 相同参数的格式检查返回 1，提示 1 file would be reformatted。
修复方向：仅整理该调用的格式，保留测试断言，再重跑 GUI 格式与 lint。

### CI 阻塞 3：planning-code 白名单指纹

fc5da9a 在 test_durable_job_control_plane.py 的前部新增 17 行链接拒绝测试。
原有唯一 h1 token 的字符偏移从 11335 变为 11923；token 与数量未变。
指纹包含 start，因此由 ff7f57cc… 变为 811bb52b…，白名单仍保存旧指纹。
release test 在 test_release_engineering.py:676 调用 verifier 后，报 mismatched=['development_kit/tests/test_durable_job_control_plane.py']。
完整旧/新 matches 和 hash 见 ci-planning-fingerprint.json。
修复方向：确认该 token 的兼容语义未变后，审阅并更新这一条精确指纹，保持现有数量与白名单要求。

### CI 阻塞 4：schema 快照与兼容期望未同步

ad184cf 修复了动态 map 被 additionalProperties=false 错误封闭的原 BUG-031。
181 个实际注册工具中有 25 个 schema 与 current snapshot 不同；逐 JSON 路径对照全部属于 additionalProperties。
3 个 schema 测试在独立基线进程均通过，在独立目标进程均失败；两分片也记录同样失败。
这是刻意修正 map 契约后缺少快照/兼容基线迁移，而不是增加 deadline 可以修复的故障。
完整差异见 CI__BUG-031__schema_comparison.json，实际 schema 另行冻结。
修复方向：审阅 map 契约变更，更新当前 snapshot、deployment identity 和 release facts，并明确历史兼容测试的允许变更。
历史 baseline 保留原件，公共对象/集合上界继续验证。

### CI 阻塞 5：semantic worker 正常短握手读取回归

131eef8 新增 _read_startup_line 使用 BufferedReader.read(4096)。
真实短管道写者已 flush 完整 ready 换行并保持运行，目标 reader 仍不返回，直到 EOF 才读出该行。
对照基线 readline 在相同场景立即返回。
实际 SemanticWorkerManager/fake worker 也做了前后对照：基线 start/health 成功，目标 startup_failed/startup deadline exceeded，清理回执 absent=true。
两分片有 14 个 semantic worker 失败，与这个确定性读取回归一致。
故障证据见 CI__BUG-053__baseline.json / target.json；探针的 exit=0 表示成功记录了行为，目标 functional_acceptance_passed=false。
修复方向：使用有界、按换行及时返回的读取原语，并保留超长行拒绝及短行正向测试。
增加 startup deadline 无法改变 read(4096) 等待缓冲区填满或 EOF 的行为。

### Debian 两分片重放

实际两分片命令完成，耗时 292.196 秒，返回 1，外层超时 False。

- shard0.log：3 failed, 2544 passed, 26 skipped, 1 deselected in 175.20s (0:02:55)
- shard1.log：15 failed, 2425 passed, 31 skipped, 13 deselected in 290.84s (0:04:50)

失败节点：

- development_kit/tests/test_release_engineering.py::test_active_implementation_has_only_enumerated_legacy_phase_codes
- development_kit/tests/test_tool_catalog.py::test_full_tool_schema_snapshot_is_stable
- development_kit/tests/test_tool_catalog.py::test_pre_h3_compatibility_snapshot_is_preserved
- development_kit/tests/test_semantic_worker_protocol.py::test_happy_path_reuses_one_worker_and_reset_verifies_absence
- development_kit/tests/test_semantic_worker_protocol.py::test_query_protocol_faults_are_contained_without_retry[query_hang]
- development_kit/tests/test_semantic_worker_protocol.py::test_query_protocol_faults_are_contained_without_retry[invalid_json]
- development_kit/tests/test_semantic_worker_protocol.py::test_query_protocol_faults_are_contained_without_retry[oversized_json]
- development_kit/tests/test_semantic_worker_protocol.py::test_query_protocol_faults_are_contained_without_retry[wrong_request_id]
- development_kit/tests/test_semantic_worker_protocol.py::test_query_protocol_faults_are_contained_without_retry[crash_before_response]
- development_kit/tests/test_semantic_worker_protocol.py::test_authentication_schema_and_message_bounds_do_not_kill_worker
- development_kit/tests/test_semantic_worker_protocol.py::test_queue_overflow_is_bounded_and_worker_recovers
- development_kit/tests/test_semantic_worker_protocol.py::test_health_remains_observable_while_query_holds_backend_lock
- development_kit/tests/test_semantic_worker_protocol.py::test_stale_identity_cleanup_reclaims_the_exact_spawned_process
- development_kit/tests/test_semantic_worker_protocol.py::test_crash_after_response_is_observed_without_process_leak
- development_kit/tests/test_semantic_worker_protocol.py::test_idle_ttl_stops_worker_lazily_without_wall_clock_margin
- development_kit/tests/test_semantic_worker_protocol.py::test_worker_pipes_are_drained_after_startup_handshake
- development_kit/tests/test_semantic_worker_protocol.py::test_context_manager_reaps_worker_when_test_body_raises
- development_kit/tests/test_tool_profiles.py::test_profile_name_and_schema_snapshots_are_exact

该命令按 CI 的 serial_test_shards.py 使用两个独立 serial pytest 分片，排除 control_plane_startup。
缓存、覆盖数据和临时目录迁出源码；保留了 CI 两分片共享 coverage base 的行为。
原始 shard0.log/shard1.log 及 helper 的失败尾输出各自保存。
合计 4969 passed、18 failed、57 skipped、14 deselected，用时约 292 秒。
18 个失败分组为：planning-code 1、schema 3、semantic worker 14。
三组均已结合代码对照、实际数据或独立新进程复验定位。
Debian 结果不能替代 Windows 两个 dependency lane 或 Python 3.15 的实际运行。

### CI 日志访问边界

GitHub 页面可读，明确显示失败 job 和失败步骤。
Actions API/gh 请求被网络代理返回 Forbidden；已申请网络读取权限，结果未改变。
访问失败原输出见 ci-api-access-failure.log。
因此，以上五组阻塞项有本地确定复现；远端完整原始 stdout、traceback、依赖版本与所有额外失败未取得。
没有将公开 annotation 的 exit code 1 写成远端完整根因证据。
Node.js 20 迁移消息是公开 warning；对应 upload-artifact action 也出现在成功 job 中。

## 修复与复验顺序

1. 处理 ACC-001/002 的跨进程临界区；要求另一进程已确认的行和 owner 约束在恢复/重试后仍成立。
2. 处理 ACC-003/004 的目录与文件身份固定；要求链接替换后不读取/删除外部目标，并返回未验证回执。
3. 修复 semantic 短握手读取回归，核对正常 worker 能及时进入 ready 状态。
4. 整理 current schema 与历史兼容期望，修正三处格式，审阅并更新单条 planning-code 指纹。
5. 重跑四个失败行为探针、五项原始复现、schema/semantic 定向复验、定向单测和 exact CI gates；保留所有失败/成功输出。
6. 在相同提交上重跑 Windows、Ubuntu、dependency lanes 和 Python 3.15；取得远端完整日志后核对额外失败。

本报告不声明 13 个提交的其余修复、完整 release gate、paired coverage 或原生 Linux 求解已验收通过。
验收补充问题共 8 条：4 条扩展行为问题、2 条 CI 功能/契约回归、格式和白名单维护问题各 1 条。
证据完整性和工作树核验见 final-verification.json；文件级执行记录见 plan.md。
