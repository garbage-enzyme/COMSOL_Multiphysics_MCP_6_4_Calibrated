# Legacy phase-code compatibility

Active implementation names are descriptive. The following phase-coded strings
remain only because they are published inputs, receipt aliases, or historical
durable-state compatibility markers:

- `comsol_mcp.h1_licensed_gate`, `comsol_mcp.h1_execution_spec`, and
  `comsol_mcp.h1_dry_run_receipt` are accepted legacy schema names for existing
  reference-power specifications and receipts.
- `--require-h1`, `--h1-spec`, `--h1-cores`, and
  `--h1-timeout-seconds` are hidden CLI aliases for the descriptive
  reference-power options.
- `require_h1` and `phases.h1` are retained receipt aliases beside
  `require_reference_power` and `phases.reference_power`.
- The schema-v1 durable cancel test retains the phrase `legacy_h1` because it
  verifies migration of already-written control artifacts.

No new schema, CLI, receipt, filename, module, fixture identifier, test name,
comment, owner label, runtime directory, or environment variable may introduce
a phase code. Historical receipts and external runtime evidence are immutable
and are not renamed.
