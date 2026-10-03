# AGENTS.md - COMSOL MCP contributor guide

## Project

`comsol-mcp` is a safety-focused MCP stdio server for reproducible COMSOL
Multiphysics 6.4 automation. It supports MPh 1.3.1 and 1.4.x through the `model.java`
ClientAPI surface. The server provides profile-scoped tools for solver
ownership, model inspection and derived edits, bounded one-point audits,
durable jobs, evidence integrity, and offline manual lookup.

The project is not a general autonomous simulation runner. It preserves
user-owned solver state and keeps execution, evidence, and scientific
interpretation as separate outcomes.

## Repository structure

- `comsol_mcp/` contains the package entry point and packaged settings resource.
- `comsol_mcp/` contains the canonical runtime implementation.
- `src/` is a repository-only legacy import compatibility layer and is not
  distributed in wheels.
- `development_kit/` contains repository-only tests, fixtures, scripts, and
  release documentation. It must not enter a wheel or sdist.
- `config/` contains MCP client configuration examples.
- `constraints/` defines reviewed dependency lanes.
- `recipes/` contains standalone examples and is not imported by runtime code.
- `settings.json` is the shared startup-settings contract.

Read `development_kit/docs/layout.md` before broad exploration. Update that
inventory in the same change whenever a tracked file is added, renamed, or
removed.

## Documentation language

