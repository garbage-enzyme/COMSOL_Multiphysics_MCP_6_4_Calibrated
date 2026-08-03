# COMSOL 6.4 MCP Server

[English](README.md) | 中文

[![CI](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
![Release: 0.6.2](https://img.shields.io/badge/release-0.6.2-blue)
![Status: alpha](https://img.shields.io/badge/status-alpha-red)
[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/stargazers)

> [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP) 的维护型 Fork，已接受 **`COMSOL 6.4.0.*` release line** 和 **MPh 1.3.1 standalone/clientapi**。licensed reference 证据使用 **COMSOL 6.4.0.293**；第三位数字变化视为新的 release family，需要重新验收。

该服务器为 AI agent 提供更安全、更紧凑的 COMSOL 接口，用于模型检查、受控单点验证、可恢复的分段扫描与离线手册检索。它适配 `mph.Client()` 返回的 `model.java` clientapi 对象；该对象与上游面向的直接 `com.comsol.model.Model` API 有实质差异。

## 特色功能

- **证据完整性与 AI 防幻觉验证（默认开启）。** 正式结论可以针对精确的 outcome
  contract、原始 artifact chain、summary citation 以及 resume producer/driver
  identity 做验证。用户可以为了探索逐项 opt-out，但受影响的结果必须携带未验证
  warning。请阅读独立的[证据完整性指南](docs/evidence_integrity/README_CN.md)。
- **COMSOL Desktop/Server 交互协作（默认关闭）。** 用户和 agent 可以在一个用户拥有的
  本地 Server、一个连接的 Desktop 和一个精确 server-held model 上明确轮流操作。
  该模式要求显式启用独立功能以及每次 session confirmation。请阅读
  [交互协作指南](docs/interactive_shared_session/README_CN.md)。
- **五种仿真运行方式。** `interactive`、`inline`、`launcher`、`standalone` 和
  `mphonly` 分别用于短反馈、直接脚本、可恢复的本地长任务、目标机无 Python 的
  Windows 交付，以及可移植的 COMSOL 集群或云端交付。agent 默认只使用前三种；准备
  跨设备方式前必须先询问目标环境。请阅读独立的
  [运行方式指南](docs/simulation_execution_modes/README_CN.md)。

## 推荐配套 Skill

Claude Code、Codex CLI、opencode、Hermes Agent 及其他支持 skill 的 agent，推荐将本服务器与
[COMSOL 6.4+ metasurface agent skill](https://github.com/garbage-enzyme/COMSOL_6_4_agentskill_for_metasurfaces)
配合使用。该 skill 采用短 `SKILL.md` 入口，并按需路由到 clientapi、周期 Wave
Optics、材料与边界、durable jobs、物理证据、资源安全和故障诊断模块，避免每轮
都把整份指南载入上下文。仓库开发和发布流程保留在本仓库的 development kit 中。

## Client 兼容性与部署

安装后的 FastMCP stdio server 已通过 Codex CLI 和 opencode 验证。Windows 11 上的
Claude Code 2.1.220 验收已完成 stdio 初始化，发现测试所用 `wave_optics` profile 的
完整工具界面，并成功调用 `capabilities`、`comsol_status` 和 `solver_status`；server
干净退出，未创建 solver lease，也未启动 COMSOL 进程。该结论只覆盖 client transport、
schema、discovery 和非启动 status 兼容性，不是 licensed runtime 验收：Claude 未调用
`comsol_start`、未运行模型、未覆盖完整错误界面，也未证明 start/solve/cleanup。
Hermes Agent 仍只有配置层指导，尚无端到端 client 测试。全新安装、精确配置路径、
profile 选择、重启规则和 solver-free 验证请阅读独立指南：

- [部署指南](DEPLOYMENT_CN.md)

关键规则：使用非 editable 安装；配置安装后的 `comsol-mcp` executable 绝对路径；
普通用户通过设置界面完成配置；修改启动设置或安装包后重启 client；保持 COMSOL 工具
串行。直接编辑 JSON 是面向开发者、自动部署和获得用户明确授权的 agent 的高级路径。
调用 `capabilities` 可在不启动 COMSOL 的情况下验证实际部署的 profile。

仓库内的 client 示例依据 Claude Code 官方
[MCP 文档](https://code.claude.com/docs/en/mcp)、Hermes 官方
[MCP 文档](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md)
和 [client 源码](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py)
编写。一个示例在其精确 client 路径被实际执行前只属于配置指导。真实 client
acceptance 报告应至少包含不启动 COMSOL 的 `initialize`、实时 `list_tools` 和
`capabilities` 回读，并把 licensed start/solve/cleanup 覆盖单独标注。已安装工具界面
以实时 discovery 为准，不以文档中复制的数量为准。

## 主要能力

- **ClientAPI 适配。** 几何、物理场、材料、网格、研究、结果、模型克隆和 Unicode 安全的 `.mph` 保存已在 COMSOL 6.4.0.293 上通过 licensed acceptance；6.4.0.* 内只改变最后 build 数字时继承 release-line 结论，其他 release family 在独立验收前均为 unknown。
- **类型化声学与偏微分方程构建。** `basic_fem` 提供可回滚的命名 Box/四边
  selection、Pressure Acoustics、Coefficient/General/Weak Form PDE，以及具有
  精确类型和属性白名单的原子边界批量配置。
- **安全的求解器所有权。** ASCII 路径租约、进程身份核验、外部客户端检测、状态和预检可避免意外启动并发 COMSOL 客户端。
- **持久化后台任务。** 分段扫描和自适应光谱表征在独立 worker 中执行，具有不可变规格、原子状态、经 `fsync` 的证据行、检查点、校验后的恢复，以及已验证的同主机取消能力。可复用的[本地启动器](launcher/README_CN.md)为项目驱动提供相同的逐点运行方式。
- **无需 Python 的独立执行。** 原生 Windows x64 启动器保留三层结构：本机已安装并
  授权的 COMSOL 6.4、由 COMSOL 编译的 Java 单点驱动，以及启动器 EXE。目标机不需要
  Python、Conda、MPh、JPype 或另装 Java；COMSOL 本身仍然必需，项目不会打包或替代它。
- **共享 Desktop 协作（默认关闭）。** 独立的 `shared_server.enabled` 功能可与任意工具 profile 组合，连接用户手动启动的本地 COMSOL Server，精确采用一个 Server 模型，执行非拥有式租约和 revision lock，运行持久化 attached job，并在 detach 时保留用户的 Server、Desktop、listener 和模型。
- **Wave Optics 验证。** 专用 profile 支持只读模型预检，以及用于周期性超表面的单波长证据审计。
- **有界离线手册检索。** SQLite FTS5/BM25 检索和页读取不在 COMSOL 控制进程中运行，返回紧凑的来源/页码引用。
- **如实标注的可选语义检索。** 隔离式 `semantic_docs.enabled` 功能可与任意工具 profile 组合，但基线模型未通过质量和内存的晋级门槛；推荐默认使用词法手册检索。

## 统一项目设置

**普通用户首选设置界面。** 它修改 Codex、opencode、Claude Code 和 Hermes 共用的
同一份 `settings.json`，避免不同 agent 悄悄使用不同的 profile、路径、Java runtime 或
证据规则。仓库中的 [`settings.json`](settings.json) 是 canonical 模板，也是开发者、
自动部署和 agent 使用的高级接口。每个设置项的含义、默认值和可接受值见
[设置指南](docs/setting_guide/README_CN.md)。用户删去设置条目时，
该条目回填安全默认值；用户输入非法值时，仅该条目保持默认值，并由 `capabilities` 和
`evidence_integrity_status` 报告 `settings_errors`；JSON 整体损坏时回退完整安全默认值
并报告错误。不要为每个 agent 创建第二份 settings 文件。

从源码树运行时，项目根目录文件可写。安装 wheel 后，包内文件只是只读模板；若没有使用
下面的绝对定位变量，持久设置写入 `%LOCALAPPDATA%/comsol_mcp/settings.json`：

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

旧的 `COMSOL_MCP_*`、`COMSOL_SEMANTIC_*` 和 Java 环境变量仍保留一个 release 的
兼容覆盖能力，但已从提交的 client 配置示例中移除。修改 `settings.json` 后，profile、
shared-server 或 Java 设置需要重启 MCP host；随后调用 `capabilities` 检查
`project_settings`。

首次设置默认把 settings 文件和支持 Unicode 的模型读取目录放在
`%LOCALAPPDATA%/comsol_mcp`，把必须仅含 ASCII 字符的 runtime 与自有 artifacts 放在
`%PROGRAMDATA%/comsol_mcp`；可选语义资产保持未设置。

安装后首次使用时，`capabilities.project_settings` 会报告 `setup_required: true`、
`configuration_source: bundled_template`、设置方式 `settings.start` 与 `agent_edit`，以及
修改后需要重启。对普通用户，只调用一次所有 profile 都有的 `settings.start`，说明启动
设置要在重启 Codex 或所属 MCP client 后生效，然后停止继续输出并等待用户下一条消息；
GUI 打开期间不要同时编辑 JSON。只有用户明确要求 agent 管理或自动配置时才使用
`agent_edit`；只修改解析出的可写文件，验证后请求同样的重启。安装后的
`comsol-mcp-settings` 命令可直接打开 GUI；从
仓库或源码分发包使用时也可运行 `./Open_Settings_GUI.ps1`。该脚本会查找受支持的 Python
3.14，也可通过 `-PythonPath` 和 `-SettingsPath` 明确指定；`-ValidateOnly` 只验证启动条件，
不会打开界面或启动 COMSOL。服务器会返回 `agent_action_required: pause_for_user`，但无法从
技术上强制任意第三方 agent 遵守暂停。
安装入口也支持 `--settings-path` 和 `--validate-only`，即使 MCP stdio 已停止也能独立工作。
“关于”页可由用户明确创建或移除每用户 `COMSOL MCP Settings.lnk`；安装和普通 GUI 操作
从不自动创建该快捷方式。快捷方式指向安装后的 Windows GUI-subsystem 入口
`comsol-mcp-settings-gui`，因此不会同时打开终端窗口；控制台入口
`comsol-mcp-settings` 仍用于验证和脚本操作。

## Profile

普通用户在启动服务器前通过设置界面选择 profile。开发者和 agent 可以修改 JSON 中等价的
`profile.name` 字段。一个 profile 在该服务器进程的整个生命周期内固定；更改后需重启。

| Profile | 适用场景 |
| --- | --- |
| `core`（默认） | 紧凑且成熟的控制面：状态、所有权、会话/模型检查、单点求解/求值及词法手册检索。 |
| `basic_fem` | 在 `core` 基础上增加传统 FEM 的类型化构建、命名 selection、Pressure Acoustics 与数学 PDE、派生几何编辑、有界导出，以及无需 Python 的独立启动器工具。 |
| `wave_optics` | 超表面推荐：在 `core` 基础上增加派生几何编辑、材料预览、locale-safe 场数据发现及有界 NPZ/manifest 提取、周期网格审计/冒烟、视觉审查合同、Wave Optics 预检和单点/参考审计。持久化分段任务仍通过 `job_submit`。 |
| `experimental` | 显式选择的通用创建、异步、属性逃生口和项目辅助工具。 |
| `full` | 宽兼容/发现界面，包含全部非 feature-gated 工具。 |

Profile 只控制 COMSOL 自动化仿真工具以及未来自主探索工具的可见性。其他正交功能使用
独立、默认关闭的 Boolean 开关；它们可与任意 profile 组合，也可同时开启：

| 功能（高级 JSON 设置） | 增加的工具界面 |
| --- | --- |
| `shared_server.enabled=true` | 需要显式确认及精确进程/listener/model 身份的受保护 shared Desktop/attached-Server 工作流。 |
| `semantic_docs.enabled=true` | 三个隔离的实验性向量辅助手册检索工具。 |

调用 `capabilities` 可在不启动 COMSOL 的情况下获知当前 profile、精确注册工具、目标版本、禁用工具组和重启要求。其中有界的 `deployment_identity` 会报告当前代码来自源码树还是已安装包，并给出冻结的 profile/schema 与 catalog 哈希；因此即使版本号相同，也能在重启后识别旧安装或源码遮蔽，且不暴露本机路径。

在设置界面的“工具配置”页开启共享 Desktop/attached-Server 工作。该功能默认关闭，且不依赖
所选 profile；高级 JSON 等价值为 `shared_server.enabled=true`。用户必须手动启动 COMSOL Server，
让 Desktop 连接它，确认 endpoint，并显式确认每次 attach。旧 `comsol_connect` 仍是
experimental 兼容界面，不能替代受保护的 shared-session 生命周期。

来自 capabilities、求解器所有权、持久化任务和词法手册的控制面响应会附带紧凑的滚动 `control_plane` 数据。每种操作最多保留 256 个样本，报告 success/busy/timeout/error 计数和 p50/p95/max 延迟；完整日志及无界遥测不会内联返回。

### Windows 独立启动器

独立路线只面向 Windows 10/11 x64 和 COMSOL 6.4 release line；当前 licensed
acceptance 精确绑定 COMSOL 6.4.0.293。不支持 Windows Server、COMSOL 6.4 以下版本、
Linux 或 macOS。生成的 EXE 只接收一个运行参数：`--comsol-path <COMSOL 根目录>`。
它使用该目录内的 `comsolcompile.exe`、`comsolbatch.exe`、COMSOL 自带 Java、求解器和
许可证。

构建使用受支持 Windows 10/11 workstation 随系统提供并维护的 64 位
`.NET Framework 4.x` C# 编译器：
`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`。不需要另装现代
`.NET Runtime`、`.NET Desktop Runtime`、`.NET SDK`、Visual Studio，也不会下载
任何包或访问网络。若系统内置编译器缺失，`standalone_build` 会直接失败，不会安装或
下载替代组件。
微软的 [.NET Framework 平台表](https://learn.microsoft.com/zh-cn/dotnet/framework/install/guide-for-developers)
列出了 Windows 10 和 Windows 11 随系统提供的 4.x 版本。

在普通 Python MCP host 中选择 `basic_fem` profile，可使用 `standalone_build`、
`standalone_start`、`standalone_status`、`standalone_pause`、
`standalone_resume`、`standalone_tail` 和 `standalone_results`。构建与任务目录必须位于
配置的 ASCII 自有产物根目录内；MCP 在执行前核对包内源码身份、构建清单与 EXE 哈希。
EXE 生成后也可复制到目标机直接操作，此时不需要该 Python MCP host。
`standalone_start` 和 `standalone_resume` 有显式 `comsol_root` 时优先使用它；省略时读取共享
设置中的 `comsol.installation_root`。

## 推荐工作流

### 常规求解

1. 调用 `solver_status`。
2. 在连接、启动 COMSOL 或提交较重任务前调用 `solver_preflight`。
3. 使用会话/模型工具，或提交持久化分段扫描。

当检测到外部 MPh/COMSOL 所有者或有效租约时，服务器会拒绝继续启动。`solver_recover_stale_lease` 只有在进程身份信息证明租约过期时才移除它，绝不会终止不属于本服务器的进程。

### 平行板电容器端到端示例

源码仓库包含一个三维介质电容器示例。脚本根据实际测得的边界中心和法向识别两块
电极，分别设置接地与电势边界，并建立静电场稳态研究。默认只构建模型；加入
`--solve` 后，会将电场能量算出的电容与
`真空介电常数 × 相对介电常数 × 面积 ÷ 间距` 的解析结果比较。

```powershell
python -m recipes.parallel_plate_capacitor `
  --output-model D:\comsol_outputs\parallel_plate_capacitor.mph `
  --solve
```

该示例与需要许可证的端到端探针共用建模和验收函数。脚本会先检查求解器所有权，
清理 COMSOL 客户端并释放租约后才发布最终模型。除非显式加入
`--overwrite-output`，否则不会覆盖已有输出。

### 最小 Pressure Acoustics 示例

源码仓库包含一个具有物理意义的二维空气管道示例。上下边界为刚性壁，左侧施加
一帕压力，右侧压力为零；默认频率低于第一横向截止频率。默认只构建模型；加入
`--solve` 后，脚本会将管道中心的数值压力与一维 Helmholtz 解析解比较，误差通过后
才把收据标记为已验证。

```powershell
python -m recipes.acoustic_duct_2d `
  --output-model D:\comsol_outputs\minimal_acoustic_duct.mph `
  --solve
```

脚本会先执行新的求解器所有权预检，要求四条命名边界互不重复，保存到唯一暂存
模型，清理 COMSOL client 和租约后才发布最终 `.mph`。除非显式加入
`--overwrite-output`，否则绝不覆盖已有输出。

持久化扫描控制工具为 `job_submit`、`job_status`、`job_tail`、`job_cancel` 和 `job_resume`。每个任务在 ASCII-only runtime 目录中保存不可变规格、状态、CSV 日志、检查点和日志文件。恢复时只接受规格一致、数值有限且成功完成的行。只有 worker/相关进程清理和租约释放都得到验证后，取消才会进入终态。此协调机制仅适用于同一台主机上共享 runtime 目录的任务，不是分布式或跨主机取消。

交互式 `mesh_convergence_study` 也会在 CSV 旁写入 manifest。对已有日志执行
resume 或 append 时必须提供 `source_model_path`；只有源文件哈希、study、表达式、
参数设置、mesh identity 以及每个 level 的精确 properties 全部一致，已完成 level
才会被跳过。CSV 或 checkpoint 持久化失败是该 level 的终止结果，不会再次求解。

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

solver-free 的 `spectral_model_compare` 会让两到三个声明的标量线形模型使用完全相同的
光谱行、support、baseline、response、polarity、拟合坐标和质量 policy。它支持在
wavelength、frequency、angular-frequency 或 energy 坐标下比较局部多项式、
Lorentzian 与 Fano 拟合，并把 peak 和 half-prominence crossings 映射回原始波长证据。
输出包含拟合诊断、窗口敏感性、AIC、可定义时的 AICc、BIC、差值和描述性 Akaike
权重。这些结果只表示数据对模型的相对支持，不证明 Lorentzian/Fano 物理机制、模式
身份或科学验收。

三个 solver-free 预览工具把 COMSOL admission 或模型修改前的配置审查变成显式契约。
`simulation_configuration_validate` 只接受封闭的 typed 字段，包括 source/producer
identity、几何语义、层顺序、材料状态与损耗符号、入射与偏振、mesh dependency、
model-tree identity、solver termination、单位和 artifact chain；它统一受支持的单位并
返回内容绑定指纹。`simulation_configuration_diff` 把字段分为 exact、容差内、semantic、
label-only 或 unavailable，标签不会被提升为物理身份。`job_spec_preview` 复用
`job_submit` 的同一个 discriminated input validator，只报告受限的 point/stage 清单、
路径与资源检查、需求及提交时副作用，不做 admission、ownership、文件写入、进程创建或
solver 启动。

solver-free 的 `thermal_kirchhoff_assess` 只有在同一方向、频率和偏振通道的线性、
时不变、互易、局域平衡及通道匹配证据全部验证后，才允许把 directional absorptivity
当作 emissivity；未知事实保持 conditional/unavailable，非互易通道为 not applicable。
`thermal_radiation_evaluate` 使用 SI Planck 定律和显式 wavelength/frequency/
wavenumber Jacobian，执行含投影固体角的积分，支持 scalar、非相干 TE/TM 或
Stokes/Mueller 偏振，并可应用受限的气体、孔径、光学、analyzer 和 detector kernel。
哈希绑定输出保留 coverage、积分 policy、extrapolation 状态、不确定度、source artifact
以及 detector/reference/background signal；它不替代 COMSOL Surface-to-Surface
Radiation solver。

solver-free 的 `thermal_material_validate` 与 `thermal_material_evaluate` 使用
versioned ledger，而不是内置第二套材料数据库。每个 state 都绑定 material/sample
identity、phase/fabrication state、source、光谱与温度有效域、不确定度、测量条件以及
measured/fitted/assumed 分类。typed entry 支持 n/k 与复介电常数表，以及 Drude、
Lorentz、TOLO 和 thermo-optic 模型。不同 carrier density、mobility、effective mass
与 phase fraction 的 state 保持独立，声明的 phase/discontinuity boundary 不会被通用
插值跨越。内部约定为 `exp(-i*omega*t)` 且被动介质 `Im(epsilon)>=0`；COMSOL 输出只
提供 mutation-free 的 `exp(+i*omega*t)` 转换预览，包含精确 property/function tag、
单位、插值/外推 policy、table hash、readback 期望和 rollback 要求。

需要许可证的持久化热到光重放任务使用
`job_type: "thermo_optomechanical_replay"`，并提供 ASCII JSON
`specification_path` 与精确 `specification_sha256`。提交前会用完整的封闭契约验证受限
manifest；这样既控制 core MCP discovery 大小，也不接受自由格式配置。manifest 绑定
不可变源模型、一个已验证的
热材料状态，以及精确的 Heat Transfer、Solid Mechanics、Moving Mesh、Wave Optics、
study、selection、parameter、mesh 和 expression tag，并要求调用方声明 resource 与
acceptance policy。五个哈希链 stage 依次保存 preflight、热-结构求解、状态证据、空间
frame 形变传递和精确光学重放。resume 会先验证孤立 evidence，再补写缺失 row，绝不
重复已完成 stage。验收分别保留 raw R/T/A、温度、应力、位移、能量、网格、波长、
zero-CTE、zero-temperature-rise、rollback、源完整性与 cleanup 证据，不把执行完成等同
于科学通过。最小支持路径禁止拓扑变化，并要求先通过 COMSOL 6.4 synthetic fixture 的
licensed gate，之后才可用于研究模型。

持久化收敛任务使用 `job_type: "convergence_campaign"`，并声明 2–8 个严格排序的
exact source 或预先构建并验证的 derived model identity。每个 level 都复用已验收的
自适应光谱任务，完整持久化哈希绑定 artifacts，并且只以各 level 自己 bracketed
own peak 进入离线 convergence evaluator。调用方必须声明 metrics、units、容差、
governing-pair 与 declared-cap 规则、总 point/wall-time 上限及任何 early-acceptance
权限。整个 campaign 只使用一个 solver owner 和一个 client，不会自行增加 level；
resume 只复用验证完整的 level rows。当前版本不会在 campaign 内应用任意 parameter
setter；derived model level 必须在提交前完成构建和验证。

持久化 branch-continuation 任务使用
`job_type: "branch_continuation_campaign"`，并声明 2–16 个严格排序的 exact
source 或预先构建并验证的 derived model states。每个 state 都绑定一个 coordinate
值、polarization/material identity、精确 source/configuration hash，以及 periodic
parent 和两个 ports 的实测 incidence readback。每个 coordinate 独立运行自己的
自适应光谱；只有完整光谱已写入 hash-chain state row，离线 continuation planner
才会使用它。调用方 policy 必须限制 guard window、绝对波长域、扩展次数、总 window、
request grid、总 point、wall time，以及是否在第一个 unresolved transition 停止。
boundary-high 和 competing-candidate 结果只能保持为 residual 或
`unresolved_at_declared_cap`；campaign 不会宣称物理 branch disappearance，也不会
启动未声明的 coordinate。整个 campaign 只使用一个 solver owner 和一个 client，
resume 只复用验证完整的 state spectra。当前版本仅支持 exact/prebuilt models，
不会在 campaign 内应用任意 incidence 或 geometry setter。

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

`semantic_docs.enabled` 是可选的隔离功能，不会干扰 COMSOL 控制。隔离 worker 向量检索只是英文诊断基线，并非多语言或生产质量声明：冻结基准中它提高了精确匹配召回率，却降低了改述/多概念召回，直接中文检索无命中，负查询没有正确弃答，长时间运行时内存也显著增长。基线模型及其索引资产已移除；替换模型需通过完整基准门槛后才能重新部署。常规工作请使用词法手册检索。

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

源码 dependency/process-only 门槛、optimized-Python production guard、compileall、
hash-locked 隔离 non-editable wheel/install、licensed attached sweep/cancellation/recovery、
PID reuse 拒绝和 detach preservation receipt 均作为 release checks 维护。单元测试无副作用：
测试收集不会启动 COMSOL；integration probe 仅在显式请求时运行，并在全新的串行子进程中对
精确进程树进行清理。仓库专用测试、release fixture、gate 与 provenance 见
`development_kit/README.md`；普通 wheel/sdist 不包含该目录。

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

licensed branch-continuation 验收使用两个不可变的中性 slab models，网格均为
4,798 个单元、1,039 个顶点，并在 **0 度和 10 度**对 periodic parent 与两个
ports 完成精确 incidence readback。两个 own peaks 分别为
**5.200823291715293** 和 **5.195931563688228 um**，实测 shift 为
**4.891728027065 nm**，位于调用方 guard window 内。peak T 分别为
**0.9999455828498354** 和 **0.9999448178081717**；FWHM 分别为
**0.4807802607560502** 和 **0.4836621446746728 um**；Q 分别为
**10.817464268472143** 和 **10.742894851080779**。20 行原始数据总体范围为
**R = 0.000422180032088570 至 0.512242443246136**、
**T = 0.487757556753878 至 0.999577819967919**、
**max |A| = 3.015050383793419e-17**；最大闭合误差为
**1.33935578502046e-14**，波长同步误差为零。独立的 18 行 boundary control
消耗了唯一一次声明的 expansion，并以 `unresolved_at_declared_cap` 完成；它没有
生成额外 request，也没有宣称 branch disappearance。两个 campaigns 均保持 source
hash 不变，结束后无 client、进程或 lease 残留。

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

# 安装后，普通用户首选设置界面：
comsol-mcp-settings

# 推荐离线手册索引；输出目录必须只含 ASCII 字符。
python -m pip install ".[manuals]"
python -m comsol_mcp.knowledge.lexical_manual build --index D:\comsol_docs_fts\manuals.sqlite3
```

如需可选的隔离语义检索（sentence-transformers，不含 ChromaDB）：

```powershell
python -m pip install ".[semantic-docs]"
# 优先使用设置界面的“文档”页；开发者/agent 自动化可编辑 JSON：
#   semantic_docs.enabled = true
#   semantic_docs.root = "D:/comsol_semantic"
#   semantic_docs.lexical_index = "D:/comsol_docs_fts/manuals.sqlite3"
#   semantic_docs.model_path = "D:/comsol_semantic/models/<model>/<revision>"
```

若 Windows 用户目录含非 ASCII 字符，请避免 editable install。源码变化后运行 `python -m pip install . --no-deps`，并重启 MCP host；服务器不会热加载 `comsol_mcp/tools/`。

MCP 客户端配置示例：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["D:\\path\\to\\python-env\\Scripts\\comsol-mcp.exe"],
      "environment": {
        "COMSOL_MCP_SETTINGS_PATH": "D:\\path\\to\\COMSOL_Multiphysics_MCP\\settings.json"
      }
    }
  }
}
```

普通用户在设置界面选择 `core` 即使用紧凑默认 profile；开发者和 agent 可在 JSON 中设置
等价的 `profile.name`。客户端示例见
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
