"""Basic tests for COMSOL MCP Server."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture()
def permissive_session_ownership(monkeypatch, tmp_path):
    """Keep session lifecycle tests independent of host-wide solver state."""
    import src.tools.session as session_module

    class PermissiveOwnership:
        runtime_dir = tmp_path

        def __init__(self):
            self.releases = 0

        def preflight(self, **_kwargs):
            return {"ready": True, "blockers": []}

        def acquire(self, **_kwargs):
            return {
                "success": True,
                "lease": {"acquisition_id": "test-acquisition"},
            }

        def heartbeat(self, **_kwargs):
            return True

        def release(self):
            self.releases += 1
            return {"success": True, "released": True}

    manager = session_module.SessionManager()
    monkeypatch.setattr(session_module, "STARTUP_RESPONSE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(session_module, "STARTUP_TIMEOUT_SECONDS", 2.0)
    ownership = PermissiveOwnership()
    monkeypatch.setattr(manager, "_ownership", ownership)
    manager._client = None
    manager._reusable_client = None
    manager._reusable_client_kind = None
    manager._models = {}
    manager._model_paths = {}
    manager._model_revisions = {}
    manager._model_cleanup_paths = {}
    manager._current_model = None
    manager._starting = False
    manager._start_error = None
    manager._start_message = ""
    manager._start_cancel_requested = False
    manager._start_cleanup_pending = False
    manager._start_timed_out = False
    manager._host_restart_required = False
    manager._start_attempt_id = None
    manager._startup_record = None
    manager._start_thread = None
    manager._start_watchdog = None
    manager._owns_solver_lease = False
    yield manager
    if manager._start_thread is not None:
        manager._start_thread.cancel()
    if manager._start_watchdog is not None:
        manager._start_watchdog.cancel()


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

    def test_version_paths_reject_dot_model_names_and_avoid_same_second_collisions(self, tmp_path):
        from src.utils.versioning import generate_version_path

        first = generate_version_path("model.mph", base_path=tmp_path)
        second = generate_version_path("model.mph", base_path=tmp_path)

        assert first != second
        with pytest.raises(ValueError, match="safe model directory"):
            generate_version_path("..", base_path=tmp_path)

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

        monkeypatch.setattr(session_module, "STARTUP_TIMEOUT_SECONDS", 60.0)
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

    def test_concurrent_start_calls_create_exactly_one_client(
        self, monkeypatch, permissive_session_ownership
    ):
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

    def test_reset_discards_client_that_finishes_starting_late(
        self, monkeypatch, permissive_session_ownership
    ):
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

    def test_start_does_not_forward_unsupported_products(
        self, monkeypatch, permissive_session_ownership
    ):
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

    def test_start_response_precedes_client_initialization(
        self, monkeypatch, permissive_session_ownership
    ):
        import json
        import time

        import src.tools.session as session_module

        sm = session_module.SessionManager()
        entered = threading.Event()

        class FakeClient:
            version = "6.4"
            cores = 2
            standalone = True

            def clear(self):
                return None

        def create_client(**_kwargs):
            entered.set()
            return FakeClient()

        monkeypatch.setattr(session_module, "STARTUP_RESPONSE_GRACE_SECONDS", 0.25)
        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        started_at = time.perf_counter()
        result = sm.start(cores=2)
        elapsed = time.perf_counter() - started_at

        assert result["starting"] is True
        assert elapsed < 0.2
        assert not entered.is_set()
        assert entered.wait(timeout=1)
        sm._start_thread.join(timeout=1)

        receipt = json.loads(sm._startup_path().read_text(encoding="utf-8"))
        phases = [item["phase"] for item in receipt["phases"]]
        assert phases[:4] == [
            "request_accepted",
            "response_ready",
            "worker_entered",
            "preflight_completed",
        ]
        response_ready = next(
            item for item in receipt["phases"] if item["phase"] == "response_ready"
        )
        worker_entered = next(
            item for item in receipt["phases"] if item["phase"] == "worker_entered"
        )
        assert worker_entered["elapsed_seconds"] - response_ready["elapsed_seconds"] >= 0.2
        sm.disconnect()

    def test_start_response_does_not_wait_for_blocking_preflight(
        self, monkeypatch, permissive_session_ownership
    ):
        import time

        import src.tools.session as session_module

        sm = session_module.SessionManager()
        delegate = sm._ownership
        entered = threading.Event()
        release = threading.Event()

        class BlockingPreflightOwnership:
            runtime_dir = delegate.runtime_dir

            def preflight(self, **kwargs):
                entered.set()
                assert release.wait(timeout=1)
                return delegate.preflight(**kwargs)

            def acquire(self, **kwargs):
                return delegate.acquire(**kwargs)

            def heartbeat(self, **kwargs):
                return delegate.heartbeat(**kwargs)

            def release(self):
                return delegate.release()

        class FakeClient:
            version = "6.4"
            cores = 2
            standalone = True

            def clear(self):
                return None

        sm._ownership = BlockingPreflightOwnership()
        monkeypatch.setattr(session_module.mph, "Client", lambda **_kwargs: FakeClient())
        monkeypatch.setattr(session_module.mph_session, "client", None)

        started_at = time.perf_counter()
        result = sm.start(cores=2)
        response_elapsed = time.perf_counter() - started_at
        assert result["starting"] is True
        assert response_elapsed < 0.2
        assert entered.wait(timeout=1)
        status = sm.get_status()
        assert status["starting"] is True
        assert status["owns_solver_lease"] is False

        release.set()
        sm._start_thread.join(timeout=1)
        assert sm.get_status()["connected"] is True
        sm.disconnect()

    def test_standalone_start_disconnect_start_reuses_exact_client(
        self, monkeypatch, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        clients = []

        class FakeClient:
            version = "6.4"
            cores = 2
            standalone = True

            def __init__(self):
                self.clear_calls = 0

            def clear(self):
                self.clear_calls += 1

        def create_client(**_kwargs):
            client = FakeClient()
            clients.append(client)
            return client

        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        assert sm.start(cores=2)["starting"] is True
        sm._start_thread.join(timeout=1)
        first_client = sm.client
        assert first_client is clients[0]

        first_disconnect = sm.disconnect()
        assert first_disconnect["success"] is True
        assert first_disconnect["client_reusable"] is True
        assert sm.client is None

        assert sm.start(cores=2)["starting"] is True
        sm._start_thread.join(timeout=1)
        assert sm.client is first_client
        assert len(clients) == 1
        sm.disconnect()

    def test_start_failure_atomically_releases_lease_and_persists_terminal_state(
        self, monkeypatch, permissive_session_ownership
    ):
        import json

        import src.tools.session as session_module

        sm = session_module.SessionManager()

        def fail_client(**_kwargs):
            raise RuntimeError("injected startup failure")

        monkeypatch.setattr(session_module.mph, "Client", fail_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        assert sm.start()["starting"] is True
        sm._start_thread.join(timeout=1)

        status = sm.get_status()
        assert status["connected"] is False
        assert status["starting"] is False
        assert status["cleanup_pending"] is False
        assert status["owns_solver_lease"] is False
        assert permissive_session_ownership._ownership.releases == 1
        receipt = json.loads(sm._startup_path().read_text(encoding="utf-8"))
        assert receipt["state"] == "failed"
        assert receipt["terminal"] is True
        assert receipt["owns_solver_lease"] is False

    def test_start_timeout_is_terminal_and_retains_lease_until_cleanup(
        self, monkeypatch, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        entered = threading.Event()
        release = threading.Event()

        class FakeClient:
            version = "6.4"
            cores = 2
            standalone = True

            def clear(self):
                return None

        def create_client(**_kwargs):
            entered.set()
            assert release.wait(timeout=3)
            return FakeClient()

        monkeypatch.setattr(session_module, "STARTUP_TIMEOUT_SECONDS", 60.0)
        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        assert sm.start()["starting"] is True
        assert entered.wait(timeout=3)
        sm._mark_start_timeout(sm._start_attempt_id)

        timed_out = sm.get_status()
        assert timed_out["starting"] is False
        assert timed_out["cleanup_pending"] is True
        assert timed_out["owns_solver_lease"] is True
        assert timed_out["startup"]["state"] == "timed_out_cleanup_pending"
        assert permissive_session_ownership._ownership.releases == 0

        release.set()
        sm._start_thread.join(timeout=3)
        cleaned = sm.get_status()
        assert cleaned["cleanup_pending"] is False
        assert cleaned["owns_solver_lease"] is False
        assert cleaned["startup"]["state"] == "timed_out"
        assert permissive_session_ownership._ownership.releases == 1

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

    def test_connect_rejects_pending_local_start_cleanup_before_loading_mph(
        self, monkeypatch, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        sm._start_cleanup_pending = True
        monkeypatch.setattr(
            session_module,
            "_load_mph",
            lambda: pytest.fail("pending cleanup must be rejected before loading MPh"),
        )
        try:
            result = sm.connect(port=2036)
        finally:
            sm._start_cleanup_pending = False

        assert result["success"] is False
        assert result["cleanup_pending"] is True

    def test_connect_rolls_back_client_when_lease_heartbeat_is_unverified(
        self, monkeypatch, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()

        class RemoteClient:
            standalone = False
            version = "6.4"

            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")

        client = RemoteClient()
        monkeypatch.setattr(sm._ownership, "heartbeat", lambda **_kwargs: False)
        monkeypatch.setattr(
            session_module,
            "_load_mph",
            lambda: (
                SimpleNamespace(Client=lambda **_kwargs: client),
                SimpleNamespace(client=None),
            ),
        )

        result = sm.connect(port=2036)

        assert result["success"] is False
        assert "heartbeat" in result["error"]
        assert client.calls == ["disconnect"]
        assert sm.client is None
        assert sm._reusable_client is client
        assert sm._owns_solver_lease is False
        assert permissive_session_ownership._ownership.releases == 1

    def test_disconnect_retains_client_and_lease_until_retirement_is_verified(
        self, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()

        class RemoteClient:
            standalone = False

            def __init__(self):
                self.calls = []
                self.fail_disconnect = True

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")
                if self.fail_disconnect:
                    raise RuntimeError("disconnect uncertain")

        client = RemoteClient()
        sm._client = client
        sm._owns_solver_lease = True

        first = sm.disconnect()

        assert first["success"] is False
        assert first["cleanup_pending"] is True
        assert sm.client is client
        assert sm._owns_solver_lease is True
        assert permissive_session_ownership._ownership.releases == 0

        client.fail_disconnect = False
        second = sm.disconnect()

        assert second["success"] is True
        assert client.calls == ["clear", "disconnect", "clear", "disconnect"]
        assert sm.client is None
        assert sm._owns_solver_lease is False
        assert permissive_session_ownership._ownership.releases == 1

    def test_cancelled_start_retires_exact_client_only_once_after_failure(
        self, monkeypatch, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()

        class RemoteClient:
            standalone = False
            version = "6.4"

            def __init__(self):
                self.calls = []

            def clear(self):
                self.calls.append("clear")

            def disconnect(self):
                self.calls.append("disconnect")

        client = RemoteClient()
        monkeypatch.setattr(
            session_module,
            "_load_mph",
            lambda: (
                SimpleNamespace(Client=lambda **_kwargs: client),
                SimpleNamespace(client=None),
            ),
        )

        def fail_heartbeat(**_kwargs):
            sm._start_cancel_requested = True
            raise RuntimeError("heartbeat failed")

        monkeypatch.setattr(sm._ownership, "heartbeat", fail_heartbeat)
        sm._start_attempt_id = "test-attempt"
        sm._starting = True

        sm._start_worker("test-attempt", {})

        assert client.calls == ["clear", "disconnect"]
        assert sm.client is None
        assert sm._owns_solver_lease is False
        assert permissive_session_ownership._ownership.releases == 1

    def test_start_preflight_refusal_is_terminal_without_mph_client(
        self, monkeypatch, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        called = False

        class RefusingOwnership:
            runtime_dir = permissive_session_ownership._ownership.runtime_dir

            def preflight(self, **kwargs):
                return {"ready": False, "blockers": ["external solver detected"]}

            def release(self):
                raise AssertionError("No lease was acquired")

        def create_client(**kwargs):
            nonlocal called
            called = True
            raise AssertionError("mph.Client must not be called after failed preflight")

        sm._ownership = RefusingOwnership()
        monkeypatch.setattr(session_module.mph, "Client", create_client)
        result = sm.start(cores=2)
        sm._start_thread.join(timeout=1)

        assert result["success"] is True
        assert result["starting"] is True
        status = sm.get_status()
        assert status["starting"] is False
        assert "external solver detected" in status["error"]
        assert status["owns_solver_lease"] is False
        assert called is False

    def test_timeout_during_mph_load_is_not_overwritten(
        self, monkeypatch, permissive_session_ownership
    ):
        import src.tools.session as session_module

        sm = session_module.SessionManager()
        client_called = False

        def load_mph_and_timeout():
            sm._mark_start_timeout(sm._start_attempt_id)
            return session_module.mph, session_module.mph_session

        def create_client(**_kwargs):
            nonlocal client_called
            client_called = True
            raise AssertionError("timed-out startup must not create a client")

        monkeypatch.setattr(session_module, "STARTUP_TIMEOUT_SECONDS", 60.0)
        monkeypatch.setattr(session_module, "_load_mph", load_mph_and_timeout)
        monkeypatch.setattr(session_module.mph, "Client", create_client)
        monkeypatch.setattr(session_module.mph_session, "client", None)

        assert sm.start()["starting"] is True
        sm._start_thread.join(timeout=3)

        status = sm.get_status()
        assert client_called is False
        assert status["starting"] is False
        assert status["cleanup_pending"] is False
        assert status["owns_solver_lease"] is False
        assert status["startup"]["state"] == "timed_out"
        assert all(
            phase["phase"] not in {"mph_loaded", "client_initialization_started"}
            for phase in status["startup"]["phases"]
        )
