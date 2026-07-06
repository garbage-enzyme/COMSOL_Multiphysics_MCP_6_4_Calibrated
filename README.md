# COMSOL MCP Server — 6.3 ClientAPI Calibrated Fork

[![GitHub stars](https://img.shields.io/github/stars/garbage-enzyme/COMSOL_Multiphysics_MCP_6_3_Calibrated?style=social)](https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_3_Calibrated/stargazers)

> **Fork of [wjc9011/COMSOL_Multiphysics_MCP](https://github.com/wjc9011/COMSOL_Multiphysics_MCP).**
> This branch calibrates the MCP server tools for **COMSOL 6.3 + MPh 1.3.1 standalone mode** (the `clientapi` wrapper layer). The upstream code targets the direct `com.comsol.model.Model` API and breaks on 6.3 standalone.

## Why this fork exists

Under `mph.Client(cores=...)` (MPh 1.3+ standalone), `model.java` returns `com.comsol.clientapi.impl.ModelClient` — a **wrapper** around the real model. Every `component()` / `physics()` / `geom()` call returns a `*Client` class whose method overloads differ from the direct `com.comsol.model.*` API the upstream code was written against. Result: most geometry/physics/study/mesh tools fail at runtime on 6.3.

This fork fixes all known clientapi mismatches in `src/tools/` and adds two missing tools. End-to-end verified via MCP: a parallel-plate capacitor returns **C = 1.8593794414 pF**, matching theory (1.8593794407 pF, err 4e-10 pF).

> ⚠️ **Provenance:** The code changes in this fork were authored by an AI assistant (opencode + glm-5.2) under human direction, then verified end-to-end through the MCP tool interface. See `git log` for the detailed change breakdown.

## What changed

All fixes live in `src/tools/` and target the `clientapi` wrappers. Summary by file:

### `model.py`
- `list_components`: iterate components via `tags()` instead of int index — `ModelEntityListClient.get` only accepts a String tag.

### `geometry.py`
- 5× `len(geom.feature())` → `geom.feature().size()` — clientapi lists don't support `len()`.
- Affects `add_block`, `add_cylinder`, `add_sphere`, `add_rectangle`, `boolean_difference`.

### `physics.py`
- New helpers `_first_component(jm)` and `_component_sdim(comp)` — `getSDim()` returns int; physics `create` needs it as a **String**.
- All `comp.get(int)` → `tags()` iteration.
- `physics().create(tag, type, sdim_string)` — **three args**, third is a String like `"3"`. Two-arg form fails with "物理场接口不支持空间维度: 0维"; int third arg fails with "No matching overloads".
- `geometry_get_boundaries`: `getNboundary()` → `getNBoundaries()`, `getNdomain()` → `getNDomains()` (capitalized in clientapi).
- `physics_add_electrostatics`: new `relpermittivity` + `domain_numbers` params. When given, auto-creates a `ChargeConservation` feature + material node — required because **6.3's default Electrostatics domain feature is `fsp1` (FreeSpace) which uses vacuum ε₀ and ignores material `relpermittivity`**.
- New generic `physics_add_domain_feature` tool (ChargeConservation / LinearElasticMaterial / Solid, …).

### `study.py`
- Study step type uses **full names** (`Stationary` / `TimeDependent` / `Eigenfrequency` / `Frequency` / `Perturbation`) via a `SHORT_TO_FULL` map. Short names (`stat` / `time` / `eig` / `freq` / `pert`) work in the direct Model API but fail in clientapi with `Operation_cannot_be_created_in_this_context`.

### `mesh.py`
- New `mesh_sequence_create` tool. COMSOL does **not** auto-create a mesh sequence — the upstream `mesh_create` only runs an existing sequence. New tool does `comp.mesh().create()` + `feature().create('FreeTet')` + `run()`, and reports element counts via `getNumElem()` / `getNumVertex()` (clientapi; not `getElement().size()`).

### Repo hygiene
- New `.gitignore` for `__pycache__/`, `*.pyc`, `opencode.json` (machine-specific paths), `knowledge_base/` (regenerable), `*.mph`.
- Stopped tracking `opencode.json` and `knowledge_base/chroma.sqlite3` — both are local-only / regenerable.

## Verification

`test_e2e_cap.py` and `test_study_mesh.py` are standalone verification scripts (drive `mph.Client` directly, no MCP layer). The same recipe was also re-run end-to-end through the MCP tool interface after restarting opencode to load the new code:

| Step | MCP tool |
| --- | --- |
| Create model + 3D component | `model_create` → `model_create_component(3D)` |
| Geometry: 10mm × 10mm × 1mm block | `geometry_create(3D)` → `geometry_add_block([0.01,0.01,0.001])` → `geometry_build` |
| Electrostatics, ε_r = 2.1 | `physics_add_electrostatics(relpermittivity=2.1, domain_numbers=[1])` |
| BCs: Ground @ z=0 (bnd 3), V=1V @ z=1mm (bnd 4) | `physics_configure_boundary(Ground,[3])`, `physics_configure_boundary(ElectricPotential,[4],{V0:'1[V]'})` |
| Mesh | `mesh_sequence_create(FreeTet, build=True)` → ~1663 elements |
| Solve | `study_create(Stationary)` → `study_solve` |
| Capacitance | `results_global_evaluate('2*es.intWe/(1[V])^2','pF')` |

**Result:** `1.8593794414 pF` vs theory `ε₀·ε_r·L²/d = 1.8593794407 pF` — error 4 × 10⁻¹⁰ pF.

### 6.3-specific gotchas (documented in source comments)

1. **Electrostatics `fsp1` FreeSpace trap** — default domain feature uses vacuum ε₀ and ignores material `relpermittivity`. Must add a `ChargeConservation` feature (`materialType='from_mat'`) plus a material node with `propertyGroup('def').set('relpermittivity', ...)`.
2. **Block boundary numbering is NOT 1–6 ↔ −x/+x/−y/+y/−z/+z.** For `Block size [0.01,0.01,0.001] pos [0,0,0]`: **bnd 3 = z=0 face, bnd 4 = z=0.001 face**; 1/2/5/6 are side faces. Identify by coordinate with a `Box` selection (`condition='inside'`).
3. **`Terminal` feature `V0` does not pin voltage correctly** (observed ΔV ≈ 0.16 V for V0=1 V). Use `ElectricPotential` boundary condition for capacitance verification.
4. **Expression syntax:** `1[V]^2` is a parse error in clientapi — must write `(1[V])^2`.
5. **`m.study().run()` does not exist** on mph 1.3.1 `Model` — use `model.java.study('std1').run()`.

## Requirements

- **COMSOL Multiphysics 6.3** (this fork is calibrated for 6.3 standalone; 5.x should still work via the direct API but is not the focus)
- **Python 3.10+** (not the Windows Store build)
- **Java runtime** — 6.3 ships Temurin Java 11, JPype works directly. (5.6 ships Java 8, which needs a Java 9+ shim for JPype — not this fork's concern.)
- **MPh 1.3.1**, plus `mcp`, `pydantic`. Optional for PDF search: `pymupdf`, `chromadb`, `sentence-transformers`.

## Installation

```bash
git clone https://github.com/garbage-enzyme/COMSOL_Multiphysics_MCP_6_3_Calibrated.git
cd COMSOL_Multiphysics_MCP_6_3_Calibrated
python -m pip install -e .
# Optional: PDF knowledge base
pip install pymupdf chromadb sentence-transformers
python scripts/build_knowledge_base.py
```

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

This fork tracks `wjc9011/COMSOL_Multiphysics_MCP` and is intended as a **6.3 compatibility patch**, not a feature fork. The upstream README (`README.md` in the original repo, preserved here as `README_upstream.md` if needed) describes the broader feature set, knowledge base, and 5.x workflows that this fork inherits unchanged.

If you're running on **6.3 standalone** and the upstream tools throw `No matching overloads`, `Operation_cannot_be_created_in_this_context`, or `'ComponentGeomListClient' object is not subscriptable` — use this fork.

## Known gaps

- `geometry_get_boundaries` returns `nBoundaries` / `nDomains` but **not per-boundary coordinates or normals**, and still throws `'ComponentGeomListClient' object is not subscriptable` (`_get_geometry_node` uses subscript access on `comp.geom()`). Workaround: identify boundary numbers with a `Box` selection by coordinate.

## License

Inherits the upstream license. See original repository for details.
