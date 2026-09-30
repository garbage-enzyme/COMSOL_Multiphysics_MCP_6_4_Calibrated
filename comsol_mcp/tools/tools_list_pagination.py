"""Experimental ``tools/list`` cursor adapter for progressive discovery.

Why an adapter and not a fork
-----------------------------

The stable 2026-07-28 core already carries ``nextCursor`` on
``ListToolsResult`` and ``cursor`` on ``PaginatedRequestParams``, and the SDK
exposes both plus a public middleware chain. The default SDK handler ignores the
cursor and always answers the complete list, which is correct for the clients the
handoff protects: MCP1 and OpenCode read one page and stop, so a server that
paged by default would silently hide tools from them.

This adapter is therefore:

* **default-off**, keyed on the ``discovery.pagination_enabled`` setting, which
  is itself default ``false``; and
* **non-destructive**: when off, the ``tools/list`` path is the untouched SDK
  handler, so the wire form is unchanged; when on, every page states
  ``totalTools``/``returnedTools``/``remainingTools`` in ``_meta`` alongside the
  cursor, so a first-page-only client still learns that more exist and can never
  mistake a page for the whole surface.

The middleware hook is documented as provisional ("the signature may change in a
2.x minor release"), so its shape is verified before use and a mismatch degrades
to the ordinary listing rather than breaking the session.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: Cursor prefix. Versioned so a cursor from a different page size or ordering is
#: refused instead of silently resolving to a wrong offset.
CURSOR_PREFIX = "tools-v1:"

#: Default page size for the experimental adapter.
DEFAULT_PAGE_SIZE = 25

#: Hard cap on a caller-supplied page size.
MAX_PAGE_SIZE = 100


class CursorError(ValueError):
    """A cursor that cannot be resolved to a deterministic offset."""


def encode_cursor(offset: int) -> str:
    """Encode a deterministic page cursor."""
    return f"{CURSOR_PREFIX}{offset}"


def decode_cursor(cursor: object) -> int:
    """Decode a cursor into a row offset, or refuse it.

    A malformed or foreign cursor is refused rather than treated as page one: a
    caller that supplied a cursor must never silently receive the first page as
    if it were the page it asked for.
    """
    if cursor is None:
        return 0
    if not isinstance(cursor, str) or not cursor.startswith(CURSOR_PREFIX):
        raise CursorError("unsupported tools/list cursor")
    raw = cursor[len(CURSOR_PREFIX) :]
    if not raw.isdigit():
        raise CursorError("malformed tools/list cursor")
    return int(raw)


def _tool_name(tool: Any) -> str:
    """Read a tool's name from either a typed model or its serialized form.

    The middleware chain sits *above* the runner's serialization step, so by the
    time a page is built the entries may already be wire dicts. Both shapes are
    accepted, and an entry whose name cannot be read is a hard error rather than
    an entry silently dropped from the page.
    """
    if isinstance(tool, Mapping):
        name = tool.get("name")
    else:
        name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name:
        raise CursorError("a tools/list entry has no readable name")
    return name


def page_tools(
    tools: list[Any],
    cursor: object,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[Any], str | None, dict[str, int]]:
    """Split one complete tool list into an ordered page plus its metadata.

    Returns the page, the next cursor (``None`` on the last page), and the
    accounting block. The accounting is always about the *whole* surface, so a
    partial page can never be presented as a complete one.

    Ordering is by name and total, so the same cursor always resolves to the same
    rows regardless of the order the registry happened to enumerate them in.
    """
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        raise CursorError("page size must be an integer")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise CursorError(f"page size must be between 1 and {MAX_PAGE_SIZE}")
    ordered = sorted(tools, key=_tool_name)
    offset = decode_cursor(cursor)
    total = len(ordered)
    if offset > total:
        raise CursorError("cursor is past the end of the tool list")
    window = ordered[offset : offset + page_size]
    end = offset + len(window)
    next_cursor = encode_cursor(end) if end < total else None
    return (
        window,
        next_cursor,
        {
            "totalTools": total,
            "returnedTools": len(window),
            "remainingTools": max(0, total - end),
        },
    )


def _middleware_supported() -> bool:
    """Whether the reviewed SDK still exposes the middleware and result shapes.

    Checked defensively because the middleware hook is documented as provisional.
    A mismatch disables the adapter instead of raising into a client session.
    """
    try:
        from mcp import types
        from mcp.server.mcpserver import MCPServer
    except Exception:
        return False
    if not isinstance(getattr(MCPServer, "middleware", None), property):
        return False
    fields = getattr(types.ListToolsResult, "model_fields", {})
    params = getattr(types.PaginatedRequestParams, "model_fields", {})
    return "next_cursor" in fields and "cursor" in params


def _decode_result(result: Any) -> dict[str, Any] | None:
    """Return the handler result as a mutable mapping, or ``None``."""
    if isinstance(result, dict):
        return dict(result)
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        try:
            value = dump(by_alias=True, exclude_none=True, mode="json")
        except Exception:
            return None
        if isinstance(value, dict):
            return value
    return None


def _page_payload(payload: dict[str, Any], cursor: object, page_size: int) -> dict[str, Any]:
    """Page one serialized ``tools/list`` payload in place-safe fashion."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return payload
    if not tools:
        # An empty surface has no cursor and no accounting to add: there is no
        # page boundary to describe, so it is returned exactly as produced.
        return payload
    window, next_cursor, accounting = page_tools(tools, cursor, page_size=page_size)
    payload["tools"] = window
    if next_cursor is None:
        payload.pop("nextCursor", None)
    else:
        payload["nextCursor"] = next_cursor
    meta = payload.get("_meta")
    merged = dict(meta) if isinstance(meta, dict) else {}
    merged.update(accounting)
    merged["adapter"] = "comsol_mcp.tools_list_pagination"
    merged["pageSize"] = page_size
    payload["_meta"] = merged
    return payload


