# 五种仿真运行方式

COMSOL MCP 把仿真运行方式分为五种。运行方式决定由谁管理任务、执行电脑需要安装什么、
中断后能保留多少进度，以及最终交给其他人或其他电脑什么文件。它不是物理设置，也不会
降低结果验证要求。

agent 默认只使用 `interactive`、`inline` 和 `launcher`。`standalone` 与
`mphonly` 用于跨设备交付。准备后两种方式前，agent 必须先询问目标环境，不能猜测。

## 快速选择

| 方式 | 适用情况 | 执行电脑需要 | 中断后的情况 | 主要输出 |
| --- | --- | --- | --- | --- |
| `interactive` | 需要边修改、边检查、边查看短反馈 | 正常的 COMSOL MCP Python 环境和已授权 COMSOL | 在线会话不是存档；必须明确保存派生 MPH 或证据文件 | 在线模型及明确保存的文件 |
| `inline` | 干运行、冒烟测试或保守预计少于 1 小时的短任务 | 本地 Python、所选 COMSOL 接口和已授权 COMSOL | 没有自动续跑；只保留脚本自己写出的文件 | 脚本生成的 MPH、数据和日志 |
| `launcher` | 本地长任务需要无人值守、查看状态和从完成点继续 | 本地 Python、已授权 COMSOL 和仓库内启动器 | 每完成并写稳一个点才进入下一点；可在点与点之间暂停和继续 | 逐点记录、状态、日志、结果和可选 MPH 存档 |
| `standalone` | 另一台 Windows 电脑没有 Python，但要运行同样的可恢复任务 | Windows 10/11 x64、已安装并授权的 COMSOL 6.4 及其自带 Java | 与 launcher 相同的逐点记录、暂停和精确续跑 | 逐点记录、状态、日志和结果文件 |
| `mphonly` | 商业超算、Linux 云端或其他 COMSOL 环境只接收一个模型文件 | 兼容且已授权的 COMSOL，以及目标调度和许可证功能 | 可使用 COMSOL 自带检查点恢复到最近检查点；不承诺每点都能恢复 | 一个最终求解完成的 MPH 文件 |

`inline` 的 1 小时只是 agent 做计划时使用的保守默认值，不是强制超时，也不是完成保证。
agent 说明数据保存与恢复差异后，用户可以明确选择其他方式。

## 选择流程

1. 需要通过 MCP 分步骤修改模型并立即读取结果时，选择 `interactive`。
2. 干运行、冒烟测试或不需要自动续跑的短任务，选择 `inline`。
3. 任务较长、在当前电脑运行且本地有 Python 时，选择 `launcher`。
4. 任务需要移到其他电脑时，先停止编写，询问下文列出的目标环境信息。
5. 目标是受支持的 Windows 10/11 x64 和 COMSOL 6.4，需要类似 launcher 的状态与
   逐点续跑，但没有 Python 时，才选择 `standalone`。
6. 目标要求只交付一个可移植 COMSOL 模型，尤其是 COMSOL 管理的批处理、集群或云端
   任务时，选择 `mphonly`，并明确说明它较弱的检查点和在线状态能力。

不要因为任务很长就直接选择 `standalone`。当前电脑有本地 Python 的长任务应使用
`launcher`。

## 跨设备或云端必须询问的信息

编写 `standalone` 包或 `mphonly` 模型前，必须询问：

- 目标操作系统、处理器架构；
- COMSOL 的精确版本、构建号和已安装模块；
- 许可证类型、许可证服务器是否可达，以及是否具备批处理或集群权限；
- 是否有 Python，是否只允许使用 COMSOL 自带 Java；
- 调度方式，例如 SLURM、PBS、LSF 或商业平台网页入口；
- 共享存储、工作目录、路径、配额和文件传输规则；
- 网络限制，以及计算节点能否连接许可证服务器；
- 是否需要在线状态、暂停、续跑、逐点导出，以及最终需要哪些输出；
- 最长运行时间、内存、核心数、节点数和文件大小限制。

信息不足时，应返回待确认问题，不能生成一个依赖猜测的运行包。

## `interactive`

普通交互方式由 agent 依次调用 MCP 工具：检查模型、修改派生副本、划分网格、求解、
回读并保存。所有调用保持串行，同时只能有一个求解器所有者。连接中的进程或内存模型
本身不是可靠存档。

如果用户还要在 COMSOL Desktop 中操作同一个模型，应改用默认关闭的
[Desktop/Server 交互协作指南](../interactive_shared_session/README_CN.md)。该协作方式
增加明确轮流操作、Server 模型认领和版本锁；普通 MCP 交互不需要这些步骤。

