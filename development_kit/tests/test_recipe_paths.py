"""Tests for standalone recipe output locations."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def _load_recipe_paths():
    path = Path(__file__).parents[2] / "recipes" / "_paths.py"
    spec = spec_from_file_location("recipe_paths", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recipe_output_uses_declared_runtime_root(monkeypatch, ascii_tmp_path):
    module = _load_recipe_paths()
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", str(ascii_tmp_path))

    output = module.recipe_output_dir()

    assert output == ascii_tmp_path.resolve() / "recipes"
    assert output.is_dir()


@pytest.mark.parametrize("configured", ["", "relative", "D:/non-ascii-路径"])
def test_recipe_output_rejects_nonportable_runtime_root(monkeypatch, configured):
    module = _load_recipe_paths()
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", configured)

    with pytest.raises(ValueError, match="absolute|ASCII"):
        module.recipe_output_dir()


def test_recipe_output_rejects_source_checkout(monkeypatch):
    module = _load_recipe_paths()
    monkeypatch.setattr(module, "__file__", "D:/portable-checkout/recipes/_paths.py")
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", "D:/portable-checkout/output")

    with pytest.raises(ValueError, match="outside the source checkout"):
        module.recipe_output_dir()


def test_recipe_output_falls_back_when_an_automatic_root_is_not_writable(
    monkeypatch, ascii_tmp_path
):
    module = _load_recipe_paths()
    blocked = ascii_tmp_path / "blocked"
    fallback = ascii_tmp_path / "fallback"
    blocked_output = blocked.resolve() / "recipes"
    original_mkdir = Path.mkdir

    def selective_mkdir(path, *args, **kwargs):
        if path == blocked_output:
            raise PermissionError("synthetic unwritable root")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(module, "settings_environment", dict)
    monkeypatch.setattr(module, "_automatic_output_roots", lambda _environment: (blocked, fallback))
    monkeypatch.setattr(Path, "mkdir", selective_mkdir)

    output = module.recipe_output_dir()

    assert output == fallback.resolve() / "recipes"
    assert output.is_dir()


def test_recipe_output_does_not_hide_an_unwritable_explicit_root(monkeypatch, ascii_tmp_path):
    module = _load_recipe_paths()
    configured = ascii_tmp_path / "configured"
    configured_output = configured.resolve() / "recipes"
    original_mkdir = Path.mkdir

    def selective_mkdir(path, *args, **kwargs):
        if path == configured_output:
            raise PermissionError("synthetic unwritable root")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(
        module,
        "settings_environment",
        lambda: {"COMSOL_MCP_RUNTIME_DIR": str(configured)},
    )
    monkeypatch.setattr(Path, "mkdir", selective_mkdir)

    with pytest.raises(PermissionError, match="synthetic unwritable root"):
        module.recipe_output_dir()


def test_recipe_output_rejects_preexisting_or_dangling_reparse_root(
    monkeypatch, ascii_tmp_path
):
    module = _load_recipe_paths()
    configured = ascii_tmp_path / "linked-root"
    monkeypatch.setattr(module, "_is_reparse_point", lambda path: path == configured)

    with pytest.raises(ValueError, match="links or reparse"):
        module._create_recipe_output(configured)


def test_recipe_output_revalidates_after_directory_creation(monkeypatch, ascii_tmp_path):
    module = _load_recipe_paths()
    configured = ascii_tmp_path / "race-root"
    output = configured.resolve() / "recipes"
    armed = False
    original_mkdir = Path.mkdir

    def mkdir_then_arm(path, *args, **kwargs):
        nonlocal armed
        result = original_mkdir(path, *args, **kwargs)
        if path == output:
            armed = True
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir_then_arm)
    monkeypatch.setattr(
        module, "_is_reparse_point", lambda path: armed and path == output
    )

    with pytest.raises(ValueError, match="links or reparse"):
        module._create_recipe_output(configured)
