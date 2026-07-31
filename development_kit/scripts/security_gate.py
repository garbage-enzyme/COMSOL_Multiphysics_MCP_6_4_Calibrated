"""Evaluate a pip-audit report against an exact expiring vulnerability allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _load_vulnerability_allowlist_value(value: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or value.get("schema_name") != "comsol_mcp.vulnerability_allowlist"
        or value.get("schema_version") != "1.0.0"
        or not isinstance(value.get("entries"), list)
    ):
        raise ValueError("vulnerability allowlist schema is invalid")
    entries = []
    identities = set()
    for item in value["entries"]:
        if not isinstance(item, dict) or set(item) != {
            "dependency",
            "vulnerability_id",
            "expires_on",
            "reason",
        }:
            raise ValueError("vulnerability allowlist entry fields are invalid")
        dependency = item["dependency"]
        vulnerability_id = item["vulnerability_id"]
        reason = item["reason"]
        try:
            expires_on = date.fromisoformat(item["expires_on"])
        except (TypeError, ValueError) as exc:
            raise ValueError("vulnerability allowlist expiry is invalid") from exc
        if (
            not isinstance(dependency, str)
            or not _NAME.fullmatch(dependency)
            or not isinstance(vulnerability_id, str)
            or not _ID.fullmatch(vulnerability_id)
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 1024
        ):
            raise ValueError("vulnerability allowlist entry is invalid")
        identity = (dependency.casefold(), vulnerability_id.casefold())
        if identity in identities:
            raise ValueError("vulnerability allowlist identities must be unique")
        identities.add(identity)
        entries.append(
            {
                "dependency": dependency.casefold(),
                "vulnerability_id": vulnerability_id,
                "expires_on": expires_on,
                "reason": reason.strip(),
            }
        )
    return entries


def load_vulnerability_allowlist(path: str | Path) -> list[dict[str, Any]]:
    value_bytes = Path(path).read_bytes()
    try:
        value = json.loads(value_bytes.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("vulnerability allowlist must be valid UTF-8") from exc
    return _load_vulnerability_allowlist_value(value)


def _findings(report: Any) -> list[dict[str, str]]:
    dependencies = report.get("dependencies") if isinstance(report, dict) else report
    if not isinstance(dependencies, list):
        raise ValueError("pip-audit report must contain a dependency list")
    findings = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("pip-audit dependency entry is invalid")
        name = dependency.get("name")
        version = dependency.get("version")
        vulnerabilities = dependency.get("vulns", [])
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or not isinstance(version, str)
            or not version
            or not isinstance(vulnerabilities, list)
        ):
            raise ValueError("pip-audit dependency fields are invalid")
        for vulnerability in vulnerabilities:
            vulnerability_id = vulnerability.get("id") if isinstance(vulnerability, dict) else None
            if not isinstance(vulnerability_id, str) or not _ID.fullmatch(vulnerability_id):
                raise ValueError("pip-audit vulnerability ID is invalid")
            findings.append(
                {
                    "dependency": name.casefold(),
                    "version": version,
                    "vulnerability_id": vulnerability_id,
                }
            )
    return sorted(
        findings,
        key=lambda item: (
            item["dependency"],
            item["vulnerability_id"].casefold(),
            item["version"],
        ),
    )


def evaluate_security_report(
    report: Any,
    allowlist: list[dict[str, Any]],
    *,
    as_of: date,
) -> dict[str, Any]:
    findings = _findings(report)
    allowed_by_id = {
        (item["dependency"], item["vulnerability_id"].casefold()): item for item in allowlist
    }
    blocked = []
    allowed = []
    for finding in findings:
        entry = allowed_by_id.get((finding["dependency"], finding["vulnerability_id"].casefold()))
        if entry is None:
            blocked.append({**finding, "reason_code": "not_allowlisted"})
        elif entry["expires_on"] < as_of:
            blocked.append({**finding, "reason_code": "allowlist_expired"})
        else:
            allowed.append(
                {
                    **finding,
                    "expires_on": entry["expires_on"].isoformat(),
                    "reason": entry["reason"],
                }
            )
    allowed_identities = {
        (item["dependency"].casefold(), item["vulnerability_id"].casefold()) for item in allowed
    }
    unused = sorted(
        f"{item['dependency']}:{item['vulnerability_id']}"
        for item in allowlist
        if (item["dependency"].casefold(), item["vulnerability_id"].casefold())
        not in allowed_identities
    )
    return {
        "schema_name": "comsol_mcp.security_gate_receipt",
        "schema_version": "1.0.0",
        "as_of": as_of.isoformat(),
        "status": "passed" if not blocked else "failed",
        "finding_count": len(findings),
        "allowlisted_count": len(allowed),
        "blocked_count": len(blocked),
        "blocked": blocked,
        "allowlisted": allowed,
        "unused_allowlist_entries": unused,
        "policy": "all_findings_require_exact_unexpired_review",
    }


def build_security_receipt(
    report_path: str | Path,
    allowlist_path: str | Path,
    *,
    as_of: date,
) -> dict[str, Any]:
    report_file = Path(report_path)
    allowlist_file = Path(allowlist_path)
    report_bytes = report_file.read_bytes()
    allowlist_bytes = allowlist_file.read_bytes()
    report = json.loads(report_bytes.decode("utf-8"))
    allowlist_value = json.loads(allowlist_bytes.decode("utf-8"))
    # Reuse the public validator without re-reading the policy path.
    allowlist = _load_vulnerability_allowlist_value(allowlist_value)
    receipt = evaluate_security_report(report, allowlist, as_of=as_of)
    receipt["report_sha256"] = _sha256_bytes(report_bytes)
    receipt["allowlist_sha256"] = _sha256_bytes(allowlist_bytes)
    return receipt


def write_security_receipt(
    path: str | Path,
    receipt: dict[str, Any],
    *,
    replace_fn=os.replace,
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        replace_fn(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allowlist", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    receipt = build_security_receipt(
        args.report,
        args.allowlist,
        as_of=args.as_of,
    )
    write_security_receipt(args.output, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
