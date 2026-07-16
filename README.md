# COMSOL MCP Server for COMSOL 6.4+

English | [中文](README_CN.md)

[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/stargazers)

> A maintained fork of [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP), calibrated for **COMSOL Multiphysics 6.4+** and **MPh 1.3.1 standalone/clientapi**.

This server gives AI agents a safer, smaller interface for COMSOL inspection, controlled one-point validation, durable staged sweeps, and offline manual lookup. It is designed for the `model.java` clientapi object returned by `mph.Client()`, whose API differs materially from the direct `com.comsol.model.Model` API targeted by the upstream project.

## Recommended companion skill

For Claude Code, Codex CLI, opencode, Hermes Agent, and other skill-aware agents, use the
[COMSOL 6.4+ metasurface agent skill](https://github.com/garbage-enzyme/COMSOL_6_4_agentskill_for_metasurfaces)
alongside this server. Its short `SKILL.md` entry routes agents to focused
reference modules for clientapi, periodic Wave Optics, materials and boundaries,
durable jobs, physical evidence, resource safety, troubleshooting, and MCP
development/release engineering without forcing the full guide into context on
every turn.

## Client compatibility and deployment

The installed FastMCP stdio server is compatible with Hermes Agent, Codex CLI,
and opencode. Use the complete deployment guide for fresh installation, exact
configuration paths, profile selection, restart behavior, and solver-free
verification:

- [Deployment guide (English)](DEPLOYMENT.md)
- [部署指南（中文）](DEPLOYMENT_CN.md)

The essential rules are: perform a non-editable install, configure the absolute
installed `comsol-mcp` executable, set `COMSOL_MCP_PROFILE` before the stdio host
starts, restart the client after changing the profile or package, and keep COMSOL
tool calls serialized. Call `capabilities` to verify the deployed profile without
starting COMSOL.

Hermes compatibility was checked against its official
[MCP documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md)
and [client source](https://github.com/NousResearch/hermes-agent/blob/main/tools/mcp_tool.py).
A solver-free installed-entry-point smoke gate completed `initialize`,
`list_tools`, and `capabilities` with server `COMSOL MCP`, 38 `core` tools,
profile `core`, and `connected=false`.

## Highlights

- **ClientAPI compatibility.** Geometry, physics, materials, meshes, studies, results, model cloning, and Unicode-safe `.mph` saving are adapted to COMSOL 6.4+ clientapi behavior.
- **Safe solver ownership.** An ASCII-path lease, process identity checks, external-client detection, status, and preflight checks prevent accidental competing COMSOL clients.
- **Durable background work.** Staged sweeps run in detached workers with immutable specifications, atomic state, fsync'd CSV journals, checkpoints, validated resume, and verified same-host cancellation.
- **Wave Optics validation.** A focused profile provides read-only model preflight and a one-wavelength evidence audit for periodic metasurfaces.
- **Bounded offline manuals.** SQLite FTS5/BM25 search and page retrieval run outside the COMSOL control process and return compact source/page citations.
- **Honest optional semantic retrieval.** The isolated semantic profile is contained, but its current MiniLM baseline did not meet quality and memory promotion gates. Lexical manual search remains the recommended default.

## Profiles

Set `COMSOL_MCP_PROFILE` before starting the server. A profile is fixed for the lifetime of that server process; restart after changing it.

| Profile | Tools | Intended use |
| --- | ---: | --- |
| `core` (default) | 38 | Compact, mature control plane: status, ownership, session/model inspection, one-point solve/evaluation, and lexical manuals. |
| `basic_fem` | 76 | `core` plus typed conventional FEM construction, derived-geometry edits, and bounded exports. |
| `wave_optics` | 63 | Recommended for metasurfaces: `core` plus derived-geometry edits, material preview, locale-safe field discovery and bounded NPZ/manifest extraction, periodic-mesh audit/smoke, visual-review contracts, Wave Optics preflight, point/reference audits, and staged workflows. |
| `semantic_docs` | 41 | `core` plus isolated experimental vector-assisted manual retrieval. |
| `experimental` | 64 | Explicit opt-in generic creation, async, property escape hatches, and project helpers. |
| `full` | 120 | Broad compatibility/discovery surface, including optional semantic tools. |

Call `capabilities` to discover the active profile, exact registered tools, target versions, disabled groups, and restart requirements without starting COMSOL. Its bounded `deployment_identity` reports source-tree versus installed-package loading plus frozen profile/schema and catalog hashes, so a host restart can detect same-version stale installs or source shadowing without exposing local paths.

Control-plane responses from capabilities, solver ownership, durable jobs, and lexical manuals include a compact rolling `control_plane` block. It retains at most 256 samples per operation and reports success/busy/timeout/error counts plus p50/p95/max latency; full logs and unbounded telemetry are never returned inline.

## Recommended workflows

### General solver work

1. Call `solver_status`.
2. Call `solver_preflight` before connecting, starting COMSOL, or submitting substantial work.
3. Use the session/model tools or submit a durable staged sweep.

The server fails closed when an external MPh/COMSOL owner or a valid lease is present. `solver_recover_stale_lease` only removes a lease that process identity evidence proves stale; it never terminates an unowned process.

Durable sweep controls are `job_submit`, `job_status`, `job_tail`, `job_cancel`, and `job_resume`. Each job has its own ASCII-only runtime directory containing its immutable specification, state, CSV journal, checkpoint, and log. Resume accepts only matching, finite, successful rows. Cancellation reaches a terminal cancelled state only after worker/process cleanup and lease release are verified. This coordination is for a shared runtime directory on one host; it is not distributed execution.

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

`manual_search` and `manual_read_pages` are the production documentation path. They use an offline SQLite FTS5/BM25 index, bounded worker processes, and compact source/page citations. The MCP control process does not import ChromaDB, Torch, or SentenceTransformer for this path.

`semantic_docs` is opt-in and isolated from COMSOL control. Its current CPU-only MiniLM implementation is an English diagnostic baseline, not a multilingual or production-quality claim: the frozen benchmark improved exact-match recall but regressed paraphrase/multi-concept recall, returned no direct-Chinese matches, failed negative-query abstention, and grew substantially in memory during soak testing. Keep `core` plus lexical manual search for normal work.

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

The current dependency/process-only gate is **643 passed, 13 deselected**. Unit tests are side-effect-free: collection does not start COMSOL, and integration probes run only when explicitly requested in fresh, sequential subprocesses with exact process-tree cleanup. Repository-only tests, release fixtures, gates, and provenance are documented in `development_kit/README.md`; ordinary wheel/sdist artifacts exclude that directory.

```bash
python -m pytest -q
python -m pytest -q -m integration development_kit/tests/integration
```

Real COMSOL checks include localized JSON transport; circle/union geometry; DXF import; parametric sweep properties; multiphysics coupling; clone cleanup; Unicode-path saving; solver ownership; durable interruption/restart/resume/cancellation; profile discovery; Wave Optics preflight and one-point audit; and bounded manual retrieval.

The parallel-plate regression returns **1.8593794419540652 pF**, versus the theoretical **1.8593794406880002 pF**.

## Requirements and installation

- COMSOL Multiphysics 6.4 or newer
- Python 3.10+ (not the Windows Store build)
- MPh 1.3.1, `mcp`, `pydantic`, and `psutil>=5.9.0`
- COMSOL's Java 21 runtime in the verified configuration

```bash
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
cd COMSOL_Multiphysics_MCP_6_4_Calibrated
python -m pip install .

# Recommended offline manual index; use an ASCII-only output path.
python -m pip install ".[manuals]"
python -m src.knowledge.lexical_manual build --index D:\comsol_docs_fts\manuals.sqlite3
```

For optional isolated semantic retrieval:

```powershell
python -m pip install ".[semantic-docs]"
$env:COMSOL_MCP_PROFILE = "semantic_docs"
$env:COMSOL_SEMANTIC_ROOT = "D:\comsol_semantic"
$env:COMSOL_SEMANTIC_LEXICAL_INDEX = "D:\comsol_docs_fts\manuals.sqlite3"
```

On Windows accounts whose user path contains non-ASCII characters, avoid editable installs. Run `python -m pip install . --no-deps` after source changes, then restart the MCP host; the server does not hot-reload `src/tools/`.

Configure an MCP client, for example:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["python", "-m", "src.server"],
      "environment": { "COMSOL_MCP_PROFILE": "wave_optics" }
    }
  }
}
```

Omit `COMSOL_MCP_PROFILE` for `core`. Client examples are available at
`config/codex-mcp.example.toml`, `config/hermes-mcp.example.yaml`, and
`config/opencode-mcp.example.json`.

## How this fork differs from upstream

This is a COMSOL 6.4+ standalone/clientapi compatibility and reliability fork, not a general replacement for upstream. It keeps the upstream project's foundation while making a deliberately narrower, safer execution surface for agent-driven COMSOL work.

| Area | Upstream orientation | This fork |
| --- | --- | --- |
| COMSOL API target | Direct `com.comsol.model.Model` API assumptions. | MPh 1.3.1 standalone `model.java` clientapi wrappers, including their different overloads, tags, lists, and Java-string transport. |
| Tool surface | Broad feature discovery by default. | Compact `core` default; larger construction and compatibility surfaces require an explicit profile. |
| Solver concurrency | No same-host ownership protocol. | Process-aware lease, external-client detection, status, preflight, and stale-lease recovery that never kills an unowned process. |
| Long runs | Interactive/current-process workflows. | Detached durable jobs with immutable specs, fsync'd rows, checkpoints, validated resume, and verified cancellation cleanup. |
| Wave Optics | General tools only. | A dedicated preflight plus one-point evidence audit for periodic metasurfaces, with raw evidence separated from caller policy. |
| Manual search | Legacy in-process semantic PDF path can load heavy dependencies. | Bounded isolated lexical manual retrieval is the production default; experimental semantic retrieval is isolated and explicitly not promoted. |
| Windows paths | No special guarantee for Unicode save paths. | Clientapi Java save path for Unicode `.mph` saves; ASCII-only runtime/index roots for native and durable artifacts. |

Use this fork when the upstream server fails under MPh standalone with errors such as `No matching overloads`, `Operation_cannot_be_created_in_this_context`, or client-list indexing errors. Use `full` only when compatibility with a broad legacy surface is genuinely required.

## License

Inherits the upstream license. See the original repository for details.
