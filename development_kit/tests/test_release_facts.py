"""Release-facts generation and consistency tests."""

from __future__ import annotations

import json
from pathlib import Path

from development_kit.scripts.release_facts import (
    FACTS_PATH,
    build_release_facts,
    check_release_facts,
)


def test_committed_release_facts_match_live_implementation():
    check_release_facts()


def test_check_rejects_type_drift_that_plain_equality_would_accept(tmp_path):
    facts = build_release_facts()
    drifted = json.loads(json.dumps(facts))
    # True == 1 and 1.0 == 1 under ==; the canonical-serialization check must
    # still reject these type mutations.
    drifted["tool_count"] = float(drifted["tool_count"])
    path = Path(tmp_path) / "release-facts.json"
    path.write_text(json.dumps(drifted), encoding="utf-8")
    try:
        check_release_facts(path)
    except SystemExit as exc:
        assert "differ from live implementation" in str(exc)
    else:
        raise AssertionError("type-drifted release facts passed the integrity check")


def test_release_facts_have_bounded_deterministic_identity_fields():
    facts = build_release_facts()
    assert facts["tool_count"] > 0
    assert facts["profiles"]
    assert all(profile["tool_count"] > 0 for profile in facts["profiles"].values())
    assert facts["features"] == {
        "lexical_docs": {"tool_count": 2, "default_enabled": False},
        "semantic_docs": {"tool_count": 3, "default_enabled": False},
        "shared_server": {"tool_count": 10, "default_enabled": False},
    }
    assert facts["schema_registry"]["entry_count"] > 0
    assert all(
        len(value) == 64 and all(character in "0123456789abcdef" for character in value)
        for value in facts["identities"].values()
    )
    assert FACTS_PATH.is_file()
