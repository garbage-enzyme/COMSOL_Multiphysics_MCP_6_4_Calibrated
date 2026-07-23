# COMSOL MCP 设置指南

`settings.json` 是所有 MCP client 共用的启动配置。应在启动 host 前编辑；修改
`profile.name`、`shared_server.enabled` 或 Java 路径后必须重启 host。随后可通过
`capabilities.project_settings` 确认实际配置，响应不会暴露本机路径。

文件必须是 UTF-8 JSON、只含一个对象、没有重复 key，且不超过 64 KiB。提交的模板
有意不包含注释。未知字段会写入 `settings_errors`。缺失字段使用下方安全默认值；非法
字段仅自身回退默认值。JSON 损坏或不可读时，使用完整安全默认配置。

## 文档身份

| 字段 | 含义 | 默认值 | 可接受值 |
| --- | --- | --- | --- |
| `schema_name` | 标识此设置 schema。 | `"comsol_mcp.settings"` | 只能是 `"comsol_mcp.settings"`。 |
| `schema_version` | 标识支持的设置格式。 | `"1.0.0"` | 只能是 `"1.0.0"`。 |

## Profile

| 字段 | 含义 | 默认值 | 可接受值 |
| --- | --- | --- | --- |
| `profile.name` | 此 MCP 进程固定的公共 tool surface。 | `"core"` | `"core"`、`"basic_fem"`、`"wave_optics"`、`"semantic_docs"`、`"desktop_shared"`、`"experimental"` 或 `"full"`（不区分大小写，保存时转为小写）。 |

`desktop_shared` 还要求 `shared_server.enabled: true`，并且不会启动或终止用户的
COMSOL Server。`semantic_docs` 需要相应检索资产可用。没有明确需求时使用 `core`。

## Runtime 与 containment 路径

下列允许路径的字段只接受绝对路径字符串。空字符串、相对路径和控制字符均非法。路径
按 host 平台标准化。Windows 部署根和 durable artifact 建议使用仅 ASCII 路径。

| 字段 | 含义 | 默认值 | 可接受值 |
| --- | --- | --- | --- |
| `runtime.directory` | Server 自有 runtime state 的根目录。 | `null` | `null` 或绝对路径字符串。`null` 使用平台安全 runtime root。 |
| `runtime.jobs_directory` | Durable job state 的根目录。 | `null` | `null` 或绝对路径字符串。`null` 使用实际 runtime root 下的默认目录。 |
| `paths.model_read_roots` | Tool 可读取 source model 的已批准根目录。 | `[]` | 由绝对路径字符串构成的 JSON 数组，标准化后不得重复。未显式批准 model 目录前保持空数组。 |
| `paths.artifact_write_root` | MCP 自有 artifact、manifest 和 evidence 的根目录。 | `null` | `null` 或绝对路径字符串。`null` 使用实际 runtime root 下默认的 owned-artifact 目录。 |

路径值通过此设置验证并不表示后续 tool 一定可用：containment、存在性、link/junction、
覆盖和具体操作的检查仍会执行。

## Shared Desktop/Server 模式

| 字段 | 含义 | 默认值 | 可接受值 |
| --- | --- | --- | --- |
| `shared_server.enabled` | 在选中 `desktop_shared` profile 时打开 shared Desktop/attached-Server gate。 | `false` | JSON `true` 或 `false`；`"true"` 这类字符串非法。 |

仅在已手动启动本地 COMSOL Server 后才设为 `true`。所需 confirmation 和 ownership
流程见[交互协作指南](../interactive_shared_session/README_CN.md)。

## 证据完整性

四项检查默认均开启。把任一项设为 `false` 是探索性 opt-out：受影响的正式结果都会明确
标记为未验证。

| 字段 | 含义 | 默认值 | 可接受值 |
| --- | --- | --- | --- |
| `evidence_integrity.checks.outcome_contract_validation` | 验证声明的 outcome 及其 machine-readable contract。 | `true` | JSON `true` 或 `false`。 |
| `evidence_integrity.checks.artifact_chain_verification` | 验证自有 artifact bytes、provenance 和 hash-chain identity。 | `true` | JSON `true` 或 `false`。 |
| `evidence_integrity.checks.summary_claim_verification` | 对照精确引用的 artifact 值验证 summary claim。 | `true` | JSON `true` 或 `false`。 |
| `evidence_integrity.checks.producer_driver_compatibility` | 恢复 continuation 前验证 producer 和 driver identity。 | `true` | JSON `true` 或 `false`。 |

opt-out 的影响和验证流程见[证据完整性指南](../evidence_integrity/README_CN.md)。

## 语义手册检索

| 字段 | 含义 | 默认值 | 可接受值 |
| --- | --- | --- | --- |
| `semantic_docs.root` | 隔离语义检索资产的部署根目录。 | `"D:/comsol_semantic"` | 绝对路径字符串；不接受 `null`。 |
| `semantic_docs.lexical_index` | 不可变的 SQLite 词法手册索引。 | `"D:/comsol_docs_fts/manuals.sqlite3"` | 绝对路径字符串；不接受 `null`。 |
| `semantic_docs.model_path` | 可选的本地 semantic model revision 目录。 | `null` | `null` 或绝对路径字符串。`null` 时 semantic retrieval 不可用。 |

所需资产尚不存在时，semantic profile 保持不可用。

## Ownership 与 Java

| 字段 | 含义 | 默认值 | 可接受值 |
| --- | --- | --- | --- |
| `ownership.owner` | MCP solver owner 的可选稳定 label。 | `null` | `null`，或不为空、最多 256 个字符且不含控制字符的字符串。`null` 时从 parent agent process 推导有界 label。 |
| `java.java_home` | 可选的 COMSOL bundled Java runtime 路径。 | `null` | `null` 或绝对路径字符串。`null` 保留 host Java environment。 |
| `java.jdk_home` | ClientAPI import 前设置的可选 JDK 路径。 | `null` | `null` 或绝对路径字符串。对于已验证的 COMSOL runtime，它通常与 `java.java_home` 相同。 |

## 示例

下面是 partial edit，不能替代提交的完整模板：

```json
{
  "profile": {"name": "wave_optics"},
  "runtime": {"directory": "D:/comsol_runtime"},
  "paths": {
    "model_read_roots": ["D:/comsol_models"],
    "artifact_write_root": "D:/comsol_runtime/owned_artifacts"
  }
}
```

如果 client 不能保留项目根目录，可把唯一的绝对文件定位变量
`COMSOL_MCP_SETTINGS_PATH` 设为所需的常规、非 link 的 `settings.json` 文件。定位文件
必须已经存在。
