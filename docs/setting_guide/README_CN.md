# COMSOL MCP 设置指南

适用于 COMSOL MCP `0.6.4` 和设置 schema `1.2.0`。

普通用户优先使用设置界面，不需要手工编辑 JSON。直接修改 `settings.json` 的方式仍然
保留，适合开发者、获得用户明确授权的 agent、批量安装、自动部署，以及界面无法打开时
的恢复。两种方式修改的是同一份设置，不会为 Codex、Claude、opencode 或其他 agent
各建一份文件。

保存设置后，必须重启 Codex 或当前使用 MCP 的客户端，新设置才会生效。

## 打开设置界面

可以直接告诉 agent“打开 COMSOL MCP 设置”，agent 会调用：

```text
settings.start
```

也可以在 Windows 命令行中运行：

```powershell
comsol-mcp-settings
comsol-mcp-settings --settings-path "D:\settings\settings.json"
comsol-mcp-settings --settings-path "D:\settings\settings.json" --validate-only
```

这个可执行文件会直接启动 GUI，不要求也不会启动 MCP stdio host、COMSOL 或 Java。
用 `--settings-path` 把它绑定到 MCP client 实际使用的同一设置文件。`--validate-only`
只验证 package、设置目标、GUI runtime 和快捷方式前提，不导入 Tk，也不写文件。

“关于”页提供明确的“创建桌面快捷方式”和“移除桌面快捷方式”操作；等价命令为：

```powershell
comsol-mcp-settings --settings-path "D:\settings\settings.json" --create-desktop-shortcut
comsol-mcp-settings --settings-path "D:\settings\settings.json" --shortcut-status
comsol-mcp-settings --settings-path "D:\settings\settings.json" --remove-desktop-shortcut
```

每用户快捷方式固定命名为 `COMSOL MCP Settings.lnk`，并持续绑定创建时的准确设置文件。
安装、部署、MCP 启动、`settings.start`、首次打开、“保存”和“应用”都不会自动创建它。
同名快捷方式若已过期或属于其他程序，只有用户在 GUI 中确认，或在创建命令中明确追加
`--replace-existing-shortcut` 后才会替换；移除操作只删除本应用拥有的快捷方式，并保留
外来或损坏的桌面项目。

从仓库源码或解压后的源码分发包运行时，也可以使用根目录手动启动器：

```powershell
.\Open_Settings_GUI.ps1
.\Open_Settings_GUI.ps1 -PythonPath "D:\path\to\python.exe"
.\Open_Settings_GUI.ps1 -PythonPath "D:\path\to\python.exe" -SettingsPath "D:\settings\settings.json"
.\Open_Settings_GUI.ps1 -ValidateOnly
```

未指定 `-PythonPath` 时，脚本依次检查当前虚拟环境、`comsol-mcp-settings` 所在环境、
`PATH` 中的 `python.exe`，以及 Windows Python launcher 提供的 CPython 3.14。明确指定的
`-SettingsPath` 必须是绝对文件路径，而且父目录必须已经存在。`-ValidateOnly` 只验证
Python、package import 和可选 settings 定位器，输出不含路径的 JSON receipt；它不会创建
settings 文件、打开 Tk 或启动 COMSOL。

打开设置界面不会启动 COMSOL，也不会开始计算。agent 打开界面后应暂停，让用户完成设置，
不能同时在后台修改同一份 JSON。

第一次使用安装版时，界面会先询问是否创建设置。只有用户确认后，才会创建：

```text
%LOCALAPPDATA%\comsol_mcp\settings.json
%LOCALAPPDATA%\comsol_mcp\models

%PROGRAMDATA%\comsol_mcp\runtime
%PROGRAMDATA%\comsol_mcp\artifacts
```

前两个位置允许 Windows 用户名中出现中文。后两个位置用于运行记录、锁和正式产物，必须
只含 ASCII 字符，所以默认放在通常不含中文的 `%PROGRAMDATA%`。可选功能的目录不会自动
创建。

如果还没有设置 COMSOL 路径，界面会直接打开 `COMSOL/Java` 页，并尝试查找本机的
COMSOL 6.4 和它自带的 Java：

