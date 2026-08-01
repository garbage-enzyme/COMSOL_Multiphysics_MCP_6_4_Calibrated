from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.jobs.validation_matrix import normalize_validation_matrix_spec
from src.jobs.validation_rows import (
    append_validation_row,
    completed_point_fingerprints,
    read_validation_rows,
)


def _point(point_id: str, wavelength: float, configuration: str) -> dict:
    return {
        "point_id": point_id,
        "configuration_sha256": configuration * 64,
        "wavelength": {"value": wavelength, "unit": "um", "parameter": "wl"},
        "collectors": [
            {
                "name": "wave_optics_point_audit",
                "inputs": {"component_tag": "comp1"},
            }
        ],
        "expected_artifact_ids": [f"artifact-{point_id}"],
    }


def _spec(tmp_path):
    source = tmp_path / "fixture.mph"
    source.write_bytes(b"controlled model")
    return normalize_validation_matrix_spec(
        {
            "job_type": "validation_matrix",
            "source_model_path": str(source),
            "points": [_point("off", 5.1, "a"), _point("target", 5.2, "b")],
            "point_limit": 2,
            "cores": 1,
            "resource_policy": {
                "wall_time_budget_seconds": 120,
                "minimum_next_point_seconds": 30,
                "max_mesh_elements": 100_000,
            },
        }
    )


def _summary(point_id: str, digest: str = "c" * 64) -> dict:
    return {
        "collector": "wave_optics_point_audit",
        "artifact_id": f"artifact-{point_id}",
        "audit_status": "measurement_complete",
        "manifest_relative_path": f"artifacts/artifact-{point_id}/manifest.json",
        "manifest_sha256": digest,
        "manifest_size_bytes": 123,
    }


def test_append_fsync_journal_replays_exact_complete_identities(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    fsync_snapshots = []
    directory_fsync_calls = []
    real_fsync = os.fsync

    def observe_fsync(fd):
        real_fsync(fd)
        descriptor = os.fstat(fd)
        if not path.exists():
            return
        journal = path.stat()
        if (descriptor.st_dev, descriptor.st_ino) != (journal.st_dev, journal.st_ino):
            return
        fsync_snapshots.append(path.read_bytes())

    monkeypatch.setattr("src.jobs.validation_rows.os.fsync", observe_fsync)
    monkeypatch.setattr(
        "src.jobs.validation_rows.fsync_directory",
        lambda path: directory_fsync_calls.append(path),
    )

    first = append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="off",
        status="ok",
        collector_summaries=[_summary("off")],
        created_at_epoch=1.0,
    )
    second = append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="target",
        status="ok",
        collector_summaries=[_summary("target", "d" * 64)],
        created_at_epoch=2.0,
    )

    assert [snapshot.count(b"\n") for snapshot in fsync_snapshots] == [1, 2]
    assert all(snapshot.endswith(b"\n") for snapshot in fsync_snapshots)
    assert directory_fsync_calls == [path.parent]
    assert second["previous_row_sha256"] == first["row_sha256"]
    assert read_validation_rows(path, spec) == [first, second]
    assert completed_point_fingerprints(path, spec) == {
        point["point_fingerprint"] for point in spec["points"]
    }


def test_error_rows_are_durable_but_never_resume_skips(tmp_path):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="off",
        status="error",
        error={"type": "SolveError", "message": "controlled failure"},
        created_at_epoch=1.0,
    )

    assert completed_point_fingerprints(path, spec) == set()
    recovered = append_validation_row(
        path,
        spec,
        attempt=2,
        point_id="off",
        status="ok",
        collector_summaries=[_summary("off")],
        created_at_epoch=2.0,
    )
    assert recovered["attempt"] == 2
    assert len(read_validation_rows(path, spec)) == 2


def test_duplicate_complete_exact_identity_is_refused(tmp_path):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="off",
        status="ok",
        collector_summaries=[_summary("off")],
    )

    with pytest.raises(ValueError, match="already exists"):
        append_validation_row(
            path,
            spec,
            attempt=2,
            point_id="off",
            status="ok",
            collector_summaries=[_summary("off")],
        )


def test_changed_immutable_spec_rejects_prior_rows(tmp_path):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="off",
        status="ok",
        collector_summaries=[_summary("off")],
    )
    changed = json.loads(json.dumps(spec))
    changed["spec_fingerprint"] = "f" * 64

    with pytest.raises(ValueError, match="spec_fingerprint differs"):
        read_validation_rows(path, changed)