Use [asd-ste100](https://github.com/danyuchn/asd-ste100-skill) for documentation changes, including every file description in `development_kit/docs/layout.md`.
Use strict writing for procedures and safety instructions. Use STE-flavored writing for explanations.
Write one instruction per sentence. Name the actor. Use consistent technical terms.
Aim for at most 20 English words per instruction and 25 per descriptive sentence.
Apply the same clarity rules to Chinese. English dictionary rules do not certify Chinese text.
Preserve requirement strength, uncertainty, conditions, exceptions, numbers, units, defaults, API names, paths, and code examples.
Keep necessary precision when a shorter sentence would change the meaning. Record that exception during review.
Check behavior against code. Distinguish implementation, tests, installed packages, native clients, and scientific acceptance.
Keep English and Chinese facts consistent. Do not change historical receipts or quoted diagnostic strings for style.
Use the skill linter as a structural check, not proof of preserved meaning or certified ASD-STE100 compliance.

## Engineering rules

All agent terminal commands use PowerShell 7 (`pwsh`), not Windows PowerShell
5.1. Legacy-shell subprocesses inside existing cross-shell acceptance tests
are test scenarios. Do not change the agent's shell or silently remove those
tests. Use current repository scripts and preserve their bounded execution.

### Dependency lane policy

The repository keeps dependency lanes deliberately small, and each lane has one
declared reason. Do not widen a lane to absorb a drift report.

- **MCP protocol:** target only the newest stable SDK major/minor (`mcp` 2.2.x)
  as the server lane, while remaining wire-compatible with MCP 1.x clients.
  MCP 1.x compatibility is a client-compatibility requirement, never a second
  server dependency lane. SDK version, date-based protocol revision, and
  extension wire generation stay separate identities: bumping the SDK is not
  implementing an extension.
- **Other runtime dependencies:** keep exactly two lanes where the library
  changes behavior across them — the newest allowed minor line and the oldest
  still-supported line under review. For MPh that is the newest `1.4.x` plus
  the retained `1.3.1` reference lane. Add a third lane only with an explicit,
  recorded reason.
- **Development and lint tooling:** use the newest available release, because
  newer tooling encodes newer and stricter rules. Adopt a new diagnostic as a
  reviewed code fix or a narrow documented exception. Never loosen a rule,
  widen an exclusion, or lower a threshold to make a tool upgrade pass.
- Raising a declared minimum, dropping a supported lane, or excluding a major
  requires an explicit recorded decision. It is never a side effect of a drift
  review.

1. Support only the Python and dependency ranges declared in `pyproject.toml`.
   Do not claim a new COMSOL or MPh compatibility range without an acceptance
   gate and corresponding release evidence.
2. Keep one COMSOL solver owner. Check ownership and preflight before creating
   a client. Never compete with an existing lease or external owner.
3. Treat source models as immutable. Mutate only provenance-tracked derived
   copies, retain source identity, and prove cleanup of owned clones.
4. Serialize every call to one MCP stdio server, including capabilities and
   status. Do not batch or retry while an earlier call might still be running.
5. Keep `settings.json` as the shared settings source. Do not add
   agent-specific settings files or split configuration by client.
6. Use profiles only for startup-time visibility of COMSOL automation,
   simulation, and future autonomous-exploration experiment tools. Model
   orthogonal optional functionality as independent, explicit, default-off
   boolean feature gates that compose with every profile and with one another.
   Changing a profile or feature gate requires an MCP host restart. GUI language
   and scale are presentation-only exceptions that apply immediately.
7. Do not start COMSOL for unit, schema, packaging, documentation, lint, or
   process-only work. Licensed COMSOL checks are explicit and serial.
8. Bound inputs, responses, retries, workers, artifact counts, and file sizes.
   Durable resume requires exact source, configuration, and driver identities.
   For subprocess JSON-lines or similar stdout protocols, keep stdout protocol-
   only, route diagnostics to stderr, and test optional native dependencies in
   a fresh environment because deprecated compatibility imports may emit text.
9. Keep evidence state separate from execution state and scientific disposition.
   A successful native call, fixed-wavelength match, or S/P label alone is not
   physical validation.
10. Keep evidence-integrity checks enabled unless an explicit exploration opt-out
   is requested. Preserve the resulting unverified state in the outcome.
11. Do not commit credentials, private assets, licensed manuals, `.mph` models,
    or unreviewed third-party data.
12. Update public tool schemas, profile snapshots, documentation, and release
    facts when a public tool, profile, or schema contract changes.

## Implementation workflow

1. Read the closest implementation, focused tests, and contract before editing.
   For tool registration, start with `comsol_mcp/tools/catalog.py` and
   `comsol_mcp/tools/profiles.py`.
2. Prefer narrow, typed, profile-compatible interfaces over generic property
   escape hatches. Preserve stable JSON schemas and bounded response contracts.
3. Keep runtime code under `comsol_mcp/`. Do not import `development_kit/` or
   recipes.
4. Add deterministic tests for observable behavior, safety invariants,
   resume/cleanup/provenance regressions, and schema changes.
5. Update user and developer documentation together with behavior or public
   configuration changes. Do not describe an untested client path as validated.
6. Before committing, inspect the staged diff and leave unrelated changes alone.

## Standalone recipes

- Recipes are examples, not MCP runtime dependencies. Keep them self-contained,
  parameterized, and free of hard-coded user paths or committed model binaries.
- `recipes/parallel_plate_capacitor.py` is the canonical source for the
  capacitor e2e model and analytical validation. It identifies electrode faces
  from probed coordinates and normals, builds and saves by default, and solves
  only with explicit `--solve` on an admitted licensed host.
- `recipes/acdc_2d_differential_coils.py` derives a two-coil AC/DC magnetic
  model from an upstream example baseline containing `comp1`, `geom1`, and the
  `mf` interface with its required default features. It verifies the baseline
  hash, saves only to a distinct output model, and requires `--overwrite-output`
  before replacing an existing output. Do not represent the upstream model as
  original work by this repository. Treat it only as an API-compatible baseline:
  the recipe uses linear air (`mu_r=1`) and does not trust upstream nonlinear
  material laws or numerical results as physical validation.
- That recipe builds and saves by default. A real 1 kHz solve requires the
  explicit `--solve` flag, a free licensed host, and a separate acceptance run.
  No result is validated until that run supplies its evidence.

## Testing and release checks

Run commands from the repository root in the declared development environment:

```powershell
python -m pytest -q development_kit/tests/test_<area>.py
python -m pytest -q -n 4 --dist loadscope `
  --basetemp D:\mcp_tests\main `
  --ignore development_kit/tests/test_control_plane_startup.py
python -m pytest -q development_kit/tests/test_control_plane_startup.py `
  --basetemp D:\mcp_tests\serial
python -m compileall -q comsol_mcp src development_kit
python development_kit/scripts/quality_gate.py --artifact-root <artifact-root>
python development_kit/scripts/release_gate.py
```

Use a provisional 10-minute timeout for complete solver-free test, coverage,
quality, and release-gate runs. Focused area suites use serial pytest unless a
measured run justifies parallel execution. The local complete suite uses the
explicit four-worker main command above plus the startup/process-inventory
serial tail. Do not use bare serial pytest or `-n auto` for the full local
suite. Record current counts/durations from fresh receipts. Old 2,000-test
benchmarks and 2,750-test reassessment triggers are no longer current routing.
Reassess parallelism only after identifying a real stall or isolation failure.

On Windows, complete quality/release gates and their pytest basetemps must use
a direct short child of `D:\mcp_tests` whose leaf is at most 12 characters, for
example `D:\mcp_tests\a65b12q`. The gate adds deep run, xdist, test-name, job,
and artifact components. Descriptive nested roots can exceed Win32 path limits
and cause misleading temporary-file `FileNotFoundError` failures. Treat a long
artifact root as an invalid gate invocation, not as a test failure.

For long gates use a measured ETA and avoid frequent polling. Keep individual
blocking tool waits bounded so progress can be communicated. If the ETA is
exceeded, inspect once, classify progress/failure, and derive a new estimate.

The quality gate applies the local split while collecting coverage. Hosted
Python 3.14 uses the checked-in serial shard helpers instead of xdist. Use
the actual `.github/workflows/ci.yml` and gate scripts as authority for job
timeouts and shard counts. Do not restore hosted xdist or edit timeouts merely
to hide a failure. The release gate's own test mode is defined by its script.
Keep clean install and installed stdio verification distinct from source tests.

For a release candidate, use the locked dependency lane from a clean tree:

```powershell
python development_kit/scripts/release_gate.py `
  --dependency-lock constraints/release_locked_py314.txt
```

Real COMSOL gates are opt-in, licensed, and serial. Follow
`development_kit/docs/release_checklist.md`. Hosted CI intentionally does not
run them.

A version-only release identity update does not require a new Settings GUI
screenshot matrix or rendered-interface acceptance when the only visible change
is the same-format `0.a.b` / `alphaa.b` version text and no layout, translated
message, icon, style, widget, state, or capture contract changes. Still require
deterministic locale regeneration/checks, focused GUI and package tests, release
facts, artifact membership checks, and exact-SHA CI. Any other visible GUI
change retains the full screenshot and rendered-interface acceptance gate.

The repository launcher keeps its PowerShell scripts directly runnable for
focused diagnosis. `development_kit/tests/test_launcher_distribution.py` is
the canonical pytest wrapper: on Windows it runs the accepted suite serially
under both Windows PowerShell 5.1 and `pwsh`, so complete local pytest and the
hosted Windows CI jobs exercise the same contract. Non-Windows collection
skips the process tests. It does not emulate Windows behavior.

## MCP and evidence contracts

- Use `capabilities` to discover the installed profile and tool surface without
  starting COMSOL. Restart the MCP host after profile, package, or settings
  changes. Live discovery is authoritative after restart.
- For Wave Optics, preflight before a point audit. Require caller-declared
  scientific policy for pass/fail classification and preserve raw R/T/A,
  closure, wavelength synchronization, mesh state, and artifact identities.
- Durable jobs persist hash-bound specifications, fsync'd rows, checkpoints, and
  cleanup evidence. Resume only complete rows with exact matching identities.
- Shared Desktop/Server mode is default-off. It requires explicit opt-in and
  must not start or terminate the external Server.
- Use outcome language precisely: `verified`, `measured`,
  `derived_from_declared_convention`, `label_only`, `unknown`,
  `not_requested`, and `not_applicable`.

## Current roadmap pointer (2026-09-29)

The caller reports 0.7.5 published after independent acceptance at
`a9e6c3a48d85356f473b712ce200e617bc1b49a2` and hosted CI run
`36340627633` (seven jobs passed). 0.7.6 is the active planning track: full
dependency drift including lint/dev tools and MCP SDK, formal MPh 1.4 support,
native nonblocking Tasks with ordinary-job fallback, live `dbmodel://` Model
Manager, and guarded Desktop/remote sessions. Python 3.14 remains the release
lane. Existing 3.15 preview CI is informational, not a migration target before
final release. Read `../Desktop/plans/comsol_mcp/ROADMAP.md`, the canonical
0.7.6 plan, and its executor handoff. The details and decisions live there,
not in this contributor guide. Publication does not imply deployment.

For file discovery, use the Everything HTTP service at `127.0.0.1:1145`
before recursive PowerShell enumeration. Use PowerShell 7 for targeted reads
and commands. Do not put development plans in AGENTS.md. For any authorized
commit use the caller's configured `garbage-enzyme` identity. Never substitute
an agent identity. Do not commit or push without task authorization.
