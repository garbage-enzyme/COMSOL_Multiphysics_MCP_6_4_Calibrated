"""Isolated gettext loading with runtime language switching."""

from __future__ import annotations

import gettext
import struct
from pathlib import Path

from comsol_mcp.settings import GUI_LANGUAGES, GUI_SCALES

from .constants import DOMAIN
from .model import GUI_SCALE_HELP_ID, PROFILE_HELP_IDS

LOCALE_ROOT = Path(__file__).resolve().parent / "locales"
LANGUAGE_LOCALES = {"en": "en", "zh-cn": "zh_CN", "zh-tw": "zh_TW"}
LANGUAGE_SELF_NAMES = {"en": "English", "zh-cn": "简体中文", "zh-tw": "繁體中文"}

# This inventory is the contract shared by the checked-in PO/MO catalogs and
# the runtime call sites. Setting keys and serialized enum values are excluded
# intentionally because they must remain literal in every language.
MESSAGE_IDS = (
    "COMSOL MCP Settings",
    "General",
    "Profile",
    "Runtime",
    "COMSOL/Java",
    "Evidence",
    "Docs",
    "Owner",
    "About",
    "Changes take effect only after restarting Codex or the owning MCP client.",
    "Save and Exit",
    "Apply",
    "Cancel",
    "Browse",
    "Clear",
    "Auto-detect",
    "Add folder",
    "Remove",
    "Choose folder",
    "Choose file",
    "Choose index file",
    "GUI release",
    "Installed package version",
    "Desktop shortcut",
    "Create desktop shortcut",
    "Remove desktop shortcut",
    "Replace existing desktop shortcut?",
    "A different shortcut already uses this name. Replace it?",
    "Desktop shortcut ready",
    "The desktop shortcut now opens this exact settings file.",
    "Desktop shortcut could not be created",
    "The existing Desktop item was preserved.",
    "Desktop shortcut removed",
    "The owned desktop shortcut was removed.",
    "No owned desktop shortcut was found.",
    "Desktop shortcut not removed",
    "The Desktop item is not owned by this application and was preserved.",
    "Repositories and acknowledgements",
    "This repository",
    "Thanks: upstream project",
    "Thanks: Ching-Chiang project",
    "MIT License",
    "English",
    "简体中文",
    "繁體中文",
    "Internal settings format name. You do not need to change it.",
    "Settings format version used when this file is saved.",
    "Language used by this Settings window.",
    GUI_SCALE_HELP_ID,
    "Follow Windows display settings",
    *PROFILE_HELP_IDS.values(),
    (
        "Folder for working files and locks. Use an ASCII-only path. "
        "\nExample: %PROGRAMDATA%\\comsol_mcp\\runtime"
    ),
    (
        "Optional separate folder for resumable jobs. Leave it empty to use the runtime folder. "
        "\nExample: %PROGRAMDATA%\\comsol_mcp\\runtime\\jobs"
    ),
    (
        "Folders where COMSOL MCP may read your .mph files. Chinese paths are supported. "
        "\nExample: %LOCALAPPDATA%\\comsol_mcp\\models"
    ),
    (
        "Folder where COMSOL MCP writes results and evidence. Use an ASCII-only path. "
        "\nExample: %PROGRAMDATA%\\comsol_mcp\\artifacts"
    ),
    ("Folder where COMSOL Multiphysics 6.4 is installed. \nExample: C:\\COMSOL64\\Multiphysics"),
    (
        "Java folder used by COMSOL. Auto-detect can fill this value. "
        "\nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre"
    ),
    (
        "JDK folder used by COMSOL. Auto-detect can fill this value. "
        "\nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre"
    ),
    (
        "Enable optional interactive collaboration with a COMSOL Desktop connected to a "
        "local Server. This feature composes with every tool profile."
    ),
    "Check that execution results and scientific conclusions are reported separately.",
    "Check saved result files and their hashes.",
    "Check that summary statements match the saved result values.",
    "Check that a resumed job uses the same producer and driver.",
    "Manual sources and lexical search",
    "Optional semantic search",
    (
        "Enable local keyword search for installed or copied PDF manuals. "
        "This is off by default because COMSOL manuals may not be installed."
    ),
    (
        "Folder scanned recursively when generating the manual index. "
        "Choose the folder containing the PDF manuals."
    ),
    (
        "SQLite FTS5/BM25 index used by manual_search. Enter an ASCII-only SQLite "
        "file or destination folder. A folder uses lexical_manuals.sqlite3."
    ),
    (
        "Enable optional semantic manual-search tools for the selected profile. "
        "This feature requires prepared local indexes and a search model."
    ),
    (
        "Optional folder containing the prepared semantic vector index. "
        "It is independent from the SQLite lexical index."
    ),
    (
        "Optional folder containing the local semantic-search model. "
        "\nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\models"
    ),
    "Optional name that identifies who owns the COMSOL session.",
    "Enter a valid absolute path or clear this setting.",
    "Enter an ASCII-only full path, or leave this setting empty.",
    "Enter a valid absolute path.",
    "Enter a valid value for this setting.",
    "Required setting is missing.",
    "Unknown setting is not supported.",
    "Disable evidence check?",
    "Disabling an evidence check makes future results not fully verified. Continue?",
    "Restart required",
    "Invalid settings",
    "Correct every highlighted field before saving.",
    "Save failed",
    "Settings were not saved. Close conflicting editors and try again.",
    "Discard changes?",
    "Close without saving the current edits?",
    "The configured COMSOL root is not a supported 6.4 installation.",
    "Multiple COMSOL 6.4 installations were found. Choose one manually.",
    "No supported COMSOL 6.4 installation was found.",
    "Replace existing values?",
    "Auto-detect would replace these settings: {keys}",
    "Settings conflict",
    "The settings file changed outside this editor. Close this window and reopen settings.",
    "The selected language catalog is unavailable; English is active.",
    (
        "No writable settings file exists. Rebuild canonical settings now?\n\n"
        "Choose No to exit without writing."
    ),
    "Another settings editor is active. Close it and reopen settings.",
    "Settings could not be prepared safely.",
    "Settings are missing or damaged. Preserve them and rebuild defaults?",
    "Generate Index",
    "Generate manual index",
    "Starting index worker...",
    "Cancelling safely...",
    "{percent}% — {files}/{total_files} PDFs, {pages}/{total_pages} pages{source}",
    "Replace existing index?",
    "A validated new index will atomically replace the existing file. Continue?",
    "Choose both the PDF folder and SQLite index file first.",
    "Index generation could not start. Check the selected paths and permissions.",
    "Manual index ready",
    "Indexed {pdfs} PDFs and {pages} pages. Corpus fingerprint: {fingerprint}",
    "Index generation failed",
    "The index worker stopped without a completion receipt.",
    "Index generation cancelled",
    "Temporary files were removed and the previous index was preserved.",
    "Manual search is not ready",
    "Choose or generate a valid SQLite manual index before enabling search.",
    "Semantic search is not ready",
    "Enable manual search and choose valid lexical, semantic-index, and model assets first.",
)


