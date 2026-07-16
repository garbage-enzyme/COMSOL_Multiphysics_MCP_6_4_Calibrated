# Development kit

This directory contains repository-only tests, integration probes, release
fixtures, build/release utilities, benchmarks, and developer documentation. It
is intentionally excluded from the ordinary wheel and source distribution.
Installed runtime imports and the `comsol-mcp` console entry point must depend
only on packaged runtime resources under `src/`.

## Layout

- `tests/`: dependency/process-only tests, licensed integration probes, frozen
  schemas, and benchmark judgments.
- `release/`: the supported-version matrix and sanitized real-COMSOL acceptance
  contracts.
- `scripts/`: compile, package, installed-discovery, knowledge-base, and serial
  licensed release gates.
- `benchmarks/`: offline evaluation drivers that are not part of the runtime.
- `docs/`: developer release procedures.

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
