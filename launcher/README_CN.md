# 本地长任务启动器

本目录提供用于本地长时间 COMSOL 任务的通用 Windows 启动器，执行电脑需要本地 Python
环境。它对应[五种运行方式指南](../docs/simulation_execution_modes/README_CN.md)中的
`launcher`，不是目标机无需 Python 的 `standalone` EXE。

启动器版本：`1.8.0`。已验收的 v1.7 是功能基线。v1.8 保留原有状态显示、暂停、逐点
记录、结束颜色和故障处理，并删除固定电脑路径、Python 路径、输出路径和盘符假设。

## 目录内容

- `powershell/DurableLauncher.psm1`：运行前检查、启动、状态显示、暂停、继续、重复启动
  拦截、资源检查和结束状态显示。
- `python/durable_control.py`：供 Python 驱动读取暂停请求并写入确认。
- `templates/Run_DurableJob.template.ps1`：通过参数配置的启动入口。
- `tests/`：合成驱动以及 PowerShell 5.1、pwsh 验收测试。

这些文件是仓库资源，不会作为 `comsol_mcp` wheel 的导入模块。不要把共享模块复制到每个
项目中；应保留一个版本，并在每次任务中记录它的哈希。

## Python 驱动必须做到什么

启动器不负责发明科学计算循环。项目驱动必须：

1. 当 `DURABLE_JOB_MODE=validate` 时，只检查不可变规格、源文件、参数、输出身份、点
   编号、验证规则和运行环境，不创建 COMSOL client。
2. 当 `DURABLE_JOB_MODE=solve` 时，只取得一个精确所有者，每次循环求解一个可恢复点。
3. 只跳过身份完全一致且验证通过的已完成点。
4. 求解前设置并回读所有会影响求解器的值。
5. 将一个通过验证的结果追加到 `results.jsonl`，执行 flush 与 `fsync`，然后才能更新
   进度或开始下一点。
6. 以 `results.jsonl` 作为完成依据；`status.json` 只是便于查看的状态摘要。
7. 只在点与点之间调用 `durable_control.pending_pause_request()`。当前点完成并写稳后，
   才能写入 `paused_after_point` 并调用 `acknowledge_pause()`。
8. 无论如何结束，都要释放精确 worker、子进程、模型、租约、锁和临时文件。

启动器向驱动传入：

| 变量 | 内容 |
| --- | --- |
| `DURABLE_JOB_MODE` | `validate` 或 `solve` |
| `DURABLE_JOB_OUTPUT` | 自有输出目录的绝对路径 |
| `DURABLE_JOB_CONTROL_DIR` | 暂停请求与确认目录的绝对路径 |

项目专用输入应放在不可变规格中，或同时明确加入 `ValidateEnvironment` 和
`RunEnvironment`。两种环境中的规格相关值必须完全一致。

## 启动任务

将模板保留在本目录，或与版本化启动器一起复制，然后传入绝对路径：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File launcher\templates\Run_DurableJob.template.ps1 `
  -Run `
  -Python C:\path\to\python.exe `
  -Driver C:\path\to\campaign_driver.py `
  -Output C:\comsol_runtime\owned_artifacts\campaign_v1 `
  -JobName "Campaign v1" `
  -JobId campaign-v1 `
  -TotalPoints 120 `
  -MinimumFreeRamGiB 8 `
  -MinimumFreeSystemDriveGiB 10 `
  -MinimumFreeOutputDriveGiB 100
```

上面的资源数值只是示例，不是适合所有电脑的建议。应根据模型、网格、求解器、电脑和
预计输出大小设置。模块检查实际 Windows 系统盘和 `-Output` 所在盘，不要求固定使用
`C:` 或 `D:`。输出必须位于本地磁盘。

先使用 `-ValidateOnly`，并要求它输出
`LAUNCHER_VALIDATE_PASS no solver client created`。然后使用 `-Run`；也可以不加运行开关，
在交互提示中选择 `RUN`。状态窗口接受 `pause`、`status`、`help`、`resume` 和
`quit`。`quit` 只关闭状态窗口，不会终止仍在运行的 worker。

## 结束状态

| 显示 | 含义 |
| --- | --- |
| 绿色 `COMPLETED SUCCESSFULLY` | 所有计划点已经可靠完成 |
| 黄色 `SCIENTIFIC / QUALITY GATE NOT MET` | 运行结束，但约定的科学或质量条件未通过 |
| 红色 `FAILED` | 运行、代码、worker 或未知的非成功故障 |
| 蓝色 `PAUSED` | 当前点可靠写入后，暂停请求得到确认 |
| 黄色持久边界 | 到达运行时限或部分完成边界，尚未完成全部点 |

非成功界面会显示简短原因，以及状态、标准输出和错误日志的精确路径；不会把大型证据数组
打印到终端。

## 验证启动器

在 Windows PowerShell 5.1 和当前 pwsh 中运行同一套测试：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File launcher\tests\Test_DurableLauncher.ps1 `
  -PythonPath C:\path\to\python.exe

pwsh.exe -NoProfile `
  -File launcher\tests\Test_DurableLauncher.ps1 `
  -PythonPath C:\path\to\python.exe
```

仓库的 pytest 包装测试也会在 Windows CI 中自动使用两个 PowerShell host 运行完整测试。
共享模块、helper 或模板行为改变时必须更新版本。已经被活动任务导入的共享启动器不能
原地修改。

## 限制

- 仅支持 Windows；模块使用 CIM 进程检查和前台控制台。
- 本地 launcher 方式需要 Python 和项目使用的 COMSOL Python 运行环境。
- 暂停只发生在当前点可靠完成后，不会中断正在进行的矩阵分解。
- 如果项目驱动本身不保存逐点进度，启动器不能凭空让它支持续跑。
- 它只管理一台电脑上的一个求解器所有者，不是分布式调度器，也不是跨电脑租约。
