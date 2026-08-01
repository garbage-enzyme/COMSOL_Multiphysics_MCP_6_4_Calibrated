"""Windows COMSOL 6.4 standalone executable build and inspection support."""

from .builder import build_standalone_executable
from .inspection import (
    read_campaign_results,
    read_campaign_status,
    read_campaign_terminal,
    tail_campaign_log,
    verify_standalone_deployment,
)

__all__ = [
    "build_standalone_executable",
    "read_campaign_results",
    "read_campaign_status",
    "read_campaign_terminal",
    "tail_campaign_log",
    "verify_standalone_deployment",
]
