# Profile and feature-gate migration

The server defaults to the compact `core` profile. Profiles now control only
the visibility of COMSOL automation/simulation tools and future
autonomous-exploration tools. Orthogonal functionality is represented by
independent, default-off Boolean feature gates.

| Need | Profile or feature | Support |
| --- | --- | --- |
| Ownership, durable jobs, model inspection, one-point solve, lexical manuals | `core` profile | Verified default |
| Typed conventional FEM construction, bounded exports, and Python-free launcher control | `basic_fem` profile | Verified |
| Periodic Wave Optics preflight, evidence audit, visual-review contracts | `wave_optics` profile | Experimental; licensed acceptance is version/model-specific |
| Generic or risky legacy helpers | `experimental` profile | Experimental |
| Maximum legacy discovery compatibility | `full` profile | Compatibility only |
| Isolated vector-assisted manuals | `semantic_docs.enabled=true` | Experimental; promotion rejected |
| User-owned local Desktop/Server collaboration | `shared_server.enabled=true` | Experimental; default-off, local-only, explicit confirmation |

The two feature gates compose with every profile and with each other. Set them
in the same shared `settings.json`, restart the MCP host, then confirm
`active_profile`, `enabled_features`, exact tool names, schemas, and deployment
hashes through `capabilities` and live discovery.

Profiles are immutable for the lifetime of one MCP host process. Independent
feature-gate selections are likewise startup-only; changing either kind of
selection requires a fresh host before discovery can reflect the new surface.

Settings schema `1.2.0` reads older files without rewriting the source during
discovery. The canonical migration rules are:

| Legacy `profile.name` | Effective 1.2 configuration |
| --- | --- |
| `semantic_docs` | `profile.name=core`, `semantic_docs.enabled=true` |
| `desktop_shared` | `profile.name=core`, `shared_server.enabled=true` |
| `full` | Retain `profile.name=full`, set `semantic_docs.enabled=true` to preserve the former broad tool surface |
| Any other unsupported value | Fall back to `profile.name=core` and report bounded fallback/error provenance |

Explicit Boolean values already present in the settings file remain
authoritative. New files default both features to `false`. A direct unsupported
profile argument also falls back to `core`; it does not silently enable a
legacy feature alias.

Migration sequence:

1. Start with `core` and call `capabilities`.
2. If simulation tools are absent, move to `basic_fem` or `wave_optics` before
   considering `full`.
3. Enable semantic or shared functionality with its Boolean feature gate,
   independently of the selected profile.
4. Restart and confirm the exact registered surface and fallback provenance.
5. Keep semantic retrieval opt-in; it is not verified as multilingual and must
   not replace the lexical production path.

For shared Desktop use, enable `shared_server.enabled`, start the Server
manually, connect Desktop, run `shared_server_preflight`, and provide explicit
confirmation to `shared_server_attach`. The workflow never replaces or
terminates the user's Server. The experimental `comsol_connect` tool remains a
legacy compatibility surface and is not a substitute for this lifecycle.
