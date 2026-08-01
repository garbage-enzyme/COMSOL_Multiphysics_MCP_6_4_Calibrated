"""Bounded coordinator for isolated field PNG rendering."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from comsol_mcp.path_policy import pin_validated_reads, validated_read_pin

MAX_RENDER_VIEWS = 2
MAX_RENDER_ARRAY_BYTES = 256 * 1024 * 1024
MAX_RENDER_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_RENDER_RESPONSE_BYTES = 64 * 1024
DEFAULT_RENDER_TIMEOUT_SECONDS = 60.0
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


def _validate_worker_response(
    value: object, *, expected_view_ids: list[str]
) -> dict[str, list[float]]:
    if not isinstance(value, Mapping) or set(value) != {"success", "views"}:
        raise RuntimeError("field plot worker response is invalid")
    if value["success"] is not True or not isinstance(value["views"], list):
        raise RuntimeError("field plot worker response is invalid")
    if len(value["views"]) != len(expected_view_ids):
        raise RuntimeError("field plot worker response view count is invalid")
    limits_by_view: dict[str, list[float]] = {}
    for item in value["views"]:
        if not isinstance(item, Mapping) or set(item) != {"view_id", "color_limits"}:
            raise RuntimeError("field plot worker response view is invalid")
        view_id = item["view_id"]
        limits = item["color_limits"]
        if (
            not isinstance(view_id, str)
            or view_id in limits_by_view
            or not isinstance(limits, list)
            or len(limits) != 2
            or any(
                isinstance(limit, bool)
                or not isinstance(limit, (int, float))
                or not math.isfinite(float(limit))
                for limit in limits
            )
        ):
            raise RuntimeError("field plot worker response view is invalid")
        normalized_limits = [float(limits[0]), float(limits[1])]
        if normalized_limits[0] >= normalized_limits[1]:
            raise RuntimeError("field plot worker response color limits are invalid")
        limits_by_view[view_id] = normalized_limits
    if list(limits_by_view) != expected_view_ids:
        raise RuntimeError("field plot worker response view identities do not match")
    return limits_by_view


def render_field_png_bundle(
    *,
    views: object,
    quantity_name: str,
    quantity_unit: str,
    coordinate_unit: str,
    color_scale: str,
    shared_color_limits: bool,
    output_root: str | os.PathLike[str],
    timeout_seconds: float = DEFAULT_RENDER_TIMEOUT_SECONDS,
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
    array_pins = []
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
        array_pins.append(validated_read_pin(array_path, array_path.parent))
        safe_name = hashlib.sha256(view_id.encode("utf-8")).hexdigest()[:16]
        png_path = root / f"{safe_name}.png"
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
        with pin_validated_reads(tuple(array_pins)):
            for index, view in enumerate(normalized):
                if _sha256_file(Path(view["array_path"])) != view["array_sha256"]:
                    raise ValueError(f"views[{index}] array SHA-256 does not match")
            command = [sys.executable, "-m", "comsol_mcp.evidence.field_plot_worker"]
            encoded_input = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
                process = subprocess.Popen(  # noqa: S603
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    creationflags=(
                        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                    ),
                )
                try:
                    process.communicate(input=encoded_input, timeout=float(timeout_seconds))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise
                stdout_file.seek(0)
                stderr_file.seek(0)
                stdout_bytes = stdout_file.read(MAX_RENDER_RESPONSE_BYTES + 1)
                stderr_bytes = stderr_file.read(MAX_RENDER_RESPONSE_BYTES + 1)
        if (
            len(stdout_bytes) > MAX_RENDER_RESPONSE_BYTES
            or len(stderr_bytes) > MAX_RENDER_RESPONSE_BYTES
        ):
            raise RuntimeError("field plot worker response exceeded its bound")
        try:
            stdout = stdout_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("field plot worker response is not UTF-8") from exc
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise RuntimeError(f"field plot worker failed: {stderr.strip()[:2000]}")
        try:
            response = json.loads(stdout)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise RuntimeError("field plot worker response is not valid JSON") from exc
        limits_by_view = _validate_worker_response(
            response, expected_view_ids=[item["view_id"] for item in normalized]
        )
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


__all__ = ["DEFAULT_RENDER_TIMEOUT_SECONDS", "render_field_png_bundle"]
