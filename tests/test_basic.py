"""Basic tests for COMSOL MCP Server."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest


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

    def test_disconnect_cancels_background_start(self, monkeypatch):
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
