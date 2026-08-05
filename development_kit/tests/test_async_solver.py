"""Thread-state tests for the asynchronous solver using fake studies."""

import threading

import pytest

from src.async_handler.solver import AsyncSolver, SolverStatus


class FakeStudy:
    def __init__(self, error=None):
        self.error = error
        self.run_count = 0

    def run(self):
        self.run_count += 1
        if self.error:
            raise self.error


class FakeStudyList:
    def __init__(self, studies):
        self.studies = studies

    def tags(self):
        return list(self.studies)


class FakeJava:
    def __init__(self, study):
        self.studies = {"std1": study}

    def study(self, tag=None):
        if tag is None:
            return FakeStudyList(self.studies)
        return self.studies[tag]


class FakeModel:
    def __init__(self, study):
        self.java = FakeJava(study)

    def name(self):
        return "fake"


def raising_callback(progress, message):
    raise RuntimeError("callback failed")


def test_callback_failure_does_not_change_completed_solve():
    study = FakeStudy()
    solver = AsyncSolver()

    assert solver.start_solve(
        FakeModel(study),
        "std1",
        progress_callback=raising_callback,
    )
    assert solver.wait(timeout=2)

    progress = solver.get_progress()
    assert progress["status"] == SolverStatus.COMPLETED.value
    assert progress["progress"] == 1.0
    assert study.run_count == 1


def test_unknown_study_tag_fails_without_running_another_study():
    study = FakeStudy()
    solver = AsyncSolver()

    assert solver.start_solve(FakeModel(study), "missing-study")
    assert solver.wait(timeout=2)

    progress = solver.get_progress()
    assert progress["status"] == SolverStatus.FAILED.value
    assert study.run_count == 0


def test_progress_callback_observes_intermediate_and_terminal_transitions():
    study = FakeStudy()
    solver = AsyncSolver()
    observations = []

    assert solver.start_solve(
        FakeModel(study),
        "std1",
        progress_callback=lambda progress, message: observations.append((progress, message)),
    )
    assert solver.wait(timeout=2)

    assert observations == [
        (0.0, "Starting solver..."),
        (0.1, "Building geometry..."),
        (0.2, "Creating mesh..."),
        (0.3, "Solving study: std1..."),
        (1.0, "Completed"),
    ]


def test_running_state_always_has_a_started_waitable_thread():
    solver = AsyncSolver()
    startup_wait_results = []

    assert solver.start_solve(
        FakeModel(FakeStudy()),
        "std1",
        progress_callback=lambda _progress, message: (
            startup_wait_results.append(solver.wait(timeout=0))
            if message == "Starting solver..."
            else None
        ),
    )

    assert startup_wait_results == [False]
    assert solver.wait(timeout=2) is True


def test_progress_property_returns_snapshot():
    solver = AsyncSolver()

    snapshot = solver.progress
    snapshot.status = SolverStatus.FAILED

    assert solver.progress.status is SolverStatus.IDLE


def test_cancel_during_blocking_run_reports_completed_truthfully():
    started = threading.Event()
    release = threading.Event()

    class BlockingStudy(FakeStudy):
        def run(self):
            self.run_count += 1
            started.set()
            assert release.wait(timeout=2)

    study = BlockingStudy()
    solver = AsyncSolver()

    assert solver.start_solve(FakeModel(study), "std1")
    assert started.wait(timeout=2)
    assert solver.cancel() is True
    assert solver.get_progress()["status"] == SolverStatus.RUNNING.value

    release.set()
    assert solver.wait(timeout=2)

    progress = solver.get_progress()
    assert progress["status"] == SolverStatus.COMPLETED.value
    assert progress["progress"] == 1.0
    assert "could not interrupt" in progress["message"]


def test_running_solve_cannot_be_reset_or_restarted():
    started = threading.Event()
    release = threading.Event()

    class BlockingStudy(FakeStudy):
        def run(self):
            self.run_count += 1
            started.set()
            assert release.wait(timeout=2)

    study = BlockingStudy()
    solver = AsyncSolver()

    assert solver.start_solve(FakeModel(study), "std1") is True
    assert started.wait(timeout=2)
    assert solver.reset() is False
    assert solver.progress.status is SolverStatus.RUNNING
    assert solver.start_solve(FakeModel(study), "std1") is False

    release.set()
    assert solver.wait(timeout=2)
    assert solver.reset() is True
    assert solver.progress.status is SolverStatus.IDLE


def test_cancellation_request_is_reported_to_progress_callback():
    started = threading.Event()
    release = threading.Event()
    observations = []

    class BlockingStudy(FakeStudy):
        def run(self):
            self.run_count += 1
            started.set()
            assert release.wait(timeout=2)

    solver = AsyncSolver()
    assert solver.start_solve(
        FakeModel(BlockingStudy()),
        "std1",
        progress_callback=lambda progress, message: observations.append((progress, message)),
    )
    assert started.wait(timeout=2)

    assert solver.cancel() is True
    assert observations[-1] == (0.3, "Cancellation requested")

    release.set()
    assert solver.wait(timeout=2)


def test_thread_start_failure_leaves_solver_resettable(monkeypatch):
    solver = AsyncSolver()
    monkeypatch.setattr(
        threading.Thread, "start", lambda _thread: (_ for _ in ()).throw(RuntimeError("limit"))
    )

    with pytest.raises(RuntimeError, match="limit"):
        solver.start_solve(FakeModel(FakeStudy()), "std1")

    assert solver.progress.status is SolverStatus.FAILED
    assert solver.wait(timeout=0) is True
    assert solver.reset() is True


def test_progress_callback_cannot_join_its_worker_thread():
    solver = AsyncSolver()
    observations = []

    assert solver.start_solve(
        FakeModel(FakeStudy()),
        "std1",
        progress_callback=lambda _progress, message: (
            observations.append(solver.wait(timeout=0))
            if message == "Building geometry..."
            else None
        ),
    )
    assert solver.wait(timeout=2) is True
    assert observations == [False]