- 只找到一个可用安装时，路径会填入输入框，但不会自动保存；
- 没有找到时，输入框保持空白，不会编造一个默认路径，也不会报错打断使用；
- 找到多个安装时，由用户选择，界面不会猜测；
- 安装或移动 COMSOL 后，可以点击“自动检测”重新查找；
- 也可以点击“浏览”手动选择目录。

## 设置界面怎么用

每个选项旁边都会显示它在 JSON 中的完整名称。路径选项带有例子，也可以用“浏览”选择。

- “应用”：检查并保存，窗口继续保持打开；
- “保存并退出”：检查并保存，然后关闭窗口；
- “取消”：直接关闭，不保存本次修改；
- 输入不合要求时，该项会标红，而且不能保存；
- 修改任何会影响 MCP 的选项后，界面会提醒需要重启；
- 自动找到 COMSOL 或 Java 时，只显示持续可见的重启提示，不会一打开就弹出重启窗口；
- 切换界面语言不会丢失尚未保存的内容，也不会跳回其他页面。
- 调整界面大小会立即预览。一般选择“跟随 Windows 显示设置”；需要固定大小时可选
  100%、125%、150% 或 200%；
- `profile.name` 下方的说明会随选择变化，直接说明该 profile 能做什么、适合什么情况，
  以及何时不应选择。

同一份设置一次只能打开一个编辑窗口。再次打开时会提示已有窗口正在使用。设置窗口打开
期间，如果其他程序改了同一文件，当前窗口会停止保存，避免互相覆盖。

语言选项显示语言自称名和保存值：

```text
English (en)
简体中文 (zh-cn)
繁體中文 (zh-tw)
```

旧设置中的 `zh_CN` 和 `zh_TW` 仍能读取，保存时会改为 `zh-cn` 和 `zh-tw`。

## 设置文件在哪里

程序按下面顺序寻找设置：

1. `COMSOL_MCP_SETTINGS_PATH` 指定的绝对文件；
2. 从源码运行时，仓库根目录的 `settings.json`；
3. 安装版的 `%LOCALAPPDATA%\comsol_mcp\settings.json`；
4. 安装包内的只读模板，它只用于第一次创建设置。

设置界面不会写入 `site-packages`。设置文件本身可以位于中文路径，但必须是普通文件，路径
中不能经过符号链接或 junction。

文件必须使用 UTF-8 编码，只包含一个 JSON 对象，不能有重复 key，大小不能超过 64 KiB。
未知字段和非法值会出现在 `capabilities.project_settings.settings_errors` 中，但返回结果
不会暴露本机路径。缺少某个字段时只补该字段的默认值；某一项非法时只回退该项；整个 JSON
损坏时才使用完整默认配置。

## 路径怎么选

读取设置时，程序会展开开头的 `%LOCALAPPDATA%` 和 `%PROGRAMDATA%`。其他环境变量写法
不会自动展开。所有路径都必须是绝对路径；空字符串、相对路径和控制字符都不允许。

| 路径用途 | 是否支持中文 | 默认位置 |
| --- | --- | --- |
| 设置文件 | 支持 | `%LOCALAPPDATA%\comsol_mcp\settings.json` |
| 模型读取目录 | 支持 | `%LOCALAPPDATA%\comsol_mcp\models` |
| 运行目录和求解器锁 | 不支持，只能用 ASCII | `%PROGRAMDATA%\comsol_mcp\runtime` |
| Durable job 单独目录 | 不支持，只能用 ASCII | `null`，从运行目录推导 |
| MCP 自有产物 | 不支持，只能用 ASCII | `%PROGRAMDATA%\comsol_mcp\artifacts` |
| COMSOL 和 Java | 取决于软件安装位置 | `null`，由界面检测 |
| 可选语义资产 | 取决于所用后端，建议用 ASCII | `null` |

模型读取目录可以包含中文。运行目录、durable job 目录和产物目录如果包含中文，设置界面会
在保存前标红；直接修改 JSON 时，后端也会拒绝这些值，不会等到开始计算后才报错。

## 全部设置项

### 文件与界面

| 设置项 | 默认值 | 作用和可填写内容 |
| --- | --- | --- |
| `schema_name` | `"comsol_mcp.settings"` | 设置格式名称，只读，必须完全一致。 |
| `schema_version` | `"1.2.0"` | 新保存的文件使用 `1.2.0`；旧版 `1.0.0` 和 `1.1.0` 可以读取，并在内存中转换。 |
| `gui.language` | `"zh-cn"` | 只能是 `"en"`、`"zh-cn"` 或 `"zh-tw"`。 |
| `gui.scale` | `"system"` | 可选 `"system"`、`"100"`、`"125"`、`"150"` 或 `"200"`；界面把数字显示为百分比。 |

