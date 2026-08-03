# Release checklist

The dependency-only gate must run from a clean tree and does not start COMSOL.
The Python 3.14 production lane additionally consumes its complete hash lock:

```powershell
python development_kit/scripts/release_gate.py `
  --dependency-lock constraints/release_locked_py314.txt
```

The gate compiles `src`, `tests`, and `scripts`; runs the default unit suite;
checks frozen tool/profile schemas; builds wheel and sdist artifacts; creates a
fresh virtual environment; installs the wheel non-editably; runs `pip check`;
and verifies installed discovery against the frozen snapshots. Discovery fails
if it starts `mph.Client` or imports ChromaDB, SentenceTransformer, or Torch.

Before a release:

1. Confirm `git status --short` is empty.
2. Run the dependency-only gate and archive its sanitized JSON report.
   When Settings GUI files change, also run `settings-gui-ci`, verify the
   deterministic locale, multi-size Windows icon, and package receipts, inspect
   the real 100%, 125%, 150%, and 200% three-language screenshots directly, and
   record the user's final rendered-interface acceptance before the GUI-bearing
   commit.
3. Confirm `development_kit/release/support_matrix.json` matches the intended
   version tuple.
4. For a Python runtime promotion, run the provenance-bound compatibility gate
   from the clean candidate commit:

   ```powershell
   python development_kit/scripts/python_compatibility_licensed_gate.py `
     --confirm RUN_REAL_COMSOL `
     --runtime-root D:\comsol_runtime `
     --output D:\comsol_release\python_compatibility.json
   ```

5. On a free, licensed, version-pinned host, run the serial real gate explicitly:

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
11. Restart the MCP host; source and profile changes are not hot-reloaded.
12. Call `capabilities`; require `deployment_identity.source_classification` to
   be `installed_site_package`, compare its profile/schema/catalog hashes with
   the clean release receipt, and then treat installed profile counts as
   authoritative. A matching version string alone is insufficient.

Hosted CI never runs licensed COMSOL integration tests. They remain explicit,
serial, and unavailable by default.
