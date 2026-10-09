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
