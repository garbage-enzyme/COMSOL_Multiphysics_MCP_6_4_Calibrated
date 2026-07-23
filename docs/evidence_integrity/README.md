# Evidence integrity and AI-hallucination resistance

AI assistants are useful for COMSOL work, but a fluent answer can still describe
the wrong run, mix old and new files, omit a failed point, repeat a result after
the model changed, or treat a plausible plot as stronger evidence than it is.
This MCP reduces those risks by making formal conclusions resolve to declared
configuration, raw evidence, artifact bytes, revisions, and software identity.
It does not make an AI infallible, and it does not replace a correct COMSOL
model, mesh convergence, physical validation, or expert review.

Evidence-integrity checks are user-controlled and **enabled by default**. An
individual check can be explicitly disabled for exploration, but the result is
then visibly unverified. The optional shared Desktop/Server collaboration mode
has a different default: it is disabled until explicitly enabled.

## One-page quick start

1. Keep formal artifacts under the configured `paths.artifact_write_root`,
   normally the `owned_artifacts` directory beneath `runtime.directory`. Use an
   absolute ASCII-only directory when overriding the template value.
2. With the project-root `settings.json`, call `evidence_integrity_status`. All
   four checks should report `enabled: true`, `source: project_settings`, and
   `strict_verification_active: true`.
3. Run exploratory work normally. Preserve raw and diagnostic rows; do not
   delete a failed or partial point to make a later summary look cleaner.
4. Build a `comsol_mcp.portfolio_evidence_request` that contains the exact
   outcome contract, artifact-chain manifest, and summary citations. Each claim
   cites an artifact ID, its SHA-256, and a JSON Pointer to the exact value.
5. Call `evidence_integrity_verify` with that request and one contained artifact
   root for every case. For a resumed result, also pass exact expected and
   observed producer/driver identities.
6. Accept a formal label only when the returned receipt says
   `verification_state: verified` and `strictly_verified: true`. Save the
   settings fingerprint, request hash, verification hash, and cited artifacts.
7. If inputs or artifacts change, create a new run identity and verify again.
   Re-enabling a check never upgrades an old unverified receipt in place.

Both public guard tools are solver-free and available in every static profile:

- `evidence_integrity_status` reports effective settings without revealing the
  settings path;
- `evidence_integrity_verify` performs deterministic formal verification and
  does not start COMSOL or mutate a model.

## Why these controls are needed

A correct-looking number can have several independent failure modes. The solve
may have completed but used the wrong model. The raw result may be correct but
the summary may cite another attempt. The computation and evidence may both be
complete while the declared scientific tolerance still fails. A screenshot may
show a field pattern without proving polarization, energy closure, or a mode
identity.

This project therefore keeps three outcomes separate:

| Outcome | Question | Example states |
| --- | --- | --- |
| Execution | Did the requested work terminate correctly? | `completed`, `failed`, `interrupted`, `cancelled` |
| Evidence | Are the required raw bytes and links present and valid? | `complete`, `incomplete`, `invalid` |
| Scientific disposition | What does the declared policy conclude? | `accepted`, `residual`, `unresolved_at_declared_cap`, `invalid_evidence`, `not_evaluated` |

`completed` is not a synonym for `accepted`. Likewise, `cancel requested` is not
the terminal `cancelled` state: terminal cancellation requires worker,
descendant, port, and lease cleanup proof.

## Protections and what the user sees

