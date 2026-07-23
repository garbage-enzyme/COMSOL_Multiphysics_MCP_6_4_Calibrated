# 证据完整性与 AI 防幻觉验证

AI 很适合辅助 COMSOL 工作，但表达流畅不等于结论可靠。它可能总结了错误的
run、混用了新旧文件、漏掉失败点、模型改变后仍复述旧结果，或者把看起来合理的
图像写成数据并不支持的强结论。本 MCP 通过把正式结论绑定到声明的配置、原始证据、
artifact 字节、模型 revision 和软件身份来降低这些风险。它不能让 AI 永不犯错，
也不能替代正确的 COMSOL 模型、网格收敛、物理验证和专家判断。

证据完整性检查由用户控制，但**默认开启**。用户可以为了探索显式关闭某一项，
此时结果必须醒目标记为未验证。可选的 Desktop/Server 共享协作模式采用另一套默认值：
它默认关闭，只有显式启用后才会出现。

## 一页快速开始

1. 把正式 artifact 保存在 `settings.json` 的 `paths.artifact_write_root` 中。默认位置是
   `runtime.directory` 下的 `owned_artifacts`；覆盖时应使用绝对、仅 ASCII 的目录。
2. 使用项目根目录 `settings.json` 调用 `evidence_integrity_status`。四项检查均应
   显示 `enabled: true`、`source: project_settings`，并且
   `strict_verification_active: true`。
3. 可以正常进行探索，但要保留原始行和 diagnostic 行；不要为了让总结更好看而删除
   失败点或 partial 数据。
4. 构造 `comsol_mcp.portfolio_evidence_request`，其中包含精确的 outcome contract、
   artifact-chain manifest 和 summary citation。每条 claim 都引用 artifact ID、
   SHA-256 以及指向精确值的 JSON Pointer。
5. 调用 `evidence_integrity_verify`，为每个 case 提供一个 contained artifact root。
   如果结果来自 resume，还要提供精确的 expected/observed producer 与 driver 身份。
6. 只有 receipt 同时返回 `verification_state: verified` 和
   `strictly_verified: true` 时，才可使用正式验证标签。保存 settings fingerprint、
   request hash、verification hash 和被引用的 artifacts。
7. 只要输入或 artifact 改变，就创建新的 run identity 并重新验证。重新开启检查不能
   原地升级旧的 unverified receipt。

两个公共 guard tool 都是 solver-free，并存在于每个 static profile：

- `evidence_integrity_status` 报告有效设置，但不泄露 settings 路径；
- `evidence_integrity_verify` 执行确定性的正式验证，不启动 COMSOL，也不修改模型。

## 为什么需要这些控制

一个看起来正确的数值可能以多种互相独立的方式出错：求解完成了，但用的是错误模型；
原始数据正确，但 summary 引用了另一个 attempt；计算和证据均完整，但声明的科学阈值
仍未通过；截图显示了场分布，却不能证明 polarization、能量闭合或 mode identity。

因此，本项目把三个 outcome 分开表示：

| Outcome | 回答的问题 | 状态示例 |
| --- | --- | --- |
| Execution | 请求的工作是否正确终止？ | `completed`、`failed`、`interrupted`、`cancelled` |
| Evidence | 所需原始字节和引用是否齐全且有效？ | `complete`、`incomplete`、`invalid` |
| Scientific disposition | 声明的 policy 得出什么结论？ | `accepted`、`residual`、`unresolved_at_declared_cap`、`invalid_evidence`、`not_evaluated` |

`completed` 不等于 `accepted`。同样，`cancel requested` 不等于终态
`cancelled`；终态取消需要 worker、descendant、port 和 lease 的清理证据。

## 有哪些保护，以及用户能看到什么

