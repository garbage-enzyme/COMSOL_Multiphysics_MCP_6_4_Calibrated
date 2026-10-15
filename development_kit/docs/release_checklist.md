# Release checklist

The dependency-only gate must run from a clean tree and does not start COMSOL.
The Python 3.14 production lane additionally consumes its complete hash lock:

```powershell
python development_kit/scripts/release_gate.py `
  --dependency-lock constraints/release_locked_py314.txt
```

The gate compiles `comsol_mcp`, `src`, and `development_kit`.
It runs the selected tests and checks frozen tool/profile schemas.
It builds wheel and sdist artifacts, then creates a fresh virtual environment.
It installs the wheel non-editably and runs `pip check`.
It checks installed discovery against the frozen snapshots.
Discovery fails if it starts `mph.Client` or imports ChromaDB, SentenceTransformer, or Torch.

Before a release:

1. Confirm `git status --short` is empty.
2. Run the dependency-only gate and archive its sanitized JSON report.
   When Settings GUI files change, require the `solver-free-settings-gui` job
   inside `solver-free-ci`, verify the deterministic locale, multi-size Windows icon, and package receipts, inspect
   the real 100%, 125%, 150%, and 200% three-language screenshots directly, and
   record the user's final rendered-interface acceptance before the GUI-bearing
   commit.
3. Confirm `development_kit/release/support_matrix.json` matches the intended
   version tuple.
4. For a Python runtime promotion, run the provenance-bound compatibility gate
   from the clean candidate commit:

   ```powershell
   $env:COMSOL_MCP_ACCEPTANCE_CORES='<caller-selected-positive-integer>'
   python development_kit/scripts/python_compatibility_licensed_gate.py `
     --confirm RUN_REAL_COMSOL `
     --cores $env:COMSOL_MCP_ACCEPTANCE_CORES `
     --runtime-root D:\comsol_runtime `
     --output D:\comsol_release\python_compatibility.json
   ```

   The environment value is also consumed by direct licensed integration
   entry points. Select it for the current host and campaign. Do not copy a
   core count from another machine or release receipt.

5. On a free, licensed, version-pinned host, run the serial real gate explicitly:

   Use an isolated interpreter with the exact accepted wheel installed non-editably.
   This interpreter also needs the declared development dependencies for pytest.
   The live-profile probe clears `PYTHONPATH` and starts the installed server outside the checkout.
   A source-only interpreter cannot satisfy that installed-package check.
   Set `COMSOL_MCP_ACCEPTANCE_CORES` explicitly for all licensed probes.

   ```powershell
   python development_kit/scripts/run_real_release_gate.py --confirm RUN_REAL_COMSOL `
     --fixture-spec D:\path\to\controlled_fixture_spec.json `
     --licensed-regression-timeout-seconds 900 `
     --output D:\comsol_release\real_gate.json
   ```

6. `--fixture-spec` supplies the controlled model/wavelength/top-air environment
   for the licensed regression suite without rerunning optional reference-power
   evidence. Use `--require-reference-power --reference-power-spec ...` only
   when that release must generate a new reference-power receipt as well.
7. Require fresh and complete final process inventory, an unchanged COMSOL PID
   set, an absent solver lease, no external collision, source-integrity
   evidence, and all fixture contracts to pass. A phase timeout or exception
   still requires this final assessment and a failed receipt.
