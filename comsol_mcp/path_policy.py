"""Configured containment policy for caller-selected model and artifact paths."""

from __future__ import annotations

import ctypes
import hashlib
import inspect
import os
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from comsol_mcp.settings import settings_environment
from comsol_mcp.utils.runtime_paths import default_runtime_dir

MODEL_READ_ROOTS_ENV = "COMSOL_MCP_MODEL_READ_ROOTS"
ARTIFACT_WRITE_ROOT_ENV = "COMSOL_MCP_ARTIFACT_WRITE_ROOT"
PATH_POLICY_SCHEMA = "comsol_mcp.path_policy"
PATH_POLICY_VERSION = "1.1.0"
SHARED_SNAPSHOT_DIRECTORY = "shared_snapshots"

_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_DEVICE_PREFIX = re.compile(r"^(?:\\\\[?.]\\|//[?.]/)")
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000


@dataclass(frozen=True)
class PathDecision:
    """One redacted path validation result."""

    kind: str
    normalized_path: Path
    root_id: str
    read_pin: "ValidatedReadPin | None" = None
    write_pin: "ValidatedWritePin | None" = None


@dataclass(frozen=True)
class ValidatedReadPin:
    """Private filesystem identity retained across a guarded read operation."""

    path: Path
    root: Path
    path_identity: tuple[int, int]
    root_identity: tuple[int, int]


@dataclass(frozen=True)
class ValidatedWritePin:
    """Private directory identity held while a validated target is written."""

    target: Path
    root: Path
    ancestor: Path
    root_identity: tuple[int, int]
    ancestor_identity: tuple[int, int]


class ReadPinError(OSError):
    """A validated input could not be held stable for the consumer call."""


def _file_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _read_pin(path: Path, root: Path) -> ValidatedReadPin:
    return ValidatedReadPin(
        path=path,
        root=root,
        path_identity=_file_identity(path),
        root_identity=_file_identity(root),
    )


def validated_read_pin(path: str | Path, root: str | Path) -> ValidatedReadPin:
    """Build one contained read pin for an existing regular file."""
    resolved_root = Path(root).resolve(strict=True)
    resolved_path = Path(path).resolve(strict=True)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("validated read path escapes its root") from exc
    if not resolved_root.is_dir() or not resolved_path.is_file():
        raise ValueError("validated read pin requires a directory root and regular file")
    return _read_pin(resolved_path, resolved_root)


