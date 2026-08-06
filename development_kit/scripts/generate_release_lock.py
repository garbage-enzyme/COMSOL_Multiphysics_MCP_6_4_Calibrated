"""Generate a complete wheel-hash lock for one Windows Python release lane."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_RELEASE_PLATFORM = "win-amd64"


def _run(command: list[str], *, cwd: Path = ROOT, capture: bool = False) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=capture,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        diagnostic = exc.stderr if isinstance(exc.stderr, str) else exc.stdout
        detail = diagnostic.strip()[-4096:] if isinstance(diagnostic, str) else ""
        message = f"release-lock command failed with exit code {exc.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc
    return completed.stdout if capture else ""


def _venv_python(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime_pins(freeze_output: str) -> list[str]:
    excluded = {"comsol-mcp", "pip", "setuptools", "wheel"}
    pins = []
    for raw in freeze_output.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise RuntimeError(f"unsupported pip freeze entry: {line}") from exc
        name = canonicalize_name(requirement.name)
        if name in excluded:
            continue
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or requirement.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise RuntimeError(f"release lock cannot hash a non-exact pip freeze entry: {line}")
        pins.append(f"{name}=={specifiers[0].version}")
    return sorted(set(pins), key=str.casefold)


def _wheel_hashes(download_dir: Path) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    for path in sorted(download_dir.glob("*.whl")):
        name, version, _build, _tags = parse_wheel_filename(path.name)
        key = (canonicalize_name(name), str(version))
        result.setdefault(key, []).append(_sha256(path))
    return result


def _validated_target_platform(target_python: Path) -> str:
    identity = _run(
        [
            str(target_python),
            "-c",
            (
                "import platform, sys, sysconfig; "
                "print(platform.python_implementation()); "
                "print(f'{sys.version_info.major}.{sys.version_info.minor}'); "
                "print(sysconfig.get_platform())"
            ),
        ],
        capture=True,
    ).splitlines()
    if len(identity) != 3 or identity[0] != "CPython" or identity[1] != "3.14":
        raise SystemExit("release locks require a standard CPython 3.14 target interpreter")
    platform_name = identity[2].strip()
    if platform_name != SUPPORTED_RELEASE_PLATFORM:
        raise SystemExit(
            "release locks require a win-amd64 target interpreter; "
            f"received {platform_name or 'an empty platform'}"
        )
    return platform_name


def _downloaded_root_wheel(source: Path, download_dir: Path) -> Path:
    source_name, source_version, _build, _tags = parse_wheel_filename(source.name)
    matches = []
    for candidate in sorted(download_dir.glob("*.whl")):
        name, version, _candidate_build, _candidate_tags = parse_wheel_filename(candidate.name)
        if canonicalize_name(name) == canonicalize_name(source_name) and version == source_version:
            matches.append(candidate)
    if len(matches) != 1:
        raise RuntimeError("downloaded wheelhouse does not contain one exact root wheel")
    return matches[0]


def _resolve_and_install_wheelhouse(
    target_python: Path,
    wheel: Path,
    temporary: Path,
) -> tuple[Path, Path, str]:
    """Resolve once, then install the exact wheel bytes whose hashes are locked."""
    venv_dir = temporary / "venv"
    download_dir = temporary / "downloads"
    download_dir.mkdir()
    _run(
        [
            str(target_python),
            "-m",
            "pip",
            "download",
            "--only-binary=:all:",
            "--dest",
            str(download_dir),
            str(wheel),
        ],
        cwd=temporary,
    )
    downloaded_root = _downloaded_root_wheel(wheel, download_dir)
    _run([str(target_python), "-m", "venv", str(venv_dir)])
    python = _venv_python(venv_dir)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(download_dir),
            str(downloaded_root),
        ],
        cwd=temporary,
    )
    freeze = _run(
        [str(python), "-m", "pip", "freeze", "--all"],
        cwd=temporary,
        capture=True,
    )
    return python, download_dir, freeze


def _render_lock(
    *, platform_name: str, lane: str, python_version: str, pins: list[str], hashes: dict
) -> str:
    if platform_name != SUPPORTED_RELEASE_PLATFORM:
        raise ValueError("release-lock platform must be win-amd64")
    lines = [
        "# Complete hash-pinned runtime dependency lock for a Windows release lane.",
        "#",
        "# Generated from a fresh non-editable wheel install. This lock covers the",
        "# package's default runtime dependencies; the local wheel is hashed separately",
        "# by release_gate.py and installed with --no-deps after this lock is applied.",
        "# Regenerate only after reviewing dependency changes and the production lane.",
        "#",
        "# Schema: comsol_mcp.release_dependency_lock / 2.0.0",
        f"# Python-Lane: {lane}",
        f"# Generated-With-Python: {python_version}",
        f"# Platform: {platform_name.replace('-', '_')}",
        "",
    ]
    for pin in pins:
        name, version = pin.split("==", 1)
        package_hashes = hashes.get((canonicalize_name(name), version), [])
        if not package_hashes:
            raise RuntimeError(f"no downloaded wheel hash found for {pin}")
        lines.append(f"{pin} \\")
        for index, digest in enumerate(sorted(package_hashes)):
            suffix = " \\" if index < len(package_hashes) - 1 else ""
            lines.append(f"    --hash=sha256:{digest}{suffix}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    target_python = args.python.expanduser().resolve()
    wheel = args.wheel.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if os.name != "nt":
        raise SystemExit("release locks must be generated on Windows")
    if not target_python.is_file() or not wheel.is_file():
        raise SystemExit("--python and --wheel must name existing files")
    platform_name = _validated_target_platform(target_python)

    with tempfile.TemporaryDirectory(prefix="comsol-lock-") as temporary_text:
        temporary = Path(temporary_text)
        python, download_dir, freeze = _resolve_and_install_wheelhouse(
            target_python,
            wheel,
            temporary,
        )
        pins = _runtime_pins(freeze)
        version_text = _run(
            [str(python), "-c", "import platform; print(platform.python_version())"],
            cwd=temporary,
            capture=True,
        ).strip()
        lane = ".".join(version_text.split(".")[:2])
        rendered = _render_lock(
            platform_name=platform_name,
            lane=lane,
            python_version=version_text,
            pins=pins,
            hashes=_wheel_hashes(download_dir),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        with temporary_output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_output, output)
    finally:
        temporary_output.unlink(missing_ok=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
