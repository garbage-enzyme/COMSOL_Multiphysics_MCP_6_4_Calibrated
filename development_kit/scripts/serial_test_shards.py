"""Run deterministic pytest file shards as independent serial processes.

This intentionally does not use pytest-xdist. Hosted Windows runners can run
several isolated pytest processes concurrently while every pytest process keeps
the repository's serial execution semantics.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_files(*, ignore: tuple[str, ...] = ()) -> list[str]:
    ignored = {Path(item).as_posix() for item in ignore}
    paths = []
    for path in sorted((ROOT / "development_kit" / "tests").rglob("test_*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative not in ignored:
            paths.append(relative)
    return paths


def shard_files(files: list[str], shard_count: int) -> list[list[str]]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    shards = [[] for _ in range(min(shard_count, max(1, len(files))))]
    for index, path in enumerate(files):
        shards[index % len(shards)].append(path)
    return shards


def run_shards(
    *,
    basetemp_root: Path,
    shard_count: int,
    ignore: tuple[str, ...] = (),
    coverage_root: Path | None = None,
) -> None:
    shards = shard_files(test_files(ignore=ignore), shard_count)
    basetemp_root.mkdir(parents=True, exist_ok=True)
    if coverage_root is not None:
        coverage_root.mkdir(parents=True, exist_ok=True)
    processes: list[tuple[int, subprocess.Popen[bytes]]] = []
    try:
        for index, files in enumerate(shards):
            environment = dict(os.environ)
            if coverage_root is not None:
                environment["COVERAGE_FILE"] = str(coverage_root / f".coverage-shard-{index}")
            command = [
                sys.executable,
                "-u",
                "-m",
                "pytest",
                "-q",
                *files,
                "--basetemp",
                str(basetemp_root / f"shard{index}"),
                "--cov=comsol_mcp",
                "--cov-branch",
                "--cov-report=",
            ]
            processes.append((index, subprocess.Popen(command, cwd=ROOT, env=environment)))  # noqa: S603
        failures: list[tuple[int, int]] = []
        for index, process in processes:
            returncode = process.wait()
            if returncode:
                failures.append((index, returncode))
        if failures:
            raise SystemExit("pytest shard failures: " + ", ".join(f"{i}={c}" for i, c in failures))
    except BaseException:
        for _index, process in processes:
            if process.poll() is None:
                process.terminate()
        for _index, process in processes:
            process.wait()
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--basetemp-root", type=Path, required=True)
    parser.add_argument("--shards", type=int, default=2)
    parser.add_argument("--coverage-root", type=Path)
    parser.add_argument("--ignore", action="append", default=[])
    args = parser.parse_args()
    run_shards(
        basetemp_root=args.basetemp_root,
        shard_count=args.shards,
        ignore=tuple(args.ignore),
        coverage_root=args.coverage_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
