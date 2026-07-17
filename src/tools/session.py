"""Session management tools for COMSOL MCP Server."""

import hashlib
import json
import threading
from pathlib import Path
from typing import Optional
from mcp.server.fastmcp import FastMCP
import mph
import mph.session as mph_session

from .ownership import ownership_manager


class SessionManager:
    """Singleton manager for COMSOL client session."""

    _instance: Optional["SessionManager"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is not None:
            return cls._instance

        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance._client = None
                instance._models = {}
                instance._model_paths = {}
                instance._model_revisions = {}
                instance._model_cleanup_paths = {}
                instance._current_model = None
                # comsol_start runs mph.Client() in this background thread.
                instance._starting = False
                instance._start_thread = None
                instance._start_error = None
                instance._start_message = ""
                instance._start_cancel_requested = False
                instance._start_lock = threading.Lock()
                instance._ownership = ownership_manager
                instance._owns_solver_lease = False
                cls._instance = instance
        return cls._instance
    
    @property
    def client(self) -> Optional[mph.Client]:
        return self._client
    
    @property
    def is_connected(self) -> bool:
        return self._client is not None
    
    @property
    def current_model(self) -> Optional[str]:
        return self._current_model
    
    @property
    def models(self) -> dict[str, mph.Model]:
        return self._models.copy()
    
    def start(self, cores: Optional[int] = None, version: Optional[str] = None, products: Optional[list[str]] = None) -> dict:
        """Start a COMSOL client session (non-blocking)."""
        # Already connected — clear and reuse.
        if self._client is not None:
            return {
                "success": True,
                "connected": True,
                "version": self._client.version,
                "cores": self._client.cores,
                "standalone": self._client.standalone,
                "message": "COMSOL session is already connected; no action taken.",
            }

        # A background start is in flight — tell caller to poll status.
        with self._start_lock:
            if self._starting:
                return {
                    "success": True,
                    "starting": True,
                    "message": self._start_message or "COMSOL is still starting. Poll comsol_status."
                }
            if self._start_error:
                return {
                    "success": False,
                    "starting": False,
                    "reset_required": True,
                    "error": self._start_error,
                    "message": (
                        "The previous COMSOL start failed. Call session_reset "
                        "before retrying."
                    ),
                }
            preflight = self._ownership.preflight(
                session_state=self.get_status(),
                requested_version=version,
            )
            if not preflight["ready"]:
                return {
                    "success": False,
                    "starting": False,
                    "error": "COMSOL start preflight failed.",
                    "preflight": preflight,
                }
            claim = self._ownership.acquire(mode="local-client")
            if not claim["success"]:
                return {
                    "success": False,
                    "starting": False,
                    "error": claim["error"],
                    "ownership": claim.get("status"),
                }
            self._owns_solver_lease = True
            # Claim the starting flag for this call.
            self._starting = True
            self._start_error = None
            self._start_message = "Starting COMSOL client in background..."
            self._start_cancel_requested = False

        # A previous failure is retained until explicit session_reset. If a
        # previous attempt succeeded, _client would be set and we'd return above.
        # MPh 1.3.1 Client accepts cores/version/port/host only. COMSOL checks
        # out licensed products on demand when a physics interface is created.
        kwargs = {"cores": cores, "version": version}
        kwargs = {k: v for k, v in kwargs.items() if v is not None}

        self._start_thread = threading.Thread(
            target=self._start_worker,
            args=(kwargs,),
            name="comsol-start",
            daemon=True,
        )
        self._start_thread.start()

        result = {
            "success": True,
            "starting": True,
            "message": (
                "COMSOL is starting in the background (JVM + back-end). "
                "This takes 30-90s. Poll comsol_status until 'connected' is true; "
                "do NOT retry comsol_start."
            )
        }
        if products:
            result["warning"] = (
                "MPh 1.3.1 does not accept a products argument; COMSOL will "
                "load and license requested physics products on demand."
            )
        return result

    def _start_worker(self, kwargs: dict) -> None:
        """Runs mph.Client() in a daemon thread. Sets _client on success."""
        client = None
        try:
            # Reuse an MPh session client if one happens to exist.
            try:
                if mph_session.client is not None:
                    client = mph_session.client
            except Exception:
                pass
            if client is None:
                client = mph.Client(**kwargs)

            with self._start_lock:
                if self._start_cancel_requested:
                    self._start_message = "Start cancelled; releasing client."
                else:
                    self._client = client
                    self._start_message = "Client ready."
                    self._ownership.heartbeat(refresh_server_processes=True)
        except Exception as e:
            with self._start_lock:
                self._start_error = str(e)
                self._start_message = f"Start failed: {e}"
        finally:
            with self._start_lock:
                cancelled = self._start_cancel_requested
                self._starting = False
                self._start_cancel_requested = False

            if cancelled and client is not None:
                try:
                    client.clear()
                except Exception:
                    pass
            if cancelled:
                self._ownership.release()
                self._owns_solver_lease = False
                try:
                    client.disconnect()
                except Exception:
                    pass
                try:
                    if mph_session.client is client:
                        mph_session.client = None
                except Exception:
                    pass
    
    def connect(self, port: int, host: str = "localhost") -> dict:
        """Connect to a remote COMSOL server."""
        with self._start_lock:
            if self._starting:
                return {
                    "success": False,
                    "error": (
                        "A local COMSOL client is still starting. Poll "
                        "comsol_status before connecting to another server."
                    ),
                }
        if self._client is not None:
            return {
                "success": False,
                "error": "COMSOL session already running. Disconnect first."
            }
        preflight = self._ownership.preflight(session_state=self.get_status())
        if not preflight["ready"]:
            return {
                "success": False,
                "error": "COMSOL connection preflight failed.",
                "preflight": preflight,
            }
        claim = self._ownership.acquire(mode="remote-connect")
        if not claim["success"]:
            return {"success": False, "error": claim["error"], "ownership": claim.get("status")}
        self._owns_solver_lease = True
        try:
            if mph_session.client is not None:
                self._client = mph_session.client
                self._ownership.heartbeat(refresh_server_processes=True)
                return {
                    "success": True,
                    "version": self._client.version,
                    "port": port,
                    "host": host,
                    "message": "Reused existing client from MPh session."
                }
        except Exception:
            pass
        try:
            self._client = mph.Client(port=port, host=host)
            self._ownership.heartbeat(refresh_server_processes=True)
            return {
                "success": True,
                "version": self._client.version,
                "port": port,
                "host": host,
            }
        except Exception as e:
            self._ownership.release()
            self._owns_solver_lease = False
            return {"success": False, "error": str(e)}
    
    def disconnect(self) -> dict:
        """Disconnect and clear the session."""
        # A blocking mph.Client() construction cannot be interrupted safely.
        # Mark it for disposal as soon as the worker receives the client.
        with self._start_lock:
            if self._client is None and self._starting:
                self._start_cancel_requested = True
                self._start_message = "Cancellation requested; waiting for COMSOL startup to return."
                return {
                    "success": True,
                    "starting": True,
                    "message": self._start_message,
                }
            self._start_error = None
            self._start_message = ""
        if self._client is None:
            release = self._ownership.release() if self._owns_solver_lease else None
            self._owns_solver_lease = False
            result = {"success": True, "message": "No active session."}
            if release is not None:
                result["lease_release"] = release
            return result

        client = self._client
        try:
            client.clear()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass
        self._client = None
        for name in list(self._model_cleanup_paths):
            self._cleanup_model_artifact(name)
        self._models.clear()
        self._model_paths.clear()
        self._model_revisions.clear()
        self._current_model = None
        try:
            mph_session.client = None
            mph_session.server = None
            mph_session.thread = None
        except Exception:
            pass
        release = self._ownership.release() if self._owns_solver_lease else None
        self._owns_solver_lease = False
        result = {"success": True, "message": "Session disconnected and client destroyed."}
        if release is not None:
            result["lease_release"] = release
        return result
    
    def get_status(self) -> dict:
        """Get current session status."""
        # Background start in flight and not yet ready.
        if self._client is None and self._starting:
            return {
                "connected": False,
                "starting": True,
                "message": self._start_message or "COMSOL is starting in background. Poll again shortly."
            }
        # Previous background start failed.
        if self._client is None and self._start_error:
            return {
                "connected": False,
                "starting": False,
                "error": self._start_error,
                "message": self._start_message,
            }
        if self._client is None:
            return {
                "connected": False,
                "message": "No active COMSOL session."
            }
        
        # Status must remain responsive while a COMSOL call is blocked. Do not
        # invoke clientapi or model methods here; report only locally tracked state.
        model_list = []
        for name in self._models:
            model_info = {"name": name}
            model_path = self._model_paths.get(name)
            if model_path is not None:
                model_info["file"] = model_path
            revision = self.get_model_revision(name)
            if revision is not None:
                model_info["revision_sha256"] = revision["revision_sha256"]
                model_info["revision_sequence"] = revision["sequence"]
            model_list.append(model_info)
        
        return {
            "connected": True,
            "version": self._client.version,
            "cores": self._client.cores,
            "standalone": self._client.standalone,
            "models": model_list,
            "current_model": self._current_model,
        }

    def clear_models(self) -> dict:
        """Remove every tracked model while preserving the connected client."""
        with self._start_lock:
            if self._starting:
                return {
                    "success": False,
                    "error": "Cannot clear models while COMSOL is starting.",
                }

        names = list(self._models)
        failed = []
        for name in names:
            if not self.remove_model(name):
                failed.append(name)

        if failed:
            return {
                "success": False,
                "removed": len(names) - len(failed),
                "failed_models": failed,
                "message": "Some tracked models could not be removed.",
            }
        return {
            "success": True,
            "removed": len(names),
            "connected": self._client is not None,
            "message": "All tracked models were removed; the client was preserved.",
        }

    def reset(self) -> dict:
        """Explicitly destroy or cancel the current client lifecycle."""
        result = self.disconnect()
        return {
            **result,
            "reset": True,
            "message": (
                "Session reset requested. All tracked models are cleared and the "
                "owned client is disconnected or discarded after startup returns."
            ),
        }
    
    def add_model(self, model: mph.Model, cleanup_path: Optional[str] = None) -> str:
        """Add a model to tracking."""
        name = model.name()
        if name in self._model_cleanup_paths:
            self._cleanup_model_artifact(name)
        self._model_revisions.pop(name, None)
        self._models[name] = model
        if cleanup_path:
            self._model_cleanup_paths[name] = str(cleanup_path)
        if self._current_model is None:
            self._current_model = name
        try:
            model_path = model.file() if hasattr(model, "file") else None
            if model_path is not None:
                self._model_paths[name] = str(model_path)
            self._initialize_model_revision(name, self._model_paths.get(name))
            self._ownership.heartbeat(model_path=str(model_path) if model_path else None)
        except Exception:
            self._initialize_model_revision(name, self._model_paths.get(name))
        return name

    @staticmethod
    def _revision_hash(body: dict) -> str:
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _initialize_model_revision(
        self, name: str, source_path: Optional[str]
    ) -> dict:
        existing = self._model_revisions.get(name)
        if existing is not None:
            return dict(existing)
        body = {
            "model_name": name,
            "sequence": 0,
            "previous_revision_sha256": None,
            "operation": "model_registered",
            "source_path_present": source_path is not None,
        }
        revision = {**body, "revision_sha256": self._revision_hash(body)}
        self._model_revisions[name] = revision
        return dict(revision)

    def get_model_revision(self, name: Optional[str] = None) -> Optional[dict]:
        """Return the local optimistic-concurrency token without clientapi calls."""
        model_name = name or self._current_model
        if model_name is None or model_name not in self._models:
            return None
        return dict(
            self._model_revisions.get(model_name)
            or self._initialize_model_revision(
                model_name, self._model_paths.get(model_name)
            )
        )

    def advance_model_revision(self, name: str, operation: str) -> dict:
        """Advance one model token after a serialized successful mutation."""
        current = self.get_model_revision(name)
        if current is None:
            raise ValueError(f"Model revision unavailable: {name}")
        body = {
            "model_name": name,
            "sequence": int(current["sequence"]) + 1,
            "previous_revision_sha256": current["revision_sha256"],
            "operation": operation,
            "source_path_present": self._model_paths.get(name) is not None,
        }
        revision = {**body, "revision_sha256": self._revision_hash(body)}
        self._model_revisions[name] = revision
        return dict(revision)

    def preflight_long_operation(
        self, *, model_path: Optional[str] = None, output_path: Optional[str] = None
    ) -> dict:
        """Require the connected session to own the solver before long work."""
        result = self._ownership.preflight(
            session_state=self.get_status(),
            model_path=model_path,
            output_path=output_path,
        )
        lease = result["ownership"]["lease"]
        if self._client is None:
            result["blockers"].append("no connected COMSOL session")
        if lease.get("state") != "active" or not lease.get("owned_by_current_process", False):
            result["blockers"].append("current MCP process does not own the solver lease")
        result["blockers"] = list(dict.fromkeys(result["blockers"]))
        result["ready"] = not result["blockers"]
        result["success"] = result["ready"]
        return result

    def _cleanup_model_artifact(self, name: str) -> None:
        """Remove a tracked clone backing file after COMSOL releases it."""
        cleanup_path = self._model_cleanup_paths.pop(name, None)
        if not cleanup_path:
            return
        path = Path(cleanup_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return
        try:
            if path.parent.name.startswith("comsol_mcp_clone_"):
                path.parent.rmdir()
        except OSError:
            pass
    
    def get_model(self, name: Optional[str] = None) -> Optional[mph.Model]:
        """Get a model by name or current model."""
        if name is None:
            name = self._current_model
        return self._models.get(name)
    
    def set_current_model(self, name: str) -> bool:
        """Set the current active model."""
        if name in self._models:
            self._current_model = name
            return True
        return False
    
    def remove_model(self, name: str) -> bool:
        """Remove a model from tracking and client."""
        if name in self._models and self._client is not None:
            try:
                self._client.remove(self._models[name])
                del self._models[name]
                self._model_paths.pop(name, None)
                self._model_revisions.pop(name, None)
                self._cleanup_model_artifact(name)
                if self._current_model == name:
                    self._current_model = next(iter(self._models.keys()), None)
                return True
            except Exception:
                pass
        return False


session_manager = SessionManager()


def register_session_tools(mcp: FastMCP) -> None:
    """Register session management tools with the MCP server."""
    
    @mcp.tool()
    def comsol_start(cores: Optional[int] = None, version: Optional[str] = None, products: Optional[list[str]] = None) -> dict:
        """
        Start a local COMSOL client session.

        Non-blocking: spawns a daemon thread that runs mph.Client() (which
        blocks for 30-90s while the JVM and COMSOL back-end initialise). This
        tool returns immediately with ``{"starting": True}`` so the MCP call
        does not time out. Poll ``comsol_status`` until ``connected`` is true
        before calling any other COMSOL tool. Do NOT retry ``comsol_start``
        while a start is in flight — the second call will just report
        ``starting`` and reuse the same background thread.
        
        Args:
            cores: Number of processor cores to use (default: all available)
            version: COMSOL version to use, e.g., '6.0' (default: latest installed)
            products: Compatibility hint only. MPh 1.3.1 cannot preload a
                     product list; COMSOL checks out licensed products on demand.
        
        Returns:
            Session info including version and core count, or error message
        """
        return session_manager.start(cores=cores, version=version, products=products)
    
    @mcp.tool()
    def comsol_connect(port: int, host: str = "localhost") -> dict:
        """
        Connect to a remote COMSOL server.
        
        Args:
            port: Port number the COMSOL server is listening on
            host: Server hostname or IP address (default: 'localhost')
        
        Returns:
            Connection info or error message
        """
        return session_manager.connect(port=port, host=host)
    
    @mcp.tool()
    def comsol_disconnect() -> dict:
        """
        Disconnect from COMSOL and clear all models from memory.
        
        Returns:
            Success status and message
        """
        return session_manager.disconnect()
    
    @mcp.tool()
    def comsol_status() -> dict:
        """
        Get the current COMSOL session status.
        
        Returns:
            Session information including connection status, version, and loaded models
        """
        return session_manager.get_status()

    @mcp.tool()
    def session_clear_models() -> dict:
        """
        Destructively remove all models tracked by this MCP session.

        The COMSOL client remains connected. Use this only when loss of all
        unsaved tracked models is intended.
        """
        return session_manager.clear_models()

    @mcp.tool()
    def session_reset() -> dict:
        """
        Destructively reset the MCP-owned COMSOL session.

        This clears all tracked models and disconnects the owned client. If a
        local client is still starting, it is marked for disposal when startup
        returns.
        """
        return session_manager.reset()
