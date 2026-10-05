# 显式严格波长验证

`study_staged_parametric_sweep` 和 `staged_sweep` 持久任务可使用 `strict_wavelength_policy`。
该策略可选。默认只记录控制值，不验证波长一致性。
严格模式只适用于精确参数名 `wl` 和 `wavelength`。
原生执行仍仅支持 Windows。

```json
{
  "relative_tolerance": 1e-8,
  "absolute_tolerance_m": 1e-15,
  "dataset_tag": "dset1"
}
```

上述数字只演示输入结构，不是推荐的科学容差。
调用者必须提供有限、非负的容差，并保证至少一个容差大于零。
未知策略字段无效。数据集标签必须指定一个明确的 COMSOL 数据集。
提供 `study_name` 和 `study_step_tag`。所选步骤的类型必须为 `Wavelength`。
数据集对应的解必须绑定到所选研究。
适配层先将 COMSOL 标签解析为唯一的 MPh 数据集节点，再执行评估。
保留 `study_step_property=plist` 和 `study_step_unit_property=punit`。
严格模式不能关闭 `record_wavelength_controls`。
数值输入必须提供 `parameter_unit`，支持 `m`、`mm`、`um`、`µm` 和 `nm`。
`5[um]` 这样的字面值自带单位。

每次求解前，工作流设置模型参数和一个明确的研究波长。
研究波长使用米，并用所选数据集进行评估。
工作流分别比较请求值、评估后的参数值和 `c_const/ewfd.freq`。
三个值都必须是以米为单位的正有限实标量。
每次比较必须满足：

```text
abs(actual - requested) <= absolute_tolerance_m + relative_tolerance * abs(requested)
```

不一致时不能提交科学成功行。传输成功或工作进程完成不能覆盖该失败。
工作流恢复原有类型的 `plist` 和 `punit`，并检查读回结果。
成功和失败后都执行恢复。恢复失败时不能报告成功。
部分执行或被 hook 跳过的执行不能报告波长已验证。
只记录模式返回 `verified=false`。
严格策略和请求值绑定续跑身份。
续跑会拒绝变更的策略，以及波长控制不一致的历史成功行。
两个相近波长无需得到不同的反射或透射值。
许可环境验收检查控制值一致性，不会根据光谱变化猜测波长一致。