def read_contained_file_snapshot(
    path: str | Path,
    *,
    root: str | Path,
    max_bytes: int,
) -> dict[str, Any]:
    """Read one contained immutable snapshot with its exact size and digest."""
    from comsol_mcp.durable.io import read_file_bytes_bounded

    pin = validated_read_pin(path, root)
    with pin_validated_reads((pin,)):
        payload = read_file_bytes_bounded(pin.path, max_bytes=max_bytes)
    return {
        "path": pin.path,
        "payload": payload,
        "byte_count": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _open_windows_read_pin(
    path: Path,
    *,
    directory: bool,
    allow_writes: bool = False,
) -> int:
    if os.name != "nt":
        raise ReadPinError("stable read-path pinning requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    # A directory pin needs actual read access so omitting FILE_SHARE_DELETE
    # continues to block rename/delete. Requesting DELETE access here would
    # conflict with ordinary pre-existing directory readers that do not share
    # delete, even though those readers do not mutate the directory.
    access = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES if directory else _GENERIC_READ
    flags = _FILE_FLAG_BACKUP_SEMANTICS if directory else _FILE_ATTRIBUTE_NORMAL
    share_mode = _FILE_SHARE_READ | (_FILE_SHARE_WRITE if directory or allow_writes else 0)
    handle = create_file(
        str(path),
        access,
        share_mode,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        error = ctypes.get_last_error()
        raise ReadPinError(
            error,
            f"validated read path could not be pinned: {path.name}",
        )
    return int(handle)


def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))


@contextmanager
def pin_validated_reads(pins: tuple[ValidatedReadPin, ...]) -> Iterator[None]:
    """Deny mutation or ancestor replacement while validated files are consumed."""
    handles: list[int] = []
    opened: set[Path] = set()
    try:
        for pin in pins:
            ancestors: list[Path] = []
            current = pin.path.parent
            while True:
                ancestors.append(current)
                if current == pin.root:
                    break
                if current.parent == current:
                    raise ReadPinError("validated read path no longer belongs to its root")
                current = current.parent
            for directory in reversed(ancestors):
                if directory not in opened:
                    handles.append(_open_windows_read_pin(directory, directory=True))
                    opened.add(directory)
            if pin.path not in opened:
                handles.append(_open_windows_read_pin(pin.path, directory=False))
                opened.add(pin.path)
            if (
                pin.path.resolve(strict=True) != pin.path
                or _file_identity(pin.path) != pin.path_identity
                or _file_identity(pin.root) != pin.root_identity
            ):
                raise ReadPinError("validated read path identity changed before use")
    except (OSError, RuntimeError, ValueError) as exc:
        for handle in reversed(handles):
            _close_windows_handle(handle)
        if isinstance(exc, ReadPinError):
            raise
        raise ReadPinError(
            f"validated read path could not be pinned: {type(exc).__name__}"
        ) from exc
    try:
        yield
    finally:
        for handle in reversed(handles):
            _close_windows_handle(handle)


@contextmanager
def pin_validated_writes(pins: tuple[ValidatedWritePin, ...]) -> Iterator[None]:
    """Prevent replacement of validated write ancestors during native writes."""
    handles: list[int] = []
    opened: set[Path] = set()
    try:
        for pin in pins:
            ancestors: list[Path] = []
            current = pin.ancestor
            while True:
                ancestors.append(current)
                if current == pin.root:
                    break
                if current.parent == current:
                    raise ReadPinError("validated write target no longer belongs to its root")
                current = current.parent
            for directory in reversed(ancestors):
                if directory not in opened:
                    handles.append(
                        _open_windows_read_pin(
                            directory,
                            directory=True,
                            allow_writes=True,
                        )
                    )
                    opened.add(directory)
            if (
                _file_identity(pin.root) != pin.root_identity
                or _file_identity(pin.ancestor) != pin.ancestor_identity
            ):
                raise ReadPinError("validated write ancestor identity changed before use")
    except (OSError, RuntimeError, ValueError) as exc:
        for handle in reversed(handles):
            _close_windows_handle(handle)
        if isinstance(exc, ReadPinError):
            raise
        raise ReadPinError(
            f"validated write path could not be pinned: {type(exc).__name__}"
        ) from exc
    try:
        yield
    finally:
        for handle in reversed(handles):
            _close_windows_handle(handle)


def _root_id(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:16]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(root)))
        ) == os.path.normcase(str(root))
    except ValueError:
        return False


def _reject_lexical_path(value: Any, *, require_ascii: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("path must be a nonempty bounded string")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError("path must use canonical NFC Unicode")
    if require_ascii and not value.isascii():
        raise ValueError("artifact write paths must be ASCII-only")
    normalized_slashes = value.replace("/", "\\")
    if _DEVICE_PREFIX.match(normalized_slashes):
        raise ValueError("device and extended-length paths are not allowed")
    if normalized_slashes.startswith("\\\\"):
        raise ValueError("UNC paths are not allowed")
    lexical_parts = normalized_slashes.split("\\")
    if any(part in {".", ".."} for part in lexical_parts[1:]):
        raise ValueError("path traversal escapes and dot aliases are not allowed")
    if any(not part for part in lexical_parts[1:-1]):
        raise ValueError("path contains ambiguous empty components")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    for part in path.parts[1:]:
        if part.endswith((" ", ".")):
            raise ValueError("path components cannot end with spaces or dots")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            raise ValueError("Windows reserved path components are not allowed")
        if ":" in part:
            raise ValueError("alternate data streams are not allowed")
    return value


def _normalize_root(value: str, *, require_ascii: bool, must_exist: bool) -> Path:
    text = _reject_lexical_path(value, require_ascii=require_ascii)
    path = Path(text)
    for candidate in (path, *path.parents):
        is_junction = getattr(candidate, "is_junction", lambda: False)
        if candidate.is_symlink() or is_junction():
            raise ValueError("configured path root contains a symlink or junction")
        if not candidate.exists():
            continue
    try:
        resolved = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"configured path root cannot be resolved: {type(exc).__name__}") from exc
    if resolved.exists() and not resolved.is_dir():
        raise ValueError("configured path root must be a directory")
    if must_exist and not resolved.exists():
        raise ValueError("configured model read root must be an existing directory")
    return resolved


