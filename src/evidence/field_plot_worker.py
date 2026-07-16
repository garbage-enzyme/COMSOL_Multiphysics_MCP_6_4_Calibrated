"""Isolated matplotlib worker for bounded scalar field PNGs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import uuid


def main() -> int:
    request = json.loads(sys.stdin.read())
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm
    import numpy as np

    quantity_key = f"quantity_{request['quantity_name']}"
    loaded = []
    finite_sets = []
    for view in request["views"]:
        with np.load(view["array_path"], allow_pickle=False) as archive:
            if quantity_key not in archive.files:
                raise ValueError(f"NPZ does not contain {quantity_key}")
            coordinate_keys = sorted(
                key for key in archive.files if key.startswith("coordinate_")
            )
            if len(coordinate_keys) != 2:
                raise ValueError("NPZ must contain exactly two coordinate axes")
            values = np.asarray(archive[quantity_key], dtype=np.float64)
            first = np.asarray(archive[coordinate_keys[0]], dtype=np.float64)
            second = np.asarray(archive[coordinate_keys[1]], dtype=np.float64)
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            raise ValueError("field quantity contains no finite values")
        if request["color_scale"] == "log" and np.any(finite <= 0):
            raise ValueError("logarithmic field rendering requires positive values")
        loaded.append((view, values, coordinate_keys, first, second))
        finite_sets.append(finite)

    if request["shared_color_limits"]:
        combined = np.concatenate(finite_sets)
        limits = [float(np.min(combined)), float(np.max(combined))]
        all_limits = [limits for _ in loaded]
    else:
        all_limits = [
            [float(np.min(finite)), float(np.max(finite))] for finite in finite_sets
        ]

    results = []
    for (view, values, coordinate_keys, first, second), limits in zip(
        loaded, all_limits
    ):
        if limits[0] == limits[1]:
            delta = max(abs(limits[0]) * 1e-12, 1e-12)
            limits = [limits[0] - delta, limits[1] + delta]
        figure, axis = plt.subplots(figsize=(6.4, 5.0), dpi=120)
        extent = [float(first[0]), float(first[-1]), float(second[0]), float(second[-1])]
        kwargs = {
            "origin": "lower",
            "aspect": "auto",
            "extent": extent,
            "cmap": "viridis",
        }
        if request["color_scale"] == "log":
            kwargs["norm"] = LogNorm(vmin=limits[0], vmax=limits[1])
        else:
            kwargs["vmin"] = limits[0]
            kwargs["vmax"] = limits[1]
        image = axis.imshow(values, **kwargs)
        axis.set_xlabel(f"{coordinate_keys[0].removeprefix('coordinate_')} ({request['coordinate_unit']})")
        axis.set_ylabel(f"{coordinate_keys[1].removeprefix('coordinate_')} ({request['coordinate_unit']})")
        axis.set_title(f"{view['view_id']}: {request['quantity_name']}")
        colorbar = figure.colorbar(image, ax=axis)
        colorbar.set_label(request["quantity_unit"])
        figure.tight_layout()
        output = Path(view["png_path"])
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex[:8]}.tmp.png")
        try:
            figure.savefig(temporary, format="png")
            os.replace(temporary, output)
        finally:
            plt.close(figure)
            temporary.unlink(missing_ok=True)
        results.append({"view_id": view["view_id"], "color_limits": limits})
    sys.stdout.write(json.dumps({"success": True, "views": results}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
