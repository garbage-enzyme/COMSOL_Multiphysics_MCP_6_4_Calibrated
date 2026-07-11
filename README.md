# COMSOL MCP Server — 6.4+ ClientAPI Calibrated Fork

English | [中文](README_CN.md)

[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated/stargazers)

> **Fork of [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP).**
> This branch calibrates the MCP server tools for **COMSOL 6.4+ with MPh 1.3.1 standalone mode** (the `clientapi` wrapper layer). It also documents and uses COMSOL 6.4+ solver capabilities such as the cuDSS GPU-accelerated direct solver. The upstream code targets the direct `com.comsol.model.Model` API and breaks on standalone clientapi.

## Why this fork exists

Under `mph.Client(cores=...)` (MPh 1.3+ standalone), `model.java` returns `com.comsol.clientapi.impl.ModelClient` — a **wrapper** around the real model. Every `component()` / `physics()` / `geom()` call returns a `*Client` class whose method overloads differ from the direct `com.comsol.model.*` API the upstream code was written against. Result: most geometry/physics/study/mesh tools fail at runtime on standalone clientapi.

This fork repairs the clientapi paths exercised by the current test matrix and adds durable workflow helpers. End-to-end verification through MCP gives a parallel-plate capacitance of **1.8593794419540652 pF**, matching the theoretical **1.8593794406880002 pF**.

> **Refactor provenance:** This COMSOL 6.4 clientapi refactor and validation pass was carried out with **OpenAI Codex** (GPT-5-based coding agent; project display label: **“GPT-5.6 Sol”**) under 陆星's direction. Earlier fork work also involved opencode/GLM-5.2. See `git log` for the tested, incremental change history.

## What changed

The refactor covers the following stable paths:

### `model.py`

- Unicode-safe `.mph` saves use `model.java.save(full_path)`.
- Model clones use clientapi Save Copy plus `client.load()`; temporary backing files are tracked and removed with the cloned model/session.
- Component tags and localized labels are normalized to Python strings before MCP transport.

### `geometry.py`

- Generic feature creation and listing use clientapi `tags()`/`size()` semantics.
- Circle, Union, and CAD Import helpers use the correct clientapi feature and selection APIs.
- Geometry responses normalize feature tags/labels for JSON transport.

### `physics.py`

- Physics interfaces use `physics().create(tag, type, sdim_string)`; child features use their integer entity dimension.
- Canonical English names resolve stable tags even when COMSOL returns localized labels.
- Domain features and materials are created in the physics-owning component and use that component's dimension.
- Existing component materials are reused correctly.
- Multiphysics couplings are created through `comp.multiphysics().create(...)`.
- Interface, feature, geometry, and material tags are normalized before uniqueness checks or MCP transport.
- `physics_add_electrostatics` can add `ChargeConservation` plus a material because COMSOL 6.3+/6.4's default `fsp1` FreeSpace feature otherwise uses vacuum permittivity.

### `study.py`

- Study creation maps aliases to clientapi types such as `Stationary`, `Transient`, `FrequencyDomain`, `Eigenfrequency`, and `Perturbation`.
- Study tags, localized labels, steps, and solve targets are resolved through stable clientapi tags.

### `mesh.py`

- `mesh_sequence_create` creates and optionally builds an explicit mesh sequence; COMSOL does not create one automatically.
- Inspection uses `getNumElem()` / `getNumVertex()` and returns JSON-safe tags and localized labels.

### `parameters.py`, `results.py`, and `workflow.py`

- Parametric sweeps use clientapi `String[]` properties (`pname`, `plistarr`, and optional `punit`) and activate the sweep.
- Real, NumPy, and complex results are normalized for MCP JSON transport.
- Staged parameter sweeps and mesh-convergence runs write success/error rows incrementally, retry failed points, resume successful rows, checkpoint through clientapi, and flush plus `fsync` every row.

### `mim_patch.py`

- Boundary probing uses `getUpDown()`, coordinates, normals, and bounding boxes.
- Periodic side classification filters by both normal and cell-edge coordinate.
- Pair, geometry, and mesh tags are normalized for transport.

### Default server profile

- The dependency-free `capabilities` tool reports the COMSOL/MPh target, verified
  areas, experimental async semantics, disabled tools, and missing long-job
  guarantees without starting COMSOL.
- `pdf_search`, `pdf_search_status`, and `pdf_list_modules` are disabled by
  default because they can initialize ChromaDB and an embedding model in the
  COMSOL control process. Their source remains available for an explicit isolated
  profile.
