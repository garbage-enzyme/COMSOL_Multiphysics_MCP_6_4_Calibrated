# Profile migration and support

The server now defaults to the compact `core` profile. Existing clients that
relied on broad discovery must select the narrowest profile containing their
workflow before the MCP process starts.

| Need | Profile | Support |
| --- | --- | --- |
| Ownership, durable jobs, model inspection, one-point solve, lexical manuals | `core` | Verified default |
| Typed conventional FEM construction and bounded exports | `basic_fem` | Verified |
| Periodic Wave Optics preflight, evidence audit, visual-review contracts | `wave_optics` | Experimental; licensed acceptance is version/model-specific |
| Isolated vector-assisted manuals | `semantic_docs` | Experimental; promotion rejected |
| User-owned local Desktop/Server collaboration | `desktop_shared` | Experimental; default-off, local-only, explicit confirmation |
| Generic or risky legacy helpers | `experimental` | Experimental |
| Maximum legacy discovery compatibility | `full` | Compatibility only |

Set `profile.name` in the shared project-root `settings.json`; see the
[settings guide](setting_guide/README.md) for the complete field reference. Reinstall
non-editably after source changes, and restart the host. Profiles are immutable
for a server process. A deleted entry uses `core`; an invalid value remains at
`core` and is reported through `project_settings.settings_errors`.

Migration sequence:

1. Start with `core` and call `capabilities`.
2. If required tools are absent, move to `basic_fem` or `wave_optics` rather
   than directly to `full`.
3. Restart and confirm the exact tool names, schemas, and deployment hashes
   through discovery.
4. Keep `semantic_docs` opt-in. It is not verified as multilingual and must not
   replace the lexical production path.
5. Use `full` only for migration/debugging, then record the narrower profile
   needed by the stable workflow.

The release support matrix records expected profile counts, version identities,
unavailable claims, and the real-integration policy. Live `capabilities` and
tool discovery remain authoritative for an installed process.

The `desktop_shared` profile implements the protected non-owning local
Desktop/attached-Server lifecycle. Set `profile.name` to `desktop_shared` and
`shared_server.enabled` to `true` in the same `settings.json`, restart the MCP
host, start the Server manually, connect Desktop, run
`shared_server_preflight`, and provide explicit confirmation to
`shared_server_attach`. It never replaces or terminates the user's Server. The
experimental `comsol_connect` tool remains legacy compatibility and is not a
substitute for this lifecycle.
