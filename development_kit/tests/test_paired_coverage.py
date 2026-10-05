"""Reject mismatched peers and preserve the branch coverage floor."""

import copy
import hashlib
import json

import pytest

from development_kit.scripts.paired_coverage import merge_reports, verify_pair


def report(lines, missing, arcs, missing_arcs):
    return {
        "meta": {"branch_coverage": True},
        "files": {
            "comsol_mcp/durable/io.py": {
                "executed_lines": lines,
                "missing_lines": missing,
                "executed_branches": arcs,
                "missing_branches": missing_arcs,
            }
        },
    }


def test_union_covers_complementary_native_branches():
    left = report([1], [2], [[1, 2]], [[1, -1]])
    right = report([2], [1], [[1, -1]], [[1, 2]])
    merged = merge_reports(left, right)
    assert merged["totals"]["percent_covered"] == 100
    assert merged["files"]["comsol_mcp/durable/io.py"]["missing_branches"] == []


def test_union_does_not_hide_an_uncovered_branch():
    left = report([1], [2], [[1, 2]], [[1, -1]])
    merged = merge_reports(left, copy.deepcopy(left))
    assert merged["totals"]["percent_covered"] == 50
    assert merged["files"]["comsol_mcp/durable/io.py"]["missing_branches"] == [(1, -1)]


@pytest.mark.parametrize("change", ["file", "line", "arc", "mode"])
def test_mismatched_measurements_are_rejected(change):
    left = report([1], [2], [[1, 2]], [[1, -1]])
    right = copy.deepcopy(left)
    entry = right["files"]["comsol_mcp/durable/io.py"]
    if change == "file":
        right["files"] = {}
    elif change == "line":
        entry["missing_lines"] = [3]
    elif change == "arc":
        entry["missing_branches"] = [[1, -2]]
    else:
        right["meta"]["branch_coverage"] = False
    with pytest.raises(ValueError):
        merge_reports(left, right)


@pytest.mark.parametrize(
    "damage", ["source", "policy", "hash", "platform", "status", "license", "peer"]
)
def test_pair_rejects_untrusted_peer_receipts(tmp_path, damage):
    from development_kit.scripts.quality_gate import POLICY_PATH

    paths = []
    for platform in ("win32", "linux"):
        folder = tmp_path / platform
        folder.mkdir()
        raw = json.dumps(report([1], [2], [[1, 2]], [[1, -1]])).encode()
        (folder / "coverage.json").write_bytes(raw)
        receipt = {
            "platform": platform,
            "source_tree_sha256": "accepted-source",
            "coverage_policy_sha256": hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest(),
            "coverage_json_sha256": hashlib.sha256(raw).hexdigest(),
            "status": "awaiting_cross_platform_coverage",
            "dependency_licenses": {"status": "passed"},
        }
        if platform == "linux":
            key = {
                "source": "source_tree_sha256",
                "policy": "coverage_policy_sha256",
                "hash": "coverage_json_sha256",
                "platform": "platform",
                "status": "status",
            }.get(damage)
            if key:
                receipt[key] = "wrong"
            if damage == "license":
                receipt["dependency_licenses"]["status"] = "failed"
        path = folder / "quality-receipt.json"
        path.write_text(json.dumps(receipt))
        paths.append(path)
    if damage == "peer":
        paths.pop()
    with pytest.raises(ValueError):
        verify_pair(paths, POLICY_PATH, "accepted-source")


@pytest.mark.parametrize("uncovered_contract", [False, True])
def test_peer_native_io_deferral_requires_all_other_targets(tmp_path, uncovered_contract):
    from development_kit.scripts.quality_gate import POLICY_PATH, load_coverage_policy

    paths = []
    for platform in ("win32", "linux"):
        measurement = (
            report([1], [2], [[1, 2]], [[1, -1]])
            if platform == "win32"
            else report([2], [1], [[1, -1]], [[1, 2]])
        )
        measurement["totals"] = {"percent_covered": 90}
        measurement["files"]["comsol_mcp/durable/io.py"]["summary"] = {"percent_covered": 50}
        for target in load_coverage_policy(POLICY_PATH)["targets"]:
            if target["path"] != "comsol_mcp/durable/io.py":
                measurement["files"][target["path"]] = {
                    "executed_lines": [1],
                    "missing_lines": [],
                    "executed_branches": [],
                    "missing_branches": [],
                    "summary": {"percent_covered": 100},
                }
        if uncovered_contract and platform == "win32":
            entry = measurement["files"]["comsol_mcp/contracts/job_submission.py"]
            entry.update(executed_lines=[], missing_lines=[1], summary={"percent_covered": 0})
        folder = tmp_path / platform
        folder.mkdir()
        raw = json.dumps(measurement).encode()
        (folder / "coverage.json").write_bytes(raw)
        receipt = {
            "platform": platform,
            "source_tree_sha256": "accepted-source",
            "coverage_policy_sha256": hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest(),
            "coverage_json_sha256": hashlib.sha256(raw).hexdigest(),
            "status": "awaiting_cross_platform_coverage",
            "dependency_licenses": {"status": "passed"},
        }
        path = folder / "quality-receipt.json"
        path.write_text(json.dumps(receipt))
        paths.append(path)
    if uncovered_contract:
        with pytest.raises(ValueError, match="outside native IO"):
            verify_pair(paths, POLICY_PATH, "accepted-source")
    else:
        assert verify_pair(paths, POLICY_PATH, "accepted-source")["status"] == "passed"
