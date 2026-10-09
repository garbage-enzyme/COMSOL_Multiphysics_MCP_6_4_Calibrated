# 第三轮验收问题与历史状态

固定目标：`c023ba9a470f0c1cd60b6692713d26c27e2f4a6a`。保留原六字段和旧复现；每项第三轮状态是当前结论。
原 203 条报告及历轮原始失败日志保持不变。ACC-001 至 ACC-009 本范围通过；ACC-010 未解决。

## ACC-001（关联 BUG-004）

**第三轮状态**：本范围通过：恢复事务内 B 阻塞，释放后两写者均确认，first/A/B 全部保留；同一探针上轮仍丢 B。



- **编号**：ACC-001 / BUG-004
- **标题**：半行恢复使用过期边界，截掉另一进程已确认的 Tasks 映射
- **严重性/优先级**：高 / P1；已确认状态丢失。
- **位置**：`comsol_mcp/jobs/tasks_bridge.py:244`，`TasksMappingStore.append()` 的读取、truncate 和追加之间；`comsol_mcp/durable/io.py:545` 的实际追加。
- **内容/复现方法**：串行原始复现已修复。扩展场景有两个写者共用任务目录：A 读取“first 完整行 + 半行”的完整边界后暂停；独立 Python 进程 B 使用真实 store 修复尾部、追加 B，并确认 `latest_for_task('B')` 存在；A 使用过期边界截断，再追加 A。最终 rows 为 `['first', 'A']`，B 已确认的行消失。探针只替换调度时机，不替换持久化函数。执行 `python independent_checks.py --case BUG-004__concurrent`。日志见 `independent-results/BUG-004__concurrent__stdout.log`；前后原始日志字节见 `fixtures/BUG-004__concurrent/`。
- **可能的解决办法**：为同一任务映射目录引入跨进程同步；在同一临界区重新读取恢复状态、截断、追加、fsync 并验证映射。锁文件与日志的类型、身份及链接检查应覆盖整个操作。保留第二写者已确认记录的回归断言。


## ACC-002（关联 BUG-005）

**第三轮状态**：本范围通过：跨进程 owner admission 串行，B 返回 task_owner_conflict；上轮仍双 owner。



- **编号**：ACC-002 / BUG-005
- **标题**：owner 检查与映射写入之间可穿插另一进程，同一作业仍能归给两个 owner
- **严重性/优先级**：中 / P2；多 owner 控制隔离失效。
- **位置**：`comsol_mcp/jobs/tasks_bridge.py:416` 至 `443`，`latest_for_job()`、owner 检查与 `append()`。
- **内容/复现方法**：串行重复提交现在返回 `task_owner_conflict`，原 owner 仍有访问权。并发扩展中，A 查到 job 尚无映射后暂停；独立进程 B 为同一个 `job-fixed` 提交并确认 `task-b`；A 用旧的空快照继续写入 `task-a`。最终真实 JSONL 有 owner-a 和 owner-b 两条映射，两者都可查询，并可使 fake engine 收到该 job 的取消请求。使用真实 TasksBridge/Store，只有求解引擎是确定性替身；没有启动 COMSOL。执行 `python independent_checks.py --case BUG-005__concurrent`。日志与映射字节见同名目录。
- **可能的解决办法**：将 job-owner 查询、冲突检查和映射持久化纳入同一跨进程临界区。对已经存在的 job-owner 绑定做原子比较；确认只有获准 owner 的映射可被确认。增加双进程共享目录的重复提交验收。


## ACC-003（关联 BUG-041 的扩展路径检查）

**第三轮状态**：本范围通过：父目录链接 inspect/acquire/release 拒绝，无未处理异常，外部字节保留。