@pytest.mark.parametrize("change", ["cores", "source"])
def test_raw_immutable_inputs_and_source_identity_bind_replayed_rows(tmp_path, change):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="off",
        status="ok",
        collector_summaries=[_summary("off")],
    )
    source = tmp_path / "changed-source.mph"
    source.write_bytes(b"different immutable model")
    raw = {
        "job_type": "validation_matrix",
        "source_model_path": str(source if change == "source" else tmp_path / "fixture.mph"),
        "points": [_point("off", 5.1, "a"), _point("target", 5.2, "b")],
        "point_limit": 2,
        "cores": 2 if change == "cores" else 1,
        "resource_policy": {
            "wall_time_budget_seconds": 120,
            "minimum_next_point_seconds": 30,
            "max_mesh_elements": 100_000,
        },
    }
    changed = normalize_validation_matrix_spec(raw)

    assert changed["spec_fingerprint"] != spec["spec_fingerprint"]
    if change == "source":
        assert changed["source_model_sha256"] != spec["source_model_sha256"]
        assert changed["points"][0]["point_fingerprint"] != spec["points"][0]["point_fingerprint"]
    with pytest.raises(ValueError, match="spec_fingerprint differs"):
        read_validation_rows(path, changed)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sequence", 2, "sequence is not contiguous"),
        ("previous_row_sha256", "e" * 64, "hash chain is discontinuous"),
        ("point_fingerprint", "f" * 64, "point fingerprint differs"),
        ("row_sha256", "0" * 64, "row_sha256 does not match"),
    ],
)
def test_tampered_rows_fail_closed(tmp_path, field, value, message):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="off",
        status="ok",
        collector_summaries=[_summary("off")],
    )
    row = json.loads(path.read_text(encoding="utf-8"))
    row[field] = value
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        read_validation_rows(path, spec)


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "C:/private/manifest.json",
        "/private/manifest.json",
        "../manifest.json",
        "nested/../manifest.json",
    ],
)
def test_blank_malformed_and_absolute_artifact_paths_are_rejected(tmp_path, unsafe_path):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="blank record"):
        read_validation_rows(path, spec)
    path.write_text("{bad json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="malformed JSON"):
        read_validation_rows(path, spec)
    unsafe = _summary("off")
    unsafe["manifest_relative_path"] = unsafe_path
    with pytest.raises(ValueError, match="portable relative path"):
        append_validation_row(
            tmp_path / "fresh.jsonl",
            spec,
            attempt=1,
            point_id="off",
            status="ok",
            collector_summaries=[unsafe],
        )


@pytest.mark.parametrize(
    ("scope", "field"),
    [
        ("spec", "spec_fingerprint"),
        ("spec", "source_model_sha256"),
        ("point", "point_fingerprint"),
        ("point", "configuration_sha256"),
        ("point", "collectors"),
        ("point", "expected_artifact_ids"),
    ],
)
def test_malformed_immutable_spec_raises_validation_error_before_row_indexing(
    tmp_path, scope, field
):
    spec = _spec(tmp_path)
    target = spec if scope == "spec" else spec["points"][0]
    target.pop(field)

    with pytest.raises(ValueError, match="validation_matrix|spec|point"):
        append_validation_row(
            tmp_path / "malformed-spec.jsonl",
            spec,
            attempt=1,
            point_id="off",
            status="ok",
            collector_summaries=[],
        )


def test_partial_or_integrity_blocked_collectors_cannot_form_complete_rows(tmp_path):
    spec = _spec(tmp_path)
    for audit_status in ("measurement_partial", "integrity_blocked"):
        summary = _summary("off")
        summary["audit_status"] = audit_status
        with pytest.raises(ValueError, match="audit_status is not complete"):
            append_validation_row(
                tmp_path / f"{audit_status}.jsonl",
                spec,
                attempt=1,
                point_id="off",
                status="ok",
                collector_summaries=[summary],
            )


def test_partial_tail_is_truncated_before_concurrent_safe_append(tmp_path):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"
    first = append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="off",
        status="ok",
        collector_summaries=[_summary("off")],
    )
    with path.open("ab") as handle:
        handle.write(b'{"sequence":2')

    second = append_validation_row(
        path,
        spec,
        attempt=1,
        point_id="target",
        status="ok",
        collector_summaries=[_summary("target", "d" * 64)],
    )

    assert read_validation_rows(path, spec) == [first, second]
    assert not (tmp_path / ".rows.jsonl.lock").exists()


def test_concurrent_validation_appends_form_one_contiguous_chain(tmp_path):
    spec = _spec(tmp_path)
    path = tmp_path / "rows.jsonl"

    def append(point_id):
        return append_validation_row(
            path,
            spec,
            attempt=1,
            point_id=point_id,
            status="ok",
            collector_summaries=[_summary(point_id)],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(append, ["off", "target"]))

    replayed = read_validation_rows(path, spec)
    assert {row["row_sha256"] for row in replayed} == {row["row_sha256"] for row in rows}
    assert [row["sequence"] for row in replayed] == [1, 2]
    assert replayed[1]["previous_row_sha256"] == replayed[0]["row_sha256"]
