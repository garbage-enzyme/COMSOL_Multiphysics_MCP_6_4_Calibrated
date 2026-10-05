# Explicit strict wavelength validation

Use `strict_wavelength_policy` with `study_staged_parametric_sweep` or a `staged_sweep` durable job.
This policy is optional. The default records controls without verifying wavelength agreement.
Strict mode applies only to the exact parameter names `wl` and `wavelength`.
Native execution remains Windows-only.

```json
{
  "relative_tolerance": 1e-8,
  "absolute_tolerance_m": 1e-15,
  "dataset_tag": "dset1"
}
```

The values above illustrate the input shape. They are not recommended scientific tolerances.
The caller must supply finite nonnegative tolerances. At least one tolerance must be positive.
Unknown policy fields are invalid. The dataset tag must identify one explicit COMSOL dataset.
Provide `study_name` and `study_step_tag`. The selected step must have type `Wavelength`.
The dataset's solution must bind to the selected study.
The adapter resolves the COMSOL tag to one exact MPh dataset node before evaluation.
Keep `study_step_property=plist` and `study_step_unit_property=punit`.
Do not disable `record_wavelength_controls` in strict mode.
Numeric values need `parameter_unit`. Supported units are `m`, `mm`, `um`, `µm`, and `nm`.
A literal such as `5[um]` includes its own unit.

Before each solve, the workflow sets the model parameter and one explicit study wavelength.
It sets the study wavelength in metres and uses the selected dataset for evaluation.
It compares the requested value with the evaluated parameter and `c_const/ewfd.freq`.
All three values must be positive finite real scalars in metres.
Each comparison must satisfy:

```text
abs(actual - requested) <= absolute_tolerance_m + relative_tolerance * abs(requested)
```

A mismatch prevents a successful scientific row. Transport or worker completion does not override this failure.
The workflow restores the original typed `plist` and `punit` properties, then checks their readback.
Restoration applies after success or failure. A restoration error prevents success.
Partial or hook-skipped execution cannot report verified wavelength agreement.
Record-only execution reports `verified=false`.
Strict policy and requested values bind the resume identity.
Resume rejects a changed policy or previously successful rows with inconsistent wavelength controls.
Two nearby wavelengths do not need different reflection or transmission values.
The licensed gate checks control agreement. It does not infer agreement from spectral variation.
