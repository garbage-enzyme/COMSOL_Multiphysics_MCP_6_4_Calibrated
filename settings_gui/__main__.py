"""Installed Settings GUI entry point."""

from __future__ import annotations

from comsol_mcp.settings_gui_handshake import publish_handshake


def main() -> None:
    try:
        from .app import run
    except ImportError, OSError, RuntimeError:
        publish_handshake("gui_runtime_unavailable")
        raise SystemExit(2) from None
    try:
        code = run()
    except Exception:
        publish_handshake("launch_failed")
        raise
    raise SystemExit(code)


if __name__ == "__main__":
    main()
