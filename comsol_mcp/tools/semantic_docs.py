"""Public bounded tools for the static opt-in semantic documentation profile."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from comsol_mcp.knowledge.semantic_runtime import get_semantic_service


def register_semantic_doc_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def semantic_search(
        query: str,
        module: str | None = None,
        limit: int = 5,
        source: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
    ) -> dict:
        """Search immutable COMSOL manuals with bounded BM25/vector fusion.

        Paths, model selection, index selection, rebuilds, and deletion are not
        request parameters. Use manual_read_pages for full cited-page context.
        """
        return get_semantic_service().search(
            query,
            module=module,
            limit=limit,
            source=source,
            page_start=page_start,
            page_end=page_end,
        )

    @mcp.tool()
    def semantic_status(warm: bool = False) -> dict:
        """Report solver-free semantic deployment/worker health.

        With warm=false this never starts a worker. With warm=true it performs
        the explicit cold load/health gate in the isolated worker.
        """
        return get_semantic_service().status(warm=warm)

    @mcp.tool()
    def semantic_worker_reset() -> dict:
        """Stop only the exact recorded semantic worker tree; never delete an index."""
        return get_semantic_service().reset()


__all__ = ["register_semantic_doc_tools"]
