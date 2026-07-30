"""Safety tests for standalone integration probe boundaries."""

import ast
import runpy
from pathlib import Path

import mph
import pytest

from development_kit.tests.integration import clientapi_property_acceptance as property_gate


def test_clientapi_property_acceptance_uses_explicit_runtime_checks():
    script = (
        Path(__file__).parents[2]
        / "development_kit"
        / "tests"
        / "integration"
        / "clientapi_property_acceptance.py"
    )
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Assert)]
    assert "_require(" in source


def test_property_acceptance_independently_reads_temporary_value_and_restores(
    monkeypatch,
):
    reads = iter(("old", "temporary", "old"))
    writes = []
    monkeypatch.setattr(
        property_gate,
        "get_existing_property",
        lambda *_args: {"success": True, "value": next(reads)},
    )
    monkeypatch.setattr(
        property_gate,
        "set_existing_property",
        lambda *_args: writes.append(_args[-1]) or {"success": True, "new_value": _args[-1]},
    )

    result = property_gate._round_trip_case(
        object(), "geometry_feature", "geom1/blk1", "base", "temporary"
    )

    assert writes == ["temporary", "old"]
    assert result["temporary"] == "temporary"
    assert result["restored"] == "old"


def test_property_acceptance_restores_after_temporary_readback_mismatch(monkeypatch):
    reads = iter(("old", "unexpected", "old"))
    writes = []
    monkeypatch.setattr(
        property_gate,
        "get_existing_property",
        lambda *_args: {"success": True, "value": next(reads)},
    )
    monkeypatch.setattr(
        property_gate,
        "set_existing_property",
        lambda *_args: writes.append(_args[-1]) or {"success": True, "new_value": _args[-1]},
    )

    with pytest.raises(RuntimeError, match="temporary property readback mismatch"):
        property_gate._round_trip_case(
            object(), "geometry_feature", "geom1/blk1", "base", "temporary"
        )

    assert writes == ["temporary", "old"]


def test_property_acceptance_verifies_exact_runtime_and_observes_solution_tags():
    class Java:
        @staticmethod
        def getComsolVersion():
            return "COMSOL Multiphysics 6.4.0.293"

    client = type("Client", (), {"version": "6.4", "java": Java()})()
    release = property_gate._verify_runtime_release(client)

    assert release["verified"] is True
    assert release["expected_build"] == "6.4.0.293"
    with pytest.raises(RuntimeError, match="not 6.4.0.293"):
        property_gate._verify_runtime_release(
            type(
                "Client",
                (),
                {
                    "version": "6.4",
                    "java": type("J", (), {"getComsolVersion": lambda _self: "6.4.0.292"})(),
                },
            )()
        )

    java_model = type(
        "Model",
        (),
        {"sol": lambda _self: type("Solutions", (), {"tags": lambda _self: ["sol2", "sol1"]})()},
    )()
    assert property_gate._solution_tags(java_model) == ["sol1", "sol2"]


@pytest.mark.parametrize(
    "script_path",
    [
        "development_kit/tests/integration/probes/capacitor.py",
        "development_kit/tests/integration/probes/study_mesh.py",
        "development_kit/tests/integration/probes/unicode_save.py",
    ],
)
def test_loading_standalone_probe_does_not_create_client(monkeypatch, script_path):
    def fail_client_creation(*args, **kwargs):
        raise AssertionError("mph.Client must not be called while loading a probe")

    monkeypatch.setattr(mph, "Client", fail_client_creation)
    full_path = Path(__file__).parents[2] / script_path

    namespace = runpy.run_path(str(full_path), run_name="probe_import_test")

    assert callable(namespace["main"])
