"""A bounded shared-model Save Copy writer (batch D, work item ten).

The 0.8.1 finding: ``Model.save(path, copy=True)`` writes inside the COMSOL
server process, so a Python-side byte counter can only observe the file
after the bytes are on disk. The accepted build (6.4.0.293) offers no
incremental write limit through the API, and pointing the save at a named
pipe crashes the server-side JVM.

This module enforces the caller-declared cap at write time through the
operating system: the copy is written into a throwaway VHDX volume whose
free space is set to the cap (cluster-aligned) by a filler file. A copy
that needs more clusters than the cap allows fails inside the volume with
the OS disk-full error; nothing ever reaches the final target. A copy that
fits is streamed out to the caller's target and hash-verified; the volume —
with any partial content — is then detached and deleted.

Boundary semantics (proven natively under ``D:/mcp_tests/p10pipe/native``):

* free space is cluster-granular, so the OS bound is ``ceil(cap / cluster)``;
* a copy larger than the cap but within one cluster can reach the volume;
  the immediate post-write byte check refuses it before a byte leaves, and
  the transient overshoot stays under one cluster inside the deleted volume;
* the final target only ever receives bytes of an accepted copy.

The volume is only a bound: it sets no other limit, and it is destroyed on
every exit path — that destruction is also the partial-file cleanup. The
writer touches the attached server only through the save call itself.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

CLUSTER_BYTES = 4096
VOLUME_SLACK_BYTES = 64 * 1024 * 1024
NO_WINDOW = 0x08000000
PWSH = ["pwsh.exe", "-NoProfile", "-NoLogo", "-Command"]


class BoundedSaveUnavailable(RuntimeError):
    """No enforceable write-time bound exists in this environment."""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail or reason_code)


class SaveCopyWriteCapExceeded(RuntimeError):
    """The copy needed more bytes than the cap allowed, at write time.

    ``enforcement_point`` says which bound refused it: ``os_volume`` (the
    volume ran out of clusters mid-write) or ``post_write_byte_check`` (the
    copy fit the cluster bound but exceeded the byte cap by less than one
    cluster).
    """

    def __init__(self, detail: str, *, enforcement_point: str = "os_volume"):
        self.enforcement_point = enforcement_point
        super().__init__(detail)


class VolumeProtocol(Protocol):
    """One prepared capped volume; ``save_path`` receives the raw copy."""

    save_path: Path

    def teardown(self) -> None: ...


def aligned_cap(cap: int) -> int:
    """The cluster-aligned free-space bound for one byte cap."""
    return (cap + CLUSTER_BYTES - 1) // CLUSTER_BYTES * CLUSTER_BYTES


def _run(command: list[str], *, what: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command,
        capture_output=True,
        text=True,
        timeout=300,
        creationflags=NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{what} failed ({completed.returncode}): "
            f"{(completed.stdout or '')[-400:]} {(completed.stderr or '')[-400:]}".strip()
        )
    return completed.stdout or ""


def _is_elevated() -> bool:
    """Whether this process runs with an elevated (admin) token."""
    if os.name != "nt":
        return False
    try:
        import ctypes.wintypes as wt

        kernel32 = getattr(ctypes, "WinDLL")("kernel32")
        advapi32 = getattr(ctypes, "WinDLL")("advapi32")
        kernel32.GetCurrentProcess.restype = wt.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        advapi32.OpenProcessToken.restype = wt.BOOL
        advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
        advapi32.GetTokenInformation.restype = wt.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wt.HANDLE,
            wt.DWORD,
            ctypes.c_void_p,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
        ]
        kernel32.CloseHandle.restype = wt.BOOL
        kernel32.CloseHandle.argtypes = [wt.HANDLE]
        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            0x0008,
            ctypes.byref(token),  # TOKEN_QUERY
        ):
            return False
        info = ctypes.create_string_buffer(4)
        try:
            returned = wt.DWORD(0)
            if not advapi32.GetTokenInformation(
                token,
                20,  # TokenElevation
                info,
                len(info),
                ctypes.byref(returned),
            ):
                return False
            return bool(ctypes.c_int.from_buffer(info).value)
        finally:
            kernel32.CloseHandle(token)
    except OSError, AttributeError:
        return False


class DiskpartVolume:
    """One attached, capped, folder-mounted VHDX volume.

    ``teardown`` detaches and deletes the VHDX; everything written inside
    disappears with it.
    """

    def __init__(self, cap: int, work_dir: Path):
        self.cap = cap
        self.work_dir = work_dir
        self.vhd = work_dir / "capvol.vhdx"
        self.mount = work_dir / "mount"
        self.mount.mkdir(parents=True)
        self.save_path = self.mount / "copy.mph"
        size_mb = (VOLUME_SLACK_BYTES + aligned_cap(cap)) // (1024 * 1024) + 8
        script = (
            f"create vdisk file={self.vhd} maximum={size_mb} type=expandable\n"
            f"select vdisk file={self.vhd}\nattach vdisk\n"
        )
        script_file = work_dir / "dp.txt"
        script_file.write_text(script, encoding="ascii")
        try:
            _run(["diskpart.exe", "/s", str(script_file)], what="create+attach vdisk")
            self._format_and_mount()
            self._fill_to_cap()
        finally:
            script_file.unlink(missing_ok=True)

    def _disk_number(self) -> int:
        # Get-Disk's Path is a SCSI device id; Location carries the VHDX path
        # exactly as passed to diskpart (native separators).
        vhd_arg = str(self.vhd)
        output = _run(
            [
                *PWSH,
                f"(Get-Disk | Where-Object {{$_.Location -eq '{vhd_arg}'}}"
                " | Select-Object -First 1).DiskNumber",
            ],
            what="identify the capped volume's disk",
        )
        text = output.strip()
        if not text.isdigit():
            raise RuntimeError(f"attached vdisk not identified: {output[-200:]}")
        return int(text)

    def _format_and_mount(self) -> None:
        number = self._disk_number()
        mount_arg = str(self.mount).replace("\\", "/") + "/"
        _run(
            [
                *PWSH,
                f"$d = Initialize-Disk -Number {number} -PartitionStyle MBR;"
                f"$p = New-Partition -DiskNumber {number} -UseMaximumSize;"
                f"$null = Format-Volume -Partition $p -FileSystem NTFS"
                f" -NewFileSystemLabel P10CAP -Confirm:$false;"
                f"Add-PartitionAccessPath -DiskNumber {number}"
                f" -PartitionNumber $p.PartitionNumber -AccessPath '{mount_arg}'",
            ],
            what="format+mount the capped volume",
        )

    def _fill_to_cap(self) -> None:
        wanted = aligned_cap(self.cap)
        filler = self.mount / "filler.bin"
        filler_size = 0
        observed = 0
        for _ in range(8):
            free = shutil.disk_usage(self.mount).free
            filler_size = max(filler_size + free - wanted, 0)
            with filler.open("wb") as handle:
                handle.truncate(filler_size)
            observed = shutil.disk_usage(self.mount).free
            if observed == wanted:
                return
        raise RuntimeError(f"capped volume free {observed} != cap bound {wanted} after adjustments")

    def teardown(self) -> None:
        try:
            _run(
                [
                    *PWSH,
                    f"Dismount-DiskImage -ImagePath '{self.vhd}' -ErrorAction SilentlyContinue",
                ],
                what="dismount the capped volume",
            )
        finally:
            self.vhd.unlink(missing_ok=True)


class FakeVolume:
    """Test seam: one directory with a Python-enforced byte budget."""

    def __init__(self, cap: int, work_dir: Path):
        from threading import Lock

        self.cap = aligned_cap(cap)
        self.directory = work_dir / "volume"
        self.directory.mkdir(parents=True)
        self.save_path = self.directory / "copy.mph"
        self._used = 0
        self._lock = Lock()
        self.teardown_called = False

    def write(self, data: bytes) -> int:
        with self._lock:
            if self._used + len(data) > self.cap:
                error = OSError(28, "No space left on device")
                error.winerror = 112  # ERROR_DISK_FULL
                raise error
            with self.save_path.open("ab") as handle:
                handle.write(data)
            self._used += len(data)
            return self._used

    def teardown(self) -> None:
        self.teardown_called = True
        shutil.rmtree(self.directory, ignore_errors=True)


def _stream_copy(source: Path, target: Path) -> None:
    """Copy with both-side hashing; the caller refuses on any mismatch."""
    source_digest = hashlib.sha256()
    target_digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        while chunk := reader.read(1024 * 1024):
            source_digest.update(chunk)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    with target.open("rb") as verify:
        while chunk := verify.read(1024 * 1024):
            target_digest.update(chunk)
    if source_digest.digest() != target_digest.digest():
        raise RuntimeError("copy-out hash mismatch; the target bytes were refused")


def save_copy_into_volume(
    client: Any,
    model_tag: str,
    target: Path,
    *,
    cap: int,
    volume: VolumeProtocol,
) -> int:
    """Write one Save Copy through a prepared capped volume; return its size.

    The volume is always torn down. The final target only ever holds a
    complete copy whose size is within the cap.
    """
    try:
        try:
            matches = [
                model for model in list(client.models()) if str(model.java.tag()) == model_tag
            ]
            if len(matches) != 1:
                raise ValueError("adopted server model is no longer uniquely available")
            matches[0].java.save(str(volume.save_path), True)
        except OSError as exc:
            windows_error = getattr(exc, "winerror", None)
            if windows_error == 112 or exc.errno in (28,):
                raise SaveCopyWriteCapExceeded(f"{cap}", enforcement_point="os_volume") from exc
            raise
        except Exception as exc:  # noqa: BLE001 - the server wraps OS errors
            text = f"{type(exc).__name__}: {exc}"
            if "磁盘空间不足" in text or "not enough space" in text.lower():
                raise SaveCopyWriteCapExceeded(f"{cap}", enforcement_point="os_volume") from exc
            raise
        if not volume.save_path.is_file():
            raise RuntimeError("Save Copy did not create a file inside the volume")
        size = volume.save_path.stat().st_size
        if size <= 0:
            raise RuntimeError("Save Copy created an empty file inside the volume")
        if size > cap:
            # The OS bound is cluster-granular; at most one cluster over the
            # cap can land inside the volume. Refuse it before any byte leaves.
            raise SaveCopyWriteCapExceeded(
                f"{size} > {cap}", enforcement_point="post_write_byte_check"
            )
        if target.exists():
            raise FileExistsError("shared snapshot target already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        _stream_copy(volume.save_path, target)
        return size
    finally:
        volume.teardown()


def windows_bounded_volume_factory(cap: int, work_dir: Path) -> VolumeProtocol:
    """Build one capped volume, or say why none can exist here."""
    if os.name != "nt":
        raise BoundedSaveUnavailable("unsupported_platform", "the capped volume needs Windows")
    if shutil.which("diskpart.exe") is None or shutil.which("pwsh.exe") is None:
        raise BoundedSaveUnavailable(
            "tooling_missing", "diskpart and pwsh are required to build the capped volume"
        )
    if not _is_elevated():
        raise BoundedSaveUnavailable(
            "elevation_required", "attaching the capped volume needs an elevated host"
        )
    return DiskpartVolume(cap, work_dir)


class BoundedWriter:
    """One call's prepared writer: the volume exists until run or discarded."""

    def __init__(self, writer: Callable[[Any, str, Path], None], volume: VolumeProtocol):
        self._writer = writer
        self._volume = volume
        self._consumed = False

    def run(self, client: Any, model_tag: str, target: Path) -> None:
        """Run the save; the writer owns the volume's teardown from here on."""
        self._consumed = True
        self._writer(client, model_tag, target)

    def discard(self) -> None:
        """Tear the prepared volume down when the save never runs."""
        if not self._consumed:
            self._consumed = True
            self._volume.teardown()