| Risk | Control | MCP implementation | User-visible evidence |
| --- | --- | --- | --- |
| A successful call is presented as scientific success | Outcome separation | Versioned outcome contracts validate execution, evidence completeness, cleanup, and scientific disposition independently | Separate machine-readable states and reason codes |
| Old rows are reused after an input or model change | Immutable configuration and source identity | Normalized configuration, source hash when declared, policy, model revision, software/build identity, and resume identity are hash-bound | Configuration/request hashes, source/build identity, revision receipts, fail-closed mismatch |
| Partial or failed rows disappear from a polished summary | Durable raw evidence | Durable jobs persist each point before summaries using atomic state or append-only/hash-chained journals, flush, `fsync`, and attempt identity | Raw rows, diagnostic labels, checkpoints, and exact artifact hashes |
| A cited file is replaced or reordered | Artifact-chain verification | `artifact_chain_verification` checks manifests, dependency closure, byte counts, schemas, row order, and SHA-256 under the owned root | Per-check receipt hash, verified artifact count, no private path |
| The AI invents a nearby peak, fit, mesh count, or wavelength | Exact summary-claim verification | `summary_claim_verification` resolves each claim to the cited artifact hash and JSON Pointer and compares canonical JSON values | Claim/check state and exact request/verification hashes |
| A resume mixes incompatible software or driver logic | Producer/driver compatibility | `producer_driver_compatibility` requires exact producer, producer version, schema version, and driver SHA-256 when `resumed: true` | `passed`, `failed`, or `not_applicable`; mismatch fields never migrate silently |
| Desktop changes a model during agent automation | Model revision and external-change guards | Serialized operations, shared-model lock identity, and expected revision/readback checks | Lock/revision hashes and explicit changed fields |
| A cancellation is claimed before cleanup | Cancellation and cleanup proof | Durable cancellation reconciles exact worker/descendant identities, port, lease, and attached-resource preservation | Terminal cleanup evidence; attached Server/Desktop/model remain non-owned |
| A caller escapes a root or overwrites evidence | Path and overwrite containment | Configured read/owned-write roots reject traversal, device/reserved names, links/junctions, aliases, and caller-selected overwrite | Redacted path-policy decision and stable root IDs |
| A screenshot or label is treated as physics proof | Physical and visual evidence gates | Applicable tools retain raw R/T/A, closure, synchronization, mesh/material/field evidence and calibrated visual-review contracts | `measured`, `unknown`, `diagnostic`, or policy result instead of a guessed claim |
| A user disables protection but the warning disappears | Effective-settings and warning propagation | Capabilities and public guarded responses carry the effective fingerprint; disabled checks force `strictly_verified: false` | Disabled-check list and stable warning code in responses and formal receipts |
| Invalid settings trigger a guessed fallback | Bounded settings fallback and machine-readable error | Missing settings use defaults; an invalid setting uses only its own default; malformed JSON uses the complete safe default; artifact/identity mismatches still fail closed | `project_settings.settings_errors`, bounded reason code, and no silent default-off behavior |

These checks are deterministic code checks. Some facts come from COMSOL/clientapi
readback, such as model and revision state. Other facts are caller declarations,
such as a scientific policy or domain interpretation. AI-authored prose is an
interpretation layer only; it is not a trust root. If a user declaration has no
independent readback, the receipt can prove which declaration was used but not
that it was truthful.

## Default-on settings and explicit opt-out

The project-root `settings.json` is the canonical settings file. See the
[settings guide](../setting_guide/README.md) for the full settings reference. Its
`evidence_integrity.checks` object contains all four checks and their defaults:

```json
{
  "evidence_integrity": {
    "checks": {
      "outcome_contract_validation": true,
      "artifact_chain_verification": true,
      "summary_claim_verification": true,
      "producer_driver_compatibility": true
    }
  }
}
```

Only an explicit JSON boolean `false` disables a check. A deleted check returns
to `true`. For exploration, change only the relevant value in the shared file,
for example set `summary_claim_verification` to `false`; affected responses
carry `strictly_verified: false` and the stable warning. Evidence-only changes
are read on each status or guarded call, but restoring a check still requires a
**fresh verification against unchanged artifacts** and never relabels an old
receipt.

