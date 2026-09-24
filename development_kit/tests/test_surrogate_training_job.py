"""Durable surrogate-training job state, crash, and cancellation tests."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from comsol_mcp.jobs.manager import JobManager
from comsol_mcp.jobs.surrogate_training import normalize_surrogate_training_spec
from comsol_mcp.surrogate.fields import build_field_schema, fit_transforms
from comsol_mcp.surrogate.manifests import build_dataset_manifest
from comsol_mcp.surrogate.splits import assign_group_disjoint_split

SOURCE_SHA = "a" * 64


def _documents() -> dict:
    groups = [f"g{index}" for index in range(10)]
    split_plan = assign_group_disjoint_split(group_ids=groups, seed=17, holdout_group_ids=["g0"])
    row_ids = [f"row-{group}" for group in groups]
    # The dataset manifest's per-row split assignments must agree with the split
    # plan's per-group assignments. This fixture previously assigned every row to
    # "train" while its own split plan placed g0 in scientific_holdout and others
    # in validation/test, so the two documents contradicted each other and the
    # spec check did not notice. The assignment is now derived from the plan.
    group_split = {item["group_id"]: item["split"] for item in split_plan["assignments"]}
    dataset = build_dataset_manifest(
        dataset_id="ds-001",
        source_kind="campaign",
        source_identity_sha256=SOURCE_SHA,
        candidate_ids=[f"cand-{group}" for group in groups],
        row_ids=row_ids,
        leakage_groups=[{"group_id": group, "row_ids": [f"row-{group}"]} for group in groups],
        split_assignments=[
            {"row_id": f"row-{group}", "split": group_split[group]} for group in groups
        ],
    )
    schema = build_field_schema(
        schema_id="waveopt-scalar-v1",
        features=[
            {"field_id": "w", "unit": "nm", "lower": 100.0, "upper": 900.0},
            {"field_id": "h", "unit": "nm", "lower": 50.0, "upper": 400.0},
        ],
        targets=[
            {
                "field_id": "R",
                "unit": "1",
                "lower": 0.0,
                "upper": 1.0,
                "support": {"coordinates": [1550.0], "unit": "nm"},
            }
        ],
    )
    transforms = fit_transforms(
        transform_id="std-v1",
        schema=schema,
        train_rows=[
            {"w": 100.0, "h": 50.0, "R": 0.1},
            {"w": 300.0, "h": 150.0, "R": 0.4},
            {"w": 900.0, "h": 400.0, "R": 0.9},
        ],
        kinds={"w": "standardize", "R": "minmax"},
    )
    return {"dataset": dataset, "split": split_plan, "schema": schema, "transforms": transforms}


def _spec(**overrides) -> dict:
    docs = _documents()
    spec = {
        "job_type": "surrogate_training",
        "campaign_id": "campaign-A",
        "dataset_manifest": docs["dataset"],
        "split_plan": docs["split"],
        "field_schema": docs["schema"],
        "transforms": docs["transforms"],
        "architecture": {
            "layers": [16, 8],
            "activation": "tanh",
            "optimizer": "adam",
            "loss": "mse",
            "batch_size": 8,
            "learning_rate": 0.001,
        },
        "seeds": [17, 29],
        "maximum_epochs": 3,
        "wall_time_budget_seconds": 600,
        "resource_policy": {"wall_time_budget_seconds": 600, "minimum_next_point_seconds": 5},
    }
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------
# Spec normalization
# --------------------------------------------------------------------------


def test_spec_binds_all_contract_identities() -> None:
    spec = normalize_surrogate_training_spec(_spec())
    assert spec["job_type"] == "surrogate_training"
    assert spec["dataset_manifest_sha256"] == spec["dataset_manifest"]["manifest_sha256"]
    assert spec["split_manifest_sha256"] == spec["split_plan"]["manifest_sha256"]
    assert spec["field_schema_sha256"] == spec["field_schema"]["manifest_sha256"]
    assert spec["transforms_sha256"] == spec["transforms"]["manifest_sha256"]
    assert len(spec["architecture_sha256"]) == 64
    assert spec["maximum_epochs"] == 3
    assert spec["spec_fingerprint"]


def test_spec_rejects_unknown_fields_and_bad_architecture() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        normalize_surrogate_training_spec(_spec(unexpected=True))
    with pytest.raises(ValueError, match="unknown fields"):
        normalize_surrogate_training_spec(
            _spec(architecture={**_spec()["architecture"], "depth": 3})
        )
    with pytest.raises(ValueError, match="unsupported activation"):
        normalize_surrogate_training_spec(
            _spec(architecture={**_spec()["architecture"], "activation": "swish"})
        )
    with pytest.raises(ValueError, match="unsupported optimizer"):
        normalize_surrogate_training_spec(
            _spec(architecture={**_spec()["architecture"], "optimizer": "lbfgs"})
        )
    with pytest.raises(ValueError, match="at most 8 hidden layers"):
        normalize_surrogate_training_spec(
            _spec(architecture={**_spec()["architecture"], "layers": [4] * 9})
        )


def test_spec_rejects_mismatched_leakage_groups() -> None:
    smaller = build_dataset_manifest(
        dataset_id="ds-002",
        source_kind="campaign",
        source_identity_sha256=SOURCE_SHA,
        candidate_ids=["c1", "c2"],
        row_ids=["r1", "r2"],
        leakage_groups=[
            {"group_id": "g1", "row_ids": ["r1"]},
            {"group_id": "g2", "row_ids": ["r2"]},
        ],
        split_assignments=[
            {"row_id": "r1", "split": "train"},
            {"row_id": "r2", "split": "test"},
        ],
    )
    with pytest.raises(ValueError, match="must match the split plan exactly"):
        normalize_surrogate_training_spec(_spec(dataset_manifest=smaller))


def test_spec_rejects_the_same_group_ids_bound_to_different_splits() -> None:
    """Regression: identical group IDs are not enough; the splits must agree.

    An earlier implementation compared only `{group_id}` set equality, so a
    dataset manifest that called group g0 "train" was accepted against a split
    plan that put g0 in "scientific_holdout". That is a holdout leak, and it is
    exactly what the leakage check exists to prevent.
    """
    docs = _documents()
    conflicting = build_dataset_manifest(
        dataset_id="ds-003",
        source_kind="campaign",
        source_identity_sha256=SOURCE_SHA,
        candidate_ids=[f"cand-g{index}" for index in range(10)],
        row_ids=[f"row-g{index}" for index in range(10)],
        leakage_groups=[
            {"group_id": f"g{index}", "row_ids": [f"row-g{index}"]} for index in range(10)
        ],
        # Every group claimed as "train" while the split plan assigns g0 to the
        # holdout and others to validation/test. Identical group IDs, so only a
        # split-aware check can catch this.
        split_assignments=[{"row_id": f"row-g{index}", "split": "train"} for index in range(10)],
    )
    # The group-ID sets really are identical, which is why the old check passed.
    assert {item["group_id"] for item in conflicting["leakage_groups"]} == {
        item["group_id"] for item in docs["split"]["assignments"]
    }
    with pytest.raises(ValueError, match="is train in the dataset manifest but"):
        normalize_surrogate_training_spec(_spec(dataset_manifest=conflicting))


def test_spec_rejects_a_group_that_spans_splits_in_the_dataset() -> None:
    """A group whose own rows disagree is refused on the specification path too."""
    docs = _documents()
    group_split = {item["group_id"]: item["split"] for item in docs["split"]["assignments"]}
    # g1's rows are split across train and test inside the dataset manifest.
    assignments = [
        {"row_id": f"row-g{index}", "split": group_split[f"g{index}"]} for index in range(10)
    ]
    assignments[1] = {
        "row_id": "row-g1",
        "split": "test" if group_split["g1"] != "test" else "train",
    }
    spanning = build_dataset_manifest(
        dataset_id="ds-004",
        source_kind="campaign",
        source_identity_sha256=SOURCE_SHA,
        candidate_ids=[f"cand-g{index}" for index in range(10)],
        row_ids=[f"row-g{index}" for index in range(10)],
        leakage_groups=[
            {"group_id": f"g{index}", "row_ids": [f"row-g{index}"]} for index in range(10)
        ],
        split_assignments=assignments,
    )
    with pytest.raises(ValueError, match="must match the split plan exactly"):
        normalize_surrogate_training_spec(_spec(dataset_manifest=spanning))


def test_the_shipped_training_fixture_is_internally_consistent() -> None:
    """The fixture the other tests rely on must not itself conflict.

    It previously assigned every row to "train" while its own split plan placed
    g0 in the scientific holdout, so it encoded the very leak the check was
    supposed to catch.
    """
    docs = _documents()
    group_split = {item["group_id"]: item["split"] for item in docs["split"]["assignments"]}
    for assignment in docs["dataset"]["split_assignments"]:
        group = assignment["row_id"].removeprefix("row-")
        assert assignment["split"] == group_split[group], assignment
    # And the fixture must survive the specification path unchanged.
    assert normalize_surrogate_training_spec(_spec())["spec_fingerprint"]


def test_spec_rejects_duplicate_seeds_and_bad_budgets() -> None:
    with pytest.raises(ValueError, match="unique"):
        normalize_surrogate_training_spec(_spec(seeds=[17, 17]))
    with pytest.raises(ValueError, match="maximum_epochs"):
        normalize_surrogate_training_spec(_spec(maximum_epochs=0))
    with pytest.raises(ValueError, match="wall_time_budget_seconds"):
        normalize_surrogate_training_spec(_spec(wall_time_budget_seconds=0))
    with pytest.raises(ValueError, match="resource policy wall time"):
        normalize_surrogate_training_spec(
            _spec(
                resource_policy={"wall_time_budget_seconds": 99999, "minimum_next_point_seconds": 5}
            )
        )


def test_continuation_requires_identical_contract_identities() -> None:
    spec = normalize_surrogate_training_spec(_spec())
    good = {
        "parent_job_id": "job-parent",
        "trained_chksum": "f" * 64,
        "dataset_manifest_sha256": spec["dataset_manifest_sha256"],
        "split_manifest_sha256": spec["split_manifest_sha256"],
        "field_schema_sha256": spec["field_schema_sha256"],
        "transforms_sha256": spec["transforms_sha256"],
        "architecture_sha256": spec["architecture_sha256"],
    }
    resumed = normalize_surrogate_training_spec(_spec(continue_from=good))
    assert resumed["continue_from"]["parent_job_id"] == "job-parent"

    mismatched = {**good, "dataset_manifest_sha256": "b" * 64}
    with pytest.raises(ValueError, match="does not match the current contract identity"):
        normalize_surrogate_training_spec(_spec(continue_from=mismatched))
    mismatched_arch = {**good, "architecture_sha256": "c" * 64}
    with pytest.raises(ValueError, match="architecture_sha256"):
        normalize_surrogate_training_spec(_spec(continue_from=mismatched_arch))


def test_spec_is_deterministic() -> None:
    first = normalize_surrogate_training_spec(_spec())
    second = normalize_surrogate_training_spec(_spec())
    assert first["spec_fingerprint"] == second["spec_fingerprint"]


# --------------------------------------------------------------------------
# Durable job behavior
# --------------------------------------------------------------------------


def _manager(root: Path) -> JobManager:
    return JobManager(root, preflight=lambda **_: {"success": True, "ready": True})


def _wait_terminal(manager: JobManager, job_id: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.store.read_state(job_id)
        if state["status"] in {"completed", "failed", "interrupted", "cancelled"}:
            return state
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def test_surrogate_job_completes_with_terminal_receipt(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "jobs")
    submitted = manager.submit(_spec())
    assert submitted["success"] is True
    job_id = submitted["job_id"]
    state = _wait_terminal(manager, job_id)
    assert state["status"] == "completed", state.get("last_error")
    assert state["progress"] == {"completed": 3, "total": 3}

    receipt_path = manager.store.job_dir(job_id) / "terminal_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["completed_epochs"] == 3
    assert receipt["claims_fem_evidence"] is False
    assert receipt["claims_model_quality"] is False
    assert receipt["scientific_disposition"] == "predicted"


def test_surrogate_job_replays_epochs_without_duplicates(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "jobs")
    job_id = manager.submit(_spec(maximum_epochs=4))["job_id"]
    state = _wait_terminal(manager, job_id)
    assert state["status"] == "completed"

    from comsol_mcp.durable.io import read_complete_jsonl

    journal = read_complete_jsonl(manager.store.job_dir(job_id) / "epochs.jsonl")
    assert journal["state"] == "current_valid"
    epochs = [record["epoch"] for record in journal["records"]]
    assert epochs == [0, 1, 2, 3]
    assert len(epochs) == len(set(epochs))


def test_duplicate_surrogate_submission_is_deduplicated(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "jobs")
    first = manager.submit(_spec())
    second = manager.submit(_spec())
    assert second["duplicate"] is True
    assert second["job_id"] == first["job_id"]


def test_surrogate_submit_rejects_tampered_contract(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "jobs")
    broken = _spec()
    broken["dataset_manifest"] = {**broken["dataset_manifest"], "row_ids": ["r1"]}
    with pytest.raises(ValueError):
        manager.submit(broken)


def test_surrogate_preflight_is_solver_free(tmp_path: Path) -> None:
    manager = JobManager(tmp_path / "jobs")
    spec = normalize_surrogate_training_spec(_spec())
    preflight = manager._preflight_surrogate_training(spec)
    assert preflight["ready"] is True
    assert preflight["solver_free"] is True
    assert preflight["mph_imported"] is False
    assert preflight["acquires_solver_lease"] is False


def test_surrogate_worker_refuses_foreign_job_type(tmp_path: Path) -> None:
    from comsol_mcp.jobs import surrogate_training_worker

    manager = JobManager(tmp_path / "jobs", allow_test_jobs=True)
    job_id = manager.submit({"job_type": "test_sequence", "delays": [0.01]})["job_id"]
    with pytest.raises(ValueError, match="refuses non-surrogate jobs"):
        surrogate_training_worker.run(str(manager.store.root), job_id)
