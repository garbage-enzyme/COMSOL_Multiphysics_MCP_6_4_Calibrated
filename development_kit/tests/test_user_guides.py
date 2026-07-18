"""Executable examples and drift guards for bilingual user guides."""

from __future__ import annotations

from pathlib import Path

from src.evidence.integrity_controls import (
    DISABLED_CHECK_WARNING,
    EVIDENCE_CHECKS,
    EVIDENCE_SETTINGS_ENV,
    load_evidence_integrity_status,
)


ROOT = Path(__file__).parents[2]
EVIDENCE_DOCS = ROOT / "docs" / "evidence_integrity"


def test_documented_default_and_exploration_settings_are_executable():
    default = load_evidence_integrity_status(
        {EVIDENCE_SETTINGS_ENV: str(EVIDENCE_DOCS / "default_settings.json")}
    )
    exploration = load_evidence_integrity_status(
        {EVIDENCE_SETTINGS_ENV: str(EVIDENCE_DOCS / "exploration_settings.json")}
    )

    assert default["strict_verification_active"] is True
    assert set(default["checks"]) == set(EVIDENCE_CHECKS)
    assert exploration["strict_verification_active"] is False
    assert exploration["disabled_checks"] == ["summary_claim_verification"]


def test_english_evidence_guide_matches_the_public_contract():
    guide = (EVIDENCE_DOCS / "README.md").read_text(encoding="utf-8")

    assert DISABLED_CHECK_WARNING in guide
    assert EVIDENCE_SETTINGS_ENV in guide
    assert "evidence_integrity_status" in guide
    assert "evidence_integrity_verify" in guide
    assert "strict_evidence_checks_disabled" in guide
    assert "strictly_verified: true" in guide
    assert "strictly_verified: false" in guide
    assert all(name in guide for name in EVIDENCE_CHECKS)
    assert "do not validate physics" in guide
