"""A bounded shared-model Save Copy writer (batch D, work item ten).

The 0.8.1 finding: ``Model.save(path, copy=True)`` writes inside the COMSOL
server process, so a Python-side byte counter can only observe the file
after the bytes are on disk. The accepted build (6.4.0.293) offers no
incremental write limit through the API, and pointing the save at a named
pipe crashes the server-side JVM.

This module enforces the caller-declared cap at write time through the
operating system: the copy is written into a throwaway virtual-disk volume
whose free space a filler file sets to the write-time bound. The OS refuses
any write that needs more space than the bound, so the copy can never grow
past it, and nothing reaches the final target until the copy-out hash
verifies. The image, its mount point and its work directory are removed on
every exit path; every cleanup failure is reported, never hidden.

Bound semantics. Free space is granular to the file-system cluster, so the
bound is the largest cluster multiple that does not exceed the cap:

* the volume uses 512-byte NTFS clusters, the smallest NTFS cluster. A VHD
  image has 512-byte physical sectors; a VHDX image reports 4 KiB sectors
  and refuses 512-byte clusters. The bound is therefore at most 511 bytes
  below the cap, and equals the cap whenever the cap is a multiple of 512;
* native output never exceeds the bound, hence never exceeds the cap;
* a copy larger than the bound is refused at write time. That includes a
  copy that would have fit in the last partial cluster under an unaligned
  cap; the typed refusal reports both the cap and the bound;
* a completed save larger than the bound means the volume failed to
  enforce. It is refused as a bound violation and never copied out.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

CLUSTER_BYTES = 512
IMAGE_NAME = "capvol.vhd"
BOUND_RULE = "largest_cluster_multiple_not_above_cap"
VOLUME_SLACK_BYTES = 64 * 1024 * 1024
NO_WINDOW = 0x08000000
PWSH = ["pwsh.exe", "-NoProfile", "-NoLogo", "-NonInteractive", "-Command"]
DISK_FULL_WINERRORS = (112, 39)  # ERROR_DISK_FULL, ERROR_HANDLE_DISK_FULL
DISK_FULL_ERRNO = 28  # ENOSPC
PENDING_CLEANUP_RECORD = "cleanup-pending.json"
PENDING_CLEANUP_SCHEMA = "comsol_mcp.bounded_save_pending_cleanup"
PENDING_CLEANUP_VERSION = "1.0.0"


class BoundedSaveUnavailable(RuntimeError):
    """No enforceable write-time bound exists in this environment."""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail or reason_code)


class VolumePreparationFailed(RuntimeError):
    """Building the capped volume failed; ``cleanup`` is the rollback report."""

    def __init__(self, detail: str, *, cleanup: Mapping[str, Any]):
        self.cleanup = cleanup
        super().__init__(detail)


class SaveCopyWriteCapExceeded(RuntimeError):
    """The OS refused the copy at the write-time bound, during the write.

    ``partial_bytes_observed`` is the size the copy reached inside the volume
    when the OS refused it; it never exceeds the bound.
    """

    enforcement_point = "os_volume"

    def __init__(
        self,
        detail: str,
        *,
        write_time_bound_bytes: int,
        partial_bytes_observed: int | None,
    ):
        self.write_time_bound_bytes = write_time_bound_bytes
        self.partial_bytes_observed = partial_bytes_observed
        super().__init__(detail)


class WriteTimeBoundViolated(RuntimeError):
    """A completed save exceeded the bound: the volume did not enforce it."""

    def __init__(self, *, observed_bytes: int, write_time_bound_bytes: int):
        self.observed_bytes = observed_bytes
        self.write_time_bound_bytes = write_time_bound_bytes
        super().__init__(f"{observed_bytes} > {write_time_bound_bytes}")


class VolumeProtocol(Protocol):
    """One prepared capped volume; ``save_path`` receives the raw copy."""

    save_path: Path

    def teardown(self) -> Mapping[str, Any]: ...


def cleanup_state_of(report: Mapping[str, Any]) -> str:
    """Classify one cleanup report: complete, residue_reported or unverified.

    Only an explicit ``residue_free: True`` counts as complete. A report that
    says nothing, or one from a teardown that raised, is unverified.
    """
    if report.get("teardown_raised"):
        return "unverified"
    residue_free = report.get("residue_free")
    if residue_free is True:
        return "complete"
    if residue_free is False:
        return "residue_reported"
    return "unverified"


def write_time_bound(cap: int) -> int:
    """The largest cluster multiple not above ``cap``: the enforced bound."""
    return cap // CLUSTER_BYTES * CLUSTER_BYTES


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


def _pwsh(script: str, *, what: str) -> str:
    """Run one PowerShell script; any error, terminating or not, fails it."""
    return _run([*PWSH, "$ErrorActionPreference = 'Stop'; " + script], what=what)


def _last_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


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


def _disk_full_markers() -> tuple[str, ...]:
    """Disk-full texts, including this host's own localized messages."""
    markers = [
        "not enough space on the disk",
        "disk is full",
        "no space left on device",
        "磁盘空间不足",
        "磁盘已满",
    ]
    format_error = getattr(ctypes, "FormatError", None)
    if format_error is not None:
        for code in DISK_FULL_WINERRORS:
            text = str(format_error(code)).strip().rstrip("。.").strip()
            if text:
                markers.append(text)
    return tuple(marker.lower() for marker in markers)


