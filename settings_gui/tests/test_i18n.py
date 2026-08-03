"""Deterministic gettext catalog coverage."""

from __future__ import annotations

import ast
from string import Formatter

from comsol_mcp.settings import GUI_LANGUAGES, GUI_SCALES
from comsol_mcp.tools.profiles import PROFILE_NAMES
from development_kit.scripts.settings_gui_locales import expected_files
from settings_gui.app import FIXED_LINKS, TAB_TITLES
from settings_gui.i18n import (
    LANGUAGE_SELF_NAMES,
    LOCALE_ROOT,
    MESSAGE_IDS,
    Translator,
    language_option_labels,
    scale_option_labels,
)
from settings_gui.model import FIELDS, PROFILE_HELP_IDS


def _placeholders(value: str) -> set[str]:
    return {
        name
        for _literal, name, _format, _conversion in Formatter().parse(value)
        if name is not None
    }


def _literal_translation_calls() -> set[str]:
    messages: set[str] = set()
    for name in ("app.py", "controller.py"):
        tree = ast.parse((LOCALE_ROOT.parent / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first = node.args[0]
            if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id == "_":
                messages.add(first.value)
            elif isinstance(function, ast.Attribute) and function.attr == "text":
                messages.add(first.value)
    return messages


def test_message_inventory_covers_runtime_labels_and_help() -> None:
    messages = set(MESSAGE_IDS)

    assert len(messages) == len(MESSAGE_IDS)
    assert _literal_translation_calls() <= messages
    assert set(TAB_TITLES.values()) <= messages
    assert {label for label, _url in FIXED_LINKS} <= messages
    assert {field.help_id for field in FIELDS} <= messages


def test_all_catalogs_are_complete_and_placeholder_safe() -> None:
    for language in GUI_LANGUAGES:
        translator = Translator(language)
        assert translator.warning is None
        for message in MESSAGE_IDS:
            translated = translator(message)
            assert translated
            assert _placeholders(translated) == _placeholders(message)
            if language != "en" and message not in {"COMSOL/Java", *LANGUAGE_SELF_NAMES.values()}:
                assert translated != message


def test_language_options_use_autonyms_and_literal_keys() -> None:
    expected = {
        "en": "English (en)",
        "zh-cn": "简体中文 (zh-cn)",
        "zh-tw": "繁體中文 (zh-tw)",
    }
    for language in GUI_LANGUAGES:
        assert language_option_labels(Translator(language)) == expected


def test_scale_options_keep_literal_values_and_localize_system_choice() -> None:
    for language in GUI_LANGUAGES:
        labels = scale_option_labels(Translator(language))

        assert tuple(labels) == GUI_SCALES
        assert labels["100"] == "100%"
        assert labels["125"] == "125%"
        assert labels["150"] == "150%"
        assert labels["200"] == "200%"
        assert labels["system"].endswith(" (system)")


def test_profile_help_covers_all_profiles_and_explains_the_safe_default() -> None:
    assert tuple(PROFILE_HELP_IDS) == PROFILE_NAMES
    assert PROFILE_HELP_IDS["core"].startswith("Safety-first default for new users.")
    assert PROFILE_HELP_IDS["basic_fem"].startswith("Recommended for most users.")

    for language in GUI_LANGUAGES:
        translator = Translator(language)
        rendered = {profile: translator(PROFILE_HELP_IDS[profile]) for profile in PROFILE_NAMES}

        assert all(rendered.values())
        assert len(set(rendered.values())) == len(PROFILE_NAMES)
        if language == "zh-cn":
            assert "安全" in rendered["core"] and "新手" in rendered["core"]
            assert "大多数用户" in rendered["basic_fem"]
        elif language == "zh-tw":
            assert "安全" in rendered["core"] and "新手" in rendered["core"]
            assert "大多數使用者" in rendered["basic_fem"]


def test_po_and_mo_outputs_are_exactly_reproducible() -> None:
    for path, expected in expected_files().items():
        assert path.read_bytes() == expected
        if path.suffix == ".po":
            raw = expected.decode("utf-8")
            assert "#, fuzzy" not in raw
            assert '\nmsgstr ""\n' not in raw


def test_gettext_sources_are_forced_to_lf_in_git_checkouts() -> None:
    attributes = (LOCALE_ROOT.parents[1] / ".gitattributes").read_text(encoding="utf-8")

    assert "settings_gui/locales/settings_gui.pot text eol=lf" in attributes
    assert "settings_gui/locales/**/settings_gui.po text eol=lf" in attributes
