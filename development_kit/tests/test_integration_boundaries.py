"""Safety tests for standalone integration probe boundaries."""

import ast
import runpy
from pathlib import Path

import mph
import pytest


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
