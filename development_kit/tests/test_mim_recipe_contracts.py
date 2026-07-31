"""Solver-free contracts for the standalone MIM recipes."""

from __future__ import annotations

import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


def _load_contracts():
    path = Path(__file__).parents[2] / "recipes" / "_mim_safety.py"
    spec = spec_from_file_location("mim_recipe_safety", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Selection:
    def __init__(self, values):
        self.values = values

    def entities(self, *_args):
        return self.values


class Component:
    def __init__(self, selections):
        self.selections = selections

    def selection(self, tag):
        return Selection(self.selections[tag])


class Geometry:
    def __init__(self, up, down):
        self.up = up
        self.down = down

    def getUpDown(self):
        return [self.up, self.down]

    def getNBoundaries(self):
        return len(self.up)


class Properties:
    def __init__(self, *, mismatch=None):
        self.values = {}
        self.mismatch = mismatch

    def set(self, name, value):
        self.values[name] = value

    def getString(self, name):
        return self.mismatch if name == "plist" and self.mismatch else self.values[name]


def test_named_domains_and_interface_are_orientation_independent():
    contracts = _load_contracts()
    component = Component({"geom1_al2_dom": [7], "geom1_air_dom": [3]})
    geometry = Geometry([0, 3, 7], [7, 0, 3])

    al2 = contracts.require_named_domains(component, "geom1_al2_dom")
    air = contracts.require_named_domains(component, "geom1_air_dom")

    assert contracts.require_interface_boundaries(geometry, al2, air) == [3]


@pytest.mark.parametrize("top,bottom", [([], [2]), ([1], []), ([1, 2], [2, 3])])
def test_ports_must_be_nonempty_and_disjoint(top, bottom):
    contracts = _load_contracts()

    with pytest.raises(ValueError):
        contracts.require_port_pair(top, bottom)


def test_ports_must_match_the_intended_exterior_domains():
    contracts = _load_contracts()
    geometry = Geometry([9, 0], [0, 4])

    assert contracts.require_port_pair(
        [1],
        [2],
        geometry=geometry,
        top_domains=[9],
        bottom_domains=[4],
    ) == ([1], [2])
    with pytest.raises(ValueError, match="intended exterior"):
        contracts.require_port_pair(
            [2],
            [1],
            geometry=geometry,
            top_domains=[9],
            bottom_domains=[4],
        )


def test_wavelength_step_is_bound_to_the_swept_parameter_with_readback():
    contracts = _load_contracts()
    step = Properties()

    contracts.bind_wavelength_step(step, "wl")

    assert step.values == {"punit": "m", "plist": "wl"}
    with pytest.raises(ValueError, match="readback"):
        contracts.bind_wavelength_step(Properties(mismatch="5e-6"), "wl")


def test_required_properties_fail_on_any_set_or_readback_error():
    contracts = _load_contracts()
    node = Properties(mismatch="wrong")

    with pytest.raises(ValueError, match="readback"):
        contracts.require_required_properties(node, {"plist": "wl"})


def test_spectrum_requires_one_finite_real_value_per_wavelength():
    contracts = _load_contracts()

    assert contracts.require_spectrum([0.1, 0.2], [1e-6, 2e-6], "R") == [0.1, 0.2]
    with pytest.raises(ValueError, match="count"):
        contracts.require_spectrum([0.1], [1e-6, 2e-6], "R")
    with pytest.raises(ValueError, match="finite real"):
        contracts.require_spectrum([0.1 + 0.2j], [1e-6], "R")


def test_partition_requires_an_observed_split_and_unique_patch_boundary():
    contracts = _load_contracts()

    assert contracts.require_partition_result(10, 12, [11]) == 11
    with pytest.raises(ValueError, match="increase"):
        contracts.require_partition_result(10, 10, [11])
    with pytest.raises(ValueError, match="ambiguous"):
        contracts.require_partition_result(10, 12, [11, 12])


def test_save_is_published_only_after_a_nonempty_staging_model(tmp_path):
    contracts = _load_contracts()

    class Model:
        @staticmethod
        def save(path):
            Path(path).write_bytes(b"model")

    target = tmp_path / "result.mph"
    assert contracts.save_required(Model(), target) == target.resolve()
    assert target.read_bytes() == b"model"

    class EmptyModel:
        @staticmethod
        def save(path):
            Path(path).write_bytes(b"")

    with pytest.raises(RuntimeError, match="nonempty"):
        contracts.save_required(EmptyModel(), tmp_path / "empty.mph")


@pytest.mark.parametrize(
    "name",
    ["mim_drude_sweep.py", "mim_lml_continuous.py", "mim_patch_partition.py"],
)
def test_mim_recipes_use_fail_closed_solve_result_port_wavelength_and_save_contracts(name):
    path = Path(__file__).parents[2] / "recipes" / name
    source = path.read_text(encoding="utf-8")
    ast.parse(source)

    for required in (
        "bind_wavelength_step",
        "require_port_pair",
        "require_required_properties",
        "require_spectrum",
        "save_required",
    ):
        assert required in source
    assert ".save(" not in source
    assert "save err" not in source
    assert "Solve FAIL" not in source
    assert "set('plist', str(5e-6))" not in source


def test_drude_recipe_solves_and_retains_distinct_baseline_and_patch_spectra():
    source = (Path(__file__).parents[2] / "recipes" / "mim_drude_sweep.py").read_text(
        encoding="utf-8"
    )

    assert "baseline_reflection = solve_required('continuous baseline')" in source
    assert "patch_reflection = solve_required('patch')" in source


def test_drude_recipe_has_one_authoritative_dispersion_expression():
    source = (Path(__file__).parents[2] / "recipes" / "mim_drude_sweep.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    assigned_names = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }

    assert {name for name in assigned_names if name.startswith("au_drude")} == {"au_drude_param"}
    assert "f_fix" not in assigned_names


def test_partition_recipe_has_no_all_face_or_highest_boundary_fallback():
    source = (Path(__file__).parents[2] / "recipes" / "mim_patch_partition.py").read_text(
        encoding="utf-8"
    )

    assert "require_partition_result" in source
    assert "[1,2,3,4,5,6]" not in source.replace(" ", "")
    assert "highest bnd number" not in source
    assert "both MIM mesh strategies failed" in source
