# Five simulation execution modes

COMSOL MCP uses five named execution modes. The mode describes who owns the
run, which software must exist on the execution computer, what survives an
interruption, and what artifact is handed to another person or machine. It is
not a physics setting and does not change the evidence required for a result.

Agents use `interactive`, `inline`, or `launcher` by default. `standalone` and
`mphonly` are cross-device delivery modes. Before preparing either one, the
agent must ask about the target environment instead of guessing.

## Quick choice

| Mode | Use it when | Execution computer needs | Interruption behavior | Main output |
| --- | --- | --- | --- | --- |
| `interactive` | Model edits and results need short, immediate feedback | The normal COMSOL MCP Python environment and licensed COMSOL | The live session is not a checkpoint; save a derived MPH or evidence artifact explicitly | Live model plus explicit saves/exports |
| `inline` | A dry run, smoke test, or short self-contained simulation is conservatively estimated below 1 hour | Local Python, the selected COMSOL API, and licensed COMSOL | No automatic resume; the script owns any files it writes | Script-defined MPH/data/log files |
| `launcher` | A long local campaign needs unattended monitoring and restart from completed points | Local Python, licensed COMSOL, and the repository launcher | One validated point is flushed before the next; pause and resume occur between points | Append-only rows, status, logs, artifacts, and optional MPH checkpoints |
| `standalone` | Another Windows workstation must run the same durable campaign without a Python environment | Windows 10/11 x64 and installed/licensed COMSOL 6.4 with its bundled Java runtime | Same pointwise journal and exact-attempt pause/resume model as the launcher | Result journal, status, logs, and generated result files |
| `mphonly` | A commercial cluster, Linux cloud, or other COMSOL environment accepts a single portable model file | A compatible licensed COMSOL installation and the target scheduler/license features | COMSOL-native checkpoints may recover to the latest checkpoint; exact per-point durability is not promised | One final solved MPH deliverable |

The 1-hour `inline` boundary is a conservative default for agent planning, not
a timeout or success guarantee. The user may choose another mode after the
agent explains the durability tradeoff.

## Decision flow

1. If the work must stay visible and editable through short MCP turns, use
   `interactive`.
2. Otherwise, if it is a dry run, smoke test, or short self-contained task that
   does not need automatic resume, use `inline`.
3. Otherwise, if it will run on the current computer with local Python, use
   `launcher`.
4. If it must move to another computer, stop and collect the target environment
   answers below.
5. Use `standalone` only for a supported Windows 10/11 x64 workstation with
   licensed COMSOL 6.4 when launcher-like status and pointwise resume are needed
   but Python is unavailable.
6. Use `mphonly` when one portable COMSOL model is the required handoff, in
   particular for COMSOL-managed batch, cluster, or cloud execution. State the
   weaker checkpoint and live-status guarantees.

Do not select `standalone` merely because a simulation is long. A long run on
the current Python-equipped workstation belongs to `launcher`.

## Questions for another device or cloud

Before writing a `standalone` package or an `mphonly` model, ask for:

- target operating system and architecture;
- exact COMSOL version/build and installed modules;
- license type, license-server reachability, and batch/cluster entitlements;
- whether Python is available and whether only COMSOL's bundled Java may be
  used;
- scheduler and submission interface, such as SLURM, PBS, LSF, or a vendor
  portal;
- shared-storage, working-directory, path, quota, and file-transfer rules;
- network restrictions and whether the compute nodes can reach the license
  server;
- required live status, pause, restart, per-point export, and final output;
- wall-time, memory, core/node, and maximum file-size limits.

Missing answers produce a target-information request, not a guessed package.

## `interactive`

In normal interactive mode, the agent calls MCP tools incrementally: inspect,
edit a derived model, mesh, solve, read back, and save. Keep calls serialized
and keep one solver owner. A connected process or in-memory model is not durable
evidence by itself.

When the user also wants to edit the same model in COMSOL Desktop, use the
separate default-off [Desktop/Server collaboration guide](../interactive_shared_session/README.md).
That shared topology adds explicit turns, server/model adoption, and revision
locks; it is not required for ordinary interactive MCP work.