| 风险 | 控制 | MCP 实现 | 用户可见证据 |
| --- | --- | --- | --- |
| 把成功调用写成科学成功 | Outcome separation | versioned outcome contract 分别验证 execution、evidence completeness、cleanup 和 scientific disposition | 独立的机器可读状态与 reason code |
| 输入或模型变化后仍复用旧行 | Immutable configuration/source identity | 在正式工作前绑定 normalized configuration、声明的 source hash、policy、model revision、software/build 与 resume identity | configuration/request hash、source/build identity、revision receipt 和 fail-closed mismatch |
| polished summary 隐藏 partial/failed 行 | Durable raw evidence | durable job 在 summary 前逐点写入，使用 atomic state 或 append-only/hash-chained journal、flush、`fsync` 和 attempt identity | 原始行、diagnostic 标签、checkpoint 和精确 artifact hash |
| 被引用文件遭到替换或重排 | Artifact-chain verification | `artifact_chain_verification` 在 owned root 下检查 manifest、dependency closure、byte count、schema、顺序和 SHA-256 | 每项 check receipt hash、artifact 数量和不含私有路径的 receipt |
| AI 编造附近的 peak、fit、mesh count 或 wavelength | Exact summary-claim verification | `summary_claim_verification` 解析 artifact hash 与 JSON Pointer，并按 canonical JSON 比较值 | claim/check state 以及精确 request/verification hash |
| Resume 混入不兼容软件或 driver | Producer/driver compatibility | `resumed: true` 时，`producer_driver_compatibility` 要求 producer、producer version、schema version 和 driver SHA-256 完全一致 | `passed`、`failed` 或 `not_applicable`；不静默迁移旧行 |
| Agent 自动化期间 Desktop 改变模型 | Model revision/external-change guards | serialization、shared-model lock identity 以及 expected revision/readback 检查 | lock/revision hash 和 changed fields |
| 只发出取消请求就宣称清理完成 | Cancellation/cleanup proof | durable cancellation 协调精确 worker/descendant identity、port、lease 和 attached-resource preservation | 终态 cleanup evidence；外部 Server/Desktop/model 仍不归 MCP 所有 |
| 逃逸目录或覆盖证据 | Path/overwrite containment | configured read/owned-write root 拒绝 traversal、device/reserved name、link/junction、alias 和 caller-selected overwrite | 脱敏 path-policy decision 与稳定 root ID |
| 把截图或标签当作物理证明 | Physical/visual evidence gates | 适用工具保留原始 R/T/A、closure、synchronization、mesh/material/field evidence 和 calibrated visual-review contract | `measured`、`unknown`、`diagnostic` 或 policy result，而不是猜测 |
| 关闭保护后 warning 丢失 | Effective settings/warning propagation | capabilities 和公共 guarded response 携带 effective fingerprint；任何 disabled check 都强制 `strictly_verified: false` | response 和正式 receipt 中的 disabled-check 列表与稳定 warning code |
| settings 输入错误后使用猜测 fallback | Bounded settings fallback 与 machine-readable error | 缺失条目使用默认值；非法条目仅回退自身默认值；JSON 损坏时回退完整安全默认；artifact/identity mismatch 仍然 fail closed | `project_settings.settings_errors`、有界 reason code，且不会静默变成 default-off |

这些检查由确定性代码执行。部分事实来自 COMSOL/clientapi readback，例如模型和 revision
状态；另一些是用户声明，例如 scientific policy 或 domain interpretation。AI 编写的
文字只是一层解释，不是 trust root。如果一项用户声明没有独立 readback，receipt 可以
证明使用了哪项声明，但不能证明声明本身诚实。

## 默认开启设置与显式 opt-out

项目根目录 `settings.json` 是 canonical settings 文件。完整设置参考见
[设置指南](../setting_guide/README_CN.md)。其中
`evidence_integrity.checks` 保存四项检查和默认值：

```json
{
  "evidence_integrity": {
    "checks": {
      "outcome_contract_validation": true,
      "artifact_chain_verification": true,
      "summary_claim_verification": true,
      "producer_driver_compatibility": true
    }
  }
}
```

只有显式 JSON boolean `false` 才能关闭某项检查；删除该项会回到 `true`。探索时只在
统一文件中修改对应值，例如把 `summary_claim_verification` 改为 `false`；受影响的
response 必须携带 `strictly_verified: false` 和稳定 warning。证据设置会在每次 status
或 guarded call 时读取，但恢复检查后仍必须针对**未改变的 artifacts 重新验证**，不能
给旧 receipt 重新贴 verified 标签。

当某个设置含非法字符、错误类型或不支持的值时，只有该设置使用文档默认值，并由
`project_settings.settings_errors` 报告 key 和 reason code。JSON 损坏或不可读时使用完整
安全默认文件并报告错误。artifact 或 identity mismatch 仍然 fail closed。旧的
`COMSOL_MCP_EVIDENCE_SETTINGS_PATH` 文件以及提交中的 `default_settings.json`/
`exploration_settings.json` 仍保留一个 release 的兼容 fixture，不是多 agent 的正常配置来源。

