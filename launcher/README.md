# Durable local launcher

This directory contains the reusable Windows launcher for long local COMSOL
campaigns that have a local Python environment. It is the `launcher` mode from
the [five execution modes guide](../docs/simulation_execution_modes/README.md).
It is not the Python-free `standalone` EXE.

Launcher version: `1.8.1`. The accepted v1.7 runtime is the baseline. Version
1.8 keeps its monitor, pause, result-journal, terminal, and failure behavior and
removes machine-specific package, Python, output, and drive assumptions. Version
1.8.1 distinguishes an idle `comsol-mcp.exe` stdio host from a real solver and
rejects ambiguous combined mode switches.

## Contents

- `powershell/DurableLauncher.psm1`: shared preflight, start, monitor, pause,
  resume, duplicate-detection, resource, and terminal module.
- `python/durable_control.py`: attempt-bound pause request reader and
  acknowledgment helper for a Python driver.
- `templates/Run_DurableJob.template.ps1`: parameterized launcher entry point.
- `tests/`: synthetic driver and PowerShell 5.1/pwsh acceptance tests.

The launcher files are repository assets, not `comsol_mcp` wheel imports. Do
not copy the shared module into each project. Keep one shared version and bind
its hash in every run.

## Driver contract

The launcher does not invent a scientific loop. The project driver must:

1. In `DURABLE_JOB_MODE=validate`, validate the immutable spec, source,
   parameters, output identity, point IDs, policy, paths, and required runtime
   without constructing a COMSOL client.
2. In `DURABLE_JOB_MODE=solve`, acquire one exact owner and solve one recoverable
   point per iteration.
3. Skip only exact verified completed point identities.
4. Apply and read back every solver-facing value before solving.
5. Append one accepted result to `results.jsonl`, flush, and `fsync` before
   publishing progress or starting the next point.
6. Treat `results.jsonl` as completion authority and `status.json` as a mutable
   projection.
7. Poll `durable_control.pending_pause_request()` only between points. Finish
   and flush the current point before writing `paused_after_point` and calling
   `acknowledge_pause()`.
8. Release the exact worker, descendants, model, lease, lock, and temporary
   files on every terminal path.

The launcher passes these environment variables to the driver:

| Variable | Value |
| --- | --- |
| `DURABLE_JOB_MODE` | `validate` or `solve` |
| `DURABLE_JOB_OUTPUT` | Absolute owned output directory |
| `DURABLE_JOB_CONTROL_DIR` | Absolute pause request/acknowledgment directory |

Project-specific inputs belong in an immutable spec or additional explicit
environment values added to both `ValidateEnvironment` and `RunEnvironment`.
The two environments must not disagree on any spec-bound value.

## Start a job

Keep the template in this directory or copy it beside a versioned launcher
package and pass absolute paths:

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

The resource values above are examples, not portable recommendations. Set them
from the model, mesh, solver, host, and expected artifact size. The module
checks the actual Windows system drive and the drive containing `-Output`; it
does not require fixed `C:` or `D:` layouts. Output must be on a local drive.

Use `-ValidateOnly` first. It must print
`LAUNCHER_VALIDATE_PASS no solver client created`. Then use `-Run` or start the
template without a mode switch and select `RUN` interactively. `-Run` starts or
resumes the driver and then opens the monitor; do not add `-Monitor` to that
command. Use `-Monitor` by itself only to inspect an existing or stopped job and
choose any offered `resume` action explicitly. `-Run`, `-Monitor`, and
`-ValidateOnly` are mutually exclusive. The monitor accepts `pause`, `status`,
`help`, `resume`, and `quit`. `quit` closes only the monitor; it does not
terminate an active worker.

An idle `comsol-mcp.exe` process is a solver-free MCP stdio host and does not
block a launcher. A real `comsol*.exe` solver/server other than that exact host,
an `mphserver*.exe`, or a COMSOL/MPh Java runtime remains a collision and blocks
startup.

## Terminal meanings

| Banner | Meaning |
| --- | --- |
| Green `COMPLETED SUCCESSFULLY` | All planned points are durably complete |
| Yellow `SCIENTIFIC / QUALITY GATE NOT MET` | Execution ended, but a declared scientific or quality gate rejected acceptance |
| Red `FAILED` | Runtime, code, worker, or unknown non-success failure |
| Blue `PAUSED` | An attempt-bound pause was acknowledged after a durable point |
| Yellow durable boundary | A wall or partial boundary stopped before all points |

Non-success views show a bounded reason and exact status, stdout, and stderr
paths. Large evidence arrays are not printed in the terminal.

## Validate the launcher

Run the same suite in Windows PowerShell 5.1 and current pwsh:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File launcher\tests\Test_DurableLauncher.ps1 `
  -PythonPath C:\path\to\python.exe

pwsh.exe -NoProfile `
  -File launcher\tests\Test_DurableLauncher.ps1 `
  -PythonPath C:\path\to\python.exe
```

The repository pytest wrapper also runs the complete PowerShell suite in both
hosts on Windows CI. Change the version whenever the shared module, helper, or
template behavior changes; never edit a shared launcher already imported by an
active campaign.

## Limits

- Windows only; the module uses CIM process inventory and a foreground console.
- Local launcher mode requires Python and the project's COMSOL Python runtime.
- Pause is cooperative and occurs only after the current point is durably
  committed; it does not interrupt a factorization.
- The module cannot make a non-durable project driver resumable.
- This launcher is for one host and one solver owner. It is not a distributed
  scheduler or a network lease.
