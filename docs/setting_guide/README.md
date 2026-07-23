# COMSOL MCP settings guide

`settings.json` is the shared startup configuration for every MCP client. Edit
it before starting the host, then restart the host after changing
`profile.name`, `shared_server.enabled`, or Java paths. Use
`capabilities.project_settings` to confirm the effective configuration without
exposing local paths.

The file must be UTF-8 JSON, contain one object, use no duplicate keys, and be
at most 64 KiB. The checked-in template intentionally contains no comments.
Unknown fields are reported in `settings_errors`. A missing field uses the safe
default below; an invalid field uses only its own default. Malformed or unreadable
JSON uses the complete safe default document.

## Document identity

| Field | Meaning | Default | Accepted value |
| --- | --- | --- | --- |
| `schema_name` | Identifies this settings schema. | `"comsol_mcp.settings"` | Exactly `"comsol_mcp.settings"`. |
| `schema_version` | Identifies the supported settings format. | `"1.0.0"` | Exactly `"1.0.0"`. |

## Profile

| Field | Meaning | Default | Accepted value |
| --- | --- | --- | --- |
| `profile.name` | Static public tool surface for this MCP process. | `"core"` | `"core"`, `"basic_fem"`, `"wave_optics"`, `"semantic_docs"`, `"desktop_shared"`, `"experimental"`, or `"full"` (case-insensitive; stored lower-case). |

`desktop_shared` also requires `shared_server.enabled: true`; it never starts
or terminates the user's COMSOL Server. `semantic_docs` requires its configured
retrieval assets to be available. Use `core` unless a larger surface is needed.

## Runtime and containment paths

All path settings below accept only an absolute path string when a path is
allowed. Empty strings, relative paths, and control characters are invalid.
Paths are normalized by the host platform. For Windows deployment roots and
durable artifacts, prefer ASCII-only paths.

| Field | Meaning | Default | Accepted value |
| --- | --- | --- | --- |
| `runtime.directory` | Root for server-owned runtime state. | `null` | `null` or an absolute path string. `null` selects the platform-safe runtime root. |
| `runtime.jobs_directory` | Root for durable job state. | `null` | `null` or an absolute path string. `null` selects the default below the effective runtime root. |
| `paths.model_read_roots` | Approved roots that tools may use for reading source models. | `[]` | A JSON array of absolute path strings, with no duplicate normalized paths. Keep it empty until an explicit model directory is approved. |
| `paths.artifact_write_root` | Root for MCP-owned artifacts, manifests, and evidence. | `null` | `null` or an absolute path string. `null` selects the default owned-artifact directory below the effective runtime root. |

An accepted path value is not proof that a later tool may use it: containment,
existence, link/junction, overwrite, and operation-specific checks still apply.

## Shared Desktop/Server mode

| Field | Meaning | Default | Accepted value |
| --- | --- | --- | --- |
| `shared_server.enabled` | Opens the shared Desktop/attached-Server gate when the `desktop_shared` profile is selected. | `false` | JSON `true` or `false`; strings such as `"true"` are invalid. |

Set this to `true` only after manually starting the local COMSOL Server. See the
[interactive shared-session guide](../interactive_shared_session/README.md) for
the required confirmation and ownership workflow.

## Evidence integrity

All four checks are enabled by default. Setting any one to `false` is an
exploration opt-out: affected formal results remain explicitly unverified.

| Field | Meaning | Default | Accepted value |
| --- | --- | --- | --- |
| `evidence_integrity.checks.outcome_contract_validation` | Verifies declared outcomes and their machine-readable contract. | `true` | JSON `true` or `false`. |
| `evidence_integrity.checks.artifact_chain_verification` | Verifies owned artifact bytes, provenance, and hash-chain identity. | `true` | JSON `true` or `false`. |
| `evidence_integrity.checks.summary_claim_verification` | Verifies summary claims against exact cited artifact values. | `true` | JSON `true` or `false`. |
| `evidence_integrity.checks.producer_driver_compatibility` | Verifies producer and driver identity before a resumed continuation. | `true` | JSON `true` or `false`. |

See the [evidence-integrity guide](../evidence_integrity/README.md) for the
effects of an opt-out and the verification workflow.

## Semantic manual retrieval

| Field | Meaning | Default | Accepted value |
| --- | --- | --- | --- |
| `semantic_docs.root` | Deployment root for isolated semantic retrieval assets. | `"D:/comsol_semantic"` | An absolute path string; `null` is invalid. |
| `semantic_docs.lexical_index` | Immutable SQLite lexical manual index. | `"D:/comsol_docs_fts/manuals.sqlite3"` | An absolute path string; `null` is invalid. |
| `semantic_docs.model_path` | Optional local semantic-model revision directory. | `null` | `null` or an absolute path string. `null` leaves semantic retrieval unavailable. |

The semantic profile remains unavailable until its required artifacts exist.

## Ownership and Java

| Field | Meaning | Default | Accepted value |
| --- | --- | --- | --- |
| `ownership.owner` | Optional stable label for the MCP solver owner. | `null` | `null`, or a non-empty string of at most 256 characters with no control characters. `null` derives a bounded label from the parent agent process. |
| `java.java_home` | Optional COMSOL-bundled Java runtime path. | `null` | `null` or an absolute path string. `null` preserves the host Java environment. |
| `java.jdk_home` | Optional JDK path supplied before ClientAPI import. | `null` | `null` or an absolute path string. It is commonly the same as `java.java_home` for the validated COMSOL runtime. |

## Example

This is a partial edit, not a replacement for the checked-in template:

```json
{
  "profile": {"name": "wave_optics"},
  "runtime": {"directory": "D:/comsol_runtime"},
  "paths": {
    "model_read_roots": ["D:/comsol_models"],
    "artifact_write_root": "D:/comsol_runtime/owned_artifacts"
  }
}
```

If a client cannot preserve the project root, set the one absolute file locator
`COMSOL_MCP_SETTINGS_PATH` to the desired regular, non-link `settings.json`
file. The locator file must already exist.
