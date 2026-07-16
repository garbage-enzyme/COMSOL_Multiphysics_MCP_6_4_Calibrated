from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from src.evidence.field_render import render_field_png_bundle


def _array(path: Path, offset: float = 0.0, *, negative: bool = False):
    x = np.linspace(-1.0, 1.0, 16)
    y = np.linspace(-2.0, 2.0, 12)
    xx, yy = np.meshgrid(x, y)
    values = xx**2 + yy**2 + offset
    if negative:
        values[0, 0] = -1.0
    np.savez_compressed(
        path,
        coordinate_x=x,
        coordinate_y=y,
        quantity_abs_ex=values,
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _view(view_id, path, digest):
    return {
        "view_id": view_id,
        "array_path": str(path),
        "array_sha256": digest,
        "png_artifact_id": f"{view_id}-png",
    }


def test_isolated_single_field_png_is_hash_bound_and_unlabeled(tmp_path):
    array = tmp_path / "single.npz"
    digest = _array(array)
    output = tmp_path / "png"

    result = render_field_png_bundle(
        views=[_view("target", array, digest)],
        quantity_name="abs_ex",
        quantity_unit="V/m",
        coordinate_unit="um",
        color_scale="linear",
        shared_color_limits=False,
        output_root=output,
    )

    descriptor = result["views"][0]
    png = output / descriptor["relative_path"]
    assert result["plot_process_isolated"] is True
    assert result["visual_review_state"] == "visual_review_required"
    assert result["semantic_mode_label"] == "not_assigned"
    assert png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert descriptor["sha256"] == hashlib.sha256(png.read_bytes()).hexdigest()


def test_paired_field_pngs_use_exact_shared_color_limits(tmp_path):
    off = tmp_path / "off.npz"
    target = tmp_path / "target.npz"
    off_hash = _array(off, 1.0)
    target_hash = _array(target, 10.0)

    result = render_field_png_bundle(
        views=[_view("off", off, off_hash), _view("target", target, target_hash)],
        quantity_name="abs_ex",
        quantity_unit="V/m",
        coordinate_unit="um",
        color_scale="linear",
        shared_color_limits=True,
        output_root=tmp_path / "paired",
    )

    assert result["views"][0]["color_limits"] == result["views"][1]["color_limits"]
    with np.load(off, allow_pickle=False) as archive:
        expected_min = float(np.min(archive["quantity_abs_ex"]))
    with np.load(target, allow_pickle=False) as archive:
        expected_max = float(np.max(archive["quantity_abs_ex"]))
    assert result["views"][0]["color_limits"][0] == pytest.approx(expected_min)
    assert result["views"][0]["color_limits"][1] == pytest.approx(expected_max)


def test_renderer_rejects_hash_policy_and_log_failures_without_residue(tmp_path):
    array = tmp_path / "negative.npz"
    digest = _array(array, negative=True)
    output = tmp_path / "failed"

    with pytest.raises(ValueError, match="SHA-256"):
        render_field_png_bundle(
            views=[_view("target", array, "0" * 64)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="linear",
            shared_color_limits=False,
            output_root=output,
        )
    with pytest.raises(ValueError, match="paired field PNGs"):
        render_field_png_bundle(
            views=[_view("a", array, digest), _view("b", array, digest)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="linear",
            shared_color_limits=False,
            output_root=output,
        )
    with pytest.raises(RuntimeError, match="logarithmic field rendering"):
        render_field_png_bundle(
            views=[_view("target", array, digest)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="log",
            shared_color_limits=False,
            output_root=output,
        )
    assert not list(output.glob("*.png"))
