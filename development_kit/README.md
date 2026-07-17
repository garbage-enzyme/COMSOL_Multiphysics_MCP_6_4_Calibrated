# Development kit

This directory contains repository-only tests, integration probes, release
fixtures, build/release utilities, benchmarks, and developer documentation. It
is intentionally excluded from the ordinary wheel and source distribution.
Installed runtime imports and the `comsol-mcp` console entry point must depend
only on packaged runtime resources under `src/`.

## Start here

Read [`docs/layout.md`](docs/layout.md) before scanning the repository. It maps
every tracked file to one short purpose statement and clearly separates shipped
runtime code from repository-only development assets.

The main boundaries are:

- `src/` is the only packaged runtime implementation.
- `development_kit/tests/` contains dependency/process tests, frozen snapshots,
  and explicit licensed integration probes.
- `development_kit/release/` contains sanitized acceptance contracts and release
  support metadata.
- `development_kit/scripts/` contains build, clean-install, installed-discovery,
  and licensed release gates.
- `development_kit/benchmarks/` contains offline evaluation drivers that are not
  part of the runtime.
- `recipes/` contains standalone examples and is not imported by the server.
- `config/` and `constraints/` contain client templates and dependency policy.

## Handoff reading order

1. Read [`docs/layout.md`](docs/layout.md) and open only the files related to the
   requested change.
2. Read `src/tools/catalog.py` and `src/tools/profiles.py` before changing tool
   registration or profile membership.
3. Read `src/tools/capabilities.py`, `src/schema_registry.py`, and the nearest
   contract module before changing public identity or artifact schemas.
4. Run the smallest related test module first, then run the complete default
   suite before commit.
5. Use [`docs/release_checklist.md`](docs/release_checklist.md) for package,
   clean-install, deployment, or licensed acceptance changes.

Do not start COMSOL for ordinary unit, schema, package, documentation, or
process-only work. Files under `tests/integration/` are explicit opt-in gates and
must run serially on a controlled licensed host.

## Test and release commands

Run the dependency/process-only suite from the repository root:

```powershell
python -m pytest -q
python development_kit/scripts/release_gate.py
```

The licensed gate is explicit and serial:

```powershell
python development_kit/scripts/run_real_release_gate.py `
  --confirm RUN_REAL_COMSOL `
  --fixture-spec <controlled-spec.json> `
  --output <new-receipt.json>
```

After every disposable build or install gate, retain the required hashes or
receipt and remove the temporary build root unless archival retention was
explicitly requested.

## Outcome and portfolio evidence contracts

`src/evidence/outcome_contract.py` keeps three decisions independent:

- `execution.state` records whether requested work completed, failed, was
  interrupted, or reached verified cancellation;
- `evidence.state` records whether the evidence is complete, incomplete, or
  invalid and preserves available raw artifacts as diagnostic evidence;
- `scientific.disposition` records accepted, residual, unresolved at the
  declared cap, invalid evidence, or not evaluated.

Every non-accepted disposition carries a reason code, a declared-cap flag, a
missing-evidence list, and the next caller-eligible action. A terminal cancelled
state requires exact process, descendant, port, and lease cleanup proof.

`src/evidence/portfolio_verifier.py` accepts a bounded list of cases. Each case
contains one hashed outcome, one artifact-chain manifest, and zero or more
summary claims. A claim names the `configuration`, `mesh`, `fit`, or
`wavelength` dimension and cites an artifact ID, exact artifact SHA-256, JSON
Pointer, and claimed JSON value. Verification fails unless that exact value is
present in the verified chain. The verifier applies no paper target, portfolio
threshold, acceptance tolerance, or automatic next action.

## Layout maintenance

`docs/layout.md` is a tested inventory, not an informal sketch. Every tracked
file must appear there with one English sentence describing its purpose; update
the layout in the same commit whenever a file is added, renamed, or removed.

## Copyright and provenance

Repository-authored code and fixture contracts are distributed under the root
MIT license. The repository license does not relicense COMSOL, its manuals,
third-party papers, models, or datasets.

The committed release fixtures are sanitized JSON acceptance contracts. They
contain no `.mph` model, licensed manual, paper-derived geometry/data, private
research evidence, credential, or absolute user path. Consequently, no
paper DOI or third-party asset license applies to the current fixture set. If a
paper-derived fixture is added later, its manifest entry and this section must
record the paper citation and DOI/publisher link, the derivation relationship,
and the separate redistribution state before it can pass the release gate.

`release/integration_fixtures/manifest.json` is the authoritative fixture
inventory. Every JSON contract entry binds a canonical JSON SHA-256 that is
stable across LF/CRLF checkout conversion, plus provenance, redistribution
state, and a `paper_derived` flag. Binary/model fixtures are not currently
committed; any future binary must include an exact raw-file SHA-256 and a
source/generated/sanitized/derived classification.