- `manual_search` and `manual_read_pages` provide the default documentation
  path through an offline SQLite FTS5/BM25 index. Each read runs in a bounded
  worker process, returns source/page references, and never imports ChromaDB,
  Torch, or SentenceTransformer in the COMSOL control process. Long natural-
  language queries first use strict significant-term matching, then automatically
  relax and rerank by term coverage plus BM25 instead of silently returning zero.
- Startup logs print a compact capability summary. The current default profile
  exposes 96 tools, including explicit `session_clear_models` and
  `session_reset` lifecycle operations.
- A failed local startup is retained as an error and cannot silently create
  another worker; call `session_reset` before an explicit retry.
- `solver_status` merges MCP session state, an ASCII-path process lease, external
  MPh/COMSOL process evidence, and active/recent durable-job summaries without starting
  COMSOL. `solver_preflight` checks process ownership, PID creation time, command
  identity, 64-bit architecture, discovered COMSOL/JRE backends, memory, and
  model/output paths. Local start and remote connect fail closed before
  `mph.Client()` when another solver owner is detected.
- `solver_recover_stale_lease` removes only a lease proven stale by PID plus
  creation-time/command evidence. It never terminates a process. The runtime
  directory defaults to `D:\comsol_runtime` when that drive exists and can be
  overridden with `COMSOL_MCP_RUNTIME_DIR`; it must remain ASCII-only.

### Repo hygiene

- New `.gitignore` for `__pycache__/`, `*.pyc`, `opencode.json` (machine-specific paths), `knowledge_base/` (regenerable), `*.mph`.
- Stopped tracking `opencode.json` and `knowledge_base/chroma.sqlite3` — both are local-only / regenerable.

`study_staged_parametric_sweep` and `mesh_convergence_study` support:

- `resume_csv=True`: skip rows already recorded with `status=success`.
- `max_retries=N`: retry a failed point or mesh level up to `N` times.
- `continue_on_error=True`: record the failed row and continue the run.
- `checkpoint_model_path=...`: save through the Java clientapi during the run.
- `checkpoint_every=N`: checkpoint after every `N` new successful rows.

The staged sweep now writes a versioned JSON manifest beside its CSV, derives or
accepts a stable `config_id`, optionally fingerprints the immutable source MPH,
records `status=ok/error` plus exception type, and resumes only finite rows whose
schema/config and required expressions match. Wavelength sweeps record the
requested value, evaluated global `wl`, and evaluated `c_const/ewfd.freq` by
default. MCP responses return counts, the last point, and a bounded tail instead
of every row. Legacy CSV adoption requires `allow_legacy_resume=true`; adopted
rows are marked `legacy_unverified` and rerun rather than silently trusted.

### Durable background staged sweeps

`job_submit`, `job_status`, `job_tail`, `job_cancel`, and `job_resume` provide the
H1 durable control plane. Each accepted `staged_sweep` runs in a detached worker,
owns the M2 solver lease, binds CSV/manifest/checkpoint/log artifacts to its
ASCII-only job directory, validates one or two smoke points against the complete
immutable M1 manifest, and resumes only matching finite `status=ok` rows.

The cancellation boundary is intentional and machine-readable: `job_cancel`
records `cancel_requested`; a worker checks it only between blocking solve points
and then records `interrupted`. H1 never reports `cancelled` and does not claim to
abort an active COMSOL `study.run()`. Verified native/process cancellation remains
H2 work.

## Verification

Run the isolated unit suite with `python -m pytest -q`. The current refactor gate is
**134 passing tests**. `python -m pytest --collect-only -q` also leaves the COMSOL
process set unchanged. Root-level
`test_*.py` files are manual integration probes that may start COMSOL and are
explicitly excluded from pytest collection; invoke them individually only when
a dedicated COMSOL client is available.

Run the three real probes explicitly, one fresh subprocess at a time, with:

```bash
python -m pytest -q -m integration tests/integration
```

The integration runner owns and time-bounds its exact Python process tree, invokes
each probe sequentially, and fails if the COMSOL PID set grows after cleanup. The
third probe verifies `model.java.save()` to a temporary Chinese path under the
repository, then removes only that temporary artifact after disconnecting.

`test_e2e_cap.py` and `test_study_mesh.py` are standalone verification scripts (drive `mph.Client` directly, no MCP layer). The same recipe was also re-run end-to-end through the MCP tool interface after restarting the MCP host to load the new code:

