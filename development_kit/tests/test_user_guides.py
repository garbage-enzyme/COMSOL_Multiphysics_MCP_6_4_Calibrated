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
INTERACTIVE_DOCS = ROOT / "docs" / "interactive_shared_session"
CHINESE_DISABLED_WARNING = "严格证据检查已关闭；这些结果未经过完整验证，可能包含 AI 生成或幻觉内容。"


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


def test_chinese_evidence_guide_is_complete_and_contract_equivalent():
    guide = (EVIDENCE_DOCS / "README_CN.md").read_text(encoding="utf-8")

    assert CHINESE_DISABLED_WARNING in guide
    assert DISABLED_CHECK_WARNING in guide
    assert EVIDENCE_SETTINGS_ENV in guide
    assert "evidence_integrity_status" in guide
    assert "evidence_integrity_verify" in guide
    assert "strict_evidence_checks_disabled" in guide
    assert "strictly_verified: true" in guide
    assert "strictly_verified: false" in guide
    assert all(name in guide for name in EVIDENCE_CHECKS)
    assert "不能验证物理" in guide


def test_english_interactive_guide_matches_the_shared_public_surface():
    guide = (INTERACTIVE_DOCS / "README.md").read_text(encoding="utf-8")

    assert "Ching-Chiang/comsol-mcp" in guide
    assert "did not copy, adapt, translate, cherry-pick, or mechanically rewrite" in guide
    assert "COMSOL_MCP_PROFILE = 'desktop_shared'" in guide
    assert "COMSOL_MCP_ENABLE_SHARED_SERVER = 'true'" in guide
    assert "6.4.0.*" in guide
    assert "6.4.0.293" in guide
    assert "localhost:<port>" in guide
    assert "username and password" in guide
    assert "occupied-model or busy warning" in guide
    assert "shared_server_preflight" in guide
    assert "shared_server_attach" in guide
    assert "shared_server_models" in guide
    assert "shared_model_adopt" in guide
    assert "shared_model_lock" in guide
    assert "shared_model_verify" in guide
    assert "shared_model_snapshot" in guide
    assert "shared_model_unlock" in guide
    assert "shared_server_detach" in guide
    assert "job_submit/status/tail/cancel/resume" in guide
    assert "Immutable source" in guide
    assert "Open working model" in guide
    assert "Save Copy snapshot/checkpoint" in guide
