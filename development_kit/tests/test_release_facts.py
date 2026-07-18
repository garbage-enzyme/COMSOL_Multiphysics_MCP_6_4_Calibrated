"""Release-facts generation and consistency tests."""

from __future__ import annotations

from development_kit.scripts.release_facts import (
    FACTS_PATH,
    build_release_facts,
    check_release_facts,
)


def test_committed_release_facts_match_live_implementation():
    check_release_facts()


def test_release_facts_have_bounded_deterministic_identity_fields():
    facts = build_release_facts()
    assert facts["tool_count"] > 0
    assert facts["profiles"]
    assert all(
        profile["tool_count"] > 0
        for profile in facts["profiles"].values()
    )
    assert facts["schema_registry"]["entry_count"] > 0
    assert all(
        len(value) == 64 and all(character in "0123456789abcdef" for character in value)
        for value in facts["identities"].values()
    )
    assert FACTS_PATH.is_file()