具有代表性的 capability 输出：

```json
{
  "evidence_integrity": {
    "configuration_state": "valid",
    "default_enabled": true,
    "strict_verification_active": true,
    "settings_source": "project_settings",
    "settings_fingerprint_sha256": "<64 个小写十六进制字符>",
    "settings_path_included": false,
    "checks": {
      "outcome_contract_validation": {"enabled": true, "source": "project_settings"},
      "artifact_chain_verification": {"enabled": true, "source": "project_settings"},
      "summary_claim_verification": {"enabled": true, "source": "project_settings"},
      "producer_driver_compatibility": {"enabled": true, "source": "project_settings"}
    },
    "tools": ["evidence_integrity_status", "evidence_integrity_verify"]
  }
}
```

只要有一项检查被关闭，affected response 和 formal receipt 就会携带稳定 code
`strict_evidence_checks_disabled`、disabled-check 列表和下面这条等价中文警告：

> 严格证据检查已关闭；这些结果未经过完整验证，可能包含 AI 生成或幻觉内容。

协议中的精确英文 warning 为：

> Strict evidence checks are disabled; these results were not fully verified and may contain AI-generated or hallucinated content.

## 从探索结果到 verified result

以一个中性的 parameter study 为例。探索阶段找到一个候选 peak。Agent 先保存精确的
point JSON，其中包括 normalized configuration ID、evaluated wavelength、mesh
evidence 和 measured value。manifest 把这些文件和 derived fit 绑定到各自 hash。
outcome contract 分别表明 execution 已完成、evidence 完整，并且声明的 scientific
policy 接受该 case。summary 随后引用某个 artifact 中的
`/evidence/wavelength_m`，以及另一个 artifact 中的 `/fit/quality_factor`。

`evidence_integrity_verify` 执行所有 enabled check，并可返回：

```json
{
  "schema_name": "comsol_mcp.evidence_integrity_verification",
  "schema_version": "1.0.0",
  "success": true,
  "verification_state": "verified",
  "strictly_verified": true,
  "reason_code": "all_enabled_checks_passed",
  "request_sha256": "<request hash>",
  "check_results": {
    "outcome_contract_validation": {"state": "passed"},
    "artifact_chain_verification": {"state": "passed"},
    "summary_claim_verification": {"state": "passed"},
    "producer_driver_compatibility": {
      "state": "not_applicable",
      "reason_code": "fresh_verification_not_resume"
    }
  },
  "paths_included": false,
  "verification_sha256": "<receipt hash>"
}
```

如果是 resumed result，设置 `resumed: true`，并提供相同结构的 `expected` 和
`observed` producer identity，其中包含 `producer`、`producer_version`、
`schema_version` 和 `driver_sha256`。缺失或不一致都会使验证失败。

如果有人修改 cited value、替换 artifact、删除失败行或关闭 summary verification，
数据仍可用于探索，但不能获得 strict label。disabled-check response 示例：

```json
{
  "success": true,
  "verification_state": "unverified",
  "strictly_verified": false,
  "reason_code": "checks_disabled_by_settings",
  "disabled_evidence_checks": ["summary_claim_verification"],
  "evidence_integrity_warning_codes": ["strict_evidence_checks_disabled"],
  "evidence_integrity_warnings": [
    "Strict evidence checks are disabled; these results were not fully verified and may contain AI-generated or hallucinated content."
  ]
}
```

## 你可以做什么 / 不能做什么

你可以：

- 快速探索并保留 raw data，之后请求新的 formal verification；
- 选择精确的 source、configuration、policy、revision 和 owned artifact root；
- 运行前检查 `capabilities` 或 `evidence_integrity_status`，并保存 settings fingerprint；
- 查看 raw/diagnostic row、artifact hash、build identity、policy decision 和 exact citation；
- 使用受支持的 job control 取消，并等待 verified terminal state；
- 分享脱敏 receipt 和 cited artifacts，让另一个过程重跑 solver-free verification；
- 显式关闭某项检查进行探索，同时保留 unverified warning。

