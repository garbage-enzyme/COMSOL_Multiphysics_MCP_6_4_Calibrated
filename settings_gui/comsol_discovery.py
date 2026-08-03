"""Bounded registry, COMSOL-root, and Java-home discovery without process launch."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class DiscoveryResult:
    comsol_root: Path | None
    java_home: Path | None
    java_source: str | None
    ambiguous_roots: tuple[Path, ...] = ()


_ROOT_MARKERS = (
    Path("bin/win64/comsol.exe"),
    Path("bin/win64/comsolmphserver.exe"),
    Path("bin/win64/comsol.ini"),
    Path("plugins"),
    Path("apiplugins"),
)


def _regular_directory(path: Path) -> bool:
    return (
        path.is_absolute()
        and path.is_dir()
        and not path.is_symlink()
        and not getattr(path, "is_junction", lambda: False)()
    )


def validate_comsol_root(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if any(ord(character) < 32 for character in str(path)) or not _regular_directory(path):
        raise ValueError("COMSOL installation root is not a regular absolute directory")
    resolved = path.resolve(strict=True)
    for marker in _ROOT_MARKERS:
        candidate = resolved / marker
        if marker.suffix and not candidate.is_file():
            raise ValueError("COMSOL installation root is missing required 6.4 files")
        if not marker.suffix and not candidate.is_dir():
            raise ValueError("COMSOL installation root is missing required 6.4 directories")
    ini = (resolved / "bin/win64/comsol.ini").read_text(
        encoding="utf-8",
        errors="replace",
    )
    if not re.search(r"(?:COMSOL|Multiphysics|6\.4)", ini, re.IGNORECASE):
        raise ValueError("COMSOL installation metadata does not identify the supported family")
    return resolved


def registry_comsol_roots(registry: Any | None = None) -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    if registry is None:
        import winreg as registry

    candidates: list[Path] = []
    keys = (
        r"SOFTWARE\COMSOL\COMSOL64",
        r"SOFTWARE\WOW6432Node\COMSOL\COMSOL64",
    )
    for key_name in keys:
        try:
            with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, key_name) as key:
                raw, _kind = registry.QueryValueEx(key, "COMSOLROOT")
        except OSError:
            continue
        if isinstance(raw, str) and raw.strip():
            try:
                candidates.append(validate_comsol_root(raw.strip()))
            except ValueError:
                continue
    unique: dict[str, Path] = {}
    for candidate in candidates:
        unique.setdefault(os.path.normcase(str(candidate)), candidate)
    return tuple(unique.values())


def _java_home_from_runtime(runtime: Path) -> Path | None:
    if not runtime.is_file():
        return None
    name = runtime.name.casefold()
    if name == "java.exe":
        home = runtime.parent.parent
    elif name == "jvm.dll" and runtime.parent.name.casefold() in {"server", "client"}:
        home = runtime.parent.parent.parent
    else:
        return None
    return home.resolve(strict=True) if _regular_directory(home) else None


def _ini_java_home(root: Path) -> Path | None:
    ini_path = root / "bin/win64/comsol.ini"
    lines = [
        line.strip().strip('"')
        for line in ini_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    ]
    for index, line in enumerate(lines):
        candidate: str | None = None
        if line == "-vm" and index + 1 < len(lines):
            candidate = lines[index + 1]
        elif line.startswith("-vm="):
            candidate = line.partition("=")[2]
        if not candidate:
            continue
        executable = Path(candidate)
        if not executable.is_absolute():
            executable = (ini_path.parent / executable).resolve(strict=False)
        home = _java_home_from_runtime(executable)
        if home is not None:
            return home
    return None


def discover_java_home(
    comsol_root: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Any = shutil.which,
) -> tuple[Path | None, str | None]:
    if comsol_root is not None:
        ini_home = _ini_java_home(comsol_root)
        if ini_home is not None:
            return ini_home, "comsol_bundled"
        fallback = _java_home_from_runtime(comsol_root / "java/win64/jre/bin/java.exe")
        if fallback is not None:
            return fallback, "comsol_bundled"

    environment = os.environ if environ is None else environ
    for name, source in (("JAVA_HOME", "system_java_home"), ("JDK_HOME", "system_jdk_home")):
        raw = environment.get(name)
        if not raw:
            continue
        home = Path(raw).expanduser()
        executable = home / "bin/java.exe"
        if _regular_directory(home) and executable.is_file():
            return home.resolve(strict=True), source
    found = which("java.exe")
    if found:
        home = _java_home_from_runtime(Path(found))
        if home is not None:
            return home, "system_path"
    return None, None


def discover_environment(
    *,
    registry: Any | None = None,
    environ: Mapping[str, str] | None = None,
    selected_root: str | Path | None = None,
) -> DiscoveryResult:
    if selected_root is not None:
        root = validate_comsol_root(selected_root)
        java_home, source = discover_java_home(root, environ=environ)
        return DiscoveryResult(root, java_home, source)
    roots = registry_comsol_roots(registry)
    if len(roots) != 1:
        return DiscoveryResult(None, None, None, roots if len(roots) > 1 else ())
    java_home, source = discover_java_home(roots[0], environ=environ)
    return DiscoveryResult(roots[0], java_home, source)


__all__ = [
    "DiscoveryResult",
    "discover_environment",
    "discover_java_home",
    "registry_comsol_roots",
    "validate_comsol_root",
]
