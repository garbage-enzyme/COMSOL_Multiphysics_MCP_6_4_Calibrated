"""Quality gate policy and branch-coverage ratchet tests."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from development_kit.scripts.quality_gate import (
    POLICY_PATH,
    evaluate_coverage,
    load_coverage_policy,
)


def _passing_report() -> dict:
    policy = load_coverage_policy(POLICY_PATH)
    return {
        "totals": {
            "percent_covered": policy["global"]["minimum_percent_covered"],
            "covered_lines": 100,
            "num_statements": 120,
            "covered_branches": 40,
            "num_branches": 50,
        },
        "files": {
            target["path"].replace("/", "\\"): {
                "summary": {"percent_covered": target["minimum_percent_covered"]}
            }
            for target in policy["targets"]
        },
    }


def test_committed_coverage_policy_is_exact_and_passes_at_its_floors() -> None:
    policy = load_coverage_policy(POLICY_PATH)
    receipt = evaluate_coverage(_passing_report(), policy)

    assert receipt["status"] == "passed"
    assert receipt["failures"] == []
    assert len(receipt["targets"]) == 8
    assert all(item["owner"] and item["removal_gate"] for item in receipt["targets"])


def test_global_and_safety_file_regressions_fail_closed() -> None:
    policy = load_coverage_policy(POLICY_PATH)
    report = _passing_report()
    report["totals"]["percent_covered"] -= 0.01
    first_path = policy["targets"][0]["path"].replace("/", "\\")
    report["files"][first_path]["summary"]["percent_covered"] -= 0.01

    receipt = evaluate_coverage(report, policy)

    assert receipt["status"] == "failed"
    assert {item["reason_code"] for item in receipt["failures"]} == {
        "global_coverage_regressed",
        "coverage_target_regressed",
    }


def test_missing_safety_target_fails_closed() -> None:
    policy = load_coverage_policy(POLICY_PATH)
    report = _passing_report()
    first_path = policy["targets"][0]["path"].replace("/", "\\")
    del report["files"][first_path]

    receipt = evaluate_coverage(report, policy)

    assert receipt["status"] == "failed"
    assert receipt["failures"][0]["reason_code"] == "coverage_target_missing"


def test_coverage_policy_rejects_unowned_exclusions(tmp_path: Path) -> None:
    policy = load_coverage_policy(POLICY_PATH)
    invalid = deepcopy(policy)
    invalid["targets"][0]["owner"] = ""
    path = tmp_path / "coverage-policy.json"
    import json

    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="values"):
        load_coverage_policy(path)
