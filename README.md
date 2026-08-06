# COMSOL MCP Server for COMSOL 6.4

English | [中文](README_CN.md)

[![CI](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/actions/workflows/ci.yml)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
![Release: 0.6.5](https://img.shields.io/badge/release-0.6.5-blue)
![Status: alpha](https://img.shields.io/badge/status-alpha-red)
[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/stargazers)

> A maintained fork of [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP), accepted for the **`COMSOL 6.4.0.*` release line** and **MPh 1.3.1 standalone/clientapi**. Licensed reference evidence uses COMSOL **6.4.0.293**; a third numeric component change is a separate release family and requires new acceptance.

This server gives AI agents a safer, smaller interface for COMSOL inspection, controlled one-point validation, durable staged sweeps, and offline manual lookup. It is designed for the `model.java` clientapi object returned by `mph.Client()`, whose API differs materially from the direct `com.comsol.model.Model` API targeted by the upstream project.

## Featured capabilities

- **Evidence integrity and anti-hallucination verification (default-on).** Formal
  claims can be checked against exact outcome contracts, raw artifact chains,
  summary citations, and resume producer/driver identity. Users may explicitly
  opt out per check for exploration, but affected results carry an unverified
  warning. Read the independent [evidence-integrity guide](docs/evidence_integrity/README.md).
- **Interactive COMSOL Desktop/Server collaboration (default-off).** A user and
  agent can take explicit turns with one user-owned local Server, one connected
  Desktop, and one exact server-held model. It requires explicit independent
  feature enablement and per-session confirmation. Read the [interactive guide](docs/interactive_shared_session/README.md).
- **Five simulation execution modes.** `interactive`, `inline`, `launcher`,
  `standalone`, and `mphonly` distinguish short feedback, direct scripts,
  durable local campaigns, Python-free Windows handoff, and portable COMSOL
  cluster/cloud delivery. Agents default to the first three and must collect
  the target environment before preparing either cross-device mode. Read the
  independent [execution-modes guide](docs/simulation_execution_modes/README.md).

## Recommended companion skill

For Claude Code, Codex CLI, opencode, Hermes Agent, and other skill-aware agents, use the
[COMSOL 6.4+ metasurface agent skill](https://github.com/garbage-enzyme/COMSOL_6_4_agentskill_for_metasurfaces)
alongside this server. Its short `SKILL.md` entry routes agents to focused
reference modules for clientapi, periodic Wave Optics, materials and boundaries,
durable jobs, physical evidence, resource safety, and troubleshooting without
forcing the full guide into context on every turn. Repository development and
release procedures remain in this repository's development kit.

## Client compatibility and deployment

The installed MCPServer stdio server has been validated with Codex CLI and
opencode. A Windows 11 acceptance run with Claude Code 2.1.220 completed stdio
initialization, discovered the complete tested `wave_optics` tool surface, and
called `capabilities`, `comsol_status`, and `solver_status` successfully. The
server exited cleanly with no solver lease or COMSOL process created. This is
client transport, schema, discovery, and non-starting status compatibility—not
licensed runtime acceptance: Claude did not call `comsol_start`, run a model,
exercise the complete error surface, or prove start/solve/cleanup behavior.
Hermes Agent remains configuration-level guidance without an end-to-end client
test. Use the complete deployment guide for fresh installation, exact
configuration paths, profile selection, restart behavior, and solver-free
verification:

- [Deployment guide](DEPLOYMENT.md)

The essential rules are: perform a non-editable install, configure the absolute
installed `comsol-mcp` executable, use the Settings GUI for ordinary user
configuration, restart the client after changing startup settings or the
package, and keep COMSOL tool calls serialized. Direct JSON editing is the
advanced path for developers, deployment automation, and agents acting on an
explicit user request. Call `capabilities` to verify the deployed profile
without starting COMSOL.

The checked-in client examples follow the official
[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp), Hermes
[MCP documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md),
and Hermes [client source](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py).
An example is configuration guidance until its exact client path is exercised.
A real client acceptance report should include `initialize`, live `list_tools`,
and `capabilities` readback without starting COMSOL, then separately label any
licensed start/solve/cleanup coverage. Treat live discovery, not a count copied
from documentation, as the authority for the installed tool surface.

Release `0.6.5` uses the MCP Python SDK `2.0.x` runtime base conservatively.
Its tools, profiles, schemas, and stdio configuration remain on the accepted
legacy-compatible application contract; the release does not opt clients into
MCP `2026-07-28` features such as multi-round-trip requests, cache hints,
subscriptions, or Tasks extensions.

The initialize response also carries a short safety instruction through the
legacy MCP initialize schema: discover capabilities and preflight first, require
an explicit user request before start/solve/mutation, keep source models
read-only, and separate execution success from evidence integrity and scientific
validation. Clients may use or ignore this hint; the server's enforced ownership
and validation checks remain the security boundary.

## Highlights

- **ClientAPI compatibility.** Geometry, physics, materials, meshes, studies, results, model cloning, and Unicode-safe `.mph` saving have licensed acceptance on COMSOL 6.4.0.293; final build changes within 6.4.0.* inherit the release-line conclusion, while other release families remain unknown.
- **Typed Acoustics and PDE construction.** The `basic_fem` profile includes
  rollback-safe named Box/side selections, Pressure Acoustics, Coefficient,
  General, and Weak Form PDE interfaces, and atomic boundary batches with exact
  type and property allowlists.
- **Safe solver ownership.** An ASCII-path lease, process identity checks, external-client detection, status, and preflight checks prevent accidental competing COMSOL clients.
- **Durable background work.** Staged sweeps and adaptive spectral characterization run in detached workers with immutable specifications, atomic state, fsync'd evidence rows, checkpoints, validated resume, and verified same-host cancellation. A reusable [local launcher](launcher/README.md) provides the same point-boundary operating pattern for project drivers.
- **Python-free standalone execution.** A reviewed native Windows x64 launcher
  keeps the same three layers: an installed and licensed COMSOL 6.4, a
  COMSOL-compiled Java point driver, and the launcher EXE. The target does not
  need Python, Conda, MPh, JPype, or an external Java installation; COMSOL
  itself is still required and is never bundled or replaced.
- **Shared Desktop collaboration (default-off).** The independent `shared_server.enabled` feature can be combined with any tool profile to attach to a manually started local COMSOL Server, adopt exactly one server-held model, enforce non-owning leases and revision locks, run durable attached jobs, and detach without shutting down the user's Server, Desktop, listener, or model.
- **Wave Optics validation.** A focused profile provides read-only model preflight and a one-wavelength evidence audit for periodic metasurfaces.
- **Bounded offline manuals (default-off).** `manuals.root` identifies the original COMSOL PDF tree, while the independent `lexical_docs.enabled` feature adds SQLite FTS5/BM25 search and page retrieval after the user generates a local index. It runs outside the COMSOL control process and returns compact source/page citations.
- **Honest optional semantic retrieval.** The isolated `semantic_docs.enabled` feature is contained and composes with any tool profile, but its baseline model did not meet quality and memory promotion gates. Lexical manual search remains the recommended default.

## Shared project settings

The **Settings GUI is the preferred configuration path for users**. It edits one
shared `settings.json` used by Codex, opencode, Claude Code, and Hermes so that
agents do not silently receive different profiles, paths, Java runtimes, or
evidence rules. The checked-in [`settings.json`](settings.json) is the canonical
template and an advanced interface for developers, deployment automation, and
agents. The complete field meanings, defaults, and accepted values are in the
[settings guide](docs/setting_guide/README.md).
Missing entries use their documented safe defaults. An invalid entry keeps only
that entry at its default and is reported by `capabilities` and
`evidence_integrity_status`; malformed JSON falls back to the complete safe default
document and reports the error. Do not create a second agent-owned settings file.

In a source checkout, the project-root file is writable. In an installed wheel,
the bundled file is a read-only template and persistent settings live at
`%LOCALAPPDATA%/comsol_mcp/settings.json` unless the one absolute locator below
selects another file:

```text
COMSOL_MCP_SETTINGS_PATH=D:\path\to\COMSOL_Multiphysics_MCP\settings.json
```

The old individual `COMSOL_MCP_*`, `COMSOL_SEMANTIC_*`, and Java variables remain
one-release compatibility overrides, but they are intentionally absent from the
checked-in client examples. Change `settings.json`, restart the MCP host for
profile/shared-server/Java changes, then call `capabilities` and inspect the
`project_settings` status.

First-run defaults keep the settings file and Unicode-capable model-read root
under `%LOCALAPPDATA%/comsol_mcp`, while ASCII-only runtime and owned artifacts
live under `%PROGRAMDATA%/comsol_mcp`. Optional semantic assets remain unset.

On first installed use, `capabilities.project_settings` reports
`setup_required: true`, `configuration_source: bundled_template`, setup methods
`settings.start` and `agent_edit`, and that a restart is required. For an
ordinary user, call profile-independent `settings.start` once, tell the user
that startup-setting changes apply after restarting Codex or the owning MCP
client, stop further task output, and wait for the user's next message. Do not
edit JSON while the GUI is open. Use `agent_edit` only when the user explicitly
requests agent-managed or automated configuration; edit only the resolved
writable file, validate it, and request the same restart.
The installed `comsol-mcp-settings` command opens the GUI directly. Repository
and source-distribution users can also run `./Open_Settings_GUI.ps1`. The script
discovers a supported Python 3.14 interpreter, or accepts explicit
`-PythonPath` and `-SettingsPath` values. `-ValidateOnly` checks the manual
launcher without opening the GUI or starting COMSOL. The server can report
`agent_action_required: pause_for_user`; it cannot technically force arbitrary
third-party agents to comply.
The installed entry also accepts `--settings-path` and `--validate-only` and
works with MCP stdio stopped. Its About page can explicitly create or remove the
per-user `COMSOL MCP Settings.lnk`; installation and ordinary GUI actions never
create that shortcut automatically. The shortcut targets the installed
`comsol-mcp-settings-gui` Windows GUI-subsystem entry, so it does not open a
terminal window; the console `comsol-mcp-settings` entry remains available for
validation and scripted actions.

## Profiles

Users select a profile in the Settings GUI before starting the server.
Developers and agents may set the equivalent `profile.name` field in
`settings.json`. A profile is fixed for the lifetime of that server process;
restart after changing it.

| Profile | Intended use |
| --- | --- |
| `core` (default) | Compact, mature control plane: status, ownership, session/model inspection, and one-point solve/evaluation. |
| `basic_fem` | `core` plus typed conventional FEM construction, named selections, Pressure Acoustics and mathematical PDE interfaces, derived-geometry edits, bounded exports, and the Python-free standalone launcher tools. |
| `wave_optics` | Recommended for metasurfaces: `core` plus derived-geometry edits, material preview, locale-safe field discovery and bounded NPZ/manifest extraction, periodic-mesh audit/smoke, visual-review contracts, Wave Optics preflight, and point/reference audits. Durable staged jobs remain under `job_submit`. |
| `experimental` | Explicit opt-in generic creation, async, property escape hatches, and project helpers. |
| `full` | Broad compatibility/discovery surface containing every non-feature-gated tool. |

The `experimental` and `full` profiles expose two solver-free alpha7 research
preparation tools. `research_campaign_compile` turns a complete goal, frozen
design space, and explicit approval into a fingerprinted manifest; it never
starts a solver. `research_robustness_plan` creates a centered one-axis-at-a-time
perturbation matrix and rejects candidates that would require bound clipping.
`research_optimizer_advance` is a stateless checkpoint/feedback step for the
experimental adaptive backend; clients own the returned checkpoint and can
resume exact proposals after a restart.
These tools prepare evidence-safe work; they do not start an autonomous
campaign, change materials, claim success without caller tolerances, or make
RCWA mandatory when an independent adapter is not applicable.

Profiles only control the visibility of COMSOL automation/simulation tools and
future autonomous-exploration tools. Orthogonal functionality uses independent,
default-off Boolean feature gates that compose with every profile and with each
other:

| Feature (advanced JSON setting) | Added surface |
| --- | --- |
| `lexical_docs.enabled=true` | Two isolated SQLite FTS5/BM25 manual-search and exact-page tools. |
| `shared_server.enabled=true` | Protected shared Desktop/attached-Server workflow with explicit confirmation and exact process/listener/model identity. |
| `semantic_docs.enabled=true` | Three isolated experimental vector-assisted manual-retrieval tools. |

Call `capabilities` to discover the active profile, exact registered tools, target versions, disabled groups, and restart requirements without starting COMSOL. Its bounded `deployment_identity` reports source-tree versus installed-package loading plus frozen profile/schema and catalog hashes, so a host restart can detect same-version stale installs or source shadowing without exposing local paths.

Enable shared Desktop/attached-Server work in the Settings GUI Profile tab. It
is a default-off feature independent of the selected profile; the advanced JSON
equivalent is `shared_server.enabled=true`.
The user must start COMSOL Server manually, connect Desktop to it, confirm
the endpoint, and explicitly confirm each attach. The legacy `comsol_connect`
tool remains an experimental compatibility surface and is not a substitute for
the protected shared-session lifecycle.

Control-plane responses from capabilities, solver ownership, durable jobs, and lexical manuals include a compact rolling `control_plane` block. It retains at most 256 samples per operation and reports success/busy/timeout/error counts plus p50/p95/max latency; full logs and unbounded telemetry are never returned inline.

### Standalone Windows launcher

The standalone route targets Windows 10/11 x64 and the COMSOL 6.4 release
line. Licensed acceptance is currently exact for COMSOL 6.4.0.293. It does not
support Windows Server, pre-6.4 COMSOL, Linux, or macOS. The generated EXE takes
one runtime value: `--comsol-path <COMSOL-root>`. It uses that installation's
`comsolcompile.exe`, `comsolbatch.exe`, bundled Java, solver, and license.

Building uses the 64-bit C# compiler from the `.NET Framework 4.x` component
supplied and serviced with supported Windows 10/11 workstations:
`%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe`. It does not require a
separate modern `.NET Runtime`, `.NET Desktop Runtime`, `.NET SDK`, Visual
Studio, package download, or network access. If that inbox compiler is absent,
`standalone_build` fails without installing or downloading a replacement.
Microsoft's [.NET Framework platform table](https://learn.microsoft.com/en-us/dotnet/framework/install/guide-for-developers)
documents the inbox 4.x versions supplied with Windows 10 and Windows 11.

Select the `basic_fem` profile in the normal Python MCP host to expose
`standalone_build`, `standalone_start`, `standalone_status`,
`standalone_pause`, `standalone_resume`, `standalone_tail`, and
`standalone_results`. The build and campaign directories must remain under the
configured owned ASCII artifact root. The MCP host verifies the packaged source
identity, build manifest, and EXE hash before execution. Once generated, the
EXE can also be copied and operated directly without that Python MCP host.
`standalone_start` and `standalone_resume` use an explicit `comsol_root` when
provided; otherwise they use `comsol.installation_root` from shared settings.

## Recommended workflows

### General solver work

1. Call `solver_status`.
2. Call `solver_preflight` before connecting, starting COMSOL, or submitting substantial work.
3. Use the session/model tools or submit a durable staged sweep.

The server fails closed when an external MPh/COMSOL owner or a valid lease is present. `solver_recover_stale_lease` only removes a lease that process identity evidence proves stale; it never terminates an unowned process.

### Parallel-plate capacitor end-to-end example

The source checkout includes a three-dimensional dielectric capacitor recipe.
It identifies the two electrode faces from their measured centers and normals,
applies Ground and Electric Potential conditions, and uses a stationary
Electrostatics study. Build-only is the default; add `--solve` to compare the
energy-derived capacitance with `epsilon_0 * epsilon_r * area / separation`.

```powershell
python -m recipes.parallel_plate_capacitor `
  --output-model D:\comsol_outputs\parallel_plate_capacitor.mph `
  --solve
```

The recipe shares its model and validation functions with the licensed e2e
probe. It performs solver-ownership preflight and publishes the final model only
after the COMSOL client and lease are released. Existing output is preserved
unless `--overwrite-output` is explicit.

### Minimal Pressure Acoustics example

The source checkout includes a physical two-dimensional air-duct recipe. It
uses rigid sidewalls, a one-pascal inlet, and a zero-pressure outlet at a
frequency below the first transverse cutoff. Build-only is the default; add
`--solve` to compare the numerical center pressure with the analytical
one-dimensional Helmholtz solution before the receipt is marked verified.

```powershell
python -m recipes.acoustic_duct_2d `
  --output-model D:\comsol_outputs\minimal_acoustic_duct.mph `
  --solve
```

The recipe performs a fresh solver-ownership preflight, requires four distinct
named side selections, saves to a unique staging model, clears the COMSOL client
and lease, and only then publishes the final `.mph` file. It never overwrites an
existing output unless `--overwrite-output` is explicit.

Durable sweep controls are `job_submit`, `job_status`, `job_tail`, `job_cancel`, and `job_resume`. Each job has its own ASCII-only runtime directory containing its immutable specification, state, CSV journal, checkpoint, and log. Resume accepts only matching, finite, successful rows. Cancellation reaches a terminal cancelled state only after worker/process cleanup and lease release are verified. This coordination is for a shared runtime directory on one host; it is not distributed execution.

The interactive `mesh_convergence_study` helper also writes a manifest beside
its CSV. Any resume or append to an existing journal requires
`source_model_path`; the source hash, study, expressions, parameter setting,
mesh identity, and each level's exact property map must match before a completed
level can be skipped. CSV and checkpoint failures are terminal persistence
outcomes for that level and never trigger another solve.

For an adaptive spectrum, submit `job_type: "spectral_characterization"` with
an explicit source/configuration identity, initial wavelength grid, expansion
and refinement policies, collector configuration, scientific tolerances, and
point/stage/resource caps. The worker solves one wavelength at a time, persists
the complete point audit and its hash-chained row before advancing, and freezes
every requested stage so an exact resume neither regenerates the plan nor
duplicates completed wavelengths. Resubmission observes an existing job only
when its normalized specification, collector, source, and exact worker-driver
identity match.

Execution status and scientific interpretation are separate. A job can finish
with `status: "completed"` while its `scientific_disposition` is `residual` or
`unresolved_at_declared_cap`; boundary peaks, missing brackets, fit sensitivity,
and exhausted expansion budgets are scientific non-acceptance outcomes, not
worker failures. `accepted` requires complete hash-resolvable raw evidence.
Partial interrupted or cancelled rows remain diagnostic, and cancellation is
terminal only after worker, descendants, port, lease, and cleanup evidence pass.
Spectral summaries retain raw R/T/A, closure, wavelength synchronization, mesh
counts, own-peak, FWHM, Q, stage hashes, and exact artifact references when the
declared collector and evidence support those quantities.

The solver-free `spectral_model_compare` tool compares two or three declared
scalar line-shape models on exactly the same spectral rows, support, baseline,
response, polarity, coordinate domain, and quality policy. It supports local
polynomial, Lorentzian, and Fano fits in wavelength, frequency, angular-frequency,
or energy coordinates, then maps peak and half-prominence crossings back to the
original wavelength evidence. It reports fit diagnostics, support sensitivity,
AIC, AICc when defined, BIC, deltas, and descriptive Akaike weights. These rank
model support only; they do not prove Lorentzian physics, Fano interference,
mode identity, or scientific acceptance.

Three solver-free preview tools make configuration review explicit before any
COMSOL admission or mutation. `simulation_configuration_validate` accepts a
closed typed contract for source/producer identity, geometry semantics, ordered
layers, material state and loss sign, incidence and polarization, mesh keys,
model-tree identities, solver termination, units, and artifact chains. It
normalizes supported units and returns a content-bound fingerprint.
`simulation_configuration_diff` classifies leaves as exact, within tolerance,
semantic, label-only, or unavailable; labels never become physical identity.
`job_spec_preview` reuses the exact discriminated input validator used by
`job_submit` and reports its bounded point/stage inventory, path/resource
checks, requirements, and declared submit-time side effects without admission,
ownership acquisition, filesystem writes, process creation, or solver startup.

The solver-free `thermal_kirchhoff_assess` tool permits directional
absorptivity-to-emissivity substitution only when the exact channel has verified
linearity, time invariance, reciprocity, local equilibrium, direction, frequency,
and polarization evidence. Unknown facts remain conditional or unavailable, and
nonreciprocal channels are not applicable. `thermal_radiation_evaluate` applies
the SI Planck law with explicit wavelength/frequency/wavenumber Jacobians,
projected-solid-angle integration, scalar, incoherent TE/TM, or Stokes/Mueller
polarization, and bounded gas/aperture/optics/analyzer/detector kernels. Its
hash-bound evidence records coverage, integration policy, extrapolation state,
uncertainty, source artifacts, and detector/reference/background signals. It
does not replace COMSOL's Surface-to-Surface Radiation solver.

The solver-free `thermal_material_validate` and `thermal_material_evaluate`
tools use a versioned ledger instead of embedding a material database. Each
state binds material/sample identity, phase and fabrication state, source,
spectral/temperature validity, uncertainty, measurement conditions, and whether
the data are measured, fitted, or assumed. Typed entries cover n/k and complex
permittivity tables plus Drude, Lorentz, TOLO, and thermo-optic models. State IDs
remain distinct across carrier density, mobility, effective mass, and phase
fraction; declared phase/discontinuity boundaries are never smoothed through.
The internal convention is `exp(-i*omega*t)` with passive `Im(epsilon)>=0`.
COMSOL output is a mutation-free `exp(+i*omega*t)` conversion preview containing
exact target property/function tags, units, interpolation/extrapolation policy,
table hashes, readback expectations, and rollback requirements.

A licensed durable thermal-to-optical replay uses
`job_type: "thermo_optomechanical_replay"` plus an ASCII JSON
`specification_path` and exact `specification_sha256`. The bounded manifest is
validated against the complete closed contract before submission; this keeps
core MCP discovery bounded without accepting free-form configuration. It binds an
immutable source, one validated thermal material state, exact Heat Transfer,
Solid Mechanics, Moving Mesh, Wave Optics, study, selection, parameter, mesh,
and expression tags, and a caller-supplied resource and acceptance policy. Five
hash-chained stages persist preflight, thermal/structural solve, state evidence,
spatial-frame deformation transfer, and exact optical replay. Resume verifies
orphaned evidence before appending its missing row and never repeats a completed
stage. Acceptance keeps raw R/T/A, temperature, stress, displacement, energy,
mesh, wavelength, zero-CTE, zero-temperature-rise, rollback, source-integrity,
and cleanup evidence separate from execution completion. The minimal supported
route forbids topology change and requires a licensed COMSOL 6.4 synthetic
fixture gate before use with research models.

A durable convergence campaign uses `job_type: "convergence_campaign"` and an
immutable ordered ladder of two to eight exact source or prebuilt derived model
identities. Every level runs the accepted adaptive spectral job, persists its
complete hash-bound artifacts, and enters the offline convergence evaluator only
at its own bracketed peak. The caller supplies metrics, units, tolerances,
governing-pair and declared-cap rules, total point/wall-time caps, and any early-
acceptance permission. One solver owner and client serve the whole campaign;
the worker never invents an extra level and resumes only verified complete level
rows. This release does not apply arbitrary parameter setters inside a campaign;
prepare and verify derived model levels before submission.

A durable branch-continuation campaign uses
`job_type: "branch_continuation_campaign"` and an immutable sequence of two to
sixteen exact source or prebuilt derived model states. Each state binds one
coordinate value, polarization and material identities, the exact source and
configuration hashes, and measured incidence readback from the periodic parent
and both ports. Every coordinate runs its own adaptive spectrum and persists a
hash-chained state row before the offline continuation planner can use it.
Caller policy bounds the guard window, absolute wavelength domain, expansion
count, total window, request grid, point count, wall time, and whether to stop at
the first unresolved transition. Boundary-high and competing-candidate results
remain residual or `unresolved_at_declared_cap`; the campaign never reports
physical branch disappearance or starts an undeclared coordinate. One solver
owner and client serve the campaign, and resume reuses only complete verified
state spectra. This release supports exact/prebuilt models only and does not
apply arbitrary incidence or geometry setters inside the campaign.

### Wave Optics metasurfaces

Use the `wave_optics` profile and follow this bounded sequence:

```text
solver_status -> wave_optics_preflight -> wave_optics_reference_audit (optional) -> wave_optics_point_audit
```

`wave_optics_preflight` is read-only and solver-free. It reports source provenance, topology, periodic/Floquet selections, ports, wavelength linkage, mesh/study metadata, and explicit unknowns.

`wave_optics_point_audit` solves exactly one declared wavelength after ownership and source-hash checks. It writes a running manifest, one fsync'd CSV row, and a final manifest. Raw evidence can include requested/evaluated wavelength, frequency linkage, caller-provenanced R/T/A and flux direction, closure, loss expressions, bounded top-air field statistics, mesh state, and source/config/policy hashes.

`wave_optics_reference_audit` is an experimental reference-power tool. It creates a fresh provenance-tracked clone, requires exact caller material/domain declarations, replaces clone component materials with lossless air, samples a bounded homogeneous region, and removes the clone before method evidence can pass. It never mutates the source model; licensed acceptance is version/model-specific.

Without a caller-supplied versioned validation policy, an audit is evidence-only: it does not declare a model pass/fail or recommend a long sweep. S/P labels and structure total fields stay explicitly qualified until an incident-reference artifact supports a stronger claim.

## Manual retrieval

`manual_search` and `manual_read_pages` are the production documentation path. They use an offline SQLite FTS5/BM25 index, bounded worker processes, and compact source/page citations. The MCP control process does not import Torch or SentenceTransformer for this path. They are independently default-off because a COMSOL installation may omit local manuals.

The Settings GUI Docs tab separates manual keyword search from semantic search. Choose a raw PDF root and an ASCII-only SQLite destination, then use **Generate Index**. The background dialog reports stage, current PDF, page counts, and percentage. It builds and validates a sibling temporary database, atomically publishes only a valid result, and preserves the previous index on cancellation or failure. Enable manual search only after a valid index exists.

`semantic_docs.enabled` is opt-in and isolated from COMSOL control. Its isolated-worker vector retrieval is an English diagnostic baseline, not a multilingual or production-quality claim: the frozen benchmark improved exact-match recall but regressed paraphrase/multi-concept recall, returned no direct-Chinese matches, failed negative-query abstention, and grew substantially in memory during soak testing. The baseline model and its index assets have been removed; a replacement model would require a full benchmark gate before re-deployment. Keep lexical manual search for normal work.

## ClientAPI compatibility notes

The fork repairs the clientapi paths exercised by its test and real-COMSOL checks, including:

- `tags()` iteration and `feature().size()` instead of direct-model indexing and `len()`.
- `physics().create(tag, type, sdim_string)` for interfaces, while child features take an integer entity dimension.
- ClientAPI mesh inspection through `getNumElem()` and `getNumVertex()`, and explicit mesh-sequence creation.
- Full study-type names and `model.java.study('std1').run()`.
- JSON-safe normalization of Java strings, localized labels, real/NumPy/complex values, and model metadata.
- `model.java.save(full_path)` for Unicode-safe `.mph` saves and clientapi-safe clone cleanup.
- Correct component-owned material and multiphysics construction.

The electrostatics helper can create `ChargeConservation` and a material node because the default `fsp1` FreeSpace domain feature in COMSOL 6.3+/6.4 otherwise uses vacuum permittivity rather than a material's relative permittivity.

## Verification

The source dependency/process-only gate, optimized-Python production guard,
compileall, hash-locked isolated non-editable wheel/install gate, licensed
attached sweep/cancellation/recovery, PID-reuse rejection, and detach-preservation
receipt are maintained as release checks. Unit tests are side-effect-free:
collection does not start COMSOL, and integration probes run only when explicitly
requested in fresh, sequential subprocesses with exact process-tree cleanup.
Repository-only tests, release fixtures, gates, and provenance are documented in
`development_kit/README.md`; ordinary wheel/sdist artifacts exclude that directory.

```bash
python -m pytest -q
python -m pytest -q -m integration development_kit/tests/integration
```

Real COMSOL checks include localized JSON transport; circle/union geometry; DXF import; parametric sweep properties; multiphysics coupling; clone cleanup; Unicode-path saving; solver ownership; durable interruption/restart/resume/cancellation; profile discovery; Wave Optics preflight and one-point audit; and bounded manual retrieval.

The Python 3.14 licensed parallel-plate regression returns **1.8593794419540677 pF**, versus the theoretical **1.8593794406880002 pF**, on COMSOL **6.4.0.293**.

Licensed adaptive spectral acceptance on the same COMSOL build used a neutral
air-dielectric-air periodic-port slab with 4,798 elements and 1,039 vertices.
The accepted 10-row spectrum found its interpolated own peak at
**5.200823291715346 um**, with **T = 0.9999455828498357**,
**FWHM = 0.4807802607560452 um**, and **Q = 10.817464268472365**.
Its raw rows span **R = 0.000428181826928114 to 0.506857218704363**,
**T = 0.493142781295616 to 0.999571818173077**, and
**max |A| = 2.985136902408465e-17**; maximum power-closure error is
**2.103241887902518e-14** and maximum wavelength-sync error is zero. A separate
9-row boundary control expanded its declared window and completed normally as
`unresolved_at_declared_cap`; its raw ranges are
**R = 0.113752050554409 to 0.697262752330585**,
**T = 0.302737247669409 to 0.886247949445593**, and
**max |A| = 1.695203805977834e-17**, with maximum closure error
**2.903982508976606e-14** and zero wavelength-sync error. Both runs preserved
the source SHA-256 and released the solver lease and client.

Licensed convergence acceptance used three neutral periodic-port slab meshes:
**2,386/560**, **4,798/1,039**, and **13,904/2,752** elements/vertices. Their
own peaks were **5.200438265718366**, **5.200823291715278**, and
**5.200959692754783 um**; fitted peak T values were
**0.9999455861474655**, **0.9999455828498416**, and
**0.9999455989864663**. The governing medium-to-fine peak shift was
**0.1364010395043668 nm**. Across 30 raw rows,
**R = 0.000426677111557779 to 0.506857218704365**,
**T = 0.493142781295614 to 0.999573322888467**, and
**max |A| = 4.526776969362989e-17**; maximum closure error was
**2.48772546066357e-14** and wavelength-sync error was zero. A separate campaign
with a declared **0.001 nm** peak-shift tolerance completed all three levels as
`residual` while its amplitude check passed. Both campaigns preserved all source
hashes and left no client, process, or lease residue.

Licensed branch-continuation acceptance used two immutable 4,798-element,
1,039-vertex neutral slab models with exact periodic-parent and two-port
incidence readback at **0 and 10 degrees**. Their own peaks were
**5.200823291715293** and **5.195931563688228 um**, a measured shift of
**4.891728027065 nm** within the caller's guard window. Peak T values were
**0.9999455828498354** and **0.9999448178081717**; FWHM values were
**0.4807802607560502** and **0.4836621446746728 um**; Q values were
**10.817464268472143** and **10.742894851080779**. Across 20 raw rows,
**R = 0.000422180032088570 to 0.512242443246136**,
**T = 0.487757556753878 to 0.999577819967919**, and
**max |A| = 3.015050383793419e-17**; maximum closure error was
**1.33935578502046e-14** and wavelength-sync error was zero. A separate 18-row
boundary control consumed its one declared expansion and completed as
`unresolved_at_declared_cap` with no additional request and no branch-
disappearance claim. Both campaigns preserved the source hashes and left no
client, process, or lease residue.

## Requirements and installation

- COMSOL Multiphysics 6.4; licensed acceptance is pinned to build 6.4.0.293
- Python 3.14 (standard GIL-enabled build, not the Windows Store build)
- MPh 1.3.1, `mcp`, `pydantic`, and `psutil>=5.9.0`
- COMSOL's Java 21 runtime in the verified configuration

This project is under active development, so dependency ranges and locked
versions may change without a long deprecation window. Before updating an
existing deployment, validate Python, COMSOL/MPh/JPype, and any optional extras
in an isolated environment, then replace the active installation and restart
the MCP host only after those checks pass.

```bash
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
cd COMSOL_Multiphysics_MCP_6_4_Calibrated
python -m pip install .

# Preferred user configuration after installation:
comsol-mcp-settings

# Recommended offline manual index; use an ASCII-only output path.
python -m pip install ".[manuals]"
python -m comsol_mcp.knowledge.lexical_manual build --index D:\comsol_docs_fts\manuals.sqlite3
```

For optional isolated semantic retrieval (sentence-transformers, not ChromaDB):

```powershell
python -m pip install ".[semantic-docs]"
# Prefer the Settings GUI Docs tab. For developer/agent JSON automation:
#   manuals.root = "D:/COMSOL64/Multiphysics/doc"
#   lexical_docs.enabled = true
#   lexical_docs.index_path = "D:/comsol_docs_fts/manuals.sqlite3"
#   semantic_docs.enabled = true
#   semantic_docs.root = "D:/comsol_semantic"
#   semantic_docs.model_path = "D:/comsol_semantic/models/<model>/<revision>"
```

On Windows accounts whose user path contains non-ASCII characters, avoid editable installs. Run `python -m pip install . --no-deps` after source changes, then restart the MCP host; the server does not hot-reload `comsol_mcp/tools/`.

Configure an MCP client, for example:

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

Select `core` in the Settings GUI for the compact default. Developers and
agents can set the equivalent `profile.name` value in JSON. Client examples are available at
`config/claude-code-mcp.example.json`, `config/codex-mcp.example.toml`,
`config/hermes-mcp.example.yaml`, and `config/opencode-mcp.example.json`.

## How this fork differs from upstream

This is a COMSOL 6.4.0.293 standalone/clientapi compatibility and reliability fork, not a general replacement for upstream. Other COMSOL builds remain unknown until independently accepted. The fork keeps the upstream project's foundation while making a deliberately narrower, safer execution surface for agent-driven COMSOL work.

| Area | Upstream orientation | This fork |
| --- | --- | --- |
| COMSOL API target | Direct `com.comsol.model.Model` API assumptions. | MPh 1.3.1 standalone `model.java` clientapi wrappers, including their different overloads, tags, lists, and Java-string transport. |
| Tool surface | Broad feature discovery by default. | Compact `core` default; larger construction and compatibility surfaces require an explicit profile. |
| Solver concurrency | No same-host ownership protocol. | Process-aware lease, external-client detection, status, preflight, and stale-lease recovery that never kills an unowned process. |
| Long runs | Interactive/current-process workflows. | Detached durable sweeps and adaptive spectra with immutable specs, fsync'd evidence rows, frozen stages, validated resume, and verified cancellation cleanup. |
| Wave Optics | General tools only. | A dedicated preflight plus one-point evidence audit for periodic metasurfaces, with raw evidence separated from caller policy. |
| Manual search | No bounded manual retrieval. | Bounded isolated lexical manual retrieval is the production default; experimental semantic retrieval is isolated and explicitly not promoted. The legacy in-process ChromaDB path has been removed. |
| Windows paths | No special guarantee for Unicode save paths. | Clientapi Java save path for Unicode `.mph` saves; ASCII-only runtime/index roots for native and durable artifacts. |

Use this fork when the upstream server fails under MPh standalone with errors such as `No matching overloads`, `Operation_cannot_be_created_in_this_context`, or client-list indexing errors. Use `full` only when compatibility with a broad legacy surface is genuinely required.

## License

This repository is distributed under the [MIT License](LICENSE). COMSOL,
licensed manuals, third-party models, papers, and datasets are not relicensed by
this repository.
