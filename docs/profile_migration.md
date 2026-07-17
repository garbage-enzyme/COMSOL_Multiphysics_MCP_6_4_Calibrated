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
| Generic or risky legacy helpers | `experimental` | Experimental |
| Maximum legacy discovery compatibility | `full` | Compatibility only |

Set `COMSOL_MCP_PROFILE` in the MCP host configuration, reinstall non-editably
after source changes, and restart the host. Profiles are immutable for a server
process. An invalid name fails startup; there is no silent fallback.

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

No current profile implements protected shared Desktop/attached-Server
operation. The experimental `comsol_connect` tool is legacy compatibility, not
a non-owning shared-model workflow.
