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


@pytest.mark.parametrize("configured", ["relative", "D:/non-ascii-路径"])
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
