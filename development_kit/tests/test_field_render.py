from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import src.evidence.field_render as field_render_module
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


def test_renderer_uses_windows_safe_filename_for_portable_view_id(tmp_path):
    array = tmp_path / "portable.npz"
    digest = _array(array)
    output = tmp_path / "portable-png"

    result = render_field_png_bundle(
        views=[_view("off:res", array, digest)],
        quantity_name="abs_ex",
        quantity_unit="V/m",
        coordinate_unit="um",
        color_scale="linear",
        shared_color_limits=False,
        output_root=output,
    )

    descriptor = result["views"][0]
    assert descriptor["view_id"] == "off:res"
    assert descriptor["relative_path"] == (hashlib.sha256(b"off:res").hexdigest()[:16] + ".png")
    assert ":" not in descriptor["relative_path"]


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


def test_worker_output_is_redirected_and_read_with_a_hard_bound(tmp_path, monkeypatch):
    array = tmp_path / "bounded.npz"
    digest = _array(array)

    class FakeProcess:
        returncode = 0

        def __init__(self, _command, **kwargs):
            assert kwargs["stdout"] is not field_render_module.subprocess.PIPE
            assert kwargs["stderr"] is not field_render_module.subprocess.PIPE
            self.stdout = kwargs["stdout"]

        def communicate(self, *, input, timeout):
            assert input and timeout > 0
            self.stdout.write(b"x" * (field_render_module.MAX_RENDER_RESPONSE_BYTES + 1))

    monkeypatch.setattr(field_render_module.subprocess, "Popen", FakeProcess)

    with pytest.raises(RuntimeError, match="response exceeded"):
        render_field_png_bundle(
            views=[_view("target", array, digest)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="linear",
            shared_color_limits=False,
            output_root=tmp_path / "output",
        )


def test_array_cannot_change_while_render_worker_consumes_it(tmp_path, monkeypatch):
    array = tmp_path / "pinned.npz"
    digest = _array(array)
    blocked = []

    class FakeProcess:
        returncode = 1

        def __init__(self, _command, **kwargs):
            self.stderr = kwargs["stderr"]
            try:
                array.write_bytes(b"replacement")
            except PermissionError:
                blocked.append(True)

        def communicate(self, *, input, timeout):
            assert input and timeout > 0
            self.stderr.write(b"controlled worker failure")

    monkeypatch.setattr(field_render_module.subprocess, "Popen", FakeProcess)

    with pytest.raises(RuntimeError, match="worker failed"):
        render_field_png_bundle(
            views=[_view("target", array, digest)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="linear",
            shared_color_limits=False,
            output_root=tmp_path / "output",
        )

    assert blocked == [True]
    assert hashlib.sha256(array.read_bytes()).hexdigest() == digest


def test_worker_failure_removes_every_owned_partial_png(tmp_path, monkeypatch):
    array = tmp_path / "partial.npz"
    digest = _array(array)
    output = tmp_path / "partial-output"

    class FakeProcess:
        returncode = 1

        def __init__(self, _command, **kwargs):
            self.stderr = kwargs["stderr"]

        def communicate(self, *, input, timeout):
            assert timeout > 0
            payload = json.loads(input)
            for view in payload["views"]:
                Path(view["png_path"]).write_bytes(b"partial")
            self.stderr.write(b"controlled worker failure")

    monkeypatch.setattr(field_render_module.subprocess, "Popen", FakeProcess)

    with pytest.raises(RuntimeError, match="worker failed"):
        render_field_png_bundle(
            views=[_view("target", array, digest)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="linear",
            shared_color_limits=False,
            output_root=output,
        )

    assert not list(output.glob("*.png"))


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"success": True, "views": [{"view_id": "wrong", "color_limits": [0, 1]}]},
        {
            "success": True,
            "views": [{"view_id": "target", "color_limits": [0, 1], "extra": True}],
        },
        {"success": True, "views": [{"view_id": "target", "color_limits": [0]}]},
        {"success": True, "views": [{"view_id": "target", "color_limits": [0, float("inf")]}]},
    ],
    ids=["non_object", "wrong_view", "extra_field", "short_limits", "nonfinite"],
)
def test_renderer_rejects_unbounded_or_mismatched_worker_responses(tmp_path, monkeypatch, response):
    array = tmp_path / "response.npz"
    digest = _array(array)

    class FakeProcess:
        returncode = 0

        def __init__(self, _command, **kwargs):
            self.stdout = kwargs["stdout"]

        def communicate(self, *, input, timeout):
            assert input and timeout > 0
            self.stdout.write(json.dumps(response).encode("utf-8"))

    monkeypatch.setattr(field_render_module.subprocess, "Popen", FakeProcess)

    with pytest.raises(RuntimeError, match="worker response"):
        render_field_png_bundle(
            views=[_view("target", array, digest)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="linear",
            shared_color_limits=False,
            output_root=tmp_path / "response-output",
        )


def test_renderer_converts_invalid_worker_encoding_to_a_stable_error(tmp_path, monkeypatch):
    array = tmp_path / "encoding.npz"
    digest = _array(array)

    class FakeProcess:
        returncode = 0

        def __init__(self, _command, **kwargs):
            self.stdout = kwargs["stdout"]

        def communicate(self, *, input, timeout):
            assert input and timeout > 0
            self.stdout.write(b"\xff")

    monkeypatch.setattr(field_render_module.subprocess, "Popen", FakeProcess)

    with pytest.raises(RuntimeError, match="not UTF-8"):
        render_field_png_bundle(
            views=[_view("target", array, digest)],
            quantity_name="abs_ex",
            quantity_unit="V/m",
            coordinate_unit="um",
            color_scale="linear",
            shared_color_limits=False,
            output_root=tmp_path / "encoding-output",
        )
