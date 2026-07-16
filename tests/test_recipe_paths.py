"""Tests for standalone recipe output locations."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def _load_recipe_paths():
    path = Path(__file__).parents[1] / "recipes" / "_paths.py"
    spec = spec_from_file_location("recipe_paths", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_recipe_output_uses_declared_runtime_root(monkeypatch, tmp_path):
    module = _load_recipe_paths()
    monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", str(tmp_path))

    output = module.recipe_output_dir()

    assert output == tmp_path / "recipes"
    assert output.is_dir()