- **编号**：ACC-003 / BUG-041
- **标题**：父目录被链接替换后，operation lock 读取接受外部锁，release 抛出未处理异常
- **严重性/优先级**：中 / P2；状态检查与清理行为不一致。
- **位置**：`comsol_mcp/durable/io.py:40` 的普通文件打开；`comsol_mcp/operation_arbiter.py:70`、`86`、`293`、`307`；guard 的 `finally` 直接调用 release。
- **内容/复现方法**：已构造 arbiter 并创建真实 claim 后，将其 runtime 父目录替换为指向另一个目录的链接。外部目录包含 claim 相同字节、不同 inode 的测试锁文件。bounded reader 跟随父目录，inspect/try_acquire 将它报告为 active；release 的后续清理在 `O_NOFOLLOW` 打开直接父目录时抛 `NotADirectoryError`，没有返回失败清理回执。外部锁保留。执行 `python independent_checks.py --case BUG-041__parent_replacement`。原始首轮异常日志也保留在 `independent-results-first-pass/`。
- **可能的解决办法**：将 runtime 目录身份和逐级无链接路径检查固定到 claim 生命周期；读取与清理复用同一目录句柄/身份。将路径变化与清理 IO 异常转换为明确的未验证回执。


## ACC-004（关联 BUG-041 的扩展路径检查）

**第三轮状态**：本范围通过：逐级无链接目录打开；祖先链接不读/删外部文件，claim inode 参与清理。



- **编号**：ACC-004 / BUG-041
- **标题**：祖先目录链接替换可使 release 删除另一目录的锁，并声称已验证清理
- **严重性/优先级**：高 / P1；删除了未由该 claim 发布的文件，清理回执失真。
- **位置**：`comsol_mcp/durable/io.py:390` 的 parent 打开，仅拒绝最后一级链接；`comsol_mcp/operation_arbiter.py:307` 的内容匹配清理。
- **内容/复现方法**：从 `container/runtime` 构造真实 arbiter；在构造和 claim 发布之后，将 container 改名，并将它替换为链接。链接目标的 runtime 是普通目录，其中测试锁与 claim 字节相同，但 inode 不同。读取和清理都跟随祖先链接，release 删除这个外部测试锁，返回 `released:true, verified:true`。最终探针不修改 arbiter 的路径字段，只改变测试文件系统。执行 `python independent_checks.py --case BUG-041__ancestor_replacement`。最终日志见 `independent-results/`；不纳入结论的早期路径字段调度探针已单独标注在 `exploratory-ancestor-schedule/`。
- **可能的解决办法**：逐级验证并固定目录身份，拒绝生命周期内的路径替换。清理需要 claim 发布时的文件身份，并使用固定目录句柄定位该 inode；相同内容不足以证明目标属于当前 claim。增加祖先目录替换后外部文件保持不变的回归断言。


## ACC-005（CI 回归，关联 BUG-053 修复）

**第三轮状态**：保持通过：短握手在 writer 存活时返回，实际 worker start/health/reset 成功。



- **编号**：ACC-005 / BUG-053
- **标题**：新 bounded 握手读取等待 4096 字节，正常 semantic worker 启动失败
- **严重性/优先级**：高 / P1；已运行的 semantic worker 正常启动功能回归。
- **位置**：`comsol_mcp/knowledge/semantic_process.py:221`，`_read_startup_line()`；引入提交 `131eef8`。
- **内容/复现方法**：默认 Popen stdout 是 BufferedReader。新的 read(4096) 会等待所请求字节或 EOF，换行不会使它提前返回。真实短管道探针确认写者已 flush 完整换行且仍活着，目标 reader 在 0.4 秒内不返回，直到结束进程触发 EOF 才读出 ready 行；基线 readline 立即返回。同一环境实际 SemanticWorkerManager 的 fake backend：基线 start/health 成功，目标 start 返回 startup_failed/startup deadline exceeded 并清理子进程。两分片原日志有 14 个 semantic-worker 失败。执行 `CI__BUG-053__semantic_startup_probe.py --repository <基线或目标仓库>`；前后 JSON 和完整输出均保存。
- **可能的解决办法**：使用不会等待填满缓冲区的有界行读取；例如 readline(maximum+1) 后检查长度/结束条件，或 read1/read 原始描述符。保留 64 KiB 上界和超长行拒绝，同时增加“短行 flush、写者保持运行”的正向测试及正常 fake/hybrid 握手验收。


