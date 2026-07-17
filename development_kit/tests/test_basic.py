"""Basic tests for COMSOL MCP Server."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest


@pytest.fixture()
def permissive_session_ownership(monkeypatch):
    """Keep session lifecycle tests independent of host-wide solver state."""
    import src.tools.session as session_module

    class PermissiveOwnership:
        def preflight(self, **_kwargs):
            return {"ready": True, "blockers": []}

        def acquire(self, **_kwargs):
            return {"success": True}

        def heartbeat(self, **_kwargs):
            return {"success": True}

        def release(self):
            return {"success": True, "released": True}

    manager = session_module.SessionManager()
    monkeypatch.setattr(manager, "_ownership", PermissiveOwnership())
    return manager


class TestVersioning:
    """Tests for version naming utilities."""
    
    def test_generate_version_name(self):
        from src.utils.versioning import generate_version_name
        
        result = generate_version_name("model.mph")
        assert result.startswith("model_")
        assert result.endswith(".mph")
        assert len(result) > len("model.mph")
    
    def test_generate_version_name_no_extension(self):
        from src.utils.versioning import generate_version_name
        
        result = generate_version_name("model")
        assert result.startswith("model_")
        assert result.endswith(".mph")
    
    def test_generate_version_path(self, tmp_path):
        from src.utils.versioning import generate_version_path

        result = generate_version_path("/path/to/model.mph", base_path=tmp_path)
        path = Path(result)
        assert path.parent == tmp_path / "model"
        assert path.name.startswith("model_")
        assert path.suffix == ".mph"

    def test_latest_path_uses_custom_base(self, tmp_path):
        from src.utils.versioning import generate_latest_path

        result = Path(generate_latest_path("nested/model.mph", base_path=tmp_path))

        assert result == tmp_path / "model" / "model_latest.mph"
        assert result.parent.is_dir()

    def test_default_model_storage_uses_runtime_root(self, monkeypatch, tmp_path):
        from src.utils.versioning import get_model_directory

        monkeypatch.setenv("COMSOL_MCP_RUNTIME_DIR", str(tmp_path))

        assert get_model_directory("model.mph") == tmp_path / "models" / "model"

    def test_list_versions_uses_custom_base(self, tmp_path):
        from src.utils.versioning import list_model_versions

        model_dir = tmp_path / "model"
        model_dir.mkdir()
        older = model_dir / "model_20260101_000000.mph"
        newer = model_dir / "model_20260102_000000.mph"
        latest = model_dir / "model_latest.mph"
        for path in (older, newer, latest):
            path.touch()
        older.touch()
        newer.touch()

        result = list_model_versions("model", base_path=tmp_path)

        assert set(result) == {str(older), str(newer)}
        assert str(latest) not in result
    
    def test_parse_version_info_valid(self):
        from src.utils.versioning import parse_version_info
        
        result = parse_version_info("model_20260215_143022.mph")
        assert result is not None
        assert result["base_name"] == "model"
        assert result["timestamp"] == "20260215_143022"
    
    def test_parse_version_info_invalid(self):
        from src.utils.versioning import parse_version_info
        
        result = parse_version_info("model.mph")
        assert result is None
        
        result = parse_version_info("model_20260215.mph")
        assert result is None


class TestSessionManager:
    """Tests for session manager (without actual COMSOL)."""
    
    def test_session_manager_singleton(self):
        from src.tools.session import SessionManager
        
        sm1 = SessionManager()
        sm2 = SessionManager()
        assert sm1 is sm2

    def test_session_manager_concurrent_singleton(self):
        from src.tools.session import SessionManager

        with ThreadPoolExecutor(max_workers=8) as executor:
            managers = list(executor.map(lambda _: SessionManager(), range(32)))

        assert all(manager is managers[0] for manager in managers)
        assert "_models" in managers[0].__dict__
        assert "_start_lock" in managers[0].__dict__
    
    def test_session_manager_initial_state(self):
        from src.tools.session import SessionManager
        
        sm = SessionManager()
        assert sm.client is None
        assert not sm.is_connected
        assert sm.current_model is None
        assert sm.models == {}
    
    def test_get_status_disconnected(self):
        from src.tools.session import SessionManager
        
        sm = SessionManager()
        status = sm.get_status()
        assert status["connected"] is False

    def test_get_status_normalizes_model_paths_for_mcp_json(self, tmp_path):
        import json

        from src.tools.session import SessionManager

        class FakeClient:
            version = "6.4"
            cores = 4
            standalone = True

            def names(self):
                return ["model"]

        class FakeModel:
            def file(self):
                return tmp_path / "model.mph"

        sm = SessionManager()
        sm._client = FakeClient()
        sm._models = {"model": FakeModel()}
        sm._model_paths = {"model": str(tmp_path / "model.mph")}
        sm._current_model = "model"
        try:
            status = sm.get_status()
            json.dumps(status)
            assert status["models"][0]["file"] == str(tmp_path / "model.mph")
            assert len(status["models"][0]["revision_sha256"]) == 64
            assert status["models"][0]["revision_sequence"] == 0
        finally:
            sm._client = None
            sm._models = {}
            sm._model_paths = {}
            sm._model_revisions = {}
            sm._current_model = None

    def test_disconnect_releases_client(self):
        from src.tools.session import SessionManager

        class FakeClient:
            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")

        sm = SessionManager()
        client = FakeClient()
        sm._client = client
        sm._models = {"model": object()}
        sm._current_model = "model"

        result = sm.disconnect()

        assert result["success"] is True
        assert client.calls == ["clear", "disconnect"]
        assert sm.client is None
        assert sm.models == {}
        assert sm.current_model is None

    def test_remove_model_cleans_tracked_clone_file(self, tmp_path):
        from src.tools.session import SessionManager

        class FakeClient:
            def remove(self, model):
                return None

        class FakeModel:
            def name(self):
                return "clone"

        clone_dir = tmp_path / "comsol_mcp_clone_test"
        clone_dir.mkdir()
        clone_file = clone_dir / "clone.mph"
        clone_file.write_bytes(b"model")

        sm = SessionManager()
        sm._client = FakeClient()
        sm._models = {}
        sm._model_cleanup_paths = {}
        sm.add_model(FakeModel(), cleanup_path=str(clone_file))

        assert sm.remove_model("clone") is True
        assert not clone_file.exists()
        assert not clone_dir.exists()
        sm._client = None

    def test_clear_models_preserves_connected_client(self):
        from src.tools.session import SessionManager

        class FakeClient:
            def __init__(self):
                self.removed = []

            def remove(self, model):
                self.removed.append(model)

        sm = SessionManager()
        client = FakeClient()
        first = object()
        second = object()
        sm._client = client
        sm._models = {"first": first, "second": second}
        sm._model_cleanup_paths = {}
        sm._current_model = "first"

        result = sm.clear_models()

        assert result["success"] is True
        assert result["removed"] == 2
        assert client.removed == [first, second]
        assert sm.client is client
        assert sm.models == {}
        assert sm.current_model is None
        sm._client = None

    def test_reset_disconnects_client_and_marks_destructive_action(self):
        from src.tools.session import SessionManager

        class FakeClient:
            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")

        sm = SessionManager()
        client = FakeClient()
        sm._client = client
        sm._models = {"model": object()}
        sm._model_cleanup_paths = {}
        sm._current_model = "model"

        result = sm.reset()

        assert result["success"] is True
        assert result["reset"] is True
        assert client.calls == ["clear", "disconnect"]
        assert sm.client is None

    def test_disconnect_cancels_background_start(self, monkeypatch, permissive_session_ownership):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        created = threading.Event()
        release = threading.Event()

        class FakeClient:
            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")

        client = FakeClient()

        def create_client(**kwargs):
            created.set()
            assert release.wait(timeout=2)
            return client

        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        started = sm.start(cores=2)
        assert started["starting"] is True
        assert created.wait(timeout=2)

        cancelled = sm.disconnect()
        assert cancelled["starting"] is True
        release.set()
        sm._start_thread.join(timeout=2)

        assert not sm._start_thread.is_alive()
        assert sm.client is None
        assert sm.get_status()["connected"] is False
        assert client.calls == ["clear", "disconnect"]

    def test_concurrent_start_calls_create_exactly_one_client(self, monkeypatch, permissive_session_ownership):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        created = threading.Event()
        release = threading.Event()
        calls = []

        class FakeClient:
            version = "6.4"
            cores = 2
            standalone = True

            def clear(self):
                return None

            def disconnect(self):
                return None

        def create_client(**kwargs):
            calls.append(kwargs)
            created.set()
            assert release.wait(timeout=2)
            return FakeClient()

        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: sm.start(cores=2), range(16)))

        assert created.wait(timeout=2)
        assert all(result.get("starting") for result in results)
        assert len(calls) == 1

        release.set()
        sm._start_thread.join(timeout=2)
        assert sm.client is not None
        sm.reset()

    def test_reset_discards_client_that_finishes_starting_late(self, monkeypatch, permissive_session_ownership):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        created = threading.Event()
        release = threading.Event()

        class FakeClient:
            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")

        client = FakeClient()

        def create_client(**kwargs):
            created.set()
            assert release.wait(timeout=2)
            return client

        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        assert sm.start()["starting"] is True
        assert created.wait(timeout=2)

        reset = sm.reset()
        assert reset["reset"] is True
        assert reset["starting"] is True

        release.set()
        sm._start_thread.join(timeout=2)

        assert sm.client is None
        assert client.calls == ["clear", "disconnect"]

    def test_start_is_idempotent_when_connected(self):
        from src.tools.session import SessionManager

        class FakeClient:
            version = "6.4"
            cores = 4
            standalone = True

            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")

        sm = SessionManager()
        client = FakeClient()
        model = object()
        sm._client = client
        sm._models = {"model": model}
        sm._current_model = "model"

        result = sm.start(cores=8)

        assert result["success"] is True
        assert result["connected"] is True
        assert client.calls == []
        assert sm.models == {"model": model}
        assert sm.current_model == "model"

        sm.disconnect()

    def test_start_does_not_forward_unsupported_products(self, monkeypatch, permissive_session_ownership):
        import src.tools.session as session_module

        captured = {}

        class FakeClient:
            def clear(self):
                return None

            def disconnect(self):
                return None

        def create_client(**kwargs):
            captured.update(kwargs)
            return FakeClient()

        sm = session_module.SessionManager()
        sm._client = None
        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        result = sm.start(cores=2, version="6.4", products=["ACDC"])
        sm._start_thread.join(timeout=2)

        assert result["success"] is True
        assert "warning" in result
        assert captured == {"cores": 2, "version": "6.4"}
        sm.disconnect()

    def test_start_failure_requires_explicit_reset(self, monkeypatch, permissive_session_ownership):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        calls = []

        def fail_client(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("planned start failure")

        monkeypatch.setattr(session_module.mph, "Client", fail_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        first = sm.start(cores=2)
        assert first["starting"] is True
        sm._start_thread.join(timeout=2)
        assert not sm._start_thread.is_alive()

        blocked = sm.start(cores=4)

        assert blocked["success"] is False
        assert blocked["reset_required"] is True
        assert len(calls) == 1

        reset = sm.reset()

        assert reset["success"] is True
        assert sm.get_status()["connected"] is False

    def test_connect_rejects_in_flight_local_start(self, monkeypatch):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        sm._client = None
        sm._starting = True
        called = False

        def create_client(**kwargs):
            nonlocal called
            called = True
            raise AssertionError("mph.Client must not be called")

        monkeypatch.setattr(session_module.mph, "Client", create_client)
        try:
            result = sm.connect(port=2036)
        finally:
            sm._starting = False

        assert result["success"] is False
        assert "still starting" in result["error"]
        assert called is False

    def test_start_preflight_refusal_never_calls_mph_client(self, monkeypatch):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        sm._client = None
        called = False

        class RefusingOwnership:
            def preflight(self, **kwargs):
                return {"ready": False, "blockers": ["external solver detected"]}

        def create_client(**kwargs):
            nonlocal called
            called = True
            raise AssertionError("mph.Client must not be called after failed preflight")

        previous = sm._ownership
        sm._ownership = RefusingOwnership()
        monkeypatch.setattr(session_module.mph, "Client", create_client)
        try:
            result = sm.start(cores=2)
        finally:
            sm._ownership = previous

        assert result["success"] is False
        assert result["preflight"]["blockers"] == ["external solver detected"]
        assert called is False
