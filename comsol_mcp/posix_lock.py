"""Stable advisory lock files for cooperating POSIX processes."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class LockBusy(RuntimeError):
    """Another cooperating process holds the lock."""


class PosixFileLock:
    """Retain the lock inode to prevent unlink-and-reopen ownership races."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def acquire(self) -> None:
        import fcntl

        if self.descriptor is not None:
            raise RuntimeError("lock is already acquired")
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise RuntimeError("lock path must not contain symlinks")
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR | int(getattr(os, "O_NOFOLLOW")) | int(getattr(os, "O_CLOEXEC")),
            0o600,
        )
        try:
            identity = os.fstat(descriptor)
            if (
                not stat.S_ISREG(identity.st_mode)
                or identity.st_uid != int(getattr(os, "getuid")())
                or identity.st_nlink != 1
                or stat.S_IMODE(identity.st_mode) & 0o077
            ):
                raise RuntimeError("lock file must be a private owned regular file")
            try:
                getattr(fcntl, "flock")(
                    descriptor, int(getattr(fcntl, "LOCK_EX")) | int(getattr(fcntl, "LOCK_NB"))
                )
            except BlockingIOError as exc:
                raise LockBusy("another editor holds the lock") from exc
            current = self.path.lstat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise RuntimeError("lock path changed during acquisition")
            if any(p.is_symlink() for p in (self.path, *self.path.parents)):
                raise RuntimeError("lock parent changed during acquisition")
            self.descriptor = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
