# Release checklist

The dependency-only gate is reproducible and must run from a clean tree. It does
not start COMSOL:

```powershell
python scripts/release_gate.py
```

The gate compiles `src`, `tests`, and `scripts`; runs the default unit suite;
checks frozen tool/profile schemas; builds wheel and sdist artifacts; creates a
fresh virtual environment; installs the wheel non-editably; runs `pip check`;
and verifies installed discovery against the frozen snapshots. Discovery fails
if it starts `mph.Client` or imports ChromaDB, SentenceTransformer, or Torch.

Before a release:

1. Confirm `git status --short` is empty.
2. Run the dependency-only gate and archive its sanitized JSON report.
3. Confirm `release/support_matrix.json` matches the intended version tuple.
4. On a free, licensed, version-pinned host, run the serial real gate explicitly:

   ```powershell
   python scripts/run_real_release_gate.py --confirm RUN_REAL_COMSOL --output D:\comsol_release\real_gate.json
   ```

5. Require an unchanged COMSOL PID set, an absent solver lease, no external
   collision, source-integrity evidence, and all fixture contracts to pass.
6. Build once more from the clean release commit and compare discovery output.
7. Install non-editably in the target MCP environment.
8. Restart the MCP host; source and profile changes are not hot-reloaded.
9. Call `capabilities`; require `deployment_identity.source_classification` to
   be `installed_site_package`, compare its profile/schema/catalog hashes with
   the clean release receipt, and then treat installed profile counts as
   authoritative. A matching version string alone is insufficient.

Hosted CI never runs licensed COMSOL integration tests. They remain explicit,
serial, and unavailable by default.
