"""Static checks for the standalone differential-coil recipe."""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]
RECIPE = ROOT / "recipes" / "acdc_2d_differential_coils.py"


def test_recipe_is_syntax_valid_and_has_explicit_runtime_inputs():
    source = RECIPE.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert isinstance(tree, ast.Module)
    assert 'parser.add_argument("--baseline-model", required=True, type=Path)' in source
    assert 'parser.add_argument("--output-model", required=True, type=Path)' in source
    assert 'parser.add_argument("--solve", action="store_true")' in source
    assert 'parser.add_argument("--overwrite-output", action="store_true")' in source
    assert "if args.solve:" in source


def test_recipe_has_no_hard_coded_user_profile_path_or_binary_fixture():
    source = RECIPE.read_text(encoding="utf-8")

    assert "C:/Users/" not in source
    assert "C:\\\\Users\\\\" not in source
    assert "EC_NDT_Model.mph" not in source


def test_recipe_preserves_the_baseline_and_requires_explicit_output_replacement():
    source = RECIPE.read_text(encoding="utf-8")

    assert "baseline_sha256 = sha256_file(baseline)" in source
    assert "if sha256_file(baseline) != baseline_sha256:" in source
    assert "if output.exists() and not args.overwrite_output:" in source
    assert "java_model.save(str(output))" in source
    assert "client.disconnect()" in source
