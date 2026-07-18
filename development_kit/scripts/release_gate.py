"""Run the dependency-only release and clean-wheel discovery gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
import platform
import subprocess
import sys
import tarfile
import tempfile
import zipfile

if __package__:
    from .planning_code_gate import (
        TEXT_SUFFIXES,
        load_planning_code_allowlist,
        verify_planning_code_texts,
    )
else:
    from planning_code_gate import (  # type: ignore[no-redef]
        TEXT_SUFFIXES,
        load_planning_code_allowlist,
        verify_planning_code_texts,
    )


ROOT = Path(__file__).resolve().parents[2]
PLANNING_CODE_ALLOWLIST = (
    ROOT / "development_kit" / "release" / "planning_code_allowlist.json"
)
_FORBIDDEN_PARTS = {
    "comsol_models",
    "development_kit",
    "knowledge_base",
    "knowledge_base_v2",
    "pdf",
}
_FORBIDDEN_SUFFIXES = {".class", ".lock", ".mph", ".pyc", ".recovery", ".status"}
_FORBIDDEN_NAMES = {".env", "credentials", "credentials.json", "id_rsa", "secrets.json"}


def _run(
    command: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=True,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )


def _git_status() -> list[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        creationflags=(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        ),
    )
    return [line for line in completed.stdout.splitlines() if line]


def _default_artifact_root() -> Path:
    configured = os.environ.get("COMSOL_MCP_RELEASE_ROOT")
    if configured:
        return Path(configured)
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/comsol_mcp_release")
    return Path(tempfile.gettempdir()) / "comsol_mcp_release"


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_console_entry(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "comsol-mcp.exe"
    return venv_dir / "bin" / "comsol-mcp"


def _lock_lane(path: Path) -> str:
    prefix = "# Python-Lane:"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read dependency lock {path}: {exc}") from exc
    values = [line[len(prefix) :].strip() for line in lines if line.startswith(prefix)]
    if len(values) != 1:
        raise ValueError(f"dependency lock must declare exactly one {prefix} header")
    return values[0]


def _validated_dependency_lock(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    expected_lane = f"{sys.version_info.major}.{sys.version_info.minor}"
    actual_lane = _lock_lane(resolved)
    if actual_lane != expected_lane:
        raise ValueError(
            f"dependency lock targets Python {actual_lane}, current interpreter is {expected_lane}"
        )
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized_archive_path(name: str) -> str:
    raw = name.replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or PureWindowsPath(raw).is_absolute() or ".." in pure.parts:
        raise RuntimeError(f"distribution contains unsafe path: {name}")
    parts = list(pure.parts)
    if parts and parts[0].startswith("comsol_mcp-"):
        parts = parts[1:]
    return "/".join(parts)


def _distribution_files(path: Path) -> tuple[list[str], dict[str, bytes]]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            members = archive.namelist()
            files = {
                _normalized_archive_path(info.filename): archive.read(info)
                for info in archive.infolist()
                if not info.is_dir()
            }
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getnames()
            files = {}
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError(f"cannot read distribution member: {member.name}")
                files[_normalized_archive_path(member.name)] = stream.read()
    else:
        raise ValueError(f"unsupported distribution artifact: {path.name}")
    return members, files


def _distribution_inventory(path: Path) -> dict:
    members, files = _distribution_files(path)
    normalized = [_normalized_archive_path(name) for name in members]
    offenders = []
    for name in normalized:
        pure = PurePosixPath(name)
        lowered_parts = {part.casefold() for part in pure.parts}
        if (
            lowered_parts & _FORBIDDEN_PARTS
            or pure.suffix.casefold() in _FORBIDDEN_SUFFIXES
            or pure.name.casefold() in _FORBIDDEN_NAMES
        ):
            offenders.append(name)
    if offenders:
        raise RuntimeError(
            f"ordinary distribution contains forbidden members: {offenders[:5]}"
        )
    texts = {}
    for name, payload in files.items():
        if PurePosixPath(name).suffix.casefold() not in TEXT_SUFFIXES:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"distribution text member is not UTF-8: {name}") from exc
        if "C:\\Users\\" in text or "C:/Users/" in text or "陆星" in text:
            raise RuntimeError(f"distribution text contains a private user path: {name}")
        texts[name] = text
    planning_receipt = verify_planning_code_texts(
        texts,
        allowlist=load_planning_code_allowlist(PLANNING_CODE_ALLOWLIST),
        require_all_allowlisted=False,
    )
    return {
        "filename": path.name,
        "sha256": _sha256(path),
        "member_count": len(members),
        "development_kit_excluded": True,
        "forbidden_entries_absent": True,
        "private_user_paths_absent": True,
        "planning_code_gate": planning_receipt,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--artifact-root", type=Path, default=_default_artifact_root())
    parser.add_argument(
        "--dependency-lock",
        type=Path,
        default=None,
        help="complete hash-pinned runtime dependency lock for this Python lane",
    )
    args = parser.parse_args()

    dirty = _git_status()
    if dirty and not args.allow_dirty:
        raise SystemExit("release gate requires a clean git tree")

    artifact_root = args.artifact_root.resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="gate-", dir=artifact_root))
    dist_dir = run_root / "dist"
    probe_result = run_root / "installed_probe.json"
    sbom_result = run_root / "sbom.cdx.json"
    stdio_probe_result = run_root / "installed_stdio_probe.json"
    dependency_lock = (
        _validated_dependency_lock(args.dependency_lock)
        if args.dependency_lock is not None
        else None
    )

    _run(
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "comsol_mcp",
            "src",
            "development_kit",
        ]
    )
    if not args.skip_tests:
        _run([sys.executable, "-m", "pytest", "-q"])
    _run([sys.executable, "-m", "build", "--outdir", str(dist_dir)])
    distributions = [
        _distribution_inventory(path)
        for path in sorted(dist_dir.iterdir())
        if path.suffix == ".whl" or path.name.endswith(".tar.gz")
    ]
    if len(distributions) != 2:
        raise RuntimeError(f"expected wheel and sdist, found {distributions}")

    if not args.skip_install:
        venv_dir = run_root / "venv"
        _run([sys.executable, "-m", "venv", str(venv_dir)])
        python = _venv_python(venv_dir)
        wheels = sorted(dist_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"expected one wheel, found {wheels}")
        if dependency_lock is not None:
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "-r",
                    str(dependency_lock),
                ],
                cwd=run_root,
            )
            _run(
                [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
                cwd=run_root,
            )
        else:
            _run([str(python), "-m", "pip", "install", str(wheels[0])], cwd=run_root)
        _run([str(python), "-m", "pip", "check"], cwd=run_root)
        probe_workdir = run_root / "probe_workdir"
        probe_workdir.mkdir()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        _run(
            [
                str(python),
                str(ROOT / "development_kit" / "scripts" / "installed_package_probe.py"),
                "--snapshot-dir",
                str(ROOT / "development_kit" / "tests" / "snapshots"),
                "--output",
                str(probe_result),
            ],
            cwd=probe_workdir,
            env=environment,
        )
        if dependency_lock is not None:
            _run(
                [
                    str(python),
                    str(ROOT / "development_kit" / "scripts" / "sbom_probe.py"),
                    "--lock",
                    str(dependency_lock),
                    "--output",
                    str(sbom_result),
                ],
                cwd=probe_workdir,
            )
        _run(
            [
                str(python),
                str(ROOT / "development_kit" / "scripts" / "installed_stdio_probe.py"),
                "--command",
                str(_venv_console_entry(venv_dir)),
                "--workdir",
                str(run_root / "stdio_probe_workdir"),
                "--output",
                str(stdio_probe_result),
            ],
            cwd=probe_workdir,
        )

    installed_probe = (
        json.loads(probe_result.read_text(encoding="utf-8"))
        if probe_result.is_file()
        else None
    )
    sbom = json.loads(sbom_result.read_text(encoding="utf-8")) if sbom_result.is_file() else None
    stdio_probe = (
        json.loads(stdio_probe_result.read_text(encoding="utf-8"))
        if stdio_probe_result.is_file()
        else None
    )
    report = {
        "schema_name": "comsol_mcp.release_gate_receipt",
        "schema_version": "2.0.0",
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "clean_tree_required": not args.allow_dirty,
        "dirty_entry_count": len(dirty),
        "compile_passed": True,
        "tests_run": not args.skip_tests,
        "package_build_passed": True,
        "distribution_artifacts": distributions,
        "non_editable_install_run": not args.skip_install,
        "dependency_lock": (
            {
                "path": str(dependency_lock.relative_to(ROOT)),
                "sha256": _sha256(dependency_lock),
                "python_lane": _lock_lane(dependency_lock),
                "require_hashes": True,
            }
            if dependency_lock is not None
            else None
        ),
        "installed_probe": installed_probe,
        "installed_stdio_probe": stdio_probe,
        "inventory_hashes": (
            installed_probe["release_inventories"] if installed_probe else None
        ),
        "sbom": (
            {
                "filename": sbom_result.name,
                "sha256": _sha256(sbom_result),
                "format": sbom["bomFormat"],
                "spec_version": sbom["specVersion"],
                "component_count": len(sbom["components"]),
                "root_package_version": sbom["metadata"]["component"]["version"],
            }
            if sbom is not None
            else None
        ),
    }
    report["receipt_sha256"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (run_root / "release_gate_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
