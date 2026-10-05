"""Solver-free progressive discovery for the public MCP surface.

Why this module exists
----------------------

Embedding the complete artifact-schema registry in ``capabilities`` made the cold
discovery payload scale with the artifact surface rather than the tool surface:
at 0.7.5 the registry was 60,293 B of an 81,845 B response (74%), growing by
roughly 300 B per registered schema while the client needed none of it to start
work. The fix is progressive delivery, not truncation:

* ``capabilities`` keeps the compact identity/count view of the registry;
* this module serves a compact domain/tool catalog, then one domain, one tool, or
  one schema on demand; and
* the full registry stays authoritative in the server and is bound to the same
  deployment identity, so an on-demand fetch cannot return a different schema set
  than the fingerprint already advertised.

Design constraints taken from the plan
--------------------------------------

* No hidden truncation and no silent compression. Every response states what it
  contains and which selector produced it.
* An unknown domain, tool, or schema is an explicit refusal. A caller that asked
  for something that does not exist must never receive a plausible substitute.
* Schemas are returned exactly as the server advertises them to MCP clients, so
  the on-demand view and the wire contract cannot drift apart.
* Nothing here imports COMSOL, MPh, or any heavy optional dependency, so cold
  discovery stays solver-free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from comsol_mcp.schema_registry import (
    get_schema_registry,
    select_schema_entry,
    summarize_schema_registry,
)

from .catalog import PROFILE_NAMES, TOOL_METADATA
from .profiles import ProfileSelection, resolve_profile

#: The exact selector keys this operation accepts. Declared so the refusal path
#: and the tests agree on one closed set rather than an open-ended query surface.
SELECTOR_NAMES = ("domain", "tool", "schema")

#: Bound on how many tool rows one domain page may carry. A domain fetch returns
#: full advertised schemas, so the page is bounded and states its own offset.
MAX_CATALOG_TOOLS = 32

#: Hard cap on a caller-requested page size.
MAX_CATALOG_PAGE = 64

CATALOG_SCHEMA_NAME = "comsol_mcp.tool_catalog"
CATALOG_SCHEMA_VERSION = "1.0.0"


def _profile_tool_names(selection: ProfileSelection) -> frozenset[str]:
    """Tool names reachable under one startup selection, without importing tools."""
    enabled_features = frozenset(selection.enabled_features)
    return frozenset(
        name
        for name, metadata in TOOL_METADATA.items()
        if selection.name in metadata.intended_profiles
        and (metadata.feature_gate is None or metadata.feature_gate in enabled_features)
    )


def _tool_summary(name: str, *, reachable: bool) -> dict[str, Any]:
    """One compact tool row: identity, risk, and reachability, but no schema."""
    metadata = TOOL_METADATA[name]
    return {
        "name": name,
        "group": metadata.group,
        "maturity": metadata.maturity,
        "side_effect_class": metadata.side_effect_class,
        "concurrency_class": metadata.concurrency_class,
        "starts_solver": metadata.starts_solver,
        "requires_model_revision": metadata.requires_model_revision,
        "advances_model_revision": metadata.advances_model_revision,
        "feature_gate": metadata.feature_gate,
        "input_contract": metadata.input_contract,
        "output_contract": metadata.output_contract,
        "reachable_in_active_profile": reachable,
    }


def _domain_rows(reachable: frozenset[str]) -> list[dict[str, Any]]:
    """Compact per-domain rows, complete over the whole registry."""
    groups: dict[str, dict[str, Any]] = {}
    for name, metadata in TOOL_METADATA.items():
        row = groups.setdefault(
            metadata.group,
            {
                "domain": metadata.group,
                "tool_count": 0,
                "reachable_tool_count": 0,
                "solver_starting_tool_count": 0,
                "feature_gates": set(),
                "maturities": set(),
            },
        )
        row["tool_count"] += 1
        if name in reachable:
            row["reachable_tool_count"] += 1
        if metadata.starts_solver:
            row["solver_starting_tool_count"] += 1
        if metadata.feature_gate is not None:
            row["feature_gates"].add(metadata.feature_gate)
        row["maturities"].add(metadata.maturity)
    rows: list[dict[str, Any]] = []
    for row in sorted(groups.values(), key=lambda item: item["domain"]):
        rows.append(
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key not in {"feature_gates", "maturities"}
                },
                "feature_gates": sorted(row["feature_gates"]),
                "maturities": sorted(row["maturities"]),
            }
        )
    return rows


def _refusal(reason_code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Build the shared bounded refusal body."""
    return {
        "success": False,
        "reason_code": reason_code,
        "message": message,
        "selectors": list(SELECTOR_NAMES),
        **extra,
    }


