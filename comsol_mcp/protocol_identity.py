"""Solver-free MCP SDK, protocol-revision, and extension wire identities.

Why three separate identities
-----------------------------

An MCP deployment has three independent compatibility facts, and conflating them
is how projects end up claiming support they do not have:

1. the **installed SDK version** — a PyPI distribution version, e.g. ``2.2.0``;
2. the **protocol revision** — a date-based revision, e.g. ``2026-07-28``;
3. the **extension wire generation** — a separately versioned extension such as
   ``io.modelcontextprotocol/tasks``, which is *not* implied by either of the
   other two.

Upgrading the SDK therefore does **not** implement an extension, and speaking a
new protocol revision does **not** mean the SDK implements every extension
defined against it. This module reports all three as distinct, checkable facts so
a receipt, a capability response, or a test can assert them separately.

Extension generations
---------------------

The Tasks extension is declared here as a generation map rather than a boolean,
because the two known generations are wire-incompatible: the experimental
``2025-11-25`` in-core dialect and the stable ``2026-07-28`` extension dialect
differ in opt-in mechanism, result envelope, field names, and method set. A
single "supports tasks" flag would hide that. The declared state for this release
is recorded explicitly so no caller can infer native Tasks support from an SDK
bump.

This module is deliberately import-light: it reads distribution metadata and
module attributes only. It never constructs an MCP session, never starts a
transport, and never imports a COMSOL client.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from comsol_mcp.durable import canonical_sha256_v1

#: The MCP extension identifier for the stable Tasks extension (SEP-2663).
TASKS_EXTENSION_IDENTIFIER = "io.modelcontextprotocol/tasks"

#: The stable protocol revision this project targets for modern-era requests.
TARGET_PROTOCOL_REVISION = "2026-07-28"

#: Protocol revisions whose wire behavior this project still serves. The list is
#: ordered oldest-to-newest and is the set a legacy client may negotiate.
SUPPORTED_LEGACY_REVISIONS = (
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
)

#: Extension generations, keyed by wire generation. ``None`` marks a generation
#: this release does not implement; a non-``None`` mapping records the exact
#: dialect facts that make the generation identifiable on the wire.
TASKS_WIRE_GENERATIONS: dict[str, dict[str, Any]] = {
    "2025-11-25": {
        "status": "not_implemented",
        "dialect": "experimental_in_core",
        "opt_in": "call_tool_task_parameter",
        "result_envelope": "nested_task_object",
        "ttl_field": "ttl",
        "retrieval_method": "tasks/result",
        "note": (
            "Experimental in-core dialect. Wire-incompatible with the stable "
            "extension; this project never emits it."
        ),
    },
    TARGET_PROTOCOL_REVISION: {
        "status": "implemented",
        "dialect": "stable_extension",
        "opt_in": "request_meta_client_capabilities_extension",
        "result_envelope": "flat_result_type_discriminator",
        "ttl_field": "ttlMs",
        "retrieval_method": "tasks/get",
        "note": (
            "Stable extension dialect, implemented in this release by a bounded "
            "adapter over the existing durable job engine. It is the only native "
            "dialect in scope; clients that do not opt in keep using the "
            "ordinary durable job tools."
        ),
    },
}


def installed_sdk_version() -> str | None:
    """Return the installed MCP SDK distribution version, or ``None``."""
    try:
        return version("mcp")
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def _importable(module_name: str, *attribute_names: str) -> tuple[Any, ...] | None:
    """Import a module and return the named attributes, or ``None`` if absent.

    A missing or broken optional hook is a *reportable* fact, not an error, so
    the probe returns ``None`` instead of propagating the import failure. A
    single narrow helper keeps that decision in one place rather than repeating
    a swallow at each probe site.
    """
    try:
        module = import_module(module_name)
    except Exception:
        return None
    try:
        return tuple(getattr(module, name) for name in attribute_names)
    except AttributeError:
        return None


def sdk_extension_capability() -> dict[str, Any]:
    """Report which public SDK extension hooks are importable.

    A project that cannot import these hooks cannot implement a standards
    extension without monkey-patching or forking the SDK, which the plan
    prohibits. Reporting the hooks as observable facts keeps that decision
    evidence-based rather than assumed.
    """
    capability: dict[str, Any] = {
        "extension_base_class": False,
        "method_binding": False,
        "tool_call_interceptor": False,
        "missing_client_capability_error_helper": False,
    }
    extension_parts = _importable("mcp.server.extension", "Extension", "MethodBinding")
    if extension_parts is not None:
        extension_class, method_binding = extension_parts
        capability["extension_base_class"] = isinstance(extension_class, type)
        capability["method_binding"] = isinstance(method_binding, type)
        capability["tool_call_interceptor"] = hasattr(extension_class, "intercept_tool_call")
    helper_parts = _importable("mcp.server.mcpserver", "require_client_extension")
    if helper_parts is not None:
        capability["missing_client_capability_error_helper"] = callable(helper_parts[0])
    return capability


def get_protocol_identity() -> dict[str, Any]:
    """Return the three separated MCP identity facts plus a stable digest."""
    sdk_version = installed_sdk_version()
    body = {
        "schema_name": "comsol_mcp.protocol_identity",
        "schema_version": "1.0.0",
        "identity_mode": "solver_free_metadata_only",
        "sdk": {
            "distribution": "mcp",
            "installed_version": sdk_version,
            "declared_range": ">=2.2.0,<2.3",
        },
        "protocol": {
            "target_revision": TARGET_PROTOCOL_REVISION,
            "supported_legacy_revisions": list(SUPPORTED_LEGACY_REVISIONS),
            "revision_is_not_sdk_version": True,
        },
        "extensions": {
            "tasks": {
                "identifier": TASKS_EXTENSION_IDENTIFIER,
                "native_implemented": True,
                "native_scope": "tools/call only, gated per request",
                "task_capable_tools": ["job_submit"],
                "declared_generations": sorted(TASKS_WIRE_GENERATIONS),
                "generations": {
                    generation: dict(details)
                    for generation, details in sorted(TASKS_WIRE_GENERATIONS.items())
                },
                "fallback": "ordinary_durable_job_tools",
            }
        },
        "sdk_extension_hooks": sdk_extension_capability(),
        "claim_boundary": {
            "sdk_bump_implements_tasks": False,
            "task_shaped_results_without_request_capability": False,
            "legacy_clients_use_ordinary_tools": True,
        },
    }
    return {**body, "protocol_identity_sha256": canonical_sha256_v1(body)}


__all__ = [
    "SUPPORTED_LEGACY_REVISIONS",
    "TARGET_PROTOCOL_REVISION",
    "TASKS_EXTENSION_IDENTIFIER",
    "TASKS_WIRE_GENERATIONS",
    "get_protocol_identity",
    "installed_sdk_version",
    "sdk_extension_capability",
]
