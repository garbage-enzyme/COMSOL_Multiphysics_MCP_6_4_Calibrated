"""Cross-platform branch coverage for comsol_mcp.durable.io.

The per-platform quality gates require durable/io.py at 97% on EACH
platform. The platform-specific implementations (the POSIX no-follow
parent walk, the POSIX quarantine-and-verify deletion, the Windows
opened-identity deletion, the non-native fsync arms and the non-native
directory-publication arms) cannot run against the other kernel, so these
tests inject scripted syscalls and ctypes bindings and drive every branch
on both platforms.

Containment: the scripts are installed ONLY on the ``durable_io.os``
module attribute (a delegating proxy with per-call overrides). The real
``os`` module that pytest and the rest of the interpreter use is never
touched, so no test machinery fd can collide with a scripted one. No
product behavior and no coverage policy changes.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat as stat_module
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comsol_mcp.durable import io as durable_io

_REGULAR_MODE = stat_module.S_IFREG | 0o644
_DIR_MODE = stat_module.S_IFDIR | 0o755
_O_DIRECTORY = 0x1000
_O_NOFOLLOW = 0x2000
_O_NONBLOCK = 0x4000


def _stat(mode: int, *, dev: int = 1, ino: int = 100):
    return os.stat_result((mode, ino, dev, 1, 0, 0, 0, 0.0, 0.0, 0.0))


class _OsProxy:
    """The real os for the module under test, with scripted overrides."""

    def __init__(self, overrides: dict, name: str | None = None):
        self._overrides = overrides
        self._name = name

    def __getattr__(self, item):
        if item == "name" and self._name is not None:
            return self._name
        return self._overrides.get(item, getattr(os, item))

    def replace(self, name: str, value) -> None:
        self._overrides[name] = value


def _install(monkeypatch, overrides: dict, *, name: str | None = None) -> _OsProxy:
    proxy = _OsProxy(overrides, name)
    monkeypatch.setattr(durable_io, "os", proxy)
    return proxy


def _posix_flag_values(monkeypatch) -> None:
    monkeypatch.setattr(os, "O_DIRECTORY", _O_DIRECTORY, raising=False)
    monkeypatch.setattr(os, "O_NOFOLLOW", _O_NOFOLLOW, raising=False)
    monkeypatch.setattr(os, "O_NONBLOCK", _O_NONBLOCK, raising=False)


# --- the POSIX no-follow parent walk ---------------------------------------


def test_open_parent_without_links_walks_and_closes_each_parent(monkeypatch, tmp_path):
    _posix_flag_values(monkeypatch)
    opened = []
    closed = []

    def fake_open(path, flags, dir_fd=None):
        opened.append((str(path), dir_fd))
        return len(opened)

    def fake_close(descriptor):
        closed.append(descriptor)

    _install(monkeypatch, {"open": fake_open, "close": fake_close}, name="posix")
    descriptor = durable_io._open_parent_without_links(tmp_path / "child")
    parts = len(Path(os.path.abspath(tmp_path)).parts) - 1
    assert descriptor == parts + 1
    assert [entry[1] for entry in opened] == [None, *range(1, parts + 1)]
    assert closed == list(range(1, parts + 1))


def test_open_parent_without_links_closes_the_walk_on_failure(monkeypatch, tmp_path):
    _posix_flag_values(monkeypatch)
    closed = []

    def fake_open(path, flags, dir_fd=None):
        if dir_fd is None:
            return 11
        raise PermissionError("denied part")

    def fake_close(descriptor):
        closed.append(descriptor)

    _install(monkeypatch, {"open": fake_open, "close": fake_close}, name="posix")
    with pytest.raises(PermissionError):
        durable_io._open_parent_without_links(tmp_path / "child")
    assert closed == [11]


# --- fsync_directory: both platform arms -----------------------------------


def test_fsync_directory_is_a_noop_on_windows(monkeypatch):
    calls = []

    def fake_open(*arguments):
        calls.append(arguments)

    _install(monkeypatch, {"open": fake_open}, name="nt")
    durable_io.fsync_directory("anywhere")
    assert calls == []


def test_fsync_directory_fsyncs_and_closes_on_posix(monkeypatch, tmp_path):
    calls = []

    def fake_open(path, flags):
        calls.append(("open", str(path)))
        return 9

    def fake_fsync(descriptor):
        calls.append(("fsync", descriptor))

    def fake_close(descriptor):
        calls.append(("close", descriptor))

    _install(
        monkeypatch,
        {"open": fake_open, "fsync": fake_fsync, "close": fake_close},
        name="posix",
    )
    durable_io.fsync_directory(tmp_path)
    assert calls == [("open", str(tmp_path)), ("fsync", 9), ("close", 9)]


def test_fsync_directory_closes_the_descriptor_when_fsync_fails(monkeypatch, tmp_path):
    closed = []

    def fake_open(path, flags):
        return 9

    def failing_fsync(descriptor):
        raise OSError("disk gone")

    def fake_close(descriptor):
        closed.append(descriptor)

    _install(
        monkeypatch,
        {"open": fake_open, "fsync": failing_fsync, "close": fake_close},
        name="posix",
    )
    with pytest.raises(OSError):
        durable_io.fsync_directory(tmp_path)
    assert closed == [9]


# --- _open_regular_file_descriptor: the verified-parent arm -----------------


def test_open_regular_file_descriptor_verifies_through_the_parent(monkeypatch, tmp_path):
    _posix_flag_values(monkeypatch)
    monkeypatch.setattr(durable_io, "_open_parent_without_links", lambda path: 102)

    def fake_open(path, flags, dir_fd=None):
        if dir_fd is None:
            raise AssertionError("the walk is stubbed; no anchor open expected")
        assert flags & _O_NONBLOCK
        return 707

    def fake_stat(target, dir_fd=None, follow_symlinks=True):
        assert dir_fd is not None
        return _stat(_REGULAR_MODE)

    def fake_fstat(descriptor):
        assert descriptor == 707
        return _stat(_REGULAR_MODE)

    closed = []

    def fake_close(descriptor):
        closed.append(descriptor)

    _install(
        monkeypatch,
        {"open": fake_open, "stat": fake_stat, "fstat": fake_fstat, "close": fake_close},
        name="posix",
    )
    descriptor, opened = durable_io._open_regular_file_descriptor(
        tmp_path / "record.jsonl", no_symlink_parents=True
    )
    assert descriptor == 707
    assert (opened.st_dev, opened.st_ino) == (1, 100)
    assert closed == [102]  # the verified parent directory


def test_open_regular_file_descriptor_refuses_a_replaced_identity(monkeypatch, tmp_path):
    _posix_flag_values(monkeypatch)
    monkeypatch.setattr(durable_io, "_open_parent_without_links", lambda path: 102)
    closed = []

    def fake_open(path, flags, dir_fd=None):
        if dir_fd is None:
            raise AssertionError("the walk is stubbed; no anchor open expected")
        return 707

    def fake_stat(target, dir_fd=None, follow_symlinks=True):
        assert dir_fd is not None
        return _stat(_REGULAR_MODE, ino=100)

    def fake_fstat(descriptor):
        assert descriptor == 707
        return _stat(_REGULAR_MODE, ino=999)

    def fake_close(descriptor):
        closed.append(descriptor)

    _install(
        monkeypatch,
        {"open": fake_open, "stat": fake_stat, "fstat": fake_fstat, "close": fake_close},
        name="posix",
    )
    with pytest.raises(durable_io.FileIdentityChangedError):
        durable_io._open_regular_file_descriptor(tmp_path / "record.jsonl", no_symlink_parents=True)
    assert closed == [102, 707]


def test_open_regular_file_descriptor_refuses_non_regular_files(monkeypatch, tmp_path):
    _posix_flag_values(monkeypatch)
    monkeypatch.setattr(durable_io, "_open_parent_without_links", lambda path: 102)
    closed = []

    def fake_open(path, flags, dir_fd=None):
        if dir_fd is None:
            raise AssertionError("the walk is stubbed; no anchor open expected")
        return 707

    def fake_stat(target, dir_fd=None, follow_symlinks=True):
        assert dir_fd is not None
        return _stat(stat_module.S_IFIFO | 0o600)

    def fake_close(descriptor):
        closed.append(descriptor)

    _install(monkeypatch, {"open": fake_open, "stat": fake_stat, "close": fake_close}, name="posix")
    with pytest.raises(ValueError, match="without links"):
        durable_io._open_regular_file_descriptor(tmp_path / "record.jsonl", no_symlink_parents=True)
    assert closed == [102]  # the verified parent directory is still released


# --- the POSIX quarantine-and-verify deletion -------------------------------


class _PosixUnlinkHarness:
    """Scripted syscalls for _posix_unlink_opened_file_if on any platform."""

    def __init__(self, monkeypatch, tmp_path, *, observed_ino=100, second_predicate=None):
        self.path = tmp_path / "target.jsonl"
        self.calls = []
        self.closed = []
        self.observed_ino = observed_ino
        self.second_predicate = second_predicate
        self.predicate_calls = 0
        self.link_error: OSError | None = None
        self.unlink_errors: dict[int, OSError] = {}
        _posix_flag_values(monkeypatch)
        monkeypatch.setattr(durable_io, "_open_parent_without_links", lambda path: 4)

        def fake_open(name, flags, dir_fd=None):
            self.calls.append(("open", name, flags))
            if flags & _O_NONBLOCK:
                return 7
            return 9

        def fake_fstat(descriptor):
            if descriptor == 7:
                return _stat(_REGULAR_MODE)
            if descriptor == 9:
                return _stat(_DIR_MODE)
            raise AssertionError(f"unexpected fstat {descriptor}")

        def fake_stat(name, dir_fd=None, follow_symlinks=True):
            assert dir_fd == 9 and name == "owned"
            return _stat(_REGULAR_MODE, ino=self.observed_ino)

        def fake_link(source, name, *, src_dir_fd, dst_dir_fd, follow_symlinks):
            self.calls.append(("link", src_dir_fd, dst_dir_fd))
            if self.link_error is not None:
                raise self.link_error

        def fake_unlink(name, dir_fd=None):
            self.calls.append(("unlink", name, dir_fd))
            error = self.unlink_errors.get(dir_fd)
            if error is not None:
                del self.unlink_errors[dir_fd]
                raise error

        def fake_rename(source, name, *, src_dir_fd, dst_dir_fd):
            self.calls.append(("rename", src_dir_fd, dst_dir_fd))

        def fake_mkdir(name, mode=0o777, dir_fd=None):
            self.calls.append(("mkdir", dir_fd))

        def fake_fsync(descriptor):
            self.calls.append(("fsync", descriptor))

        def fake_rmdir(name, dir_fd=None):
            self.calls.append(("rmdir", dir_fd))

        def fake_close(descriptor):
            self.closed.append(descriptor)

        self.proxy = _install(
            monkeypatch,
            {
                "open": fake_open,
                "fstat": fake_fstat,
                "stat": fake_stat,
                "link": fake_link,
                "unlink": fake_unlink,
                "rename": fake_rename,
                "mkdir": fake_mkdir,
                "fsync": fake_fsync,
                "rmdir": fake_rmdir,
                "close": fake_close,
            },
            name="posix",
        )

    def keep(self, descriptor, opened) -> bool:
        self.predicate_calls += 1
        if self.second_predicate is None or self.predicate_calls == 1:
            return True
        return self.second_predicate(descriptor, opened)


def test_posix_unlink_refuses_a_fifo(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path)

    def fifo_fstat(descriptor):
        assert descriptor == 7
        return _stat(stat_module.S_IFIFO | 0o600)

    harness.proxy.replace("fstat", fifo_fstat)
    assert durable_io._posix_unlink_opened_file_if(harness.path, lambda d, s: True) is False
    assert ("mkdir", 4) not in harness.calls
    assert 7 in harness.closed and 4 in harness.closed


def test_posix_unlink_keeps_the_file_when_the_predicate_refuses(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path)
    assert durable_io._posix_unlink_opened_file_if(harness.path, lambda d, s: False) is False
    assert ("mkdir", 4) not in harness.calls


def test_posix_unlink_deletes_a_verified_inode(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path, observed_ino=100)
    assert durable_io._posix_unlink_opened_file_if(harness.path, harness.keep) is True
    assert ("fsync", 4) in harness.calls
    assert ("unlink", "owned", 9) in harness.calls
    assert ("rmdir", 4) in harness.calls
    assert 7 in harness.closed and 9 in harness.closed and 4 in harness.closed


def test_posix_unlink_restores_a_replaced_inode(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path, observed_ino=999)
    assert durable_io._posix_unlink_opened_file_if(harness.path, harness.keep) is False
    assert ("link", 9, 4) in harness.calls
    assert ("unlink", "owned", 9) in harness.calls
    assert ("rmdir", 4) in harness.calls


def test_posix_unlink_restores_when_the_second_check_refuses(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(
        monkeypatch, tmp_path, observed_ino=100, second_predicate=lambda d, s: False
    )
    assert durable_io._posix_unlink_opened_file_if(harness.path, harness.keep) is False
    assert ("link", 9, 4) in harness.calls


def test_posix_unlink_restores_after_a_late_file_not_found(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path)
    harness.unlink_errors[9] = FileNotFoundError()
    assert durable_io._posix_unlink_opened_file_if(harness.path, harness.keep) is False
    assert ("link", 9, 4) in harness.calls


def test_posix_unlink_reports_a_retained_quarantine_on_restore_conflict(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path, observed_ino=999)
    harness.link_error = OSError("destination exists")
    with pytest.raises(RuntimeError, match="quarantined replacement was retained"):
        durable_io._posix_unlink_opened_file_if(harness.path, harness.keep)
    assert ("rmdir", 4) not in harness.calls  # the quarantine is deliberately kept


def test_posix_unlink_returns_false_when_the_target_is_gone(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path)

    def missing(name, flags, dir_fd=None):
        raise FileNotFoundError(name)

    harness.proxy.replace("open", missing)
    assert durable_io._posix_unlink_opened_file_if(harness.path, lambda d, s: True) is False
    assert 4 in harness.closed


def test_posix_unlink_returns_false_when_rename_loses_the_race(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path)

    def rename_missing(source, name, *, src_dir_fd, dst_dir_fd):
        raise FileNotFoundError(name)

    harness.proxy.replace("rename", rename_missing)
    assert durable_io._posix_unlink_opened_file_if(harness.path, lambda d, s: True) is False
    assert ("rmdir", 4) in harness.calls


def test_posix_unlink_propagates_predicate_errors_without_a_quarantine(monkeypatch, tmp_path):
    harness = _PosixUnlinkHarness(monkeypatch, tmp_path)

    def refuse(descriptor, opened):
        raise ValueError("predicate refused")

    with pytest.raises(ValueError, match="predicate refused"):
        durable_io._posix_unlink_opened_file_if(harness.path, refuse)
    assert ("mkdir", 4) not in harness.calls


# --- the Windows opened-identity deletion -----------------------------------


class _FakeApi:
    """A ctypes-like function object with settable prototypes."""

    def __init__(self, action):
        self.action = action
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *arguments):
        self.calls.append(arguments)
        return self.action(*arguments)


class _WindowsUnlinkHarness:
    """Scripted ctypes and msvcrt bindings on any platform."""

    def __init__(self, monkeypatch, tmp_path, *, handle=55, disposition_result=1):
        self.path = tmp_path / "target.jsonl"
        self.calls = []
        self.closed = []
        create = _FakeApi(lambda *a: handle)
        self.set_information = _FakeApi(lambda *a: disposition_result)
        self.close_handle = _FakeApi(lambda *a: self.calls.append(("CloseHandle",)))
        kernel32 = SimpleNamespace(
            CreateFileW=create,
            SetFileInformationByHandle=self.set_information,
            CloseHandle=self.close_handle,
        )
        monkeypatch.setattr(
            durable_io.ctypes,
            "WinDLL",
            lambda name, use_last_error=True: kernel32,
            raising=False,
        )
        self.open_osfhandle = _FakeApi(lambda handle_value, flags: 77)
        self.get_osfhandle = _FakeApi(lambda descriptor: 999)
        fake_msvcrt = SimpleNamespace(
            open_osfhandle=self.open_osfhandle, get_osfhandle=self.get_osfhandle
        )
        monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)

        def fake_fstat(descriptor):
            assert descriptor == 77
            return _stat(_REGULAR_MODE)

        def fake_close(descriptor):
            self.closed.append(descriptor)

        self.proxy = _install(monkeypatch, {"fstat": fake_fstat, "close": fake_close}, name="nt")

    def keep(self, descriptor, opened) -> bool:
        return True


def test_windows_unlink_requires_windows(monkeypatch, tmp_path):
    _install(monkeypatch, {}, name="posix")
    with pytest.raises(OSError, match="requires Windows"):
        durable_io._windows_unlink_opened_file_if(tmp_path / "x", lambda d, s: True)


def test_windows_unlink_reports_an_unopenable_handle(monkeypatch, tmp_path):
    harness = _WindowsUnlinkHarness(monkeypatch, tmp_path, handle=None)
    assert durable_io._windows_unlink_opened_file_if(harness.path, harness.keep) is False
    invalid = _WindowsUnlinkHarness(monkeypatch, tmp_path, handle=ctypes.c_void_p(-1).value)
    assert durable_io._windows_unlink_opened_file_if(invalid.path, invalid.keep) is False


def test_windows_unlink_refuses_non_regular_and_unwanted_identities(monkeypatch, tmp_path):
    harness = _WindowsUnlinkHarness(monkeypatch, tmp_path)

    def fifo_fstat(descriptor):
        assert descriptor == 77
        return _stat(stat_module.S_IFIFO | 0o600)

    harness.proxy.replace("fstat", fifo_fstat)
    assert durable_io._windows_unlink_opened_file_if(harness.path, harness.keep) is False
    harness2 = _WindowsUnlinkHarness(monkeypatch, tmp_path)
    assert durable_io._windows_unlink_opened_file_if(harness2.path, lambda d, s: False) is False
    assert 77 in harness2.closed


def test_windows_unlink_deletes_the_opened_identity(monkeypatch, tmp_path):
    harness = _WindowsUnlinkHarness(monkeypatch, tmp_path)
    assert durable_io._windows_unlink_opened_file_if(harness.path, harness.keep) is True
    assert len(harness.set_information.calls) == 1
    assert harness.get_osfhandle.calls == [(77,)]
    assert 77 in harness.closed


def test_windows_unlink_reports_a_failed_disposition(monkeypatch, tmp_path):
    harness = _WindowsUnlinkHarness(monkeypatch, tmp_path, disposition_result=0)
    assert durable_io._windows_unlink_opened_file_if(harness.path, harness.keep) is False
    assert 77 in harness.closed


def test_windows_unlink_closes_the_handle_when_the_descriptor_cannot_be_made(monkeypatch, tmp_path):
    harness = _WindowsUnlinkHarness(monkeypatch, tmp_path)

    def broken(handle_value, flags):
        raise OSError("no descriptor")

    harness.open_osfhandle.action = broken
    with pytest.raises(OSError, match="no descriptor"):
        durable_io._windows_unlink_opened_file_if(harness.path, harness.keep)
    assert ("CloseHandle",) in harness.calls
    assert harness.closed == []


def test_windows_unlink_closes_the_descriptor_when_the_predicate_raises(monkeypatch, tmp_path):
    harness = _WindowsUnlinkHarness(monkeypatch, tmp_path)

    def refuse(descriptor, opened):
        raise ValueError("predicate refused")

    with pytest.raises(ValueError, match="predicate refused"):
        durable_io._windows_unlink_opened_file_if(harness.path, refuse)
    assert 77 in harness.closed


# --- exclusive directory publication: both platform arms --------------------


def test_publish_directory_renames_directly_on_windows(monkeypatch, tmp_path):
    renamed = []

    def fake_rename(source, destination):
        renamed.append((source, destination))

    _install(monkeypatch, {"rename": fake_rename}, name="nt")
    durable_io.publish_directory_exclusive(tmp_path / "source", tmp_path / "target")
    assert renamed == [(tmp_path / "source", tmp_path / "target")]


def _fake_libc(monkeypatch, renameat2):
    libc = SimpleNamespace(renameat2=renameat2)
    monkeypatch.setattr(durable_io.ctypes, "CDLL", lambda name, use_errno=True: libc, raising=False)
    return libc


def test_publish_directory_requires_renameat2_on_posix(monkeypatch, tmp_path):
    _install(monkeypatch, {}, name="posix")
    _fake_libc(monkeypatch, None)
    with pytest.raises(RuntimeError, match="unavailable"):
        durable_io.publish_directory_exclusive(tmp_path / "source", tmp_path / "target")


def test_publish_directory_publishes_without_replacement_on_posix(monkeypatch, tmp_path):
    calls = []
    rename = _FakeApi(lambda *a: (calls.append(1), 0)[1])
    _install(monkeypatch, {}, name="posix")
    _fake_libc(monkeypatch, rename)
    durable_io.publish_directory_exclusive(tmp_path / "source", tmp_path / "target")
    assert calls == [1]
    assert rename.argtypes is not None


@pytest.mark.parametrize("code,expected", [(errno.EEXIST, FileExistsError), (13, OSError)])
def test_publish_directory_maps_renameat2_failures(monkeypatch, tmp_path, code, expected):
    _install(monkeypatch, {}, name="posix")
    _fake_libc(monkeypatch, _FakeApi(lambda *a: 1))
    monkeypatch.setattr(durable_io.ctypes, "get_errno", lambda: code)
    with pytest.raises(expected):
        durable_io.publish_directory_exclusive(tmp_path / "source", tmp_path / "target")
