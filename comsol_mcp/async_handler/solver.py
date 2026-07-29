"""Asynchronous solver handler for COMSOL simulations."""

import threading
from typing import Optional, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class SolverStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class SolverProgress:
    """Progress information for a solving operation."""
    status: SolverStatus = SolverStatus.IDLE
    progress: float = 0.0
    message: str = ""
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    error: Optional[str] = None
    study_name: Optional[str] = None
    model_name: Optional[str] = None
    
    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "progress": self.progress,
            "message": self.message,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "error": self.error,
            "study_name": self.study_name,
            "model_name": self.model_name,
            "elapsed_seconds": (
                (self.end_time or datetime.now()) - self.start_time
            ).total_seconds() if self.start_time else 0,
        }


class AsyncSolver:
    """Manages asynchronous solving operations for COMSOL models."""
    
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._progress: SolverProgress = SolverProgress()
        self._cancel_flag: bool = False
        self._progress_callback: Optional[Callable[[float, str], None]] = None
        self._lock: threading.Lock = threading.Lock()
        self._launch_gate: Optional[threading.Event] = None
        self._launch_owner_ident: Optional[int] = None
    
    @property
    def progress(self) -> SolverProgress:
        with self._lock:
            return replace(self._progress)
    
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._progress.status == SolverStatus.RUNNING

    def _cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_flag
    
    def start_solve(
        self,
        model,
        study_name: Optional[str] = None,
        progress_callback: Optional[Callable[[float, str], None]] = None
    ) -> bool:
        """
        Start solving a study in a background thread.
        
        Args:
            model: The COMSOL model to solve
            study_name: Name of the study to solve (None for all studies)
            progress_callback: Optional callback for progress updates
        
        Returns:
            True if solving started, False if already running
        """
        launch_gate = threading.Event()

        def solve_thread():
            launch_gate.wait()
            try:
                with self._lock:
                    self._progress.message = "Building geometry..."
                    self._progress.progress = 0.1
                self._notify_progress(progress_callback, 0.1, "Building geometry...")

                if self._cancel_requested():
                    self._set_cancelled()
                    return

                with self._lock:
                    self._progress.message = "Creating mesh..."
                    self._progress.progress = 0.2
                self._notify_progress(progress_callback, 0.2, "Creating mesh...")

                if self._cancel_requested():
                    self._set_cancelled()
                    return

                with self._lock:
                    self._progress.message = f"Solving study: {study_name or 'all'}..."
                    self._progress.progress = 0.3
                self._notify_progress(
                    progress_callback,
                    0.3,
                    f"Solving study: {study_name or 'all'}...",
                )

                if self._cancel_requested():
                    self._set_cancelled()
                    return

                # Use the Java API directly so we can run by *tag* (the
                # canonical identifier). mph's ``model.solve(name)`` only
                # accepts the study *label*, but callers now pass a tag
                # (or None for "all studies").
                jm = model.java
                if study_name is None:
                    for t in jm.study().tags():
                        if self._cancel_requested():
                            self._set_cancelled()
                            return
                        jm.study(t).run()
                else:
                    jm.study(study_name).run()

                with self._lock:
                    self._progress.status = SolverStatus.COMPLETED
                    self._progress.progress = 1.0
                    if self._cancel_flag:
                        self._progress.message = (
                            "Solving completed; the cancellation request could not "
                            "interrupt the blocking COMSOL study.run() call."
                        )
                    else:
                        self._progress.message = "Solving completed successfully."
                    self._progress.end_time = datetime.now()

                self._notify_progress(progress_callback, 1.0, "Completed")

            except Exception as e:
                error_msg = str(e)

                with self._lock:
                    self._progress.status = SolverStatus.FAILED
                    self._progress.error = error_msg
                    self._progress.message = f"Solving failed: {error_msg}"
                    self._progress.end_time = datetime.now()

                self._notify_progress(
                    progress_callback,
                    -1.0,
                    f"Error: {error_msg}",
                )

        with self._lock:
            if (
                self._progress.status == SolverStatus.RUNNING
                or self._thread is not None
                and self._thread.is_alive()
            ):
                return False

            self._cancel_flag = False
            self._progress_callback = progress_callback
            self._progress = SolverProgress(
                status=SolverStatus.RUNNING,
                progress=0.0,
                message="Starting solver...",
                start_time=datetime.now(),
                study_name=study_name,
                model_name=model.name() if hasattr(model, 'name') else None,
            )
            self._thread = threading.Thread(target=solve_thread, daemon=True)
            self._launch_gate = launch_gate
            self._launch_owner_ident = threading.get_ident()
            self._thread.start()
        try:
            self._notify_progress(progress_callback, 0.0, "Starting solver...")
        finally:
            launch_gate.set()
            with self._lock:
                if self._launch_gate is launch_gate:
                    self._launch_gate = None
                    self._launch_owner_ident = None
        return True

    @staticmethod
    def _notify_progress(
        callback: Optional[Callable[[float, str], None]],
        progress: float,
        message: str,
    ) -> None:
        """Notify a caller without allowing callback errors to alter solve state."""
        if callback is None:
            return
        try:
            callback(progress, message)
        except Exception:
            pass
    
    def _set_cancelled(self):
        """Set status to cancelled."""
        with self._lock:
            self._progress.status = SolverStatus.CANCELLED
            self._progress.message = "Solving was cancelled by user."
            self._progress.end_time = datetime.now()
            progress = self._progress.progress
            callback = self._progress_callback
        self._notify_progress(callback, progress, "Cancelled")
    
    def cancel(self) -> bool:
        """
        Request cancellation of the current solving operation.
        
        This sets a cooperative Python flag. It can prevent a solve before
        ``study.run()`` begins, but it cannot interrupt a blocking COMSOL solve.
        
        Returns:
            True if cancellation was requested, False if not running
        """
        with self._lock:
            if self._progress.status != SolverStatus.RUNNING:
                return False
            self._cancel_flag = True
            self._progress.message = (
                "Cancellation requested. A blocking COMSOL study.run() cannot "
                "be interrupted by this flag."
            )
            progress = self._progress.progress
            callback = self._progress_callback
        self._notify_progress(callback, progress, "Cancellation requested")
        return True
    
    def wait(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for the solving operation to complete.
        
        Args:
            timeout: Maximum time to wait in seconds (None for indefinite)
        
        Returns:
            True if solving completed, False if timeout reached
        """
        with self._lock:
            thread = self._thread
            launch_gate = self._launch_gate
            launch_owner_ident = self._launch_owner_ident
        if thread is None:
            return True
        if (
            launch_gate is not None
            and not launch_gate.is_set()
            and launch_owner_ident == threading.get_ident()
        ):
            return False

        thread.join(timeout=timeout)
        return not thread.is_alive()
    
    def get_progress(self) -> dict:
        """Get current solving progress as a dictionary."""
        with self._lock:
            return self._progress.to_dict()
    
    def reset(self) -> bool:
        """Reset the solver state."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._progress = SolverProgress()
            self._cancel_flag = False
            self._progress_callback = None
            self._thread = None
            return True


async_solver = AsyncSolver()