你不能通过下列方法获得或保留 `strictly_verified: true`：

- 要求 AI 推测缺失的 configuration、raw value、failed point、source identity、policy
  或 artifact bytes；
- 修改 model、artifact、manifest、settings、row 或 summary 后继续引用旧 receipt；
- 把 completed solve、漂亮 plot、screenshot、label、fit 或好看的数值当作充分证据；
- 在没有受支持 migration 的情况下混用不同 run、attempt、model、build 或 producer/driver；
- 把 cancel request、PID 消失或 GUI 断开当作 cleanup proof；
- automation-exclusive 工作期间修改 Desktop model 并保留旧 revision；
- 关闭 required check、隐藏 warning，或恢复检查后升级旧 receipt；
- 在缺少相应科学证据时，声称 provenance/hash 已证明物理正确性、收敛、polarization、
  power closure 或 publication readiness。

发生拒绝时，应保留 diagnostic evidence。恢复检查、修正输入；如果 artifacts 完全
未改变，可以只运行缺失的 deterministic verification；只要输入改变，就创建新的
run identity。不要通过删除失败数据来让验证通过。

## `strictly_verified` 表示什么

`strictly_verified: true` 表示所有 effective check 都处于 enabled 状态，并且所有
适用的 deterministic check 已针对该 receipt 指定的精确 request 和 artifact bytes
通过。它绑定 settings fingerprint、request hash、artifact hash 与 verification hash。

它**不表示** equations、boundary conditions、material data、mesh、port convention、
polarization interpretation 或 scientific policy 正确。hash 能发现改变和替换，不能验证物理。
独立审阅者仍应检查 raw row 与模型假设，重跑 solver-free verification，
并完成该结论所需的项目级 convergence 和 physical validation。

只要 cited artifact、manifest、model/configuration identity、settings fingerprint、
policy、producer/driver identity 或 summary claim 发生改变，旧 verified receipt 就不能
继续作为新结果的验证证据。

## 故障处理表

| 现象 | 含义 | 安全的下一步 |
| --- | --- | --- |
| `configuration_state: degraded` | 一个或多个 settings 在验证报错后使用了安全默认值 | 修正文件、检查 `settings_errors`；static setting 改变后重启，不要增加竞争配置来源 |
| `outcome_contract_validation: failed` | execution/evidence/scientific state 或 hash 不一致 | 从保留的 raw state 修复 contract；不要编造 cleanup/acceptance |
| `artifact_chain_verification: failed` | manifest、dependency、byte count、schema 或 hash 改变 | 保留两个版本，恢复目标字节，或创建新的 chain identity |
| `summary_claim_verification: failed` | claim 在 cited hash/JSON Pointer 处不存在 | 修正 summary/citation 后重新验证 |
| `producer_driver_compatibility: failed` | resume producer、schema 或 driver identity 改变 | 使用受支持 migration，或创建新的 attempt；不要 relabel stale row |
| `resume_compatibility_missing` | resumed result 缺少 exact compatibility evidence | 提供 expected/observed identity，或作为 fresh run 处理 |
| `partial`、`diagnostic` 或 `incomplete` | 有可用数据，但 formal evidence 不完整 | 保留标签，只补齐缺失 evidence |
| `strict_evidence_checks_disabled` | exploration opt-out 正在生效 | 保留 warning，恢复检查并重新做 formal verification |
| Artifact root 被拒绝 | 目录不在 configured owned root 下或使用不安全 alias | 通过受支持 owned workflow 移动/复制，不要降低 containment |
| `unresolved_at_declared_cap` 或 `residual` | 有效的 non-acceptance outcome，不是 execution failure | 原样报告；改变 policy/cap 必须作为新决定 |

## 威胁模型与限制

这些控制用于发现声明的 provenance 和 consistency failure。它们不能防御已被攻陷的
操作系统、能够同时改写数据和 trust root 的恶意代码、内部自洽但错误的物理模型、
不充分网格，或没有独立 readback 的不诚实声明。AI review 也不会因此成为 numerical
policy authority。

安全的 assistant 用词应保留精确 evidence state 与 citation。证据缺失时写
`unknown` 或 `unavailable`；绝不能把 `unverified`、`diagnostic`、`partial`、
`residual` 或 `unresolved_at_declared_cap` 静默改写成成功。
