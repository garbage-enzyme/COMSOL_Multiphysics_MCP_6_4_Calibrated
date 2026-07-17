"""Solver-free convergence campaign level journal and artifact replay tests."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from development_kit.tests.spectral_job_fixtures import write_fake_point_audit
from development_kit.tests.test_convergence_campaign_job import _raw_campaign
from src.jobs.convergence_campaign import normalize_convergence_campaign_spec
from src.jobs.convergence_campaign_rows import (
    append_convergence_campaign_level,
    read_convergence_campaign_levels,
)
from src.jobs.convergence_campaign_runner import convergence_level_directory
from src.jobs.spectral_runner import run_spectral_characterization


def _complete_level(spec: dict, root, ordinal: int):
    level = spec["levels"][ordinal]
    child = level["spectral_job"]
    directory = convergence_level_directory(root, ordinal)

    def execute(point, artifact_dir):
        wavelength = point["wavelength"]["value"]
        coordinate = (wavelength - (5.0e-6 + ordinal * 1.0e-9)) / 0.4e-6
        absorption = 0.1 + 0.8 / (1.0 + coordinate * coordinate)
        return write_fake_point_audit(
            artifact_dir, child, point, absorption=absorption
        )

    result = run_spectral_characterization(
        child,
        directory,
        attempt=1,
        point_executor=execute,
    )
    assert result["completed"] is True
    return directory


def test_completed_levels_append_in_declared_order_and_replay_exact_artifacts(tmp_path):
    spec = normalize_convergence_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "convergence_levels.jsonl"
    first_dir = _complete_level(spec, root, 0)
    first = append_convergence_campaign_level(
        journal, spec, attempt=1, level_dir=first_dir, artifact_root=root
    )
    second_dir = _complete_level(spec, root, 1)
    second = append_convergence_campaign_level(
        journal, spec, attempt=1, level_dir=second_dir, artifact_root=root
    )

    replayed = read_convergence_campaign_levels(journal, spec, artifact_root=root)
    assert replayed == [first, second]
    assert second["previous_row_sha256"] == first["row_sha256"]
    assert first["mesh_counts"] == {"element_count": 12, "vertex_count": 8}
    assert first["scientific_disposition"] == "accepted"
    assert set(first["artifacts"]) == {
        "spectral_summary", "spectral_bundle", "spectral_decision",
        "spectral_characterization", "spectral_rows",
    }


def test_duplicate_append_and_out_of_order_level_directory_fail_closed(tmp_path):
    spec = normalize_convergence_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "convergence_levels.jsonl"
    first_dir = _complete_level(spec, root, 0)
    append_convergence_campaign_level(
        journal, spec, attempt=1, level_dir=first_dir, artifact_root=root
    )
    with pytest.raises(ValueError, match="summary does not match"):
        append_convergence_campaign_level(
            journal, spec, attempt=1, level_dir=first_dir, artifact_root=root
        )


@pytest.mark.parametrize("target", ["summary", "rows", "journal"])
def test_artifact_and_row_tampering_fail_closed(tmp_path, target):
    spec = normalize_convergence_campaign_spec(_raw_campaign(tmp_path / "sources"))
    root = tmp_path / "campaign"
    journal = root / "convergence_levels.jsonl"
    level_dir = _complete_level(spec, root, 0)
    append_convergence_campaign_level(
        journal, spec, attempt=1, level_dir=level_dir, artifact_root=root
    )
    if target == "summary":
        path = level_dir / "analysis" / "summary.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["reason_code"] = "tampered"
        path.write_text(json.dumps(value), encoding="utf-8")
    elif target == "rows":
        with (level_dir / "spectral_rows.jsonl").open("ab") as handle:
            handle.write(b" ")
    else:
        value = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
        value["mesh_counts"]["element_count"] += 1
        journal.write_text(json.dumps(value) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash|size|mesh|replay"):
        read_convergence_campaign_levels(journal, spec, artifact_root=root)


def test_changed_campaign_identity_cannot_reuse_level_rows(tmp_path):
    raw = _raw_campaign(tmp_path / "sources")
    spec = normalize_convergence_campaign_spec(raw)
    root = tmp_path / "campaign"
    journal = root / "convergence_levels.jsonl"
    level_dir = _complete_level(spec, root, 0)
    append_convergence_campaign_level(
        journal, spec, attempt=1, level_dir=level_dir, artifact_root=root
    )
    changed = deepcopy(spec)
    changed["spec_fingerprint"] = "f" * 64
    with pytest.raises(ValueError, match="chain identity"):
        read_convergence_campaign_levels(journal, changed, artifact_root=root)
