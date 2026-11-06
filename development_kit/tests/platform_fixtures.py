"""Directory-link fixtures that preserve native platform semantics."""

import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def fake_lease_artifact_lock(path):
    """Serialize synthetic lease artifacts; this never admits a Linux solver."""
    from comsol_mcp.posix_lock import LockBusy, PosixFileLock

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = PosixFileLock(path)
    deadline = time.monotonic() + 5
    try:
        while True:
            try:
                lock.acquire()
                break
            except LockBusy:
                if time.monotonic() >= deadline:
                    raise TimeoutError("synthetic lease artifact lock is busy") from None
                time.sleep(0.01)
        yield
    finally:
        lock.close()


def platform_test_root(label: str = "") -> Path:
    """Use the declared test root without inventing a Windows drive on POSIX."""
    root = (
        Path("D:/mcp_tests")
        if os.name == "nt"
        else Path(os.environ.get("COMSOL_MCP_TEST_ASCII_ROOT", str(Path.home() / "mcp_tests")))
    )
    if not root.is_absolute() or not str(root).isascii():
        raise ValueError("test root must be an absolute ASCII path")
    return root / label


def synthetic_runtime_root() -> Path:
    """Create a fresh ASCII runtime root isolated to this test worker.

    The production default deliberately shares one runtime directory (and one
    solver lease) across agent processes; that is intentional exclusion, not a
    defect. Synthetic tests must not contend on it. Each pytest-xdist worker is
    a separate process, so a unique directory per call also separates workers,
    and the returned path propagates to subprocesses through the environment
    variable that selects it.
    """
    import tempfile
    import uuid

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    parent = platform_test_root("runtime-isolation")
    parent.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(prefix=f"comsol-mcp-rt-{worker}-{uuid.uuid4().hex[:8]}-", dir=parent)
    )


def select_synthetic_runtime_dir() -> Path | None:
    """Return an isolated root, or None when a runtime dir is already configured.

    An explicitly configured runtime directory always wins: test isolation
    must never override caller configuration, and selecting a synthetic root
    is never solver authorization.
    """
    if os.environ.get("COMSOL_MCP_RUNTIME_DIR"):
        return None
    return synthetic_runtime_root()


def create_directory_link(target: str, link: str) -> None:
    """Create a Windows junction or a POSIX directory symlink."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(target, link)
    else:
        Path(link).symlink_to(target, target_is_directory=True)


def remove_directory_link(link: Path) -> None:
    """Remove the link without touching its target."""
    if link.is_symlink():
        link.unlink()
    else:
        link.rmdir()