def prepare_bounded_writer(
    max_snapshot_bytes: int,
    *,
    volume_factory: Callable[[int, Path], VolumeProtocol] | None = None,
    volume_root: Path | None = None,
) -> dict[str, Any]:
    """Prepare one bounded writer for a snapshot call, or refuse.

    Returns ``{"available": True, "bounded": BoundedWriter, "mechanism":
    "os_volume_free_space", "write_time_bound_bytes": ...}`` or
    ``{"available": False, "reason_code": ..., "detail": ...}``. The caller
    must ``discard()`` the bounded writer when it does not run the save, and
    the writer itself tears the volume down after the save.
    """
    factory = volume_factory or windows_bounded_volume_factory

    def resolve_root() -> Path:
        if volume_root is not None:
            return volume_root
        from comsol_mcp.path_policy import PathPolicy

        policy = PathPolicy.from_environment()
        resolved = Path(policy.shared_snapshot_root) / ".bounded-volume"
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved

    work = resolve_root() / f"run-{uuid.uuid4().hex}"
    work.mkdir(parents=True)
    try:
        volume = factory(max_snapshot_bytes, work)
    except BoundedSaveUnavailable as exc:
        shutil.rmtree(work, ignore_errors=True)
        return {"available": False, "reason_code": exc.reason_code, "detail": str(exc)}

    def writer(client: Any, model_tag: str, target: Path) -> None:
        save_copy_into_volume(client, model_tag, target, cap=max_snapshot_bytes, volume=volume)

    return {
        "available": True,
        "bounded": BoundedWriter(writer, volume),
        "mechanism": "os_volume_free_space",
        "write_time_bound_bytes": aligned_cap(max_snapshot_bytes),
    }


__all__ = [
    "BoundedSaveUnavailable",
    "BoundedWriter",
    "DiskpartVolume",
    "FakeVolume",
    "SaveCopyWriteCapExceeded",
    "aligned_cap",
    "prepare_bounded_writer",
    "save_copy_into_volume",
    "windows_bounded_volume_factory",
]
