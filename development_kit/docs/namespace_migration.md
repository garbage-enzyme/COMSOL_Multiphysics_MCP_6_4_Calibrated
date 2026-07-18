# Canonical Python namespace

The shipped implementation package is `comsol_mcp`. The console entry point,
release tooling, schema producers, durable worker identities, and new command
strings use that namespace.

The `src` package is a compatibility interval only. Its alias finder maps
`src.*` imports to the already-loaded `comsol_mcp.*` module objects, so legacy
callers share the same session, ownership, and tool registries. It contains no
second implementation tree.

During the declared interval, readers accept legacy producer identifiers such
as `src.jobs.worker` and current `comsol_mcp.jobs.worker`. New durable records
write only the canonical identifier; accepted legacy records are not rewritten
in place.

Removal of the compatibility package is a separately reviewed future release
decision after the current consumer inventory and fixture migration are
complete. It is independent of any MCP SDK major-version migration.
