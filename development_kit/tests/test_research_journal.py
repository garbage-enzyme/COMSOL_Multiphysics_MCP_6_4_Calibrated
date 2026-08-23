"""Crash-safe append-only research journal contracts."""

from __future__ import annotations

import json
import threading

import pytest

from comsol_mcp.research import journal as journal_module
from comsol_mcp.research.decisions import normalize_decision_record
from comsol_mcp.research.journal import (
    append_research_journal_record,
    recover_research_journal,
)
from comsol_mcp.research.records import normalize_candidate_record
from development_kit.tests.test_research_candidates import _candidate
from development_kit.tests.test_research_contracts import _space
from development_kit.tests.test_research_decisions import _decision


def _candidate_payload() -> dict:
    return normalize_candidate_record(_candidate(), _space())


def _decision_payload() -> dict:
    return normalize_decision_record(_decision())


def test_append_and_recover_exact_hash_chain(tmp_path):
    path = tmp_path / "research.jsonl"
    first = append_research_journal_record(
        path,
        "candidate",
        _candidate_payload(),
        expected_previous_record_fingerprint=None,
    )
    second = append_research_journal_record(
        path,
        "decision",
        _decision_payload(),
        expected_previous_record_fingerprint=first["record_fingerprint"],
    )
    recovered = recover_research_journal(path)
    assert recovered["state"] == "current_valid"
    assert recovered["record_count"] == 2
    assert recovered["last_record_fingerprint"] == second["record_fingerprint"]
    assert path.read_bytes().endswith(b"\n")


def test_stale_predecessor_fails_without_appending(tmp_path):
    path = tmp_path / "research.jsonl"
    append_research_journal_record(
        path,
        "candidate",
        _candidate_payload(),
        expected_previous_record_fingerprint=None,
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="predecessor is stale"):
        append_research_journal_record(
            path,
            "decision",
            _decision_payload(),
            expected_previous_record_fingerprint=None,
        )
    assert path.read_bytes() == before


def test_partial_tail_is_recovered_truncated_and_followed_by_valid_append(tmp_path):
    path = tmp_path / "research.jsonl"
    first = append_research_journal_record(
        path,
        "candidate",
        _candidate_payload(),
        expected_previous_record_fingerprint=None,
    )
    complete = path.read_bytes()
    with path.open("ab") as handle:
        handle.write(b'{"partial":')
    observed = recover_research_journal(path)
    assert observed["state"] == "incomplete"
    assert observed["record_count"] == 1
    repaired = recover_research_journal(path, repair_partial_tail=True)
    assert repaired["partial_tail_repaired"] is True
    assert path.read_bytes() == complete
    append_research_journal_record(
        path,
        "decision",
        _decision_payload(),
        expected_previous_record_fingerprint=first["record_fingerprint"],
    )
    assert recover_research_journal(path)["record_count"] == 2


def test_complete_chain_tampering_is_rejected_without_repair(tmp_path):
    path = tmp_path / "research.jsonl"
    first = append_research_journal_record(
        path,
        "candidate",
        _candidate_payload(),
        expected_previous_record_fingerprint=None,
    )
    append_research_journal_record(
        path,
        "decision",
        _decision_payload(),
        expected_previous_record_fingerprint=first["record_fingerprint"],
    )
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[1]["previous_record_fingerprint"] = "0" * 64
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="predecessor chain"):
        recover_research_journal(path, repair_partial_tail=True)
    assert path.read_bytes() == before


def test_payload_tampering_is_rejected_before_append(tmp_path):
    payload = _candidate_payload()
    payload["normalized_values"]["patch_length_x"] = 101.0
    with pytest.raises(ValueError, match="payload fingerprint is invalid"):
        append_research_journal_record(
            tmp_path / "research.jsonl",
            "candidate",
            payload,
            expected_previous_record_fingerprint=None,
        )


def test_unknown_kind_and_payload_schema_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="kind is unsupported"):
        append_research_journal_record(
            tmp_path / "research.jsonl",
            "script",
            _candidate_payload(),
            expected_previous_record_fingerprint=None,
        )
    with pytest.raises(ValueError, match="schema does not match"):
        append_research_journal_record(
            tmp_path / "research.jsonl",
            "decision",
            _candidate_payload(),
            expected_previous_record_fingerprint=None,
        )


@pytest.mark.parametrize("bad_kind", [["candidate"], {"kind": 1}, 42, None])
def test_unhashable_or_non_string_kinds_raise_value_errors_not_type_errors(tmp_path, bad_kind):
    # kind is an untrusted JSON leaf; membership testing must never leak a
    # TypeError from unhashable values.
    with pytest.raises(ValueError, match="kind is unsupported"):
        append_research_journal_record(
            tmp_path / "research.jsonl",
            bad_kind,
            _candidate_payload(),
            expected_previous_record_fingerprint=None,
        )


def test_concurrent_appends_serialize_and_preserve_the_hash_chain(tmp_path):
    # Recover -> stale-check -> append is one critical section: with both
    # writers claiming the same tail exactly one commits and the loser must
    # observe a stale predecessor instead of corrupting or forking the chain.
    path = tmp_path / "research.jsonl"
    first = append_research_journal_record(
        path,
        "candidate",
        _candidate_payload(),
        expected_previous_record_fingerprint=None,
    )
    results: list[str] = []
    errors: list[str] = []

    def worker(kind: str, payload_factory) -> None:
        try:
            record = append_research_journal_record(
                path,
                kind,
                payload_factory(),
                expected_previous_record_fingerprint=first["record_fingerprint"],
            )
            results.append(record["record_fingerprint"])
        except ValueError as error:
            errors.append(str(error))

    threads = [
        threading.Thread(target=worker, args=("decision", _decision_payload)),
        threading.Thread(target=worker, args=("candidate", _candidate_payload)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert "predecessor is stale" in errors[0]
    recovered = recover_research_journal(path)
    assert recovered["record_count"] == 2
    assert recovered["last_record_fingerprint"] == results[0]


def test_append_runs_recovery_inside_the_exclusive_lock(tmp_path, monkeypatch):
    events: list[str] = []

    class RecordingLock(journal_module.JobLock):
        def __enter__(self):
            events.append("enter")
            return super().__enter__()

        def __exit__(self, *args: object):
            events.append("exit")
            return super().__exit__(*args)

    monkeypatch.setattr(journal_module, "JobLock", RecordingLock)
    append_research_journal_record(
        tmp_path / "research.jsonl",
        "candidate",
        _candidate_payload(),
        expected_previous_record_fingerprint=None,
    )

    assert events[0] == "enter"
    assert events[-1] == "exit"