class PathPolicy:
    """Strict path policy for recommended server profiles."""

    def __init__(self, model_read_roots: tuple[Path, ...], artifact_write_root: Path):
        keys = [os.path.normcase(str(path)) for path in model_read_roots]
        if len(keys) != len(set(keys)):
            raise ValueError("model read roots contain ambiguous case-normalized aliases")
        self.model_read_roots = model_read_roots
        self.artifact_write_root = artifact_write_root

    @property
    def shared_snapshot_root(self) -> Path:
        """Return the single owned ASCII root reserved for shared snapshots."""
        return self.artifact_write_root / SHARED_SNAPSHOT_DIRECTORY

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        runtime_root: str | Path | None = None,
    ) -> "PathPolicy":
        environment = settings_environment(environ)
        configured_roots = environment.get(MODEL_READ_ROOTS_ENV, "")
        root_values = tuple(value for value in configured_roots.split(os.pathsep) if value)
        read_roots = tuple(
            _normalize_root(value, require_ascii=False, must_exist=False) for value in root_values
        )
        runtime = Path(runtime_root or default_runtime_dir()).resolve()
        write_value = environment.get(
            ARTIFACT_WRITE_ROOT_ENV,
            str(runtime / "owned_artifacts"),
        )
        write_root = _normalize_root(write_value, require_ascii=True, must_exist=False)
        return cls(read_roots, write_root)

    def capability(self, *, enforced: bool) -> dict[str, Any]:
        return {
            "schema_name": PATH_POLICY_SCHEMA,
            "schema_version": PATH_POLICY_VERSION,
            "enforced": enforced,
            "model_read_roots_configured": len(self.model_read_roots),
            "artifact_write_root_ascii": str(self.artifact_write_root).isascii(),
            "shared_source_roots_configured": len(self.model_read_roots),
            "shared_snapshot_root_owned": True,
            "shared_snapshot_root_ascii": str(self.shared_snapshot_root).isascii(),
            "caller_selected_overwrite_allowed": False if enforced else None,
            "paths_included": False,
        }

    def validate_model_read(
        self, value: Any, *, suffixes: tuple[str, ...] | None = None
    ) -> PathDecision:
        text = _reject_lexical_path(value, require_ascii=False)
        if not self.model_read_roots:
            raise ValueError(f"no model read root is configured in {MODEL_READ_ROOTS_ENV}")
        try:
            resolved = Path(text).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"model input cannot be resolved: {type(exc).__name__}") from exc
        if not resolved.is_file():
            raise ValueError("model input must be an existing regular file")
        if suffixes and resolved.suffix.casefold() not in {
            suffix.casefold() for suffix in suffixes
        }:
            raise ValueError("model input has an unsupported file extension")
        for root in self.model_read_roots:
            if _is_relative_to(resolved, root):
                return PathDecision(
                    "model_read",
                    resolved,
                    _root_id(root),
                    read_pin=_read_pin(resolved, root),
                )
        raise ValueError("model input escapes the configured read roots")

    def validate_artifact_read(self, value: Any) -> PathDecision:
        text = _reject_lexical_path(value, require_ascii=True)
        try:
            resolved = Path(text).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"artifact input cannot be resolved: {type(exc).__name__}") from exc
        root = self.artifact_write_root.resolve(strict=False)
        if not resolved.is_file() or not _is_relative_to(resolved, root):
            raise ValueError("artifact input escapes the owned artifact root")
        return PathDecision(
            "artifact_read",
            resolved,
            _root_id(root),
            read_pin=_read_pin(resolved, root),
        )

    def validate_artifact_read_root(self, value: Any) -> PathDecision:
        """Validate one caller-selected directory beneath the owned artifact root."""
        text = _reject_lexical_path(value, require_ascii=True)
        path = Path(text)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"artifact root cannot be resolved: {type(exc).__name__}") from exc
        is_junction = getattr(path, "is_junction", lambda: False)
        root = self.artifact_write_root.resolve(strict=False)
        if (
            path.is_symlink()
            or is_junction()
            or not resolved.is_dir()
            or not _is_relative_to(resolved, root)
        ):
            raise ValueError("artifact root escapes the owned artifact root")
        return PathDecision(
            "artifact_read_root",
            resolved,
            _root_id(root),
            write_pin=ValidatedWritePin(
                target=resolved,
                root=root,
                ancestor=resolved,
                root_identity=_file_identity(root),
                ancestor_identity=_file_identity(resolved),
            ),
        )

    def validate_artifact_write(self, value: Any, *, directory: bool = False) -> PathDecision:
        return self._validate_write_under_root(
            value,
            root=self.artifact_write_root,
            directory=directory,
            kind_prefix="artifact_write",
        )

    def validate_shared_source(self, value: Any) -> PathDecision:
        """Validate one immutable shared-session source model."""
        decision = self.validate_model_read(value, suffixes=(".mph",))
        return PathDecision(
            "shared_source_read",
            decision.normalized_path,
            decision.root_id,
            read_pin=decision.read_pin,
        )

    def validate_shared_snapshot_write(self, value: Any) -> PathDecision:
        """Validate one new file beneath the fixed shared snapshot root."""
        return self._validate_write_under_root(
            value,
            root=self.shared_snapshot_root,
            directory=False,
            kind_prefix="shared_snapshot_write",
        )

    @staticmethod
    def _validate_write_under_root(
        value: Any,
        *,
        root: Path,
        directory: bool,
        kind_prefix: str,
    ) -> PathDecision:
        text = _reject_lexical_path(value, require_ascii=True)
        target = Path(text)
        root = root.resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        if target.exists():
            resolved_target = target.resolve(strict=True)
            if not _is_relative_to(resolved_target, root):
                raise ValueError("artifact target escapes the owned write root")
            if not directory:
                raise ValueError("caller-selected artifact targets must not already exist")
            resolved_directory = resolved_target
            if not resolved_directory.is_dir():
                raise ValueError("artifact directory escapes the owned write root")
            return PathDecision(
                f"{kind_prefix}_directory",
                resolved_directory,
                _root_id(root),
                write_pin=ValidatedWritePin(
                    target=resolved_directory,
                    root=root,
                    ancestor=resolved_directory,
                    root_identity=_file_identity(root),
                    ancestor_identity=_file_identity(resolved_directory),
                ),
            )
        existing_ancestor = target.parent
        while not existing_ancestor.exists():
            if existing_ancestor.parent == existing_ancestor:
                raise ValueError("artifact target has no resolvable parent")
            existing_ancestor = existing_ancestor.parent
        try:
            resolved_ancestor = existing_ancestor.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"artifact parent cannot be resolved: {type(exc).__name__}") from exc
        if not _is_relative_to(resolved_ancestor, root):
            raise ValueError("artifact target escapes the owned write root")
        normalized = target.resolve(strict=False)
        if not _is_relative_to(normalized, root):
            raise ValueError("artifact target escapes the owned write root")
        return PathDecision(
            f"{kind_prefix}_directory" if directory else kind_prefix,
            normalized,
            _root_id(root),
            write_pin=ValidatedWritePin(
                target=normalized,
                root=root,
                ancestor=resolved_ancestor,
                root_identity=_file_identity(root),
                ancestor_identity=_file_identity(resolved_ancestor),
            ),
        )