## `inline`

Inline mode means the agent writes a bounded Python script and runs it directly
from a shell. It is suitable for syntax checks, build-only runs, one-point
smokes, and short simulations. The script must still:

- refuse another COMSOL/Java/MPh owner;
- use explicit inputs and an ASCII-safe output directory;
- keep caller-owned source models immutable;
- write logs and the intended output before exiting;
- clear owned models and verify process cleanup;
- label a finite result as execution evidence, not automatic physical
  validation.

Promote the task to `launcher` before starting when interruption would lose
meaningful work, multiple points need exact deduplication, or the conservative
estimate is at least 1 hour.

## `launcher`

Launcher mode uses the reusable [launcher package](../../launcher/README.md).
The driver owns the scientific loop; the PowerShell module owns preflight,
start, monitor, pause requests, duplicate detection, terminal presentation, and
resource checks.

The append-only result journal is completion authority. `status.json` is only a
projection and may lag after a Windows sharing failure. A driver must finish and
flush the current point before acknowledging pause, then resume by skipping only
verified rows with the exact spec, driver, source, and point identity.

## `standalone`

Standalone mode is the alpha4 Python-free target route. Build and control tools
remain in the `basic_fem` profile:

`standalone_build`, `standalone_start`, `standalone_status`,
`standalone_pause`, `standalone_resume`, `standalone_tail`, and
`standalone_results`.

The generated EXE runs on Windows 10/11 x64 with installed/licensed COMSOL 6.4.
It uses that installation's `comsolcompile`, `comsolbatch`, solver, license, and
bundled Java. It does not bundle COMSOL and does not support Windows Server,
Linux, macOS, or pre-6.4 COMSOL. The target needs no Python, Conda, MPh, JPype,
external Java, modern .NET runtime, SDK, Visual Studio, download, or network
installation step.

## `mphonly`

MPH-only mode prepares one model whose study/job configuration and parameters
are complete before handoff. The final deliverable is one solved MPH file. The
target run may still create temporary, log, status, synchronization, or recovery
files; "one MPH" describes the final deliverable, not an impossible no-temporary
I/O promise.

The accepted COMSOL 6.4 host stored a three-point analytical capacitor sweep in
one MPH. A fresh process reopened the file and recovered all three parameter
values and capacitances with relative errors of about `6.81e-10`; the file hash
was unchanged by inspection. This verifies solved-point storage and reload for
that host. It does not verify interruption recovery.

COMSOL 6.4 Job Configuration Parametric Sweep can synchronize at every Nth
parameter per group, save a recovery file at each checkpoint, and recover
progress up to the latest checkpoint. It can also save the synchronized solved
model to an MPH file. This is useful, but it is weaker than launcher mode:

- recovery is checkpoint-granular, not necessarily one parameter at a time;
- completed values after the last checkpoint can be lost;
- distributed execution, restart count, alive-time handling, schedulers, and
  file paths depend on the target COMSOL license and cluster setup;
- a normal Study Parametric Sweep does not by itself prove the Job
  Configuration checkpoint contract;
- a final MPH file does not provide the launcher's exact point journal,
  attempt-bound pause acknowledgment, or live monitor.

See COMSOL 6.4's official documentation for
[Parametric Sweep job configurations](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_solver.36.230.html)
and [Cluster Computing](https://doc.comsol.com/6.4/doc/com.comsol.help.comsol/comsol_ref_solver.36.042.html).

For the strongest portability, keep the model self-contained: no local absolute
paths, missing interpolation files, machine-only materials, undeclared methods,
or external scripts. Validate the exact target version and modules before
claiming it will solve there.

## Rules shared by all modes

- Keep one solver owner and serialize MCP calls.
- Treat input models as immutable; save to distinct outputs.
- Validate requested parameters and read back applied values before solving.
- Preserve raw results, units, source identity, mesh/study identity, and cleanup
  evidence before interpretation.
- Do not call a run successful solely because the process exited with code zero
  or produced an MPH file.
- Switch modes before a run starts. Do not retrofit checkpoint claims after a
  foreground loop is already consuming the only state.