## ACC-006（CI 契约回归，关联 BUG-031 修复）

**第三轮状态**：本范围通过：current schema 与实际广告一致，3 项 schema 回归测试通过，deployment hash 已更新。



- **编号**：ACC-006 / BUG-031
- **标题**：动态 map schema 修复后，当前快照和历史兼容测试未同步
- **严重性/优先级**：中 / P2；发布与客户端 schema 契约不一致。
- **位置**：`comsol_mcp/contracts/structural.py:106`；`development_kit/tests/snapshots/full_tool_schemas.json`；`test_tool_catalog.py:56`、`103`、`test_tool_profiles.py:120`；引入提交 `ad184cf`。
- **内容/复现方法**：新的 bounded_public_schema 不再把无固定 properties 的 map 强制封闭，这是对 BUG-031 的功能性修复。但当前冻结 snapshot 未改，兼容测试还用旧闭合 schema 作比较。181 个实际注册工具中有 25 个与当前快照不同，递归差异全部在 additionalProperties。3 个 schema 测试在独立基线进程全部通过，在独立目标进程全部失败，两分片也记录同样失败。运行 `CI__BUG-031__schema_comparison.py` 得到逐工具逐 JSON 路径差异；原始历史快照未修改。
- **可能的解决办法**：审阅并记录此次 map 契约变更，使用项目当前快照生成流程更新 current schema/deployment identity/release facts。保留历史 baseline，明确兼容测试允许的 map 变化并保留 maxProperties 等上界；复验安装入口和客户端 schema。


## ACC-007（CI 格式门禁）

**第三轮状态**：本范围通过：backend lint/format 与 GUI format 均通过。



- **编号**：ACC-007
- **标题**：三处新增格式不符合当前 Ruff，阻断 backend 和 Windows GUI CI
- **严重性/优先级**：中 / P2；发布门禁失败。
- **位置**：`comsol_mcp/durable/io.py:365`、`comsol_mcp/tools/model_identity.py:61`、`settings_gui/tests/test_discovery.py:71`；引入提交 `1ce4cce`、`131eef8`。
- **内容/复现方法**：精确 quality_gate 的 lint 通过、format 失败，提示两个文件需要重排版，receipt 的 stage=format。workflow 原样 GUI 格式范围提示 test_discovery.py 一处需要重排版。对应 GitHub job 公共 annotation 分别显示失败在 quality gate 与 GUI formatting/lint。原始 exact command 输出保存在 ci-local-results。早期 ruff '.' 扫描包含非 CI recipes，不作为这三处的门禁依据。
- **可能的解决办法**：按当前 Ruff 规范整理这三个调用/表达式的格式，保持功能和断言；重跑项目原 gate 与 workflow GUI 格式、lint 范围。


## ACC-008（CI 发布白名单）

**第三轮状态**：保持通过：57 个发布契约测试通过，远端 integration-contracts succeeded。



- **编号**：ACC-008
- **标题**：新增测试移动历史 planning-code 偏移，白名单精确指纹未更新
- **严重性/优先级**：中 / P2；发布契约门禁失败。
- **位置**：`development_kit/tests/test_durable_job_control_plane.py:32`；`development_kit/release/planning_code_allowlist.json:23`；`development_kit/scripts/planning_code_gate.py:45`；引入提交 `fc5da9a`。
- **内容/复现方法**：新增 17 行链接拒绝测试后，原有唯一 h1 token 的 start 从 11335 移到 11923，匹配数量与 token 未变。指纹包含 start，旧值 ff7f57cc…、目标值 811bb52b…；白名单未同步。精确 integration-contract 命令为 56 passed/1 failed，两分片也有同一失败。ci-planning-fingerprint.json 保存完整匹配及哈希对照。
- **可能的解决办法**：审阅该历史 token 语义未变后更新这条精确白名单指纹，保持枚举数量和 require_all_allowlisted 校验；重新运行 release engineering 和 CI 契约套件。


