"""Isolated matplotlib worker for bounded scalar field PNGs."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path


def _expand_constant_limits(value, *, logarithmic: bool, np):
    if logarithmic:
        lower = float(np.nextafter(value, 0.0))
        upper = float(np.nextafter(value, np.inf))
        if lower <= 0.0:
            lower = value
        if not np.isfinite(upper):
            upper = value
        if not 0.0 < lower < upper:
            raise ValueError("constant logarithmic field range cannot be expanded")
        return [lower, upper]
    lower = float(np.nextafter(value, -np.inf))
    upper = float(np.nextafter(value, np.inf))
    if not np.isfinite(lower):
        lower = value
    if not np.isfinite(upper):
        upper = value
    if not lower < upper:
        raise ValueError("constant linear field range cannot be expanded")
    return [lower, upper]


def main() -> int:
    request = json.loads(sys.stdin.read())
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.colors import LogNorm

    quantity_key = f"quantity_{request['quantity_name']}"
    loaded = []
    finite_sets = []
    for view in request["views"]:
        with np.load(view["array_path"], allow_pickle=False) as archive:
            if quantity_key not in archive.files:
                raise ValueError(f"NPZ does not contain {quantity_key}")
            coordinate_keys = sorted(key for key in archive.files if key.startswith("coordinate_"))
            if len(coordinate_keys) != 2:
                raise ValueError("NPZ must contain exactly two coordinate axes")
            values = np.asarray(archive[quantity_key], dtype=np.float64)
            first = np.asarray(archive[coordinate_keys[0]], dtype=np.float64)
            second = np.asarray(archive[coordinate_keys[1]], dtype=np.float64)
        if (
            first.ndim != 1
            or second.ndim != 1
            or first.size == 0
            or second.size == 0
            or not np.all(np.isfinite(first))
            or not np.all(np.isfinite(second))
            or np.any(np.diff(first) <= 0.0)
            or np.any(np.diff(second) <= 0.0)
        ):
            raise ValueError("field coordinates must be finite increasing nonempty vectors")
        if values.ndim != 2 or values.shape != (second.size, first.size):
            raise ValueError("field quantity shape does not match its coordinate axes")
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
        all_limits = [[float(np.min(finite)), float(np.max(finite))] for finite in finite_sets]

    results = []
    for (view, values, coordinate_keys, first, second), limits in zip(loaded, all_limits):
        if limits[0] == limits[1]:
            limits = _expand_constant_limits(
                limits[0],
                logarithmic=request["color_scale"] == "log",
                np=np,
            )
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
        axis.set_xlabel(
            f"{coordinate_keys[0].removeprefix('coordinate_')} ({request['coordinate_unit']})"
        )
        axis.set_ylabel(
            f"{coordinate_keys[1].removeprefix('coordinate_')} ({request['coordinate_unit']})"
        )
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