_MODEL_READ_ARGUMENTS = {
    "model_load": (("file_path", (".mph",)),),
    "geometry_import": (("file_path", None),),
    "solver_preflight": (("model_path", (".mph",)),),
    "wave_optics_preflight": (("expected_source_path", (".mph",)),),
    "study_staged_parametric_sweep": (("source_model_path", (".mph",)),),
}
_SHARED_SOURCE_READ_ARGUMENTS = {
    "shared_model_lock": ("immutable_source_path",),
}
_ARTIFACT_READ_ARGUMENTS = {
    "wave_optics_point_audit": ("air_reference_artifact_path",),
}
_ARTIFACT_READ_ROOT_ARGUMENTS = {
    "standalone_start": ("deployment_directory",),
    "standalone_status": ("deployment_directory",),
    "standalone_pause": ("deployment_directory",),
    "standalone_resume": ("deployment_directory",),
    "standalone_tail": ("deployment_directory",),
    "standalone_results": ("deployment_directory",),
}
_ARTIFACT_WRITE_ARGUMENTS = {
    "model_save": (("file_path", False, True),),
    "model_save_version": (("base_path", True, False),),
    "results_export_data": (("file_path", False, True),),
    "results_export_image": (("file_path", False, True),),
    "solver_preflight": (("output_path", False, False),),
    "study_staged_parametric_sweep": (
        ("csv_path", False, False),
        ("checkpoint_model_path", False, False),
        ("save_model_path", False, False),
        ("manifest_path", False, False),
    ),
    "mesh_convergence_study": (
        ("csv_path", False, False),
        ("checkpoint_model_path", False, False),
        ("save_model_path", False, False),
    ),
    "standalone_build": (("output_directory", True, True),),
}

