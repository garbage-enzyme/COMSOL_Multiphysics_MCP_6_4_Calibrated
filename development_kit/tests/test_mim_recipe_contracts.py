"""Solver-free contracts for the standalone MIM recipes."""

from __future__ import annotations

import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_contracts():
    path = Path(__file__).parents[2] / "recipes" / "_mim_safety.py"
    spec = spec_from_file_location("mim_recipe_safety", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PowerModel:
    def __init__(self, values):
        self.values = values
        self.dataset = SimpleNamespace(tag=lambda: "dset1")

    def __truediv__(self, name):
        assert name == "datasets"
        return [self.dataset]

    def evaluate(self, expression, *, dataset, outer, unit=None):
        assert dataset is self.dataset
        if expression in ("wl", "c_const/freq"):
            return [1e-6 * outer]
        return [self.values[expression][outer - 1]]


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

    with pytest.raises(ValueError, match="must not overlap"):
        contracts.require_interface_boundaries(geometry, [7], [7, 3])


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
    with pytest.raises(ValueError, match="outside the built topology"):
        contracts.require_port_pair(
            [3],
            [2],
            geometry=geometry,
            top_domains=[9],
            bottom_domains=[4],
        )


def test_ports_reject_out_of_range_boundaries_and_overlapping_domain_groups():
    contracts = _load_contracts()
    geometry = Geometry([9, 0], [0, 4])

    with pytest.raises(ValueError, match="outside the built topology"):
        contracts.require_port_pair(
            [3], [2], geometry=geometry, top_domains=[9], bottom_domains=[4]
        )
    with pytest.raises(ValueError, match="domains must not overlap"):
        contracts.require_port_pair(
            [1], [2], geometry=geometry, top_domains=[9], bottom_domains=[9]
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


def test_required_properties_accept_equivalent_numeric_readback():
    contracts = _load_contracts()

    class NumericReadback(Properties):
        def getString(self, _name):
            return "0.001"

    contracts.require_required_properties(NumericReadback(), {"value": 1.0e-3})


def test_spectrum_requires_one_finite_real_value_per_wavelength():
    contracts = _load_contracts()

    assert contracts.require_spectrum([0.1, 0.2], [1e-6, 2e-6], "R") == [0.1, 0.2]
    with pytest.raises(ValueError, match="count"):
        contracts.require_spectrum([0.1], [1e-6, 2e-6], "R")
    with pytest.raises(ValueError, match="finite real"):
        contracts.require_spectrum([0.1 + 0.2j], [1e-6], "R")


def test_passive_reflection_allows_solver_tolerance_but_rejects_nonphysical_values():
    contracts = _load_contracts()

    assert contracts.require_passive_reflection([-5e-7, 1.0000005], "R") == [
        -5e-7,
        1.0000005,
    ]
    with pytest.raises(ValueError, match="passive reflection"):
        contracts.require_passive_reflection([1.01], "R")


@pytest.mark.parametrize("power", [(0.17, 0.834867, -0.004867), (0.2, 0.7, 0.2)])
def test_passive_power_rejects_gain_and_nonclosure(power):
    contracts = _load_contracts()

    model = PowerModel(
        dict(zip(("ewfd.Rtotal", "ewfd.Ttotal", "ewfd.Atotal"), ([v] for v in power)))
    )
    with pytest.raises(ValueError, match="passive power|closure"):
        contracts.require_passive_power(model, [1e-6], "fixture")


def test_passive_power_accepts_passive_closed_spectrum():
    contracts = _load_contracts()

    model = PowerModel(
        {"ewfd.Rtotal": [0.2, 0.3], "ewfd.Ttotal": [0.7, 0.6], "ewfd.Atotal": [0.1, 0.1]}
    )
    assert contracts.require_passive_power(model, [1e-6, 2e-6], "fixture")["R"] == [0.2, 0.3]


def test_power_rejects_requested_wavelength_drift():
    contracts = _load_contracts()
    model = PowerModel({"ewfd.Rtotal": [0.2], "ewfd.Ttotal": [0.7], "ewfd.Atotal": [0.1]})
    with pytest.raises(ValueError, match="wavelengths differ"):
        contracts.require_passive_power(model, [2e-6], "fixture")


def test_pointwise_solve_synchronizes_material_and_solver_before_each_solve(monkeypatch):
    contracts = _load_contracts()
    events = []
    node = SimpleNamespace(
        active=lambda value: events.append(("active", value)),
        set=lambda key, value: events.append((key, value)),
    )
    study = SimpleNamespace(feature=lambda tag: node, run=lambda: events.append(("solve",)))
    model = SimpleNamespace(java=SimpleNamespace(study=lambda tag: study, param=lambda: node))

    def verify(model, wavelengths, label, **kwargs):
        events.append(("verified", wavelengths[0]))
        return {"R": [0.2], "T": [0.7], "A": [0.1]}

    monkeypatch.setattr(contracts, "require_passive_power", verify)
    result = contracts.solve_passive_wavelengths(model, [1e-6, 2e-6], "fixture")
    assert result["A"] == [0.1, 0.1]
    assert events[0] == ("active", False)
    for index, wavelength in enumerate([1e-6, 2e-6]):
        block = events[1 + 4 * index : 5 + 4 * index]
        assert block == [
            ("wl", f"{wavelength:.17g}[m]"),
            ("plist", f"{wavelength:.17g}"),
            ("solve",),
            ("verified", wavelength),
        ]


def test_pointwise_invalid_later_wavelength_is_refused_before_model_access():
    contracts = _load_contracts()
    with pytest.raises(ValueError, match="finite and positive"):
        contracts.solve_passive_wavelengths(object(), [1e-6, float("nan")], "fixture")


def test_partition_requires_an_observed_split_and_unique_patch_boundary():
    contracts = _load_contracts()

    assert contracts.require_partition_result(10, 12, [11]) == 11
    with pytest.raises(ValueError, match="increase"):
        contracts.require_partition_result(10, 10, [11])
    with pytest.raises(ValueError, match="ambiguous"):
        contracts.require_partition_result(10, 12, [11, 12])


def test_opaque_power_requires_explicit_termination_and_does_not_read_a_covered_port():
    contracts = _load_contracts()

    model = PowerModel({"ewfd.Rtotal": [0.7], "ewfd.Atotal": [0.3]})
    with pytest.raises(KeyError):
        contracts.require_passive_power(model, [1e-6], "fixture")
    assert contracts.require_passive_power(model, [1e-6], "fixture", opaque_termination=True) == {
        "R": [0.7],
        "A": [0.3],
        "T": [0.0],
    }


def test_save_is_published_only_after_a_nonempty_staging_model(tmp_path):
    contracts = _load_contracts()

    class Model:
        @staticmethod
        def save(path, copy):
            assert copy is True
            Path(path).write_bytes(b"model")

    target = tmp_path / "result.mph"
    assert contracts.save_required(Model(), target) == target.resolve()
    assert target.read_bytes() == b"model"

    class EmptyModel:
        @staticmethod
        def save(path, copy):
            assert copy is True
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


def test_drude_recipe_uses_continuous_film_and_routes_patterned_film_to_partition_recipe():
    source = (Path(__file__).parents[2] / "recipes" / "mim_drude_sweep.py").read_text(
        encoding="utf-8"
    )

    assert "baseline_reflection = solve_required('continuous baseline')" in source
    assert "mim_patch_partition.py" in source
    assert "patch_lth" not in source


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
    assert "pf.selection('face').set('b_air', [5])" in source
    assert "after_partition_boundaries" in source
    assert source.index("before_partition_boundaries =") < source.index("wp =")
    assert "g.measureFinal()" in source
    assert "math.isclose(area, ax*ax" in source
    assert "copy_face.selection('source').set" in source
    assert "copy_face.selection('destination').set" in source


def test_layered_transition_retains_layer_stack_and_explicit_material_properties():
    source = (Path(__file__).parents[2] / "recipes" / "mim_lml_continuous.py").read_text(
        encoding="utf-8"
    )

    assert "LayeredMaterialLink" in source
    assert "lml_au.set('link','lm_au')" in source
    assert "'epsilonr_mat': 'userdef'" in source
    assert "'lth': str(t_au)" in source


@pytest.mark.parametrize(
    "name", ["mim_drude_sweep.py", "mim_lml_continuous.py", "mim_patch_partition.py"]
)
def test_mim_sweep_mesh_uses_verified_named_domains(name):
    source = (Path(__file__).parents[2] / "recipes" / name).read_text(encoding="utf-8")

    assert "sw.selection().set(al2_domains + air_domains)" in source
    assert "sw.selection().set([1,2])" not in source
