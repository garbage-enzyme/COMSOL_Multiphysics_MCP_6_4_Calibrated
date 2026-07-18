"""Fail closed unless every declared runtime dependency has a live license review."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from collections.abc import Callable
from datetime import date
from importlib.metadata import Distribution, PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SIGNAL = re.compile(r"^[a-z-]+:.{1,512}$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalize_name(value: object) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("dependency name is invalid")
    return re.sub(r"[-_.]+", "-", value).casefold()


def declared_runtime_dependencies(path: str | Path) -> tuple[str, ...]:
    """Return normalized direct runtime dependency names from one pyproject."""
    value = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    project = value.get("project") if isinstance(value, dict) else None
    requirements = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("pyproject runtime dependencies are missing")
    names = []
    for requirement in requirements:
        match = _NAME.match(requirement) if isinstance(requirement, str) else None
        if match is None:
            raise ValueError("pyproject runtime dependency is invalid")
        names.append(_normalize_name(match.group(0)))
    if len(names) != len(set(names)):
        raise ValueError("pyproject runtime dependencies contain duplicate names")
    return tuple(sorted(names))


def load_license_review(path: str | Path) -> dict[str, Any]:
    """Load one exact, bounded dependency-license review policy."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schema_name",
        "schema_version",
        "reviewed_on",
        "expires_on",
        "entries",
    }:
        raise ValueError("dependency license review fields are invalid")
    if (
        value["schema_name"] != "comsol_mcp.dependency_license_review"
        or value["schema_version"] != "1.0.0"
    ):
        raise ValueError("dependency license review schema is unsupported")
    try:
        reviewed_on = date.fromisoformat(value["reviewed_on"])
        expires_on = date.fromisoformat(value["expires_on"])
    except (TypeError, ValueError) as exc:
        raise ValueError("dependency license review dates are invalid") from exc
    if expires_on < reviewed_on:
        raise ValueError("dependency license review expires before its review date")
    if not isinstance(value["entries"], list) or not value["entries"]:
        raise ValueError("dependency license review entries are missing")
    entries: dict[str, dict[str, Any]] = {}
    for item in value["entries"]:
        if not isinstance(item, dict) or set(item) != {
            "dependency",
            "accepted_signals",
            "reason",
        }:
            raise ValueError("dependency license review entry fields are invalid")
        name = _normalize_name(item["dependency"])
        signals = item["accepted_signals"]
        reason = item["reason"]
        if (
            not isinstance(signals, list)
            or not signals
            or len(signals) != len(set(signals))
            or not all(isinstance(signal, str) and _SIGNAL.fullmatch(signal) for signal in signals)
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1024
        ):
            raise ValueError("dependency license review entry is invalid")
        if name in entries:
            raise ValueError("dependency license review names must be unique")
        entries[name] = {
            "accepted_signals": tuple(sorted(signals)),
            "reason": reason.strip(),
        }
    return {
        "reviewed_on": reviewed_on,
        "expires_on": expires_on,
        "entries": entries,
    }


def distribution_license_record(item: Distribution) -> dict[str, Any]:
    """Extract bounded license signals without copying full license text."""
    metadata = item.metadata
    name = _normalize_name(metadata.get("Name"))
    version = metadata.get("Version")
    if not isinstance(version, str) or not version or len(version) > 128:
        raise ValueError(f"installed dependency version is invalid for {name}")
    signals = set()
    expression = metadata.get("License-Expression")
    if isinstance(expression, str) and expression.strip():
        signals.add(f"license-expression:{expression.strip()}")
    license_value = metadata.get("License")
    if (
        isinstance(license_value, str)
        and license_value.strip()
        and "\n" not in license_value
        and len(license_value.strip()) <= 256
    ):
        signals.add(f"license:{license_value.strip()}")
    for classifier in metadata.get_all("Classifier", []):
        if isinstance(classifier, str) and classifier.startswith("License ::"):
            signals.add(f"classifier:{classifier}")
    return {
        "dependency": name,
        "version": version,
        "signals": tuple(sorted(signals)),
    }


def build_license_receipt(
    pyproject_path: str | Path,
    review_path: str | Path,
    *,
    as_of: date,
    distribution_provider: Callable[[str], Distribution] = distribution,
) -> dict[str, Any]:
    """Evaluate installed metadata against exact declared dependency reviews."""
    pyproject = Path(pyproject_path)
    review_file = Path(review_path)
    declared = declared_runtime_dependencies(pyproject)
    review = load_license_review(review_file)
    reviewed = tuple(sorted(review["entries"]))
    failures = []
    if review["expires_on"] < as_of:
        failures.append({"reason_code": "review_expired"})
    for name in sorted(set(declared) - set(reviewed)):
        failures.append({"dependency": name, "reason_code": "unreviewed_dependency"})
    for name in sorted(set(reviewed) - set(declared)):
        failures.append({"dependency": name, "reason_code": "stale_review_entry"})

    records = []
    for name in declared:
        try:
            installed = distribution_license_record(distribution_provider(name))
        except PackageNotFoundError:
            failures.append({"dependency": name, "reason_code": "not_installed"})
            continue
        if installed["dependency"] != name:
            failures.append({"dependency": name, "reason_code": "metadata_name_mismatch"})
            continue
        entry = review["entries"].get(name)
        matched = (
            sorted(set(installed["signals"]) & set(entry["accepted_signals"]))
            if entry is not None
            else []
        )
        if not installed["signals"]:
            failures.append({"dependency": name, "reason_code": "license_metadata_missing"})
        elif entry is not None and not matched:
            failures.append({"dependency": name, "reason_code": "license_metadata_unmatched"})
        records.append(
            {
                "dependency": name,
                "version": installed["version"],
                "observed_signals": list(installed["signals"]),
                "matched_signals": matched,
            }
        )

    return {
        "schema_name": "comsol_mcp.dependency_license_receipt",
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "status": "passed" if not failures else "failed",
        "dependency_count": len(declared),
        "dependencies": records,
        "failures": failures,
        "reviewed_on": review["reviewed_on"].isoformat(),
        "expires_on": review["expires_on"].isoformat(),
        "pyproject_sha256": _sha256(pyproject),
        "review_sha256": _sha256(review_file),
        "policy": "every direct runtime dependency requires an exact unexpired license review",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    receipt = build_license_receipt(
        args.pyproject,
        args.review,
        as_of=args.as_of,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