class Translator:
    def __init__(self, language: str) -> None:
        normalized = language.casefold().replace("_", "-")
        self.language = normalized if normalized in GUI_LANGUAGES else "en"
        self.warning: str | None = None
        try:
            self._catalog = gettext.translation(
                DOMAIN,
                localedir=LOCALE_ROOT,
                languages=[LANGUAGE_LOCALES[self.language]],
                fallback=False,
            )
        except FileNotFoundError, OSError, struct.error:
            self._catalog = gettext.NullTranslations()
            if self.language != "en":
                self.warning = "The selected language catalog is unavailable; English is active."

    def get(self, message: str) -> str:
        return self._catalog.gettext(message)

    __call__ = get


def language_option_labels(translator: Translator) -> dict[str, str]:
    return {key: f"{translator(LANGUAGE_SELF_NAMES[key])} ({key})" for key in GUI_LANGUAGES}


def scale_option_labels(translator: Translator) -> dict[str, str]:
    return {
        key: (
            f"{translator('Follow Windows display settings')} (system)"
            if key == "system"
            else f"{key}%"
        )
        for key in GUI_SCALES
    }


__all__ = [
    "LANGUAGE_LOCALES",
    "LANGUAGE_SELF_NAMES",
    "LOCALE_ROOT",
    "MESSAGE_IDS",
    "Translator",
    "language_option_labels",
    "scale_option_labels",
]
