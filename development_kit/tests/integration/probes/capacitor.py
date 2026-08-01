"""Standalone ParallelPlateCapacitor integration probe for COMSOL 6.4."""

from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path

import mph

ROOT = Path(__file__).parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_recipe = import_module("recipes.parallel_plate_capacitor")
build_parallel_plate_capacitor = _recipe.build_parallel_plate_capacitor
validate_solution = _recipe.validate_solution


def _geometry_size_expressions() -> tuple[str, str, str]:
    return _recipe._geometry_size_expressions()


def _theoretical_capacitance_pf() -> float:
    return _recipe._theoretical_capacitance_pf()


def main() -> None:
    """Build, solve, and validate the capacitor in a dedicated process."""
    client = None
    cleanup_errors = []
    try:
        client = mph.Client(version="6.4")
        model = client.create("ParallelPlateCap")
        build = build_parallel_plate_capacitor(model)
        validation = validate_solution(model)
        print(
            json.dumps(
                {"success": True, "build": build, "validation": validation},
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
    finally:
        if client is not None:
            try:
                client.clear()
            except Exception as exc:
                cleanup_errors.append(f"client.clear: {type(exc).__name__}")
            if getattr(client, "port", None):
                try:
                    client.disconnect()
                except Exception as exc:
                    cleanup_errors.append(f"client.disconnect: {type(exc).__name__}")
        if cleanup_errors:
            raise RuntimeError(f"capacitor probe cleanup failed: {cleanup_errors}")


if __name__ == "__main__":
    main()