_ALWAYS_ENFORCED_TOOLS = frozenset(
    {"geometry_import", "standalone_build", *_ARTIFACT_READ_ROOT_ARGUMENTS}
)


def validate_tool_paths(
    function: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    tool_name: str,
    profile_name: str,
) -> tuple[
    tuple[Any, ...],
    dict[str, Any],
    dict[str, Any],
    tuple[ValidatedReadPin, ...],
    tuple[ValidatedWritePin, ...],
]:
    """Validate and normalize known caller-selected path arguments."""
    if profile_name == "full" and tool_name not in _ALWAYS_ENFORCED_TOOLS:
        return (
            args,
            kwargs,
            {
                "schema_name": PATH_POLICY_SCHEMA,
                "schema_version": PATH_POLICY_VERSION,
                "enforced": False,
                "compatibility_mode": "legacy_broad_paths",
                "validated_input_count": 0,
            },
            (),
            (),
        )
    policy = PathPolicy.from_environment()
    signature = inspect.signature(function)
    bound = signature.bind(*args, **kwargs)
    decisions = []
    for argument, suffixes in _MODEL_READ_ARGUMENTS.get(tool_name, ()):
        value = bound.arguments.get(argument)
        if value is not None:
            decision = policy.validate_model_read(value, suffixes=suffixes)
            bound.arguments[argument] = str(decision.normalized_path)
            decisions.append(decision)
    for argument in _SHARED_SOURCE_READ_ARGUMENTS.get(tool_name, ()):
        value = bound.arguments.get(argument)
        if value is not None:
            decision = policy.validate_shared_source(value)
            bound.arguments[argument] = str(decision.normalized_path)
            decisions.append(decision)
    for argument in _ARTIFACT_READ_ARGUMENTS.get(tool_name, ()):
        value = bound.arguments.get(argument)
        if value is not None:
            decision = policy.validate_artifact_read(value)
            bound.arguments[argument] = str(decision.normalized_path)
            decisions.append(decision)
    for argument in _ARTIFACT_READ_ROOT_ARGUMENTS.get(tool_name, ()):
        value = bound.arguments.get(argument)
        if value is not None:
            decision = policy.validate_artifact_read_root(value)
            bound.arguments[argument] = str(decision.normalized_path)
            decisions.append(decision)
    for argument, directory, required in _ARTIFACT_WRITE_ARGUMENTS.get(tool_name, ()):
        value = bound.arguments.get(argument)
        if value is None:
            if required:
                raise ValueError(f"{tool_name}.{argument} is required by the contained path policy")
            continue
        decision = policy.validate_artifact_write(value, directory=directory)
        bound.arguments[argument] = str(decision.normalized_path)
        decisions.append(decision)
    if tool_name == "job_submit":
        spec = bound.arguments.get("spec")
        if isinstance(spec, Mapping) and spec.get("source_model_path") is not None:
            decision = policy.validate_model_read(spec["source_model_path"], suffixes=(".mph",))
            normalized_spec = dict(spec)
            normalized_spec["source_model_path"] = str(decision.normalized_path)
            bound.arguments["spec"] = normalized_spec
            decisions.append(decision)
    evidence = {
        **policy.capability(enforced=True),
        "validated_input_count": len(decisions),
        "validated_kinds": sorted(decision.kind for decision in decisions),
        "root_ids": sorted({decision.root_id for decision in decisions}),
    }
    read_pins = tuple(decision.read_pin for decision in decisions if decision.read_pin is not None)
    write_pins = tuple(
        decision.write_pin for decision in decisions if decision.write_pin is not None
    )
    return bound.args, bound.kwargs, evidence, read_pins, write_pins


__all__ = [
    "ARTIFACT_WRITE_ROOT_ENV",
    "MODEL_READ_ROOTS_ENV",
    "PATH_POLICY_SCHEMA",
    "PATH_POLICY_VERSION",
    "SHARED_SNAPSHOT_DIRECTORY",
    "PathDecision",
    "ReadPinError",
    "ValidatedReadPin",
    "ValidatedWritePin",
    "validated_read_pin",
    "read_contained_file_snapshot",
    "pin_validated_reads",
    "pin_validated_writes",
    "PathPolicy",
    "validate_tool_paths",
]