def _tools_of(result: Any) -> list[Any] | None:
    """Return the tool list of a handler result, or ``None`` if not paginable."""
    tools = getattr(result, "tools", None)
    if isinstance(tools, list):
        return tools
    if isinstance(result, dict):
        candidate = result.get("tools")
        if isinstance(candidate, list):
            return candidate
    return None


def build_tools_list_pagination_middleware(
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Any:
    """Build the opt-in ``tools/list`` paging middleware.

    The returned callable is appended to ``MCPServer.middleware`` only when the
    caller enabled ``discovery.pagination_enabled``. It pages only
    ``tools/list``; every other method passes through untouched, and a cursor it
    cannot resolve is refused loudly rather than answered with page one.

    The handler result is paginated *as a typed result* whenever possible, so the
    SDK keeps ownership of serialization and the page cannot drift from the
    declared ``ListToolsResult`` shape.
    """

    async def paginate_tools_list(ctx: Any, call_next: Any) -> Any:
        if getattr(ctx, "method", None) != "tools/list":
            return await call_next(ctx)
        params = getattr(ctx, "params", None)
        cursor = (
            params.get("cursor") if isinstance(params, dict) else getattr(params, "cursor", None)
        )
        result = await call_next(ctx)
        tools = _tools_of(result)
        if not tools:
            return result
        try:
            window, next_cursor, accounting = page_tools(tools, cursor, page_size=page_size)
        except CursorError as exc:
            from mcp.shared.exceptions import MCPError

            raise MCPError(code=-32602, message=str(exc)) from exc
        setter = getattr(result, "model_copy", None)
        if callable(setter) and not isinstance(result, dict):
            meta = getattr(result, "meta", None)
            merged = dict(meta) if isinstance(meta, dict) else {}
            merged.update(accounting)
            merged["adapter"] = "comsol_mcp.tools_list_pagination"
            merged["pageSize"] = page_size
            return setter(update={"tools": window, "next_cursor": next_cursor, "meta": merged})
        payload = _decode_result(result)
        if payload is None:
            return result
        return _page_payload(payload, cursor, page_size)

    return paginate_tools_list


def install_tools_list_pagination(server: Any, *, page_size: int = DEFAULT_PAGE_SIZE) -> bool:
    """Install the opt-in adapter, returning whether it was actually installed.

    Returns ``False`` when the SDK no longer exposes the required public shapes.
    The caller records that outcome rather than assuming paging took effect.
    """
    if not _middleware_supported():
        return False
    try:
        server.middleware.append(build_tools_list_pagination_middleware(page_size=page_size))
    except Exception:
        return False
    return True


def pagination_accounting_sha256(accounting: dict[str, int]) -> str:
    """Bind one page's accounting block to a stable digest for receipts."""
    from comsol_mcp.durable import canonical_sha256_v1

    return canonical_sha256_v1(accounting)


def dumps_accounting(accounting: dict[str, int]) -> str:
    """Compact deterministic rendering used by tests and receipts."""
    return json.dumps(accounting, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CURSOR_PREFIX",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "CursorError",
    "build_tools_list_pagination_middleware",
    "decode_cursor",
    "dumps_accounting",
    "encode_cursor",
    "install_tools_list_pagination",
    "page_tools",
    "pagination_accounting_sha256",
]
