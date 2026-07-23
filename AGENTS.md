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
- `src/` contains runtime implementation imported by the server.
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
6. Do not start COMSOL for unit, schema, packaging, documentation, lint, or
   process-only work. Licensed COMSOL checks are explicit and serial.
7. Bound inputs, responses, retries, workers, artifact counts, and file sizes.
   Durable resume requires exact source, configuration, and driver identities.
8. Keep evidence state separate from execution state and scientific disposition.
   A successful native call, fixed-wavelength match, or S/P label alone is not
   physical validation.
9. Keep evidence-integrity checks enabled unless an explicit exploration opt-out
   is requested; preserve the resulting unverified state in the outcome.
10. Do not commit credentials, private assets, licensed manuals, `.mph` models,
    or unreviewed third-party data.
11. Update public tool schemas, profile snapshots, documentation, and release
    facts when a public tool, profile, or schema contract changes.

## Implementation workflow

1. Read the closest implementation, focused tests, and contract before editing.
   For tool registration, start with `src/tools/catalog.py` and
   `src/tools/profiles.py`.
2. Prefer narrow, typed, profile-compatible interfaces over generic property
   escape hatches. Preserve stable JSON schemas and bounded response contracts.
3. Keep runtime code under `src/`; do not import `development_kit/` or recipes.
4. Add deterministic tests for observable behavior, safety invariants,
   resume/cleanup/provenance regressions, and schema changes.
5. Update user and developer documentation together with behavior or public
   configuration changes. Do not describe an untested client path as validated.
6. Before committing, inspect the staged diff and leave unrelated changes alone.

## Standalone recipes

- Recipes are examples, not MCP runtime dependencies. Keep them self-contained,
  parameterized, and free of hard-coded user paths or committed model binaries.
- `recipes/acdc_2d_differential_coils.py` derives a two-coil Induction Currents
  model from an upstream example baseline containing `comp1`, `geom1`, and the
  `mf` interface with its required default features. It verifies the baseline
  hash, saves only to a distinct output model, and requires `--overwrite-output`
  before replacing an existing output; do not represent the upstream model as
  original work by this repository.
- That recipe builds and saves by default. A real 1 kHz solve requires the
  explicit `--solve` flag, a free licensed host, and a separate acceptance run;
  no result is validated until that run supplies its evidence.

## Testing and release checks

Run commands from the repository root in the declared development environment:

```powershell
python -m pytest -q development_kit/tests/test_<area>.py
python -m pytest -q
python -m compileall -q comsol_mcp src development_kit
python development_kit/scripts/quality_gate.py --artifact-root <artifact-root>
python development_kit/scripts/release_gate.py
```

For a release candidate, use the locked dependency lane from a clean tree:

```powershell
python development_kit/scripts/release_gate.py `
  --dependency-lock constraints/release_locked_py314.txt
```

Real COMSOL gates are opt-in, licensed, and serial. Follow
`development_kit/docs/release_checklist.md`; hosted CI intentionally does not
run them.

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