8. When the standalone launcher changes, run its explicit serial gate on a free
   Windows 10/11 x64 host with licensed COMSOL 6.4:

   ```powershell
   python development_kit/scripts/standalone_licensed_gate.py `
     --confirm RUN_REAL_COMSOL `
     --comsol-root D:\path\to\COMSOL64\Multiphysics `
     --output-directory D:\comsol_release\standalone_acceptance
   ```

   Require a one-plus-two attempt split, `3/3` points, a passed physical
   summary, matching terminal/result identity, and zero process residue.
9. Build once more from the clean release commit and compare discovery output.
10. Install non-editably in the target MCP environment.
    Confirm `comsol-mcp-settings`, all three compiled catalogs, and
    profile-independent `settings.start` are installed while GUI tests and PO
    sources remain outside the wheel.
11. Restart the MCP host. Source and profile changes are not hot-reloaded.
12. Call `capabilities`. Require `deployment_identity.source_classification` to
   be `installed_site_package`, compare its profile/schema/catalog hashes with
   the clean release receipt, and then treat installed profile counts as
   authoritative. A matching version string alone is insufficient.

Hosted CI never runs licensed COMSOL integration tests. They remain explicit,
serial, and unavailable by default.

The native optimizer development gate accepts an explicit fixed loading path for its fresh finalist.
Use `--finalist-continuation-fractions` with a JSON list of at most 32 strictly increasing fractions.
The final fraction must equal `1.0`.
Also supply `--material-jacobian-expression` and `--minimum-material-jacobian`.
The gate reserves one solve for every fraction, plus the baseline, before allocating optimizer solves.
Each step checks initialization readback, solved patch dimensions, geometry mapping and material mapping.
The gate stops on failure without automatic path refinement or a direct-solve fallback.
Without this option, the gate retains its direct finalist solve.
These CLI options apply to the development gate.
Durable adjoint manifests at version `1.1.0` require an explicit `execution_policy`.
Version `1.0.0` retains its direct finalist path.
The execution policy declares smoothing, geometry order, adjoint gradients and fixed finalist fractions.
It also declares geometry and material Jacobian expressions, minimum values and minimum mesh quality.
Caller passivity and closure tolerances remain explicit.
For example, a caller can declare this fixed loading policy:

```json
{
  "smoothing_type": "hyperelastic",
  "geometry_shape_order": "1",
  "gradient_method": "adjoint",
  "finalist_fractions": [0.25, 0.5, 0.75, 1.0],
  "geometry_jacobian_expression": "reldetjac",
  "minimum_geometry_jacobian": 0.0,
  "material_jacobian_expression": "comp1.material.detF",
  "minimum_material_jacobian": 0.0,
  "minimum_element_quality": 0.1,
  "passivity_absolute_tolerance": 0.000001,
  "closure_absolute_tolerance": 0.000001
}
```

These values are an example, not universal solver or scientific defaults.
Jacobian minima must strictly exceed their declared limits.
Mesh quality must meet its declared minimum under the recorded native quality measure.
The passivity tolerance applies separately to dimensionless R, T and A.
The closure tolerance applies to the absolute difference between their sum and one.
Constraint residuals use each declared expression unit and the optimizer configuration tolerance.
Failed mapping and constraint checks retain their measured values in the runtime receipt.
The submission resource policy must declare `max_mesh_elements`.
The runtime reserves every finalist solve and checks each solved dimension and mapping.
The new durable path requires separate native acceptance before release.
Fixed loading does not establish adaptive continuation, mesh convergence or physical acceptance.
Review raw reflectance, transmittance and absorption separately from their algebraic sum.

Standalone MIM recipes check each power component and the requested, evaluated and solved wavelengths.
The dispersive recipes solve one requested wavelength at a time.
The continuous-film Drude example uses the linked global material loss convention verified for that formulation.
Use the partitioned-patch example for patterned films.
Its fallback mesh copies every exterior periodic source face to its paired destination face.
The partitioned-patch example declares an opaque impedance termination and checks R+A closure.
Its reported zero transmission is a boundary assumption, not a port measurement.
The continuous dielectric example retains its measured transmission.
All examples publish a completed Save Copy without moving a model file that COMSOL still owns.
These checks do not establish mesh convergence or research-device acceptance.

For the experimental `electro_chemistry` profile, retain the solver-free
registry/schema/profile tests and a separate exact-version COMSOL 6.4 probe
receipt. The probe may create and inspect bounded derived objects, but must not
run a scientific solve unless a separate caller-owned scientific policy is
authorized. Record module/interface findings, rollback, lease release, and
process cleanup. Do not treat upstream COMSOL 6.3 records as release evidence.
