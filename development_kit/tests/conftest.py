"""Shared test-environment preparation for ASCII runtime fixtures."""

from pathlib import Path


def pytest_configure(config) -> None:
    """Create the documented ASCII runtime parent when the drive is present."""
    del config
    root = Path("D:/comsol_runtime")
    if Path("D:/").exists():
        root.mkdir(parents=True, exist_ok=True)