When a setting has an illegal character, wrong type, or unsupported value, only
that setting uses its documented default and `project_settings.settings_errors`
reports the key and reason code. If the JSON is malformed or unreadable, the
complete safe default document is used and the error is reported. This is
different from an artifact or identity mismatch: those verification inputs still
fail closed. The old `COMSOL_MCP_EVIDENCE_SETTINGS_PATH` file and the checked-in
`default_settings.json`/`exploration_settings.json` remain one-release
compatibility fixtures, not the normal multi-agent configuration source.

Representative capability output:

```json
{
  "evidence_integrity": {
    "configuration_state": "valid",
    "default_enabled": true,
    "strict_verification_active": true,
    "settings_source": "project_settings",
    "settings_fingerprint_sha256": "<64 lowercase hexadecimal characters>",
    "settings_path_included": false,
    "checks": {
      "outcome_contract_validation": {"enabled": true, "source": "project_settings"},
      "artifact_chain_verification": {"enabled": true, "source": "project_settings"},
      "summary_claim_verification": {"enabled": true, "source": "project_settings"},
      "producer_driver_compatibility": {"enabled": true, "source": "project_settings"}
    },
    "tools": ["evidence_integrity_status", "evidence_integrity_verify"]
  }
}
```

When any check is disabled, affected responses and formal receipts carry the
stable code `strict_evidence_checks_disabled`, list the disabled checks, and
include this exact warning:

> Strict evidence checks are disabled; these results were not fully verified and may contain AI-generated or hallucinated content.

## From exploration to a verified result

Consider a neutral parameter study. Exploration finds a candidate peak. The
agent first persists exact point JSON files, including the normalized
configuration ID, evaluated wavelength, mesh evidence, and measured values. A
manifest binds those files and a derived fit to their hashes. The outcome
contract says separately that execution completed, evidence is complete, and a
declared scientific policy accepted the case. The summary then cites, for
example, `/evidence/wavelength_m` in one exact artifact and
`/fit/quality_factor` in another.

`evidence_integrity_verify` performs the enabled checks and returns a compact
receipt such as:

```json
{
  "schema_name": "comsol_mcp.evidence_integrity_verification",
  "schema_version": "1.0.0",
  "success": true,
  "verification_state": "verified",
  "strictly_verified": true,
  "reason_code": "all_enabled_checks_passed",
  "request_sha256": "<request hash>",
  "check_results": {
    "outcome_contract_validation": {"state": "passed"},
    "artifact_chain_verification": {"state": "passed"},
    "summary_claim_verification": {"state": "passed"},
    "producer_driver_compatibility": {
      "state": "not_applicable",
      "reason_code": "fresh_verification_not_resume"
    }
  },
  "paths_included": false,
  "verification_sha256": "<receipt hash>"
}
```

For a resumed result, set `resumed: true` and supply matching `expected` and
`observed` producer identities with `producer`, `producer_version`,
`schema_version`, and `driver_sha256`. Missing or mismatched resume identity
fails verification.

If someone changes the cited value, replaces an artifact, removes a failed row,
or disables summary verification, the data may still be useful for exploration
but cannot receive the strict label. A disabled-check response looks like:

```json
{
  "success": true,
  "verification_state": "unverified",
  "strictly_verified": false,
  "reason_code": "checks_disabled_by_settings",
  "disabled_evidence_checks": ["summary_claim_verification"],
  "evidence_integrity_warning_codes": ["strict_evidence_checks_disabled"],
  "evidence_integrity_warnings": [
    "Strict evidence checks are disabled; these results were not fully verified and may contain AI-generated or hallucinated content."
  ]
}
```

## You can / You cannot

You can:

- explore quickly, preserve the raw data, then request fresh formal verification;
- select the exact source, configuration, policy, revision, and owned artifact root;
- inspect `capabilities` or `evidence_integrity_status` before a run and retain
  the settings fingerprint;
- inspect raw and diagnostic rows, artifact hashes, build identity, policy
  decisions, and exact claim citations;
- cancel through supported job controls and wait for a verified terminal state;
- share a redacted receipt and cited artifacts for another solver-free verification;
- explicitly disable a check for exploration while preserving its warning.

