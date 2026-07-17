"""Generate a complete wheel-hash lock for one Windows Python release lane."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

from packaging.utils import canonicalize_name, parse_wheel_filename


ROOT = Path(__file__).resolve().parents[2]


def _run(command: list[str], *, cwd: Path = ROOT, capture: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=capture,
        text=True,
    )
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
        if not line or "==" not in line:
            continue
        name, version = line.split("==", 1)
        if canonicalize_name(name) in excluded:
            continue
        pins.append(f"{canonicalize_name(name)}=={version}")
    return sorted(set(pins), key=str.casefold)


def _wheel_hashes(download_dir: Path) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    for path in sorted(download_dir.glob("*.whl")):
        name, version, _build, _tags = parse_wheel_filename(path.name)
        key = (canonicalize_name(name), str(version))
        result.setdefault(key, []).append(_sha256(path))
    return result


def _render_lock(*, lane: str, python_version: str, pins: list[str], hashes: dict) -> str:
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
        "# Platform: win_amd64",
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

    with tempfile.TemporaryDirectory(prefix="comsol-lock-") as temporary_text:
        temporary = Path(temporary_text)
        venv_dir = temporary / "venv"
        download_dir = temporary / "downloads"
        pins_file = temporary / "pins.txt"
        download_dir.mkdir()
        _run([str(target_python), "-m", "venv", str(venv_dir)])
        python = _venv_python(venv_dir)
        _run([str(python), "-m", "pip", "install", str(wheel)], cwd=temporary)
        freeze = _run(
            [str(python), "-m", "pip", "freeze", "--all"],
            cwd=temporary,
            capture=True,
        )
        pins = _runtime_pins(freeze)
        pins_file.write_text("\n".join(pins) + "\n", encoding="utf-8")
        _run(
            [
                str(python),
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(download_dir),
                "-r",
                str(pins_file),
            ],
            cwd=temporary,
        )
        version_text = _run(
            [str(python), "-c", "import platform; print(platform.python_version())"],
            cwd=temporary,
            capture=True,
        ).strip()
        lane = ".".join(version_text.split(".")[:2])
        rendered = _render_lock(
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
