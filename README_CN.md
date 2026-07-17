# COMSOL 6.4 MCP Server

[English](README.md) | 中文

[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/stargazers)

> [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP) 的维护型 Fork，已针对 **COMSOL Multiphysics 6.4.0.293** 和 **MPh 1.3.1 standalone/clientapi** 校准。其他 COMSOL build 需要各自的 licensed acceptance 证据。

该服务器为 AI agent 提供更安全、更紧凑的 COMSOL 接口，用于模型检查、受控单点验证、可恢复的分段扫描与离线手册检索。它适配 `mph.Client()` 返回的 `model.java` clientapi 对象；该对象与上游面向的直接 `com.comsol.model.Model` API 有实质差异。

## 推荐配套 Skill

Claude Code、Codex CLI、opencode、Hermes Agent 及其他支持 skill 的 agent，推荐将本服务器与
[COMSOL 6.4+ metasurface agent skill](https://github.com/garbage-enzyme/COMSOL_6_4_agentskill_for_metasurfaces)
配合使用。该 skill 采用短 `SKILL.md` 入口，并按需路由到 clientapi、周期 Wave
Optics、材料与边界、durable jobs、物理证据、资源安全和故障诊断模块，避免每轮
都把整份指南载入上下文。仓库开发和发布流程保留在本仓库的 development kit 中。

## Client 兼容性与部署

安装后的 FastMCP stdio server 已通过 Codex CLI 和 opencode 验证。按照标准 stdio
配置，它在理论上兼容 Claude Code 和 Hermes Agent，但本项目尚未对这两个 client
完成端到端测试；欢迎提交测试结果和 PR。全新安装、精确配置路径、profile 选择、
重启规则和 solver-free 验证请阅读独立指南：

- [部署指南（中文）](DEPLOYMENT_CN.md)
- [Deployment guide (English)](DEPLOYMENT.md)

关键规则：使用非 editable 安装；配置安装后的 `comsol-mcp` executable 绝对路径；
在 stdio host 启动前设置 `COMSOL_MCP_PROFILE`；修改 profile 或安装包后重启
client；保持 COMSOL 工具串行。调用 `capabilities` 可在不启动 COMSOL 的情况下
验证实际部署的 profile。

未经测试的 client 配置依据 Claude Code 官方
[MCP 文档](https://code.claude.com/docs/en/mcp)、Hermes 官方
[MCP 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md)
和 [client 源码](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py)
编写。这只表示配置层面的理论兼容，不构成验证声明。真实 client acceptance 报告应
至少包含不启动 COMSOL 的 `initialize`、`list_tools` 和 `capabilities` 回读。
已安装工具界面以实时 discovery 为准，不以文档中复制的数量为准。

## 主要能力

- **ClientAPI 适配。** 几何、物理场、材料、网格、研究、结果、模型克隆和 Unicode 安全的 `.mph` 保存已在 COMSOL 6.4.0.293 上通过 licensed acceptance；其他 build 在独立验收前均为 unknown。
- **安全的求解器所有权。** ASCII 路径租约、进程身份核验、外部客户端检测、状态和预检可避免意外启动并发 COMSOL 客户端。
- **持久化后台任务。** 分段扫描和自适应光谱表征在独立 worker 中执行，具有不可变规格、原子状态、经 `fsync` 的证据行、检查点、校验后的恢复，以及已验证的同主机取消能力。
- **Wave Optics 验证。** 专用 profile 支持只读模型预检，以及用于周期性超表面的单波长证据审计。
- **有界离线手册检索。** SQLite FTS5/BM25 检索和页读取不在 COMSOL 控制进程中运行，返回紧凑的来源/页码引用。
- **如实标注的可选语义检索。** 隔离式语义 profile 已具备进程隔离，但基线模型未通过质量和内存的晋级门槛；推荐默认使用词法手册检索。

## Profile

在启动服务器前设置 `COMSOL_MCP_PROFILE`。一个 profile 在该服务器进程的整个生命周期内固定；更改后需重启。

| Profile | 适用场景 |
| --- | --- |
| `core`（默认） | 紧凑且成熟的控制面：状态、所有权、会话/模型检查、单点求解/求值及词法手册检索。 |
| `basic_fem` | 在 `core` 基础上增加传统 FEM 的类型化构建、派生几何编辑和有界导出。 |
| `wave_optics` | 超表面推荐：在 `core` 基础上增加派生几何编辑、材料预览、locale-safe 场数据发现及有界 NPZ/manifest 提取、周期网格审计/冒烟、视觉审查合同、Wave Optics 预检、单点/参考审计和分段工作流。 |
| `semantic_docs` | 在 `core` 基础上增加隔离的实验性向量辅助手册检索。 |
| `experimental` | 显式选择的通用创建、异步、属性逃生口和项目辅助工具。 |
| `full` | 宽兼容/发现界面，包含所有 profile 的全部工具。 |

调用 `capabilities` 可在不启动 COMSOL 的情况下获知当前 profile、精确注册工具、目标版本、禁用工具组和重启要求。其中有界的 `deployment_identity` 会报告当前代码来自源码树还是已安装包，并给出冻结的 profile/schema 与 catalog 哈希；因此即使版本号相同，也能在重启后识别旧安装或源码遮蔽，且不暴露本机路径。

当前版本**尚未**提供不拥有外部进程的共享 Desktop/attached-Server 工作流。
旧 `comsol_connect` 只存在于 experimental 兼容 profile，不能视为安全的共享模型连接；
它尚不具备显式用户确认、外部 Server 所有权保护和模型身份锁定。

来自 capabilities、求解器所有权、持久化任务和词法手册的控制面响应会附带紧凑的滚动 `control_plane` 数据。每种操作最多保留 256 个样本，报告 success/busy/timeout/error 计数和 p50/p95/max 延迟；完整日志及无界遥测不会内联返回。

## 推荐工作流

### 常规求解

1. 调用 `solver_status`。
2. 在连接、启动 COMSOL 或提交较重任务前调用 `solver_preflight`。
3. 使用会话/模型工具，或提交持久化分段扫描。

当检测到外部 MPh/COMSOL 所有者或有效租约时，服务器会拒绝继续启动。`solver_recover_stale_lease` 只有在进程身份信息证明租约过期时才移除它，绝不会终止不属于本服务器的进程。

持久化扫描控制工具为 `job_submit`、`job_status`、`job_tail`、`job_cancel` 和 `job_resume`。每个任务在 ASCII-only runtime 目录中保存不可变规格、状态、CSV 日志、检查点和日志文件。恢复时只接受规格一致、数值有限且成功完成的行。只有 worker/相关进程清理和租约释放都得到验证后，取消才会进入终态。此协调机制仅适用于同一台主机上共享 runtime 目录的任务，不是分布式或跨主机取消。

自适应光谱任务使用 `job_type: "spectral_characterization"`，并显式声明
源模型/配置身份、初始波长网格、扩展与细化 policy、collector 配置、科学容差，
以及点数、stage 和资源上限。worker 每次只求解一个波长；完整 point audit 及其
哈希链证据行持久化后才进入下一点。每个请求 stage 都会冻结，因此精确恢复不会
重新生成计划，也不会重复已完成波长。只有规范化规格、collector、源模型和精确
worker driver 身份均相同时，重新提交才会观察到已有任务。

执行状态与科学解释彼此独立。任务可以以 `status: "completed"` 完成，同时
`scientific_disposition` 为 `residual` 或
`unresolved_at_declared_cap`；边界峰、缺少有效 bracket、fit sensitivity 和扩展
预算耗尽属于科学未验收结果，而不是 worker 执行失败。`accepted` 必须具有完整且
可由哈希解析的原始证据。中断或取消前留下的部分行仍是 diagnostic；只有 worker、
后代进程、端口、租约与清理证据全部通过，取消才进入终态。在声明的 collector 与
证据支持相应量时，光谱 summary 会保留原始 R/T/A、闭合误差、波长同步、网格计数、
own peak、FWHM、Q、stage 哈希及精确 artifact 引用。

持久化收敛任务使用 `job_type: "convergence_campaign"`，并声明 2–8 个严格排序的
exact source 或预先构建并验证的 derived model identity。每个 level 都复用已验收的
自适应光谱任务，完整持久化哈希绑定 artifacts，并且只以各 level 自己 bracketed
own peak 进入离线 convergence evaluator。调用方必须声明 metrics、units、容差、
governing-pair 与 declared-cap 规则、总 point/wall-time 上限及任何 early-acceptance
权限。整个 campaign 只使用一个 solver owner 和一个 client，不会自行增加 level；
resume 只复用验证完整的 level rows。当前版本不会在 campaign 内应用任意 parameter
setter；derived model level 必须在提交前完成构建和验证。

### Wave Optics 超表面

使用 `wave_optics` profile，并遵循下面的有界流程：

```text
solver_status -> wave_optics_preflight -> wave_optics_reference_audit（可选） -> wave_optics_point_audit
```

`wave_optics_preflight` 只读且不求解，报告来源溯源、拓扑、周期/Floquet 选择、端口、波长关联、网格/研究元数据和明确的未知项。

`wave_optics_point_audit` 会在所有权和源文件哈希检查通过后，恰好求解一个指定波长。它写入运行中 manifest、一行经 `fsync` 的 CSV 和最终 manifest。原始证据可包括请求/实际波长、频率关联、调用方溯源的 R/T/A 与通量方向、闭合误差、损耗表达式、上方空气区域的有界场统计、网格状态以及源/配置/policy 哈希。

`wave_optics_reference_audit` 是实验性的 reference-power 工具。它创建新的溯源 clone，要求调用方精确声明材料和域，在 clone 中以无损空气替换组件材料，采样有界均匀区域，并且只有在 clone 清理得到证明后才允许参考方法证据通过。它不会修改源模型；licensed acceptance 仍须针对具体 COMSOL 版本和模型执行。

若调用方未提供版本化 validation policy，审计仅输出证据：不会宣称模型通过/失败，也不会建议开始长扫描。在有独立的入射场参考 artifact 前，S/P 标签和结构总场都会被明确限定其证据等级。

## 手册检索

`manual_search` 和 `manual_read_pages` 是正式的文档检索路径。它们使用离线 SQLite FTS5/BM25 索引、有截止时间的 worker 进程以及紧凑的来源/页码引用；此路径不会在 MCP 控制进程中导入 Torch 或 SentenceTransformer。

`semantic_docs` 是可选的隔离 profile，不会干扰 COMSOL 控制。隔离 worker 向量检索只是英文诊断基线，并非多语言或生产质量声明：冻结基准中它提高了精确匹配召回率，却降低了改述/多概念召回，直接中文检索无命中，负查询没有正确弃答，长时间运行时内存也显著增长。基线模型及其索引资产已移除；替换模型需通过完整基准门槛后才能重新部署。常规工作请使用 `core` 加词法手册检索。

## ClientAPI 适配要点

本 Fork 已修复测试和真实 COMSOL 验证所覆盖的 clientapi 路径，包括：

- 使用 `tags()` 遍历、`feature().size()` 计数，替代直接 Model API 的索引和 `len()`。
- 物理场接口使用 `physics().create(tag, type, sdim_string)`；子 feature 使用整数实体维度。
- 使用 `getNumElem()` / `getNumVertex()` 检查网格，并显式创建 mesh sequence。
- 使用完整 study type 名称，以及 `model.java.study('std1').run()`。
- 对 Java 字符串、本地化标签、实数/NumPy/复数值和模型元数据进行 JSON 安全转换。
- 使用 `model.java.save(full_path)` 安全保存 Unicode 路径 `.mph` 文件，并正确清理 clientapi 模型克隆。
- 在正确组件中创建/复用材料和多物理场耦合。

静电场 helper 可创建 `ChargeConservation` 和材料节点，因为 COMSOL 6.3+/6.4 默认的 `fsp1` FreeSpace domain feature 使用真空介电常数，而不会使用材料的相对介电常数。

## 验证

当前 dependency/process-only 门槛为 **886 passed, 13 deselected**。单元测试无副作用：测试收集不会启动 COMSOL；integration probe 仅在显式请求时运行，并在全新的串行子进程中对精确进程树进行清理。仓库专用测试、release fixture、gate 与 provenance 见 `development_kit/README.md`；普通 wheel/sdist 不包含该目录。

```bash
python -m pytest -q
python -m pytest -q -m integration development_kit/tests/integration
```

真实 COMSOL 验证包括：本地化 JSON 传输、Circle/Union 几何、DXF 导入、参数扫描属性、多物理场耦合、模型克隆清理、Unicode 路径保存、求解器所有权、持久化中断/重启/恢复/取消、profile 发现、Wave Optics 预检与单点审计，以及有界手册检索。

Python 3.14 licensed 平行板回归结果为 **1.8593794419540677 pF**；理论值为 **1.8593794406880002 pF**，COMSOL 精确版本为 **6.4.0.293**。

同一 COMSOL build 上的 licensed 自适应光谱验收采用中性的空气—介质—空气周期
port slab，网格为 4,798 个单元和 1,039 个顶点。通过验收的 10 行光谱得到插值
own peak **5.200823291715346 um**、**T = 0.9999455828498357**、
**FWHM = 0.4807802607560452 um** 和 **Q = 10.817464268472365**。
原始行范围为 **R = 0.000428181826928114 至 0.506857218704363**、
**T = 0.493142781295616 至 0.999571818173077**、
**max |A| = 2.985136902408465e-17**；最大功率闭合误差为
**2.103241887902518e-14**，最大波长同步误差为零。独立的 9 行边界 control
按声明扩展 window 后，以 `unresolved_at_declared_cap` 正常完成；其原始范围为
**R = 0.113752050554409 至 0.697262752330585**、
**T = 0.302737247669409 至 0.886247949445593**、
**max |A| = 1.695203805977834e-17**，最大闭合误差为
**2.903982508976606e-14**，波长同步误差为零。两次运行均保持源模型 SHA-256
不变，并释放 solver lease 和 client。

licensed convergence 验收使用三层中性 periodic-port slab mesh：单元/顶点数分别为
**2,386/560**、**4,798/1,039** 和 **13,904/2,752**。各层 own peak 为
**5.200438265718366**、**5.200823291715278**、**5.200959692754783 um**，拟合
peak T 分别为 **0.9999455861474655**、**0.9999455828498416**、
**0.9999455989864663**；governing medium-to-fine peak shift 为
**0.1364010395043668 nm**。30 行原始数据总体范围为
**R = 0.000426677111557779 至 0.506857218704365**、
**T = 0.493142781295614 至 0.999573322888467**、
**max |A| = 4.526776969362989e-17**；最大闭合误差为
**2.48772546066357e-14**，波长同步误差为零。独立 campaign 使用声明的
**0.001 nm** peak-shift tolerance，三层执行全部完成但 disposition 为 `residual`，
同时 amplitude gate 通过。两次 campaign 均保持全部 source hash 不变，结束后无
client、进程或 lease 残留。

## 环境要求与安装

- COMSOL Multiphysics 6.4；licensed acceptance 固定于 build 6.4.0.293
- Python 3.14（标准 GIL 版本，不要使用 Windows Store 版本）
- MPh 1.3.1、`mcp`、`pydantic` 和 `psutil>=5.9.0`
- 已验证配置中使用 COMSOL 自带的 Java 21 runtime

本项目仍在积极开发，依赖范围与锁定版本可能随时调整，不保证较长的弃用过渡期。
更新现有部署前，请先在隔离环境中验证 Python、COMSOL/MPh/JPype 及所需可选
extra 的适配性；全部检查通过后，再替换当前安装并重启 MCP host。

```bash
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
cd COMSOL_Multiphysics_MCP_6_4_Calibrated
python -m pip install .

# 推荐离线手册索引；输出目录必须只含 ASCII 字符。
python -m pip install ".[manuals]"
python -m src.knowledge.lexical_manual build --index D:\comsol_docs_fts\manuals.sqlite3
```

如需可选的隔离语义检索（sentence-transformers，不含 ChromaDB）：

```powershell
python -m pip install ".[semantic-docs]"
$env:COMSOL_MCP_PROFILE = "semantic_docs"
$env:COMSOL_SEMANTIC_ROOT = "D:\comsol_semantic"
$env:COMSOL_SEMANTIC_LEXICAL_INDEX = "D:\comsol_docs_fts\manuals.sqlite3"
```

若 Windows 用户目录含非 ASCII 字符，请避免 editable install。源码变化后运行 `python -m pip install . --no-deps`，并重启 MCP host；服务器不会热加载 `src/tools/`。

MCP 客户端配置示例：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["D:\\path\\to\\python-env\\Scripts\\comsol-mcp.exe"],
      "environment": { "COMSOL_MCP_PROFILE": "wave_optics" }
    }
  }
}
```

省略 `COMSOL_MCP_PROFILE` 即使用 `core`。客户端示例见
`config/claude-code-mcp.example.json`、`config/codex-mcp.example.toml`、
`config/hermes-mcp.example.yaml` 和 `config/opencode-mcp.example.json`。

## 与上游 Fork 的区别

这是面向 COMSOL 6.4.0.293 standalone/clientapi 的兼容性和可靠性 Fork，而非上游项目的通用替代品；其他 COMSOL build 在独立验收前均为 unknown。它保留上游项目的基础能力，但为 agent 驱动的 COMSOL 工作流提供了更窄、更安全的执行界面。

| 方面 | 上游定位 | 本 Fork |
| --- | --- | --- |
| COMSOL API 目标 | 假定直接使用 `com.comsol.model.Model` API。 | 适配 MPh 1.3.1 standalone 的 `model.java` clientapi 包装层，包括不同的方法重载、tag、列表和 Java 字符串传输。 |
| 工具界面 | 默认提供较宽的功能发现面。 | 默认采用紧凑 `core`；较大的构建和兼容界面须显式选择 profile。 |
| 求解器并发 | 没有同主机所有权协议。 | 通过进程感知租约、外部客户端检测、状态、预检和过期租约恢复来防止冲突；不会终止不属于本服务器的进程。 |
| 长任务 | 以交互式/当前进程工作流为主。 | 使用独立的持久化扫描和自适应光谱任务：不可变规格、经 `fsync` 的证据行、冻结 stage、校验恢复和已验证的取消清理。 |
| Wave Optics | 只有通用工具。 | 提供周期性超表面专用的预检和单点证据审计，原始证据与调用方 policy 分离。 |
| 手册检索 | 无有界手册检索。 | 默认使用有界、隔离的词法手册检索；实验性语义检索被隔离且明确未晋级。旧式进程内 ChromaDB 路径已移除。 |
| Windows 路径 | 不特别保证 Unicode 保存路径。 | 通过 clientapi Java 保存 Unicode `.mph`；原生/持久化 runtime 和索引使用 ASCII-only 根目录。 |

若在 MPh standalone 下使用上游工具时遇到 `No matching overloads`、`Operation_cannot_be_created_in_this_context` 或 client-list 索引错误，请使用本 Fork。只有确实需要宽泛旧接口兼容时才选择 `full`。

## 许可证

本仓库采用 [MIT License](LICENSE)。COMSOL、授权手册、第三方模型、论文和数据集
不因本仓库许可证而被重新授权。