| Step | MCP tool |
| --- | --- |
| Create model + 3D component | `model_create` → `model_create_component(3D)` |
| Geometry: 10mm × 10mm × 1mm block | `geometry_create(3D)` → `geometry_add_block([0.01,0.01,0.001])` → `geometry_build` |
| Electrostatics, ε_r = 2.1 | `physics_add_electrostatics(relpermittivity=2.1, domain_numbers=[1])` |
| BCs: Ground @ z=0 (bnd 3), V=1V @ z=1mm (bnd 4) | `physics_configure_boundary(Ground,[3])`, `physics_configure_boundary(ElectricPotential,[4],{V0:'1[V]'})` |
| Mesh | `mesh_sequence_create(FreeTet, build=True)` → ~1663 elements |
| Solve | `study_create(Stationary)` → `study_solve` |
| Capacitance | `results_global_evaluate('2*es.intWe/(1[V])^2','pF')` |

**Result:** `1.8593794419540652 pF` vs theory `ε₀·ε_r·L²/d = 1.8593794406880002 pF`.

Additional real COMSOL 6.4 checks include localized component/geometry/physics/mesh/study JSON responses, Circle plus Union geometry, DXF import (5 domains and 33 boundaries), active Parametric sweep properties, model clone cleanup, and an `ElectromechanicalForces` multiphysics coupling.

### 6.3+/6.4 clientapi gotchas (documented in source comments)

1. **Electrostatics `fsp1` FreeSpace trap** — default domain feature uses vacuum ε₀ and ignores material `relpermittivity`. Must add a `ChargeConservation` feature (`materialType='from_mat'`) plus a material node with `propertyGroup('def').set('relpermittivity', ...)`.
2. **Block boundary numbering is NOT 1–6 ↔ −x/+x/−y/+y/−z/+z.** For `Block size [0.01,0.01,0.001] pos [0,0,0]`: **bnd 3 = z=0 face, bnd 4 = z=0.001 face**; 1/2/5/6 are side faces. Identify by coordinate with a `Box` selection (`condition='inside'`).
3. **`Terminal` feature `V0` does not pin voltage correctly** (observed ΔV ≈ 0.16 V for V0=1 V). Use `ElectricPotential` boundary condition for capacitance verification.
4. **Expression syntax:** `1[V]^2` is a parse error in clientapi — must write `(1[V])^2`.
5. **`m.study().run()` does not exist** on mph 1.3.1 `Model` — use `model.java.study('std1').run()`.

## Requirements

- **COMSOL Multiphysics 6.4 or newer**. This fork targets COMSOL 6.4+ standalone clientapi because current workflows may use 6.4+ solver features such as **cuDSS** GPU-accelerated direct solving.
- **Python 3.10+** (not the Windows Store build)
- **Java runtime** — COMSOL 6.4 ships Java 21, and JPype works directly in the verified setup. Older COMSOL/Java combinations are not this fork's focus.
- **MPh 1.3.1**, plus `mcp`, `pydantic`. Offline manual-index building optionally
  uses `pymupdf`; legacy semantic PDF search additionally uses `chromadb` and
  `sentence-transformers`.

## Installation

```bash
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_4_Calibrated.git
cd COMSOL_Multiphysics_MCP_6_4_Calibrated
python -m pip install .
# Recommended offline lexical manual index (ASCII-only output path)
python -m pip install ".[manuals]"
python -m src.knowledge.lexical_manual build --index D:\comsol_docs_fts\manuals.sqlite3
# Optional legacy semantic profile
python -m pip install ".[semantic-pdf]"
python scripts/build_knowledge_base.py
```

On Windows accounts with a non-ASCII user path, do not use editable installs for
this repository. Re-run `python -m pip install . --no-deps` after source changes.
The MCP server does not hot-reload `src/tools/`; restart the host agent/CLI after
installing a new source revision.

Start COMSOL Multiphysics first (MCP bridges via MPh/JPype), then point your MCP client (opencode / Claude Desktop) at the server:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "comsol": {
      "type": "local",
      "command": ["python", "-m", "src.server"]
    }
  }
}
```

## Relationship to upstream

This fork tracks `wjc9011/COMSOL_Multiphysics_MCP` and is intended as a **6.4+ standalone clientapi compatibility fork**, not a general feature fork. The upstream README (`README.md` in the original repo, preserved here as `README_upstream.md` if needed) describes the broader feature set, knowledge base, and 5.x workflows that this fork inherits unchanged.

If you're running on **6.4+ standalone** and the upstream tools throw `No matching overloads`, `Operation_cannot_be_created_in_this_context`, or `'ComponentGeomListClient' object is not subscriptable` — use this fork. The last error is fixed here: `geometry_get_boundaries` now returns per-boundary `normal` + `center` + whole-geometry `bounding_box` (via `faceNormal`/`faceX`/`edgeNormal`/`edgeX` on the parameter midpoint), so you can identify which boundary is which face directly (e.g. z=0 face has normal `[0,0,-1]`) — no manual `Box` selection needed.

## License

Inherits the upstream license. See original repository for details.
