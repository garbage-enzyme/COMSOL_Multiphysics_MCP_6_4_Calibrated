"""Solver-free checks for the parallel-plate capacitor recipe."""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

RECIPE = Path(__file__).parents[2] / "recipes" / "parallel_plate_capacitor.py"
CAPACITOR_FIXTURE = (
    Path(__file__).parents[1]
    / "release"
    / "integration_fixtures"
    / "capacitor_clientapi_regression.json"
)


def _namespace(monkeypatch):
    monkeypatch.setitem(sys.modules, "mph", SimpleNamespace(Model=object))
    loaded = runpy.run_path(str(RECIPE), run_name="capacitor_recipe_test")
    return loaded["main"].__globals__


def test_theory_and_parameterized_geometry_match_release_fixture(monkeypatch):
    namespace = _namespace(monkeypatch)
    fixture = json.loads(CAPACITOR_FIXTURE.read_text(encoding="utf-8"))

    assert namespace["_geometry_size_expressions"]() == (
        "plate_side",
        "plate_side",
        "plate_gap",
    )
    expected = fixture["acceptance"]["theory_capacitance_pf"]
    assert namespace["_theoretical_capacitance_pf"]() == pytest.approx(expected)


def test_electrodes_require_distinct_bounding_planes_and_normals(monkeypatch):
    namespace = _namespace(monkeypatch)
    bounding_box = [0.0, 0.01, 0.0, 0.01, 0.0, 0.001]
    faces = [
        {"boundary": 1, "center_m": [0.0, 0.005, 0.0005], "normal": [-1.0, 0.0, 0.0]},
        {"boundary": 2, "center_m": [0.01, 0.005, 0.0005], "normal": [1.0, 0.0, 0.0]},
        {"boundary": 3, "center_m": [0.005, 0.005, 0.0], "normal": [0.0, 0.0, -1.0]},
        {"boundary": 4, "center_m": [0.005, 0.005, 0.001], "normal": [0.0, 0.0, 1.0]},
    ]

    electrodes = namespace["_identify_electrode_faces"](faces, bounding_box)

    assert electrodes["ground"]["boundary"] == 3
    assert electrodes["potential"]["boundary"] == 4
    with pytest.raises(RuntimeError, match="not uniquely identified"):
        namespace["_identify_electrode_faces"]([*faces, dict(faces[-1])], bounding_box)


def test_staged_model_is_published_only_after_client_and_lease_cleanup(ascii_tmp_path, monkeypatch):
    namespace = _namespace(monkeypatch)
    output = ascii_tmp_path / "capacitor.mph"
    receipt = ascii_tmp_path / "receipt.json"
    staging = ascii_tmp_path / ".capacitor.staging.mph"
    events = []
    model = SimpleNamespace(java=object())

    class Ownership:
        def preflight(self, **_kwargs):
            return {"ready": True}

        def acquire(self, **_kwargs):
            return {"success": True}

        def heartbeat(self, **_kwargs):
            return None

        def release(self):
            events.append("release")
            return {"success": True}

    class Client:
        port = None

        def create(self, _name):
            return model

        def clear(self):
            events.append("clear")

    namespace["parse_args"] = lambda: SimpleNamespace(
        output_model=output,
        receipt=receipt,
        plate_side_m=0.01,
        plate_separation_m=0.001,
        relative_permittivity=2.1,
        potential_v=1.0,
        maximum_relative_error=1e-6,
        solve=True,
        overwrite_output=False,
    )
    namespace["SolverOwnership"] = lambda owner: Ownership()
    namespace["mph"] = SimpleNamespace(Client=lambda version: Client())
    namespace["build_parallel_plate_capacitor"] = lambda *_args, **_kwargs: {"built": True}
    namespace["validate_solution"] = lambda *_args, **_kwargs: {"status": "verified"}

    def save(_java, _output):
        staging.write_bytes(b"model")
        events.append("save")
        return staging

    def publish(source, destination, *, overwrite):
        assert events == ["save", "clear", "release"]
        events.append("publish")
        source.replace(destination)

    namespace["save_staged_model"] = save
    namespace["publish_staged_model"] = publish
    namespace["_atomic_json"] = lambda *_args: events.append("receipt")

    namespace["main"]()

    assert events == ["save", "clear", "release", "publish", "receipt"]
    assert output.read_bytes() == b"model"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("plate_side_m", 0.0, "--plate-side-m"),
        ("plate_separation_m", float("nan"), "--plate-separation-m"),
        ("relative_permittivity", 1001.0, "must not exceed 1000"),
        ("maximum_relative_error", 0.2, "must not exceed 0.1"),
    ],
)
def test_invalid_inputs_fail_before_solver_ownership(
    ascii_tmp_path, monkeypatch, field, value, message
):
    namespace = _namespace(monkeypatch)
    values = {
        "plate_side_m": 0.01,
        "plate_separation_m": 0.001,
        "relative_permittivity": 2.1,
        "potential_v": 1.0,
        "maximum_relative_error": 1e-6,
    }
    values[field] = value
    ownership_calls = []
    namespace["parse_args"] = lambda: SimpleNamespace(
        output_model=ascii_tmp_path / "invalid.mph",
        receipt=ascii_tmp_path / "invalid.receipt.json",
        overwrite_output=False,
        solve=False,
        **values,
    )
    namespace["SolverOwnership"] = lambda **_kwargs: ownership_calls.append(True)

    with pytest.raises(ValueError, match=message):
        namespace["main"]()
    assert ownership_calls == []