## ACC-009（本轮发现，前一版本也可复现）

**第三轮状态**：本范围通过：watchdog 暂时空命令行有界重试；持续为空、错签名、退出拒绝；真实 adjoint 测试通过。



- **编号**：ACC-009
- **标题**：watchdog 刚启动时仅尝试一次进程身份读取，暂时为空的命令行使正常作业提交失败
- **严重性/优先级**：中 / P2；正常作业提交间歇失败，相关 CI 测试不稳定。
- **位置**：`comsol_mcp/jobs/manager.py:791`，`_arm_robust_wall_watchdog_if_required()`；`comsol_mcp/jobs/store.py:118`，`process_identity()`。
- **内容/复现方法**：本轮真实两分片中 `test_native_adjoint_worker_fails_closed_without_armed_watchdog` 在 manager.submit() 失败，尚未执行测试的 watchdog 删除断言。traceback 显示新启动的 watchdog PID 19305 的 cmdline 为空，status 不是 zombie，process_identity 抛 OSError。manager 将其记录为 launch_failed 并触发 exact-attempt cancel，最终抛 JobLaunchError。相同测试文件定向复验：目标 16 passed；上轮 bf9132e 为 15 passed/1 failed，在 `test_real_adjoint_submission_arms_the_wall_watchdog` 中出现同一调用链、同一 OSError。实际 watchdog Popen 后立即读取一次，无有界重试。此处进程身份读取失败有真实日志证明；空 cmdline 的具体内核时序未做跟踪，不声明每次执行必现。复验入口 `python recheck_adjoint.py`，完整日志见 `ci-dependency-shards/shard0.log` 和 `ci-local-results/adjoint_recheck_previous__stdout.log`。测试未安装或启动 COMSOL。
- **可能的解决办法**：为 watchdog 复用 worker 的有界身份确认机制；未到截止且子进程仍运行时，对暂时不可读/空命令行做短间隔重试，成功后仍严格核对 PID、创建时间和期望命令签名。截止、退出或身份不符继续拒绝并返回清理证据。增加首次为空、随后有效的测试及持续为空/退出负向测试；不要降低身份校验要求。

## ACC-010（关联 BUG-052）

- **编号**：ACC-010 / BUG-052
- **标题**：ready 后新建并脱离进程组的后代，在主 worker 退出后仍被漏清理，却返回 absent=true
- **严重性/优先级**：中 / P2；Linux 子进程清理和回执真实性缺口。
- **位置**：`comsol_mcp/knowledge/semantic_process.py`，start() ready 时的 `_posix_descendants` 快照，`_terminate_posix_group()` 和 `_terminate_owned()`。
- **内容/复现方法**：真实有界 fake worker 先完成 ready 握手。manager 此时记录的 descendants 为 {}。验收进程随后明确发送文件信号，worker 才创建 start_new_session=True 的 Python 等待子进程并退出。reset 在主进程已退出后不再查询 children；进程组扫描不能发现已脱离组的子进程，返回 success=true、absent=true、cleanup_errors=[]，实际子进程仍活着。复现 `python BUG-052__late_detached_child.py`，输出 passed=false 与 owned_detached_child_alive_after_reset=true。此探针只替换 worker 命令，使用真实生产 start/reset 和真实进程，未改 manager 路径或身份字段；最后根据 PID/create_time 清理该测试子进程。新增仓库测试只覆盖 ready 前已存在的子进程，未覆盖这一时序。
- **可能的解决办法**：在运行期间持续识别和记录后代身份，或使用能约束并回收整个任务的 Linux containment；明确是否允许子进程自行脱离 session。不能仅用 ready/reset 时的快照证明整个进程树已清理。无法完整追踪时返回未验证清理，不得给 absent=true。增加 ready 后派生、脱离 session、父进程先退出的验收。