### 工具范围

| 设置项 | 默认值 | 作用和可填写内容 |
| --- | --- | --- |
| `profile.name` | `"core"` | 可选 `core`、`basic_fem`、`wave_optics`、`experimental` 或 `full`；保存为小写。不支持的值会回落到 `core` 并报告来源。 |

新手在重视安全、希望减少可用操作时，可以从 `core` 开始。大多数进行常规仿真的用户应
选择 `basic_fem`。Profile 只控制 COMSOL 自动化仿真及未来自主探索工具的可见性；共享
协作和语义检索使用独立 Boolean 开关，可用于任意 profile，也可同时启用。

| Profile | 适用情况 |
| --- | --- |
| `core` | 面向新手的安全默认项：操作较少，可查看模型、管理任务、进行谨慎的单点检查与手册搜索。 |
| `basic_fem` | 推荐大多数用户选择：常规 FEM 建模、结果导出和 Windows standalone 包。 |
| `wave_optics` | 光学与超表面、场结果查看、Wave Optics 检查、单点审计和分阶段参数流程。 |
| `experimental` | 范围更广或尚未成熟、需要仔细检查输出的额外工具。 |
| `full` | 需要几乎全部非 feature 工具且接受较弱文件范围保护的旧流程迁移；不建议新用户使用。 |

### 运行与文件范围

| 设置项 | 默认值 | 作用和可填写内容 |
| --- | --- | --- |
| `runtime.directory` | `%PROGRAMDATA%\comsol_mcp\runtime` | MCP 的运行记录和锁目录。自定义值必须是只含 ASCII 的绝对路径。`null` 只用于兼容旧的平台默认行为。 |
| `runtime.jobs_directory` | `null` | 可选的 durable job 单独目录。`null` 表示从实际运行目录推导；自定义值必须是只含 ASCII 的绝对路径。 |
| `paths.model_read_roots` | `[%LOCALAPPDATA%\comsol_mcp\models]` | 允许读取且不得原地修改的源模型目录。每项必须是不同的绝对路径，可以包含中文；`[]` 表示拒绝读取任何模型。 |
| `paths.artifact_write_root` | `%PROGRAMDATA%\comsol_mcp\artifacts` | MCP 自有的结果、manifest 和证据目录。自定义值必须是只含 ASCII 的绝对路径。`null` 只用于兼容旧的推导方式。 |

设置文件接受某个路径，不代表所有工具都能立刻使用它。实际操作仍会检查文件是否存在、
是否位于允许范围内、扩展名、link/junction、是否覆盖旧文件，以及该工具自己的限制。

### COMSOL 与 Java

| 设置项 | 默认值 | 作用和可填写内容 |
| --- | --- | --- |
| `comsol.installation_root` | `null` | COMSOL Multiphysics 6.4 安装根目录。设置界面可以自动查找。Standalone 工具明确传入的路径优先。 |
| `java.java_home` | `null` | 可选 Java runtime 目录。自动检测优先使用 COMSOL 自带 Java。 |
| `java.jdk_home` | `null` | 可选 JDK 目录。在已验证的 COMSOL 安装中通常与 `java.java_home` 相同。 |

Java 查找顺序是：COMSOL 自带且可用的 Java、`JAVA_HOME`、`JDK_HOME`、`PATH`。自动检测
需要替换已有非空值时，一定会先询问用户。

### 共享模式与所有者

| 设置项 | 默认值 | 作用和可填写内容 |
| --- | --- | --- |
| `shared_server.enabled` | `false` | 独立控制本机 Desktop/Server 交互协作流程；可与任意 profile 组合，也不会启动或关闭用户自己的 COMSOL Server。 |
| `ownership.owner` | `null` | 可选的所有者名称。最多 256 个字符，不能为空且不能含控制字符；`null` 时从父进程生成有限长度的名称。 |

### 证据检查

