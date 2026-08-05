"""Quality gate policy and branch-coverage ratchet tests."""

from __future__ import annotations

import json
import math
import subprocess
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest

from development_kit.scripts.quality_gate import (
    POLICY_PATH,
    _create_short_pytest_roots,
    _main_pytest_command,
    evaluate_coverage,
    load_coverage_policy,
    run_quality_gate,
    validate_quality_target_inventory,
    validate_windows_gate_root,
)


def test_hosted_quality_main_suite_is_serial_but_local_suite_keeps_four_workers(
    tmp_path: Path,
) -> None:
    hosted = _main_pytest_command(tmp_path, hosted_ci=True)
    local = _main_pytest_command(tmp_path, hosted_ci=False)

    assert "-n" not in hosted
    assert "--dist" not in hosted
    assert local[local.index("-n") + 1] == "4"
    assert local[local.index("--dist") + 1] == "loadscope"
    assert local[local.index("--basetemp") + 1] == str(tmp_path)


def test_configured_pytest_roots_remain_direct_short_siblings(monkeypatch) -> None:
    seed = Path("D:/mcp_tests/a65b13p")
    monkeypatch.setenv("COMSOL_MCP_TEST_ASCII_ROOT", str(seed))
    monkeypatch.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)

    observed_main, observed_serial = _create_short_pytest_roots()
    second_main, second_serial = _create_short_pytest_roots()

    assert observed_main.parent == observed_serial.parent == Path("D:/mcp_tests").resolve()
    assert observed_main.name[:-1] == observed_serial.name[:-1]
    assert observed_main.name.endswith("m")
    assert observed_serial.name.endswith("s")
    assert len(observed_main.name) <= 12
    assert len(observed_serial.name) <= 12
    assert {observed_main, observed_serial}.isdisjoint({second_main, second_serial})


def test_local_windows_gate_roots_fail_fast_before_deep_path_generation() -> None:
    accepted = validate_windows_gate_root(
        Path("D:/mcp_tests/a65b13q"),
        label="quality artifact root",
        platform_name="nt",
        hosted_ci=False,
    )
    assert accepted == Path("D:/mcp_tests/a65b13q").resolve()

    with pytest.raises(ValueError, match="generated-path budget"):
        validate_windows_gate_root(
            Path("D:/mcp_tests/descriptive-quality-gate"),
            label="quality artifact root",
            platform_name="nt",
            hosted_ci=False,
        )
    with pytest.raises(ValueError, match="direct child"):
        validate_windows_gate_root(
            Path("D:/mcp_tests/alpha65/b13q"),
            label="quality artifact root",
            platform_name="nt",
            hosted_ci=False,
        )
    with pytest.raises(ValueError, match="must be ASCII"):
        validate_windows_gate_root(
            Path("C:/Users/测试/q"),
            label="quality artifact root",
            platform_name="nt",
            hosted_ci=False,
        )


def test_hosted_and_non_windows_gate_roots_are_not_subject_to_local_budget() -> None:
    long_root = Path("C:/runner/work/project/_temp/descriptive-quality-root")
    assert (
        validate_windows_gate_root(
            long_root, label="quality artifact root", platform_name="nt", hosted_ci=True
        )
        == long_root.resolve()
    )
    assert (
        validate_windows_gate_root(
            long_root, label="quality artifact root", platform_name="posix", hosted_ci=False
        )
        == long_root.resolve()
    )


def _passing_report() -> dict:
    policy = load_coverage_policy(POLICY_PATH)
    return {
        "totals": {
            "percent_covered": 100.0,
            "covered_lines": 100,
            "num_statements": 120,
            "covered_branches": 40,
            "num_branches": 50,
        },
        "files": {
            target["path"].replace("/", "\\"): {"summary": {"percent_covered": 100.0}}
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


def test_quality_target_inventory_covers_every_production_module(tmp_path: Path) -> None:
    receipt = validate_quality_target_inventory()
    assert receipt["production_module_count"] >= receipt["lint_targeted_count"]
    assert receipt["production_module_count"] >= receipt["typing_targeted_count"]

    (tmp_path / "comsol_mcp").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "comsol_mcp" / "unclassified.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="classification changed"):
        validate_quality_target_inventory(tmp_path)


def test_global_and_safety_file_regressions_fail_closed() -> None:
    policy = load_coverage_policy(POLICY_PATH)
    report = _passing_report()
    report["totals"]["percent_covered"] = policy["global"]["minimum_percent_covered"] - 0.01
    first_path = policy["targets"][0]["path"].replace("/", "\\")
    report["files"][first_path]["summary"]["percent_covered"] = (
        policy["targets"][0]["minimum_percent_covered"] - 0.01
    )

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


@pytest.mark.parametrize("scope", ["global", "target"])
@pytest.mark.parametrize("field", ["owner", "rationale", "removal_gate"])
@pytest.mark.parametrize("mutation", ["missing", "blank"])
def test_coverage_policy_requires_complete_metadata(
    tmp_path: Path, scope: str, field: str, mutation: str
) -> None:
    policy = load_coverage_policy(POLICY_PATH)
    item = policy["global"] if scope == "global" else policy["targets"][0]
    if mutation == "missing":
        del item[field]
    else:
        item[field] = " "
    path = tmp_path / "coverage-policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match="global coverage policy|values"):
        load_coverage_policy(path)


@pytest.mark.parametrize("threshold", [True, math.nan, math.inf, -math.inf])
def test_coverage_policy_rejects_nonfinite_and_boolean_floors(
    tmp_path: Path,
    threshold: object,
) -> None:
    policy = load_coverage_policy(POLICY_PATH)
    policy["global"]["minimum_percent_covered"] = threshold
    path = tmp_path / "coverage-policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match="values"):
        load_coverage_policy(path)


@pytest.mark.parametrize("observed", [True, math.nan, math.inf, -math.inf])
def test_coverage_report_rejects_nonfinite_and_boolean_percentages(observed: object) -> None:
    policy = load_coverage_policy(POLICY_PATH)
    report = _passing_report()
    report["totals"]["percent_covered"] = observed

    with pytest.raises(ValueError, match="percentage"):
        evaluate_coverage(report, policy)

    report = _passing_report()
    first_path = policy["targets"][0]["path"].replace("/", "\\")
    report["files"][first_path]["summary"]["percent_covered"] = observed
    receipt = evaluate_coverage(report, policy)
    assert receipt["status"] == "failed"
    assert receipt["failures"][0]["reason_code"] == "coverage_target_missing"


def test_command_failure_writes_one_machine_readable_receipt(
    ascii_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_command(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(7, ["private", "command"])

    monkeypatch.setattr(subprocess, "run", fail_command)
    receipt = run_quality_gate(ascii_tmp_path, as_of=date(2026, 7, 28))
    outputs = list(ascii_tmp_path.glob("run-*/quality-receipt.json"))

    assert receipt["status"] == "failed"
    assert receipt["command_failure"] == {"stage": "lint", "returncode": 7}
    assert len(outputs) == 1
    assert json.loads(outputs[0].read_text(encoding="utf-8")) == receipt
    assert "private" not in outputs[0].read_text(encoding="utf-8")


def test_quality_runs_allocate_independent_evidence_directories(
    ascii_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_command(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["command"])

    monkeypatch.setattr(subprocess, "run", fail_command)
    first = run_quality_gate(ascii_tmp_path, as_of=date(2026, 7, 28))
    second = run_quality_gate(ascii_tmp_path, as_of=date(2026, 7, 28))

    assert first["run_id"] != second["run_id"]
    assert len(list(ascii_tmp_path.glob("run-*/quality-receipt.json"))) == 2
