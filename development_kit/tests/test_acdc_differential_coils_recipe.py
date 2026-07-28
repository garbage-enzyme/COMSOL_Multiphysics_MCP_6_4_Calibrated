"""Static checks for the standalone differential-coil recipe."""

import ast
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    assert "java_model.save(str(staging))" in source
    assert "os.link(staging, output)" in source
    assert (
        source.index("staging = save_staged_model")
        < source.index("if sha256_file(baseline) != baseline_sha256:")
        < source.index("publish_staged_model(staging, output")
    )
    assert "client.remove(model)" in source
    assert "tag.startswith(" not in source
    assert 'study.feature().create("freq", "Frequency")' in source
    assert 'ampere_air.set("mur", "1")' in source
    assert 'ampere_air.set("epsilonr", "1")' in source
    assert "LangevinFunction" not in source


def test_recipe_staging_never_overwrites_a_competing_output(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace())
    namespace = runpy.run_path(str(RECIPE))
    output = tmp_path / "derived.mph"

    class FakeJava:
        def save(self, staging):
            Path(staging).write_bytes(b"ours")

    staging = namespace["save_staged_model"](FakeJava(), output)
    output.write_bytes(b"competitor")

    with pytest.raises(FileExistsError):
        namespace["publish_staged_model"](staging, output, overwrite=False)

    assert output.read_bytes() == b"competitor"
    assert not staging.exists()
