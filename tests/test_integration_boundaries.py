"""Safety tests for standalone integration probe boundaries."""

from pathlib import Path
import runpy

import mph
import pytest


@pytest.mark.parametrize("script_name", ["test_e2e_cap.py", "test_study_mesh.py"])
def test_loading_standalone_probe_does_not_create_client(monkeypatch, script_name):
    def fail_client_creation(*args, **kwargs):
        raise AssertionError("mph.Client must not be called while loading a probe")

    monkeypatch.setattr(mph, "Client", fail_client_creation)
    script_path = Path(__file__).parents[1] / script_name

    namespace = runpy.run_path(str(script_path), run_name="probe_import_test")

    assert callable(namespace["main"])
