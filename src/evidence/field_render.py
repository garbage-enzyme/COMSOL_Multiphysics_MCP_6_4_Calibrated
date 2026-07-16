"""Bounded coordinator for isolated field PNG rendering."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping


MAX_RENDER_VIEWS = 2
MAX_RENDER_ARRAY_BYTES = 256 * 1024 * 1024
MAX_RENDER_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_RENDER_RESPONSE_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return dict(value)


def render_field_png_bundle(
    *,
    views: object,
    quantity_name: str,
    quantity_unit: str,
    coordinate_unit: str,
    color_scale: str,
    shared_color_limits: bool,
    output_root: str | os.PathLike[str],
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Render one or two immutable NPZ views in an isolated plotting process."""
    if not isinstance(views, list) or not 1 <= len(views) <= MAX_RENDER_VIEWS:
        raise ValueError("views must contain one or two entries")
    if not isinstance(quantity_name, str) or not _IDENTIFIER.fullmatch(quantity_name):
        raise ValueError("quantity_name must be a portable identifier")
    for value, label in ((quantity_unit, "quantity_unit"), (coordinate_unit, "coordinate_unit")):
        if not isinstance(value, str) or not value.strip() or len(value) > 64:
            raise ValueError(f"{label} must be bounded nonempty text")
    if color_scale not in {"linear", "log"}:
        raise ValueError("color_scale must be linear or log")
    if not isinstance(shared_color_limits, bool):
        raise ValueError("shared_color_limits must be boolean")
    if len(views) == 2 and not shared_color_limits:
        raise ValueError("paired field PNGs require shared color limits")
    if len(views) == 1 and shared_color_limits:
        raise ValueError("shared color limits require exactly two views")
    if not isinstance(timeout_seconds, (int, float)) or not 1 <= timeout_seconds <= 120:
        raise ValueError("timeout_seconds must be between 1 and 120")

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    normalized = []
    seen_ids = set()
    for index, value in enumerate(views):
        item = _mapping(value, f"views[{index}]")
        if set(item) != {"view_id", "array_path", "array_sha256", "png_artifact_id"}:
            raise ValueError(f"views[{index}] has missing or unsupported fields")
        view_id = item["view_id"]
        artifact_id = item["png_artifact_id"]
        if not isinstance(view_id, str) or not _IDENTIFIER.fullmatch(view_id):
            raise ValueError(f"views[{index}].view_id must be portable")
        if not isinstance(artifact_id, str) or not _IDENTIFIER.fullmatch(artifact_id):
            raise ValueError(f"views[{index}].png_artifact_id must be portable")
        if view_id in seen_ids:
            raise ValueError("view IDs must be unique")
        seen_ids.add(view_id)
        array_path = Path(item["array_path"]).expanduser().resolve()
        if not array_path.is_file() or not 0 < array_path.stat().st_size <= MAX_RENDER_ARRAY_BYTES:
            raise ValueError(f"views[{index}].array_path is missing or oversized")
        if _sha256_file(array_path) != str(item["array_sha256"]).lower():
            raise ValueError(f"views[{index}] array SHA-256 does not match")
        png_path = root / f"{view_id}.png"
        if png_path.exists():
            raise FileExistsError(f"field PNG already exists: {view_id}")
        normalized.append(
            {
                "view_id": view_id,
                "array_path": str(array_path),
                "array_sha256": str(item["array_sha256"]).lower(),
                "png_artifact_id": artifact_id,
                "png_path": str(png_path),
            }
        )

    payload = {
        "quantity_name": quantity_name,
        "quantity_unit": quantity_unit.strip(),
        "coordinate_unit": coordinate_unit.strip(),
        "color_scale": color_scale,
        "shared_color_limits": shared_color_limits,
        "views": normalized,
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "src.evidence.field_plot_worker"],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=float(timeout_seconds),
            check=False,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        if len(completed.stdout.encode("utf-8")) > MAX_RENDER_RESPONSE_BYTES or len(
            completed.stderr.encode("utf-8")
        ) > MAX_RENDER_RESPONSE_BYTES:
            raise RuntimeError("field plot worker response exceeded its bound")
        if completed.returncode != 0:
            raise RuntimeError(
                f"field plot worker failed: {completed.stderr.strip()[:2000]}"
            )
        response = json.loads(completed.stdout)
        if response.get("success") is not True:
            raise RuntimeError("field plot worker did not report success")
        limits_by_view = {
            item["view_id"]: item["color_limits"] for item in response["views"]
        }
        descriptors = []
        for view in normalized:
            path = Path(view["png_path"])
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_RENDER_OUTPUT_BYTES:
                raise RuntimeError("field plot worker output is missing or oversized")
            with path.open("rb") as handle:
                if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise RuntimeError("field plot worker output is not a PNG")
            descriptors.append(
                {
                    "view_id": view["view_id"],
                    "artifact_id": view["png_artifact_id"],
                    "relative_path": path.relative_to(root).as_posix(),
                    "media_type": "image/png",
                    "sha256": _sha256_file(path),
                    "byte_count": path.stat().st_size,
                    "color_limits": limits_by_view[view["view_id"]],
                }
            )
        return {
            "success": True,
            "quantity_name": quantity_name,
            "color_scale": color_scale,
            "shared_color_limits": shared_color_limits,
            "views": descriptors,
            "visual_review_state": "visual_review_required",
            "semantic_mode_label": "not_assigned",
            "plot_process_isolated": True,
        }
    except Exception:
        for view in normalized:
            Path(view["png_path"]).unlink(missing_ok=True)
        raise


__all__ = ["render_field_png_bundle"]
