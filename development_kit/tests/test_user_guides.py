"""Executable examples and drift guards for bilingual user guides."""

from __future__ import annotations

import json
from pathlib import Path
import re

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
    assert '"profile": { "name": "desktop_shared" }' in guide
    assert '"shared_server": { "enabled": true }' in guide
    assert "COMSOL_MCP_SETTINGS_PATH=" in guide
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


def test_chinese_interactive_guide_is_complete_and_contract_equivalent():
    guide = (INTERACTIVE_DOCS / "README_CN.md").read_text(encoding="utf-8")

    assert "Ching-Chiang/comsol-mcp" in guide
    assert "没有复制、改写、翻译、cherry-pick 或机械重写" in guide
    assert '"profile": { "name": "desktop_shared" }' in guide
    assert '"shared_server": { "enabled": true }' in guide
    assert "COMSOL_MCP_SETTINGS_PATH=" in guide
    assert "6.4.0.*" in guide
    assert "6.4.0.293" in guide
    assert "localhost:<port>" in guide
    assert "username 和 password" in guide
    assert "occupied-model 或 busy warning" in guide
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


def test_root_readmes_expose_two_separate_feature_entry_points():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README_CN.md").read_text(encoding="utf-8")

    assert "## Featured capabilities" in english
    assert "## 特色功能" in chinese
    for guide_path in (
        "docs/evidence_integrity/README.md",
        "docs/evidence_integrity/README_CN.md",
        "docs/interactive_shared_session/README.md",
        "docs/interactive_shared_session/README_CN.md",
    ):
        assert guide_path in english
        assert guide_path in chinese
    assert "default-on" in english
    assert "default-off" in english
    assert "默认开启" in chinese
    assert "默认关闭" in chinese


def test_deployment_guides_explain_the_shared_settings_file_and_fallbacks():
    english = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    chinese = (ROOT / "DEPLOYMENT_CN.md").read_text(encoding="utf-8")
    for guide in (english, chinese):
        assert "settings.json" in guide
        assert "COMSOL_MCP_SETTINGS_PATH" in guide
        assert "settings_errors" in guide
        assert "shared_server" in guide


def test_embedded_guidance_no_longer_denies_the_shared_profile():
    documents = [
        ROOT / "docs" / "profile_migration.md",
        ROOT / "comsol_mcp" / "knowledge" / "prompts" / "workflow.md",
        ROOT / "comsol_mcp" / "knowledge" / "prompts" / "mph_api.md",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in documents)

    assert "no protected shared Desktop" not in combined
    assert "No current profile implements protected shared" not in combined
    assert "desktop_shared" in combined
    assert "shared_server_preflight" in combined


def test_every_documented_json_example_is_machine_parseable():
    guides = [
        EVIDENCE_DOCS / "README.md",
        EVIDENCE_DOCS / "README_CN.md",
        INTERACTIVE_DOCS / "README.md",
        INTERACTIVE_DOCS / "README_CN.md",
    ]

    for path in guides:
        blocks = re.findall(
            r"(?ms)^```json\s*\n(.*?)\n```$",
            path.read_text(encoding="utf-8"),
        )
        assert blocks, path
        for block in blocks:
            assert isinstance(json.loads(block), dict), path
