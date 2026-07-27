"""Continuation campaign state journal and artifact replay tests."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from development_kit.tests.spectral_job_fixtures import write_fake_point_audit
from development_kit.tests.test_branch_continuation_campaign_job import _raw_campaign
from src.jobs.branch_continuation_campaign import normalize_branch_continuation_campaign_spec
from src.jobs.branch_continuation_campaign_rows import (
    append_branch_continuation_campaign_state,
    read_branch_continuation_campaign_states,
)
from src.jobs.spectral_runner import run_spectral_characterization


def _complete_state(spec: dict, root, ordinal: int):
    state = spec["states"][ordinal]
    child = state["spectral_job"]
    directory = root / f"s{ordinal}"

    def execute(point, artifact_dir):
        wavelength = point["wavelength"]["value"]
        coordinate = (wavelength - (5.0e-6 + ordinal * 20e-9)) / 0.4e-6
        absorption = 0.1 + 0.8 / (1.0 + coordinate * coordinate)
        return write_fake_point_audit(artifact_dir, child, point, absorption=absorption)

    result = run_spectral_characterization(child, directory, attempt=1, point_executor=execute)
    assert result["completed"] is True
    return directory


def test_completed_states_append_in_order_and_replay_exact_artifacts(tmp_path):
    spec = normalize_branch_continuation_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "continuation_states.jsonl"
    first_dir = _complete_state(spec, root, 0)
    first = append_branch_continuation_campaign_state(
        journal, spec, attempt=1, state_dir=first_dir, artifact_root=root
    )
    second_dir = _complete_state(spec, root, 1)
    second = append_branch_continuation_campaign_state(
        journal, spec, attempt=1, state_dir=second_dir, artifact_root=root
    )

    replayed = read_branch_continuation_campaign_states(journal, spec, artifact_root=root)
    assert replayed == [first, second]
    assert second["previous_row_sha256"] == first["row_sha256"]
    assert first["mesh_counts"] == {"element_count": 12, "vertex_count": 8}
    assert first["search_window_m"] == {"lower_m": 4e-6, "upper_m": 6e-6}
    assert first["expansion_count"] == 0
    assert first["incidence_readback_sha256"] == spec["states"][0]["incidence_readback"]["evidence_sha256"]


def test_duplicate_append_with_previous_state_directory_fails_closed(tmp_path):
    spec = normalize_branch_continuation_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "continuation_states.jsonl"
    first_dir = _complete_state(spec, root, 0)
    append_branch_continuation_campaign_state(
        journal, spec, attempt=1, state_dir=first_dir, artifact_root=root
    )
    with pytest.raises(ValueError, match="summary does not match"):
        append_branch_continuation_campaign_state(
            journal, spec, attempt=1, state_dir=first_dir, artifact_root=root
        )


@pytest.mark.parametrize("target", ["summary", "rows", "journal"])
def test_artifact_and_row_tampering_fail_closed(tmp_path, target):
    spec = normalize_branch_continuation_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "continuation_states.jsonl"
    state_dir = _complete_state(spec, root, 0)
    append_branch_continuation_campaign_state(
        journal, spec, attempt=1, state_dir=state_dir, artifact_root=root
    )
    if target == "summary":
        path = state_dir / "analysis" / "summary.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["reason_code"] = "tampered"
        path.write_text(json.dumps(value), encoding="utf-8")
    elif target == "rows":
        with (state_dir / "spectral_rows.jsonl").open("ab") as handle:
            handle.write(b" ")
    else:
        value = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
        value["search_window_m"]["lower_m"] += 1e-9
        journal.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash|size|search|replay"):
        read_branch_continuation_campaign_states(journal, spec, artifact_root=root)


def test_changed_campaign_identity_cannot_reuse_state_rows(tmp_path):
    spec = normalize_branch_continuation_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "continuation_states.jsonl"
    state_dir = _complete_state(spec, root, 0)
    append_branch_continuation_campaign_state(
        journal, spec, attempt=1, state_dir=state_dir, artifact_root=root
    )
    changed = deepcopy(spec)
    changed["spec_fingerprint"] = "f" * 64
    with pytest.raises(ValueError, match="chain identity"):
        read_branch_continuation_campaign_states(journal, changed, artifact_root=root)


def test_partial_campaign_tail_is_removed_before_next_state(tmp_path):
    spec = normalize_branch_continuation_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "continuation_states.jsonl"
    first_dir = _complete_state(spec, root, 0)
    first = append_branch_continuation_campaign_state(
        journal, spec, attempt=1, state_dir=first_dir, artifact_root=root
    )
    with journal.open("ab") as handle:
        handle.write(b'{"ordinal":1')
    second_dir = _complete_state(spec, root, 1)

    second = append_branch_continuation_campaign_state(
        journal, spec, attempt=1, state_dir=second_dir, artifact_root=root
    )

    assert read_branch_continuation_campaign_states(
        journal, spec, artifact_root=root
    ) == [first, second]
    assert not (root / ".continuation_states.jsonl.lock").exists()