def _is_disk_full(exc: BaseException) -> bool:
    """Whether a save failure is the volume refusing more space.

    The server reports the OS error inside its own exception text, so the
    text is matched as well as the structured codes.
    """
    if isinstance(exc, OSError) and (
        getattr(exc, "winerror", None) in DISK_FULL_WINERRORS or exc.errno == DISK_FULL_ERRNO
    ):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _disk_full_markers())


def _remove_dir_if_empty(path: Path, errors: list[str], *, label: str) -> None:
    if not os.path.lexists(path):
        return
    try:
        os.rmdir(path)
    except OSError as exc:
        errors.append(f"{label}: {type(exc).__name__}: {exc}")


class DiskpartVolume:
    """One attached, capped, folder-mounted VHD volume with 512-byte clusters.

    Construction is failure-atomic: a failure after any step rolls the volume
    back and raises ``VolumePreparationFailed`` with the rollback report.
    ``teardown`` is idempotent, never raises, and reports what remains.
    """

    def __init__(self, cap: int, work_dir: Path):
        self.cap = cap
        self.bound = write_time_bound(cap)
        self.work_dir = work_dir
        self.vhd = work_dir / IMAGE_NAME
        self.mount = work_dir / "mount"
        self.save_path = self.mount / "copy.mph"
        self.size_bytes = ((VOLUME_SLACK_BYTES + self.bound) // (1024 * 1024) + 8) * 1024 * 1024
        self._report: dict[str, Any] | None = None
        script_file = work_dir / "dp.txt"
        try:
            try:
                self.mount.mkdir(parents=True)
                script_file.write_text(
                    f"create vdisk file={self.vhd} maximum={self.size_bytes // (1024 * 1024)}"
                    " type=expandable\n"
                    f"select vdisk file={self.vhd}\nattach vdisk\n",
                    encoding="ascii",
                )
                _run(["diskpart.exe", "/s", str(script_file)], what="create+attach vdisk")
                self._format_and_mount()
                self._fill_to_bound()
            finally:
                script_file.unlink(missing_ok=True)
        except BaseException as exc:
            cleanup = self._rollback()
            if not isinstance(exc, Exception):
                raise
            raise VolumePreparationFailed(f"{type(exc).__name__}: {exc}", cleanup=cleanup) from exc

    def _disk_number(self) -> int:
        # Get-Disk's Path is a SCSI device id; Location carries the image path
        # exactly as passed to diskpart (native separators).
        output = _pwsh(
            f"(Get-Disk | Where-Object {{$_.Location -eq '{self.vhd}'}}"
            " | Select-Object -First 1).DiskNumber",
            what="identify the capped volume's disk",
        )
        text = output.strip()
        if not text.isdigit():
            raise RuntimeError(f"attached vdisk not identified: {output[-200:]}")
        return int(text)

    def _format_and_mount(self) -> None:
        number = self._disk_number()
        mount_arg = str(self.mount).replace("\\", "/") + "/"
        unit = _last_line(
            _pwsh(
                f"$null = Initialize-Disk -Number {number} -PartitionStyle MBR;"
                f"$p = New-Partition -DiskNumber {number} -UseMaximumSize;"
                "$v = Format-Volume -Partition $p -FileSystem NTFS"
                f" -AllocationUnitSize {CLUSTER_BYTES} -NewFileSystemLabel BOUNDCAP"
                " -Confirm:$false;"
                f"Add-PartitionAccessPath -DiskNumber {number}"
                f" -PartitionNumber $p.PartitionNumber -AccessPath '{mount_arg}';"
                "$v.AllocationUnitSize",
                what="format+mount the capped volume",
            )
        )
        if unit != str(CLUSTER_BYTES):
            raise RuntimeError(f"capped volume cluster is {unit!r}, not {CLUSTER_BYTES}")
        # Every later size decision reads this folder's free space; it must be
        # the new volume, never the host drive underneath.
        if not os.path.ismount(self.mount):
            raise RuntimeError("the capped volume is not mounted at its folder")

    def _fill_to_bound(self) -> None:
        wanted = self.bound
        total = shutil.disk_usage(self.mount).total
        if total > self.size_bytes:
            raise RuntimeError(f"free-space probe reads {total} bytes, not the capped volume")
        filler = self.mount / "filler.bin"
        filler_size = 0
        observed = 0
        for _ in range(12):
            free = shutil.disk_usage(self.mount).free
            filler_size = max(filler_size + free - wanted, 0)
            with filler.open("wb") as handle:
                handle.truncate(filler_size)
            observed = shutil.disk_usage(self.mount).free
            if observed == wanted:
                return
        raise RuntimeError(f"capped volume free {observed} != bound {wanted} after adjustments")

    def recovery_identity(self) -> dict[str, str]:
        """Where a later verified cleanup finds this volume."""
        return {
            "work_dir": str(self.work_dir),
            "image_path": str(self.vhd),
            "mount_point": str(self.mount),
        }

    def residue(self) -> dict[str, bool]:
        """What of this volume still exists on the host."""
        return {
            "image_exists": self.vhd.exists(),
            "mount_point_exists": os.path.lexists(self.mount),
            "work_dir_exists": os.path.lexists(self.work_dir),
        }

    def _detach(self, errors: list[str]) -> bool:
        """Detach the image and read the attachment back; True once detached."""
        if not self.vhd.exists():
            return True
        try:
            state = _last_line(
                _pwsh(
                    f"$i = Get-DiskImage -ImagePath '{self.vhd}' -ErrorAction SilentlyContinue;"
                    f"if ($i -and $i.Attached) {{ Dismount-DiskImage -ImagePath '{self.vhd}'"
                    " | Out-Null };"
                    f"$a = Get-DiskImage -ImagePath '{self.vhd}' -ErrorAction SilentlyContinue;"
                    "if ($a -and $a.Attached) { 'attached' } else { 'detached' }",
                    what="detach the capped volume",
                )
            )
        except Exception as exc:  # noqa: BLE001 - every cleanup failure is evidence
            errors.append(f"detach: {type(exc).__name__}: {exc}")
            return False
        if state != "detached":
            errors.append(f"detach: the image still reads {state!r}")
            return False
        return True

    def _sweep(self, errors: list[str], *, detached: bool) -> None:
        """Remove the image, then its mount point and work directory.

        The image is deleted only after a verified detach; when the detach
        cannot be verified, the image is retained and the reason recorded.
        The mount point and work directory are removed only once the image
        is gone: the mount point is then a dangling junction (or an empty
        folder), and removing it never touches a live volume.
        """
        if self.vhd.exists():
            if not detached:
                errors.append("image retained: its detach was not verified")
            else:
                try:
                    self.vhd.unlink()
                except OSError as exc:
                    errors.append(f"image: {type(exc).__name__}: {exc}")
        if self.vhd.exists():
            return
        _remove_dir_if_empty(self.mount, errors, label="mount point")
        for name in ("dp.txt", PENDING_CLEANUP_RECORD):
            try:
                (self.work_dir / name).unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        _remove_dir_if_empty(self.work_dir, errors, label="work directory")

    def teardown(self) -> dict[str, Any]:
        """Detach and delete the image, then remove its mount point and work dir."""
        if self._report is None:
            errors: list[str] = []
            self._sweep(errors, detached=self._detach(errors))
            residue = self.residue()
            self._report = {
                **residue,
                "errors": errors,
                "residue_free": not any(residue.values()),
            }
        return self._report

    def _rollback(self) -> dict[str, Any]:
        """Undo a partial construction and read the residue back independently."""
        teardown_report = self.teardown()
        errors: list[str] = []
        if any(self.residue().values()):
            # Do not trust a teardown that left residue or never ran: sweep
            # again under the same safety rule.
            self._sweep(errors, detached=self._detach(errors))
        residue = self.residue()
        report: dict[str, Any] = {
            **residue,
            "teardown": teardown_report,
            "errors": errors,
            "residue_free": not any(residue.values()),
        }
        report["cleanup_state"] = cleanup_state_of(report)
        if report["cleanup_state"] != "complete":
            report["recovery_identity"] = self.recovery_identity()
        return report


class FakeVolume:
    """Test seam: one directory with a Python-enforced budget at the bound.

    Like the OS, a write that needs more room than remains fails whole.
    """

    def __init__(self, cap: int, work_dir: Path):
        from threading import Lock

        self.cap = cap
        self.bound = write_time_bound(cap)
        self.work_dir = work_dir
        self.directory = work_dir / "volume"
        self.directory.mkdir(parents=True)
        self.save_path = self.directory / "copy.mph"
        self._used = 0
        self._lock = Lock()
        self.teardown_called = False
        self._report: dict[str, Any] | None = None

    def write(self, data: bytes) -> int:
        with self._lock:
            if self._used + len(data) > self.bound:
                error = OSError(DISK_FULL_ERRNO, "No space left on device")
                # ERROR_DISK_FULL on Windows; POSIX readers match on errno.
                setattr(error, "winerror", 112)
                raise error
            with self.save_path.open("ab") as handle:
                handle.write(data)
            self._used += len(data)
            return self._used

    def recovery_identity(self) -> dict[str, str]:
        return {"work_dir": str(self.work_dir), "volume_directory": str(self.directory)}

    @property
    def bytes_written(self) -> int:
        """Every byte the writer has ever placed in this volume."""
        return self._used

    def teardown(self) -> dict[str, Any]:
        if self._report is None:
            self.teardown_called = True
            errors: list[str] = []
            try:
                shutil.rmtree(self.directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"volume: {type(exc).__name__}: {exc}")
            _remove_dir_if_empty(self.work_dir, errors, label="work directory")
            residue = {
                "image_exists": self.directory.exists(),
                "mount_point_exists": False,
                "work_dir_exists": os.path.lexists(self.work_dir),
            }
            self._report = {
                **residue,
                "errors": errors,
                "residue_free": not any(residue.values()),
            }
        return self._report


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


def _size_or_none(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def save_copy_into_volume(
    client: Any,
    model_tag: str,
    target: Path,
    *,
    cap: int,
    volume: VolumeProtocol,
) -> int:
    """Write one Save Copy through a prepared capped volume; return its size.

    The final target only ever holds a complete copy within the bound. The
    caller owns the volume's teardown.
    """
    bound = write_time_bound(cap)
    matches = [model for model in list(client.models()) if str(model.java.tag()) == model_tag]
    if len(matches) != 1:
        raise ValueError("adopted server model is no longer uniquely available")
    try:
        matches[0].java.save(str(volume.save_path), True)
    except Exception as exc:  # noqa: BLE001 - the server wraps OS errors
        if _is_disk_full(exc):
            raise SaveCopyWriteCapExceeded(
                f"the copy needs more than the {bound}-byte write-time bound",
                write_time_bound_bytes=bound,
                partial_bytes_observed=_size_or_none(volume.save_path),
            ) from exc
        raise
    if not volume.save_path.is_file():
        raise RuntimeError("Save Copy did not create a file inside the volume")
    size = volume.save_path.stat().st_size
    if size <= 0:
        raise RuntimeError("Save Copy created an empty file inside the volume")
    if size > bound:
        raise WriteTimeBoundViolated(observed_bytes=size, write_time_bound_bytes=bound)
    if target.exists():
        raise FileExistsError("shared snapshot target already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    _stream_copy(volume.save_path, target)
    return size


def windows_bounded_volume_factory(cap: int, work_dir: Path) -> VolumeProtocol:
    """Build one capped volume, or say why none can exist here."""
    if os.name != "nt":
        raise BoundedSaveUnavailable("unsupported_platform", "the capped volume needs Windows")
    if shutil.which("diskpart.exe") is None or shutil.which("pwsh.exe") is None:
        raise BoundedSaveUnavailable(
            "tooling_missing", "diskpart and pwsh are required to build the capped volume"
        )
    if not str(work_dir).isascii():
        raise BoundedSaveUnavailable(
            "volume_root_not_ascii", "the capped volume's work directory must be ASCII"
        )
    if not _is_elevated():
        raise BoundedSaveUnavailable(
            "elevation_required", "attaching the capped volume needs an elevated host"
        )
    return DiskpartVolume(cap, work_dir)


class BoundedWriter:
    """One call's prepared writer; it owns the volume until teardown.

    ``run`` performs the save and always tears the volume down; ``discard``
    tears it down when the save never runs. Either may be called on any exit
    path, in any order: the volume is torn down exactly once, and
    ``cleanup_report`` keeps the result.
    """

    def __init__(self, volume: VolumeProtocol, *, cap: int, bound: int):
        self._volume = volume
        self.cap = cap
        self.write_time_bound_bytes = bound
        self._consumed = False
        self.cleanup_report: dict[str, Any] | None = None

    def run(self, client: Any, model_tag: str, target: Path) -> int:
        """Run the save once; the volume is torn down before this returns."""
        if self._consumed:
            raise RuntimeError("a prepared bounded writer runs at most once")
        self._consumed = True
        try:
            return save_copy_into_volume(
                client, model_tag, target, cap=self.cap, volume=self._volume
            )
        finally:
            self._teardown()

    def discard(self) -> dict[str, Any]:
        """Tear the volume down if the save never ran; return the report."""
        self._consumed = True
        return self._teardown()

    def _teardown(self) -> dict[str, Any]:
        if self.cleanup_report is None:
            identity = _recovery_identity(self._volume)
            try:
                report = dict(self._volume.teardown())
            except Exception as exc:  # noqa: BLE001 - a failed teardown is evidence
                report = {
                    "residue_free": False,
                    "teardown_raised": True,
                    "errors": [f"teardown: {type(exc).__name__}: {exc}"],
                }
            report["cleanup_state"] = cleanup_state_of(report)
            if report["cleanup_state"] != "complete":
                # Keep what a later verified cleanup needs, durably while the
                # work directory still exists.
                report["recovery_identity"] = identity
                report.update(_record_pending_cleanup(identity, report))
            self.cleanup_report = report
        return self.cleanup_report


def _recovery_identity(volume: Any) -> dict[str, str]:
    describe = getattr(volume, "recovery_identity", None)
    if callable(describe):
        try:
            return {str(key): str(value) for key, value in dict(describe()).items()}
        except Exception as exc:  # noqa: BLE001 - identity is evidence, never fatal
            return {"identity_error": f"{type(exc).__name__}: {exc}"}
    return {"work_dir": str(getattr(volume, "work_dir", ""))}


def _record_pending_cleanup(
    identity: Mapping[str, str], report: Mapping[str, Any]
) -> dict[str, str]:
    """Persist one pending-cleanup record inside the volume's work directory."""
    location = identity.get("work_dir") or ""
    work_dir = Path(location)
    if not location or not work_dir.is_dir():
        return {"cleanup_record_error": "the work directory is not present"}
    record = work_dir / PENDING_CLEANUP_RECORD
    body = {
        "schema_name": PENDING_CLEANUP_SCHEMA,
        "schema_version": PENDING_CLEANUP_VERSION,
        "recorded_at_epoch": time.time(),
        "recovery_identity": dict(identity),
        "cleanup_state": report.get("cleanup_state"),
        "report": {key: value for key, value in report.items() if key != "recovery_identity"},
    }
    try:
        record.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        return {"cleanup_record_error": f"{type(exc).__name__}: {exc}"}
    return {"cleanup_record_path": str(record)}


def _refusal_cleanup(work: Path, errors: list[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "image_exists": False,
        "mount_point_exists": False,
        "work_dir_exists": os.path.lexists(work),
        "errors": errors,
        "residue_free": not errors and not os.path.lexists(work),
    }
    report["cleanup_state"] = cleanup_state_of(report)
    if report["cleanup_state"] != "complete":
        report["recovery_identity"] = {"work_dir": str(work)}
    return report


def prepare_bounded_writer(
    max_snapshot_bytes: int,
    *,
    volume_factory: Callable[[int, Path], VolumeProtocol] | None = None,
    volume_root: Path | None = None,
) -> dict[str, Any]:
    """Prepare one bounded writer for a snapshot call, or refuse.

    Returns ``{"available": True, "bounded": BoundedWriter, ...}`` with the
    write-time bound, or ``{"available": False, "reason_code": ...,
    "detail": ...}``; a refusal after a volume was started carries the
    rollback report under ``cleanup``. The caller must ``discard()`` the
    bounded writer on every path where it does not run the save.
    """
    bound = write_time_bound(max_snapshot_bytes)
    facts = {
        "write_time_bound_bytes": bound,
        "granularity_bytes": CLUSTER_BYTES,
        "bound_rule": BOUND_RULE,
    }
    if bound <= 0:
        return {
            "available": False,
            "reason_code": "cap_below_write_granularity",
            "detail": f"no {CLUSTER_BYTES}-byte multiple fits a {max_snapshot_bytes}-byte cap",
            **facts,
        }
    factory = volume_factory or windows_bounded_volume_factory
    try:
        if volume_root is not None:
            root = volume_root
        else:
            from comsol_mcp.path_policy import PathPolicy

            root = Path(PathPolicy.from_environment().shared_snapshot_root) / ".bounded-volume"
        root.mkdir(parents=True, exist_ok=True)
        work = root / f"run-{uuid.uuid4().hex}"
        work.mkdir()
    except Exception as exc:  # noqa: BLE001 - refuse with the reason
        return {
            "available": False,
            "reason_code": "volume_root_unavailable",
            "detail": f"{type(exc).__name__}: {exc}",
            **facts,
        }
    try:
        volume = factory(max_snapshot_bytes, work)
    except BoundedSaveUnavailable as exc:
        errors: list[str] = []
        _remove_dir_if_empty(work, errors, label="work directory")
        return {
            "available": False,
            "reason_code": exc.reason_code,
            "detail": str(exc),
            "cleanup": _refusal_cleanup(work, errors),
            **facts,
        }
    except VolumePreparationFailed as exc:
        return {
            "available": False,
            "reason_code": "volume_preparation_failed",
            "detail": str(exc),
            "cleanup": exc.cleanup,
            **facts,
        }
    except Exception as exc:  # noqa: BLE001 - a non-atomic factory still refuses
        errors = []
        _remove_dir_if_empty(work, errors, label="work directory")
        return {
            "available": False,
            "reason_code": "volume_preparation_failed",
            "detail": f"{type(exc).__name__}: {exc}",
            "cleanup": _refusal_cleanup(work, errors),
            **facts,
        }
    return {
        "available": True,
        "bounded": BoundedWriter(volume, cap=max_snapshot_bytes, bound=bound),
        "mechanism": "os_volume_free_space",
        **facts,
    }


__all__ = [
    "BOUND_RULE",
    "CLUSTER_BYTES",
    "BoundedSaveUnavailable",
    "BoundedWriter",
    "DiskpartVolume",
    "FakeVolume",
    "SaveCopyWriteCapExceeded",
    "VolumePreparationFailed",
    "WriteTimeBoundViolated",
    "cleanup_state_of",
    "prepare_bounded_writer",
    "save_copy_into_volume",
    "windows_bounded_volume_factory",
    "write_time_bound",
]
