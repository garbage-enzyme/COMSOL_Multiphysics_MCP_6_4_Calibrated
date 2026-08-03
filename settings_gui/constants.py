"""Shared bounded GUI constants."""

from __future__ import annotations

from comsol_mcp.settings import MAX_SETTINGS_BYTES

APP_NAME = "COMSOL MCP Settings"
DOMAIN = "settings_gui"
LOCK_POLL_MS = 1000
MAX_ERROR_CHARS = 512
SAVE_RETRY_SECONDS = 1.5
SAVE_RETRY_INTERVAL_SECONDS = 0.05

__all__ = [
    "APP_NAME",
    "DOMAIN",
    "LOCK_POLL_MS",
    "MAX_ERROR_CHARS",
    "MAX_SETTINGS_BYTES",
    "SAVE_RETRY_INTERVAL_SECONDS",
    "SAVE_RETRY_SECONDS",
]
