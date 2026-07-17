"""Run the dependency-only release and clean-wheel discovery gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def _run(command: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def _git_status() -> list[str]:
    completed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
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


def _distribution_inventory(path: Path) -> dict:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            members = archive.namelist()
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getnames()
    else:
        raise ValueError(f"unsupported distribution artifact: {path.name}")
    offenders = [name for name in members if "development_kit" in Path(name).parts]
    if offenders:
        raise RuntimeError(
            f"ordinary distribution contains development_kit members: {offenders[:5]}"
        )
    return {
        "filename": path.name,
        "sha256": _sha256(path),
        "member_count": len(members),
        "development_kit_excluded": True,
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
        subprocess.run(
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
            check=True,
        )

    report = {
        "schema_version": "1.0.0",
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
        "installed_probe": (
            json.loads(probe_result.read_text(encoding="utf-8"))
            if probe_result.is_file()
            else None
        ),
    }
    (run_root / "release_gate_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