## `inline`

`inline` 表示 agent 编写一个有明确范围的 Python 脚本，并直接从命令行运行。它适合
语法检查、只构建不求解、单点冒烟测试和短仿真。脚本仍必须：

- 拒绝与其他 COMSOL、Java 或 MPh 求解进程同时运行；
- 使用明确输入和只含英文字符的输出目录；
- 不修改用户提供的源模型；
- 退出前写出日志和约定结果；
- 清理自己创建的模型和进程，并核对清理结果；
- 不把“得到了有限数值”直接说成“物理结论已验证”。

如果中断会损失明显工作、需要精确区分多个点，或保守预计不少于 1 小时，应在开始前
改用 `launcher`。

## `launcher`

`launcher` 使用仓库内的[通用启动器](../../launcher/README_CN.md)。Python 驱动负责
科学计算循环；PowerShell 模块负责运行前检查、启动、状态显示、暂停请求、重复启动
拦截、结束状态显示和资源检查。

追加写入的结果记录才是完成进度的依据。`status.json` 只是便于查看的状态摘要，遇到
Windows 文件占用时可能落后。驱动必须完成并写稳当前点后才确认暂停；继续运行时，只能
跳过规格、驱动、源文件和点身份都完全一致的已验证记录。

## `standalone`

`standalone` 是 alpha4 已验收的目标机无 Python 方案。相关工具仍位于 `basic_fem`
profile：

`standalone_build`、`standalone_start`、`standalone_status`、
`standalone_pause`、`standalone_resume`、`standalone_tail` 和
`standalone_results`。

生成的 EXE 面向 Windows 10/11 x64 和已安装、已授权的 COMSOL 6.4。它使用目标安装
中的 `comsolcompile`、`comsolbatch`、求解器、许可证和自带 Java。它不会附带或替代
COMSOL，也不支持 Windows Server、Linux、macOS 或 6.4 以前的 COMSOL。目标机不需要
另装 Python、Conda、MPh、JPype、外部 Java、新版 .NET 运行时、开发工具或 Visual
Studio，也不需要为了运行而在线下载组件。

## `mphonly`

`mphonly` 在交付前把研究、参数和任务设置完整写入一个模型。最终只交付一个求解完成的
MPH 文件。运行期间 COMSOL 仍可能产生临时文件、日志、状态、同步文件或恢复文件；
“一个 MPH”指最终交付物，不表示运行过程中完全不写临时文件。

在已验收的 COMSOL 6.4 电脑上，三点解析电容器扫描保存为一个 MPH。新的独立进程重新
打开该文件后，能读回全部三个参数值和电容值，相对解析误差约为 `6.81e-10`，读取前后
文件哈希不变。这证明该电脑能保存并重新读取多点解，但不能证明中断后恢复。

COMSOL 6.4 的 Job Configuration Parametric Sweep 可以按每组每 N 个参数同步一次，
在每个检查点保存恢复文件，并从最近检查点恢复；同步完成后也可以把包含解的模型保存为
MPH。这一能力可以使用，但弱于 `launcher`：

- 恢复粒度是检查点，不一定是单个参数；
- 最近检查点之后已经算完的值仍可能丢失；
- 分布式执行、重启次数、存活检查、调度器和文件路径取决于目标许可证与集群配置；
- 普通 Study Parametric Sweep 本身不能证明 Job Configuration 的检查点能力；
- 最终 MPH 不提供 launcher 的精确逐点记录、与本次尝试绑定的暂停确认和在线监视器。

参见 COMSOL 6.4 官方的
[Parametric Sweep 任务配置](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_solver.36.230.html)
与[集群计算](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_solver.36.042.html)文档。

为了获得最好的移植性，模型不能依赖本机绝对路径、缺失的插值文件、只存在于某台电脑的
材料、未说明的方法或外部脚本。只有核对目标版本和模块后，才能声称模型能在目标环境求解。

## 所有方式共同遵守的规则

- 同时只能有一个求解器所有者；MCP 调用必须串行。
- 输入模型保持不变，结果保存到不同文件。
- 求解前核对请求参数，并回读 COMSOL 实际采用的值。
- 解释结果前保存原始数值、单位、源文件身份、网格与研究身份以及清理证据。
- 进程返回零或生成了 MPH 文件，不等于仿真结论已经验证。
- 必须在运行前选好方式。不能等前台循环已经运行后，再补称它具有断点恢复能力。
