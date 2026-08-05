# AGENTS.md - COMSOL MCP contributor guide

## Project

`comsol-mcp` is a safety-focused MCP stdio server for reproducible COMSOL
Multiphysics 6.4 automation. It targets MPh 1.3.1 and the `model.java`
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
  release documentation; it must not enter a wheel or sdist.
- `config/` contains MCP client configuration examples.
- `constraints/` defines reviewed dependency lanes.
- `recipes/` contains standalone examples and is not imported by runtime code.
- `settings.json` is the shared startup-settings contract.

Read `development_kit/docs/layout.md` before broad exploration. Update that
inventory in the same change whenever a tracked file is added, renamed, or
removed.

## Engineering rules

1. Support only the Python and dependency ranges declared in `pyproject.toml`.
   Do not claim a new COMSOL or MPh compatibility range without an acceptance
   gate and corresponding release evidence.
2. Keep one COMSOL solver owner. Check ownership and preflight before creating
   a client; never compete with an existing lease or external owner.
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
   Changing a profile or feature gate requires an MCP host restart; GUI language
   and scale are presentation-only exceptions that apply immediately.
7. Do not start COMSOL for unit, schema, packaging, documentation, lint, or
   process-only work. Licensed COMSOL checks are explicit and serial.
8. Bound inputs, responses, retries, workers, artifact counts, and file sizes.
   Durable resume requires exact source, configuration, and driver identities.
9. Keep evidence state separate from execution state and scientific disposition.
   A successful native call, fixed-wavelength match, or S/P label alone is not
   physical validation.
10. Keep evidence-integrity checks enabled unless an explicit exploration opt-out
   is requested; preserve the resulting unverified state in the outcome.
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
3. Keep runtime code under `comsol_mcp/`; do not import `development_kit/` or
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
  before replacing an existing output; do not represent the upstream model as
  original work by this repository. Treat it only as an API-compatible baseline:
  the recipe uses linear air (`mu_r=1`) and does not trust upstream nonlinear
  material laws or numerical results as physical validation.
- That recipe builds and saves by default. A real 1 kHz solve requires the
  explicit `--solve` flag, a free licensed host, and a separate acceptance run;
  no result is validated until that run supplies its evidence.

## Testing and release checks

Run commands from the repository root in the declared development environment:

```powershell
python -m pytest -q development_kit/tests/test_<area>.py
python -m pytest -q -n 4 --dist loadscope `
  --basetemp D:\comsol_pytest\local-main `
  --ignore development_kit/tests/test_control_plane_startup.py
python -m pytest -q development_kit/tests/test_control_plane_startup.py `
  --basetemp D:\comsol_pytest\local-serial
python -m compileall -q comsol_mcp src development_kit
python development_kit/scripts/quality_gate.py --artifact-root <artifact-root>
python development_kit/scripts/release_gate.py
```

Use a provisional 10-minute timeout for complete solver-free test, coverage,
quality, and release-gate runs. The 2,000-test reassessment retained that
timeout and the local four-worker split: seven consecutive main-suite runs from
1,965 through 2,000 tests completed without a stall in 125.00-131.94 seconds
(median 128.86 seconds). Focused and broader area suites use ordinary serial
pytest unless a measured run justifies parallel execution. A local complete
suite must use the explicit four-worker main command above, followed by the
startup/process-inventory file as a serial tail; do not use bare
`python -m pytest -q` as the local complete-suite command. Reassess local xdist,
hosted CI execution, and these timeouts together when collected tests reach
2,750, or after another execution stall occurs and that stall's cause has been
repaired, whichever happens first.

On Windows, complete quality/release gates and their pytest basetemps must use
a direct short child of `D:\mcp_tests` whose leaf is at most 12 characters, for
example `D:\mcp_tests\a65b12q`. The gate adds deep run, xdist, test-name, job,
and artifact components; descriptive nested roots can exceed Win32 path limits
and cause misleading temporary-file `FileNotFoundError` failures. Treat a long
artifact root as an invalid gate invocation, not as a test failure.

For regression tests, gates, CI, builds, or any other wait with a usable ETA,
wait once for the current ETA plus one minute. Do not poll early or send
intermediate progress updates during that wait, inspect status/logs, or spend
tokens on progress checks. If the command is still active afterward, derive a
new ETA from observed progress and again wait for that ETA plus one minute.

The quality gate applies the same local split while collecting coverage: four
workers for the isolated main suite and a serial startup/process-inventory
tail. The release gate currently runs its embedded complete pytest stage
serially. GitHub-hosted Python 3.14 also uses serial pytest because two observed
Windows Actions runs stalled without progress under xdist in different jobs,
consistent with upstream pytest-xdist issue #1313 around worker shutdown or
`loadscope` dispatch. Hosted serial execution trades speed for deterministic
termination; do not restore hosted xdist without an upstream fix and a new
stability benchmark. Do not replace the local bounded worker count with
`-n auto` without a new timing and isolation benchmark. At the 2,000-test
reassessment, the latest ten hosted serial runs had no execution stall; one
failed fast for a deterministic standalone-script import regression and passed
after correction. A 17-minute workflow elapsed time in another run was runner
queue delay before the unit job, whose actual execution remained about seven
minutes. Keep hosted pytest serial and the 15-minute per-job workflow timeout.

For a release candidate, use the locked dependency lane from a clean tree:

```powershell
python development_kit/scripts/release_gate.py `
  --dependency-lock constraints/release_locked_py314.txt
```

Real COMSOL gates are opt-in, licensed, and serial. Follow
`development_kit/docs/release_checklist.md`; hosted CI intentionally does not
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
skips the process tests; it does not emulate Windows behavior.

## MCP and evidence contracts

- Use `capabilities` to discover the installed profile and tool surface without
  starting COMSOL. Restart the MCP host after profile, package, or settings
  changes; live discovery is authoritative after restart.
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