You cannot obtain or preserve `strictly_verified: true` by:

- asking the AI to infer missing configuration, raw values, failed points,
  source identity, policy, or artifact bytes;
- editing a model, artifact, manifest, settings file, row, or summary and citing
  the old receipt;
- treating a completed solve, plot, screenshot, label, fit, or attractive
  number as sufficient evidence by itself;
- mixing runs, attempts, models, builds, or producer/driver identities without
  a supported explicit migration;
- treating a cancellation request, missing PID, or disconnected GUI as cleanup proof;
- changing the Desktop model during automation-exclusive work and keeping the
  old revision;
- disabling a check, hiding the warning, or re-enabling it and upgrading an old receipt;
- claiming that provenance or hashes prove physical correctness, convergence,
  polarization, power closure, or publication readiness without those data.

On refusal, preserve the diagnostic evidence. Restore the check, correct the
input, run only the missing deterministic verification if every artifact is
unchanged, or create a new run identity if an input changed. Never delete a
failed row merely to make verification pass.

## What `strictly_verified` means

`strictly_verified: true` means that every effective check was enabled and all
applicable deterministic checks passed against the exact request and artifact
bytes named by that receipt. It is tied to the settings fingerprint, request
hash, artifact hashes, and verification hash.

It does **not** mean that the equations, boundary conditions, material data,
mesh, port conventions, polarization interpretation, or scientific policy are
correct. Hashes detect change and substitution; they do not validate physics.
Independent reviewers should inspect the raw rows and model assumptions, rerun
the solver-free verification, and perform the project-specific convergence and
physical validation required by the claim.

A verified receipt becomes invalid evidence for a changed result if any cited
artifact, manifest, model/configuration identity, settings fingerprint, policy,
producer/driver identity, or summary claim changes.

## Troubleshooting decision table

| Observation | Meaning | Safe next action |
| --- | --- | --- |
| `configuration_state: degraded` | One or more settings used safe defaults after validation reported an error | Correct the file, inspect `settings_errors`, and restart if a static setting changed; do not add a competing config source |
| `outcome_contract_validation: failed` | Execution/evidence/scientific states or hash are inconsistent | Repair the contract from retained raw state; do not invent cleanup or acceptance |
| `artifact_chain_verification: failed` | A manifest, dependency, byte count, schema, or hash differs | Preserve both versions, restore exact intended bytes, or create a new chain identity |
| `summary_claim_verification: failed` | A claim is absent at the cited hash and JSON Pointer | Correct the summary or citation and verify again |
| `producer_driver_compatibility: failed` | Resume producer, schema, or driver identity changed | Use a supported migration or create a new attempt; never relabel stale rows |
| `resume_compatibility_missing` | A resumed result lacks exact compatibility evidence | Supply expected and observed identities or treat it as a fresh run |
| `partial`, `diagnostic`, or `incomplete` | Useful data exists but formal evidence is not complete | Keep it labeled; acquire only the missing evidence |
| `strict_evidence_checks_disabled` | Exploration opt-out is active | Keep the warning, restore checks, and run a fresh formal verification |
| Artifact root rejected | The directory is outside the configured owned root or uses an unsafe alias | Move/copy evidence through the supported owned workflow; do not weaken containment |
| `unresolved_at_declared_cap` or `residual` | A valid non-acceptance outcome, not execution failure | Report it exactly and change the declared policy/cap only as a new decision |

## Threat model and limits

The controls detect declared provenance and consistency failures. They do not
defend against a compromised operating system, malicious code that rewrites
both data and trust roots, an incorrect but internally consistent physics
model, an inadequate mesh, or a dishonest declaration with no independent
readback. They also do not turn AI review into numerical policy authority.

Safe assistant wording uses exact evidence states and citations. Say `unknown`
or `unavailable` when evidence is absent. Never silently rewrite `unverified`,
`diagnostic`, `partial`, `residual`, or `unresolved_at_declared_cap` as success.