| 设置项 | 默认值 | 作用 |
| --- | --- | --- |
| `evidence_integrity.checks.outcome_contract_validation` | `true` | 检查执行结果和结论的约定格式。 |
| `evidence_integrity.checks.artifact_chain_verification` | `true` | 检查产物内容、来源和哈希链。 |
| `evidence_integrity.checks.summary_claim_verification` | `true` | 对照引用的产物数值检查摘要结论。 |
| `evidence_integrity.checks.producer_driver_compatibility` | `true` | 继续旧任务前检查生成器和驱动身份。 |

关闭任何一项都属于探索性跳过。受影响的正式结果会继续标记为“未完整验证”。

### 可选语义检索

| 设置项 | 默认值 | 作用和可填写内容 |
| --- | --- | --- |
| `semantic_docs.enabled` | `false` | 独立控制隔离式语义工具；可与任意 profile 以及 `shared_server.enabled` 组合。 |
| `semantic_docs.root` | `null` | 预处理语义检索资产的可选根目录。它不是 COMSOL 安装包自带的 manual 目录，也不会自动检测。 |
| `semantic_docs.lexical_index` | `null` | 可选的只读 SQLite 词法索引文件。 |
| `semantic_docs.model_path` | `null` | 可选的本地语义模型版本目录。 |

三项资产路径保持 `null` 是正常状态。只有事先生成并放好所需资产后，开启
`semantic_docs.enabled` 才能提供相应检索功能。

## 开发者和 Agent 的 JSON 设置方式（高级）

开发者或 agent 需要可复现自动化、设置由安装器/部署脚本统一管理、界面无法使用，或需要
恢复时，可以直接编辑 JSON。Agent 编辑必须获得用户明确请求。编辑前先停止 MCP host 并
关闭设置界面；只修改上文解析出的可写文件，验证后重启真正拥有 MCP 的客户端。

完整默认模板如下：

```json
{
  "schema_name": "comsol_mcp.settings",
  "schema_version": "1.2.0",
  "profile": {"name": "core"},
  "runtime": {
    "directory": "%PROGRAMDATA%/comsol_mcp/runtime",
    "jobs_directory": null
  },
  "paths": {
    "model_read_roots": ["%LOCALAPPDATA%/comsol_mcp/models"],
    "artifact_write_root": "%PROGRAMDATA%/comsol_mcp/artifacts"
  },
  "shared_server": {"enabled": false},
  "evidence_integrity": {
    "checks": {
      "outcome_contract_validation": true,
      "artifact_chain_verification": true,
      "summary_claim_verification": true,
      "producer_driver_compatibility": true
    }
  },
  "semantic_docs": {
    "enabled": false,
    "root": null,
    "lexical_index": null,
    "model_path": null
  },
  "ownership": {"owner": null},
  "java": {"java_home": null, "jdk_home": null},
  "comsol": {"installation_root": null},
  "gui": {"language": "zh-cn", "scale": "system"}
}
```

现有的 `COMSOL_MCP_*`、`COMSOL_SEMANTIC_*`、`JAVA_HOME` 和 `JDK_HOME` 环境变量仍
保留兼容覆盖能力。如果某个变量在 MCP 进程启动前已经存在，它优先于 JSON 转换出的同名
环境值。普通用户的新安装应使用设置界面；开发者和 agent 自动化可使用同一份 JSON，但
不要再建立一套只靠环境变量的独立配置。

## 更新与恢复

更新或重装 MCP package 前，先备份实际生效的可写 `settings.json`。如果使用了
`COMSOL_MCP_SETTINGS_PATH`，应备份它指向的准确文件。安装后恢复或重新检查设置，重启
MCP 客户端，再查看 `capabilities.project_settings`：

```text
configuration_state: valid
settings_errors: []
setup_required: false
```

文件缺失、JSON 损坏、key 重复、不是 UTF-8、超过大小限制或使用不支持的未来 schema 时，
设置界面只提供恢复或退出，不会猜测如何修补。用户确认恢复后，程序会保留一份受大小限制
的损坏文件副本，再用原子写入方式保存标准 `1.2.0` 设置。

证据检查的详细含义见
[`../evidence_integrity/README_CN.md`](../evidence_integrity/README_CN.md)。默认关闭的共享
Desktop/Server 工作流见
[`../interactive_shared_session/README_CN.md`](../interactive_shared_session/README_CN.md)。