def _domain_response(
    domain: str,
    reachable: frozenset[str],
    advertised: Mapping[str, Any] | None,
    *,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """Return one domain's tools with their full advertised schemas.

    A domain is served in an explicitly bounded page. The response always states
    the total, the returned count, and the exact offset to continue from, so a
    partially returned domain is never mistaken for a complete one. Nothing is
    truncated silently and no page is served without a way to reach the rest.
    """
    names = sorted(name for name, metadata in TOOL_METADATA.items() if metadata.group == domain)
    if not names:
        return _refusal(
            "unknown_domain",
            "unknown tool domain; the summary lists every valid domain",
            requested_domain=domain,
            known_domains=sorted({metadata.group for metadata in TOOL_METADATA.values()}),
        )
    if offset >= len(names) and names:
        return _refusal(
            "offset_out_of_range",
            "the requested offset is past the end of this domain",
            requested_domain=domain,
            domain_tool_count=len(names),
            requested_offset=offset,
        )
    page = names[offset : offset + limit]
    rows = []
    schema_unavailable = []
    for name in page:
        row = _tool_summary(name, reachable=name in reachable)
        schema = None
        if advertised is not None:
            candidate = advertised.get(name)
            schema = dict(candidate) if isinstance(candidate, Mapping) else None
        if schema is None:
            schema_unavailable.append(name)
        row["input_schema"] = schema
        rows.append(row)
    next_offset = offset + len(page)
    return {
        "success": True,
        "selector": "domain",
        "domain": domain,
        "tool_count": len(names),
        "returned_tool_count": len(rows),
        "offset": offset,
        "limit": limit,
        "next_offset": next_offset if next_offset < len(names) else None,
        "complete": next_offset >= len(names),
        "schemas_unavailable_for": schema_unavailable,
        "input_schema_source": "registered_tool_manager",
        "tools": rows,
        "registry_sha256": get_schema_registry()["registry_sha256"],
    }


def _tool_response(
    tool: str,
    reachable: frozenset[str],
    advertised: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    if tool not in TOOL_METADATA:
        return _refusal(
            "unknown_tool",
            "unknown tool name; the summary lists every valid domain",
            requested_tool=tool,
        )
    metadata = TOOL_METADATA[tool]
    # The schema is returned exactly as the deployment advertises it to MCP
    # clients, so this view and the wire contract cannot drift apart. A tool that
    # is registered but absent from the supplied surface reports that honestly
    # rather than fabricating or omitting the field silently.
    schema = None
    if advertised is not None:
        candidate = advertised.get(tool)
        schema = dict(candidate) if isinstance(candidate, Mapping) else None
    return {
        "success": True,
        "selector": "tool",
        "tool": {
            **_tool_summary(tool, reachable=tool in reachable),
            "intended_profiles": list(metadata.intended_profiles),
            "required_features": list(metadata.required_features),
            "artifact_path_classes": list(metadata.artifact_path_classes),
            "structural_limits": [list(item) for item in metadata.structural_limits],
            "replacement_tool": metadata.replacement_tool,
            "deprecation_state": metadata.deprecation_state,
        },
        "input_schema": schema,
        "input_schema_available": schema is not None,
        "input_schema_source": "registered_tool_manager",
        "registry_sha256": get_schema_registry()["registry_sha256"],
    }


def _schema_response(schema: str) -> dict[str, Any]:
    selected = select_schema_entry(schema)
    if not selected["found"]:
        return _refusal(
            selected["reason_code"],
            "unknown schema name; the summary carries the exact entry count",
            requested_schema=schema,
            entry_count=get_schema_registry()["entry_count"],
        )
    return {
        "success": True,
        "selector": "schema",
        "entry": selected["entry"],
        "registry_sha256": selected["registry_sha256"],
    }


def _advertised_tools(surface: Any | None) -> Mapping[str, Any] | None:
    """Read the input schema of every registered tool from the live surface.

    Deliberately reads the surface the client is served from: progressive
    discovery must return the schema the client would actually receive, never a
    copy that can drift from the wire contract. ``None`` means no live surface was
    supplied, which the tool response reports as an unavailable schema rather
    than substituting a guess.
    """
    reader = getattr(surface, "advertised_input_schemas", None)
    if not callable(reader):
        return None
    advertised = reader()
    return advertised if isinstance(advertised, Mapping) else None


def get_tool_catalog(
    selection: ProfileSelection | None = None,
    *,
    domain: str | None = None,
    tool: str | None = None,
    schema: str | None = None,
    offset: int = 0,
    limit: int = MAX_CATALOG_TOOLS,
    surface: Any | None = None,
) -> dict[str, Any]:
    """Return the compact catalog or one bounded on-demand expansion.

    With no selector the response is the domain summary plus the schema
    fingerprint. Exactly one selector may be supplied; supplying more than one is
    refused rather than resolved by precedence, because a caller that asked two
    different questions must not silently receive one answer.
    """
    supplied = [
        name
        for name, value in (("domain", domain), ("tool", tool), ("schema", schema))
        if value is not None
    ]
    if len(supplied) > 1:
        return _refusal(
            "multiple_selectors",
            "supply at most one selector per call",
            supplied_selectors=supplied,
        )
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return _refusal(
            "invalid_offset", "offset must be a non-negative integer", requested_offset=offset
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CATALOG_PAGE:
        return _refusal(
            "invalid_limit",
            f"limit must be an integer between 1 and {MAX_CATALOG_PAGE}",
            requested_limit=limit,
            maximum_limit=MAX_CATALOG_PAGE,
        )
    active = selection or resolve_profile()
    reachable = _profile_tool_names(active)
    if supplied:
        selector = supplied[0]
        raw = {"domain": domain, "tool": tool, "schema": schema}[selector]
        if not isinstance(raw, str) or not raw.strip():
            return _refusal(
                "invalid_selector_value",
                "the selector value must be a non-empty string",
                selector=selector,
            )
        value = raw.strip()
        if selector == "domain":
            return _domain_response(
                value, reachable, _advertised_tools(surface), offset=offset, limit=limit
            )
        if selector == "tool":
            return _tool_response(value, reachable, _advertised_tools(surface))
        return _schema_response(value)
    return {
        "success": True,
        "selector": None,
        "profile": active.name,
        "enabled_features": list(active.enabled_features),
        "tool_count": len(TOOL_METADATA),
        "reachable_tool_count": len(reachable),
        "domain_count": len({metadata.group for metadata in TOOL_METADATA.values()}),
        "domains": _domain_rows(reachable),
        "available_profiles": list(PROFILE_NAMES),
        "schema_registry": summarize_schema_registry(),
        "selectors": list(SELECTOR_NAMES),
        "registry_sha256": get_schema_registry()["registry_sha256"],
    }


def iter_reachable_tool_names(selection: ProfileSelection) -> Iterable[str]:
    """Deterministic reachable tool names for one selection."""
    return sorted(_profile_tool_names(selection))


__all__ = [
    "CATALOG_SCHEMA_NAME",
    "CATALOG_SCHEMA_VERSION",
    "MAX_CATALOG_PAGE",
    "MAX_CATALOG_TOOLS",
    "SELECTOR_NAMES",
    "get_tool_catalog",
    "iter_reachable_tool_names",
]
