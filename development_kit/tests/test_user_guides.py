"""Executable examples and drift guards for bilingual user guides."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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
MODES_DOCS = ROOT / "docs" / "simulation_execution_modes"
SETTINGS_DOCS = ROOT / "docs" / "setting_guide"
CHINESE_DISABLED_WARNING = (
    "严格证据检查已关闭；这些结果未经过完整验证，可能包含 AI 生成或幻觉内容。"
)


def _leaf_paths(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    paths: set[str] = set()
    for key, item in value.items():
        child = f"{prefix}.{key}" if prefix else key
        paths.update(_leaf_paths(item, child))
    return paths


def _tracked_markdown_paths() -> list[Path]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git is required to enumerate checked-in Markdown")
    completed = subprocess.run(
        [git, "ls-files", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return [ROOT / line for line in completed.stdout.splitlines() if line]


def _json_fence_blocks(text: str) -> list[str]:
    opening = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>.*?)[ \t]*$")
    blocks: list[str] = []
    active: tuple[str, int, bool] | None = None
    body: list[str] = []
    for line in text.splitlines():
        if active is None:
            match = opening.fullmatch(line)
            if match is None:
                continue
            info = match.group("info").strip().casefold()
            tokens = info.replace("{", " ").replace("}", " ").split()
            is_json = bool(tokens) and (tokens[0] == "json" or ".json" in tokens)
            active = (match.group("fence")[0], len(match.group("fence")), is_json)
            body = []
            continue
        marker, minimum, is_json = active
        if re.fullmatch(rf" {{0,3}}{re.escape(marker)}{{{minimum},}}[ \t]*", line):
            if is_json:
                blocks.append("\n".join(body))
            active = None
            body = []
        else:
            body.append(line)
    if active is not None and active[2]:
        raise AssertionError("unclosed JSON fence")
    return blocks


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
    assert all(
        phrase in guide
        for phrase in (
            "did not copy",
            "adapt, translate, cherry-pick",
            "mechanically rewrite",
        )
    )
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
    assert all(phrase in guide for phrase in ("没有复制", "改写、翻译、挑选提交", "机械重写"))
    assert '"profile": { "name": "desktop_shared" }' in guide
    assert '"shared_server": { "enabled": true }' in guide
    assert "COMSOL_MCP_SETTINGS_PATH=" in guide
    assert "6.4.0.*" in guide
    assert "6.4.0.293" in guide
    assert "localhost:<port>" in guide
    assert "用户名和密码" in guide
    assert "模型占用或忙碌提示" in guide
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
    assert "不可变源模型" in guide
    assert "当前工作模型" in guide
    assert "Save Copy 快照或检查点" in guide
    for avoidable_english in (
        "behavioral research",
        "optimistic model/revision lock",
        "partial edit",
        "hot reload",
        "fail closed",
        "simultaneous co-editing",
    ):
        assert avoidable_english not in guide


def test_execution_mode_guides_are_bilingual_and_contract_equivalent():
    english = (MODES_DOCS / "README.md").read_text(encoding="utf-8")
    chinese = (MODES_DOCS / "README_CN.md").read_text(encoding="utf-8")
    modes = ("interactive", "inline", "launcher", "standalone", "mphonly")

    assert all(f"`{mode}`" in english for mode in modes)
    assert all(f"`{mode}`" in chinese for mode in modes)
    assert "Agents use `interactive`, `inline`, or `launcher` by default" in english
    assert "agent 默认只使用 `interactive`、`inline` 和 `launcher`" in chinese
    assert "below 1 hour" in english
    assert "少于 1 小时" in chinese
    assert "target operating system and architecture" in english
    assert "目标操作系统、处理器架构" in chinese
    assert "Windows 10/11 x64" in english
    assert "Windows 10/11 x64" in chinese
    assert "exact per-point durability is not promised" in english
    assert "不承诺每点都能恢复" in chinese
    assert "relative errors of about `6.81e-10`" in english
    assert "相对解析误差约为 `6.81e-10`" in chinese
    assert "It does not verify interruption recovery" in english
    assert "不能证明中断后恢复" in chinese
    assert "comsol_ref_solver.36.230.html" in english
    assert "comsol_ref_solver.36.230.html" in chinese
    assert "comsol_ref_solver.36.042.html" in english
    assert "comsol_ref_solver.36.042.html" in chinese


def test_root_readmes_expose_same_language_feature_and_settings_guides():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README_CN.md").read_text(encoding="utf-8")

    assert "## Featured capabilities" in english
    assert "## 特色功能" in chinese
    for guide_path in (
        "docs/evidence_integrity/README.md",
        "docs/interactive_shared_session/README.md",
        "docs/simulation_execution_modes/README.md",
        "docs/setting_guide/README.md",
    ):
        assert guide_path in english
        assert guide_path not in chinese
    for guide_path in (
        "docs/evidence_integrity/README_CN.md",
        "docs/interactive_shared_session/README_CN.md",
        "docs/simulation_execution_modes/README_CN.md",
        "docs/setting_guide/README_CN.md",
    ):
        assert guide_path in chinese
        assert guide_path not in english
    assert "default-on" in english
    assert "default-off" in english
    assert "默认开启" in chinese
    assert "默认关闭" in chinese


def test_settings_guides_cover_every_checked_in_setting_and_keep_languages_separate():
    english = (SETTINGS_DOCS / "README.md").read_text(encoding="utf-8")
    chinese = (SETTINGS_DOCS / "README_CN.md").read_text(encoding="utf-8")
    fields = _leaf_paths(json.loads((ROOT / "settings.json").read_text(encoding="utf-8")))

    assert all(field in english for field in fields)
    assert all(field in chinese for field in fields)
    assert "README_CN.md" not in english
    assert "../evidence_integrity/README.md" in english
    assert "../interactive_shared_session/README.md" in english
    assert "../evidence_integrity/README_CN.md" in chinese
    assert "../interactive_shared_session/README_CN.md" in chinese
    assert "back up the effective" in english
    assert "更新或重装 MCP package 前，先备份实际生效的" in chinese


def test_language_switch_is_limited_to_the_main_readmes():
    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README_CN.md").read_text(encoding="utf-8")

    assert "English | [中文](README_CN.md)" in english
    assert "[English](README.md) | 中文" in chinese

    for path in (
        ROOT / "DEPLOYMENT.md",
        ROOT / "DEPLOYMENT_CN.md",
        EVIDENCE_DOCS / "README.md",
        EVIDENCE_DOCS / "README_CN.md",
        INTERACTIVE_DOCS / "README.md",
        INTERACTIVE_DOCS / "README_CN.md",
        MODES_DOCS / "README.md",
        MODES_DOCS / "README_CN.md",
        ROOT / "launcher" / "README.md",
        ROOT / "launcher" / "README_CN.md",
        SETTINGS_DOCS / "README.md",
        SETTINGS_DOCS / "README_CN.md",
    ):
        content = path.read_text(encoding="utf-8")
        assert "[English]" not in content, path
        assert "[中文]" not in content, path


def test_deployment_guides_explain_the_shared_settings_file_and_fallbacks():
    english = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    chinese = (ROOT / "DEPLOYMENT_CN.md").read_text(encoding="utf-8")
    for guide in (english, chinese):
        assert "settings.json" in guide
        assert "COMSOL_MCP_SETTINGS_PATH" in guide
        assert "settings_errors" in guide
        assert "shared_server" in guide
        assert all(
            mode in guide for mode in ("interactive", "inline", "launcher", "standalone", "mphonly")
        )


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
    paths = _tracked_markdown_paths()
    assert paths
    block_count = 0
    for path in paths:
        blocks = _json_fence_blocks(path.read_text(encoding="utf-8"))
        block_count += len(blocks)
        for block in blocks:
            assert isinstance(json.loads(block), dict), path
    assert block_count > 0


def test_json_fence_parser_accepts_commonmark_variants():
    blocks = _json_fence_blocks(
        '  ```JSON title=example  \n{"first": 1}\n  ```  \n'
        '~~~{.json data-kind=fixture}\n{"second": 2}\n~~~~\n'
    )

    assert [json.loads(block) for block in blocks] == [{"first": 1}, {"second": 2}]
