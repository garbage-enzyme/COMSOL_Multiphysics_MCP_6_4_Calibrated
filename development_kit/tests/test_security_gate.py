"""Vulnerability report and expiring allowlist policy tests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from development_kit.scripts.security_gate import (
    build_security_receipt,
    evaluate_security_report,
    load_vulnerability_allowlist,
    write_security_receipt,
)


def _report(vulnerabilities=()):
    return {
        "dependencies": [
            {
                "name": "example",
                "version": "1.0.0",
                "vulns": [{"id": item} for item in vulnerabilities],
            }
        ]
    }


def test_empty_report_passes_with_empty_allowlist():
    receipt = evaluate_security_report(_report(), [], as_of=date(2026, 7, 17))
    assert receipt["status"] == "passed"
    assert receipt["finding_count"] == 0
    assert receipt["blocked"] == []


def test_unreviewed_or_expired_findings_fail_closed():
    blocked = evaluate_security_report(_report(["CVE-2099-0001"]), [], as_of=date(2026, 7, 17))
    assert blocked["status"] == "failed"
    assert blocked["blocked"][0]["reason_code"] == "not_allowlisted"

    expired = evaluate_security_report(
        _report(["CVE-2099-0001"]),
        [
            {
                "dependency": "example",
                "vulnerability_id": "CVE-2099-0001",
                "expires_on": date(2026, 7, 16),
                "reason": "Temporary review",
            }
        ],
        as_of=date(2026, 7, 17),
    )
    assert expired["status"] == "failed"
    assert expired["blocked"][0]["reason_code"] == "allowlist_expired"


def test_exact_unexpired_review_is_reported_and_unused_entries_remain_visible():
    receipt = evaluate_security_report(
        _report(["GHSA-abcd-1234-zzzz"]),
        [
            {
                "dependency": "example",
                "vulnerability_id": "GHSA-abcd-1234-zzzz",
                "expires_on": date(2026, 8, 1),
                "reason": "Mitigation is independently verified.",
            },
            {
                "dependency": "unused",
                "vulnerability_id": "CVE-2099-0002",
                "expires_on": date(2026, 8, 1),
                "reason": "Pending removal.",
            },
        ],
        as_of=date(2026, 7, 17),
    )
    assert receipt["status"] == "passed"
    assert receipt["allowlisted_count"] == 1
    assert receipt["unused_allowlist_entries"] == ["unused:CVE-2099-0002"]


def test_allowlist_loader_rejects_unknown_fields_or_duplicate_identities(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.vulnerability_allowlist",
                "schema_version": "1.0.0",
                "entries": [
                    {
                        "dependency": "example",
                        "vulnerability_id": "CVE-2099-0001",
                        "expires_on": "2026-08-01",
                        "reason": "Reviewed.",
                        "unexpected": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="fields"):
        load_vulnerability_allowlist(path)


def test_unused_allowlist_matching_uses_the_same_casefolded_identity():
    receipt = evaluate_security_report(
        _report(["cve-2099-0001"]),
        [
            {
                "dependency": "example",
                "vulnerability_id": "CVE-2099-0001",
                "expires_on": date(2027, 1, 1),
                "reason": "Reviewed.",
            }
        ],
        as_of=date(2026, 7, 18),
    )

    assert receipt["status"] == "passed"
    assert receipt["unused_allowlist_entries"] == []


def test_security_receipt_hashes_each_input_snapshot_once(tmp_path, monkeypatch):
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_report()), encoding="utf-8")
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(
        json.dumps(
            {
                "schema_name": "comsol_mcp.vulnerability_allowlist",
                "schema_version": "1.0.0",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    expected = {
        report.resolve(): report.read_bytes(),
        allowlist.resolve(): allowlist.read_bytes(),
    }
    calls = {path: 0 for path in expected}
    original = Path.read_bytes

    def tracked(path):
        resolved = path.resolve()
        if resolved in calls:
            calls[resolved] += 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", tracked)
    receipt = build_security_receipt(report, allowlist, as_of=date(2026, 7, 18))

    import hashlib

    assert calls == {report.resolve(): 1, allowlist.resolve(): 1}
    assert receipt["report_sha256"] == hashlib.sha256(expected[report.resolve()]).hexdigest()
    assert receipt["allowlist_sha256"] == hashlib.sha256(expected[allowlist.resolve()]).hexdigest()


def test_security_receipt_atomic_failure_preserves_prior_evidence(tmp_path):
    output = tmp_path / "security.json"
    output.write_bytes(b"prior-valid-evidence")

    def fail_replace(_source, _target):
        raise OSError("injected replacement failure")

    with pytest.raises(OSError, match="replacement"):
        write_security_receipt(
            output,
            {"status": "failed"},
            replace_fn=fail_replace,
        )

    assert output.read_bytes() == b"prior-valid-evidence"
    assert list(tmp_path.iterdir()) == [output]
