"""Complete field descriptors and form-state validation."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

from comsol_mcp.settings import GUI_LANGUAGES, GUI_SCALES, normalize_settings_document
from comsol_mcp.tools.profiles import PROFILE_NAMES

FieldKind = Literal["readonly", "choice", "boolean", "directory", "file", "roots", "text"]
ASCII_PATH_FIELDS = frozenset(
    {"runtime.directory", "runtime.jobs_directory", "paths.artifact_write_root"}
)
PROFILE_HELP_IDS = {
    "core": (
        "Safety-first default for new users. It offers fewer operations, which lowers the "
        "risk of an unintended change. Choose it while learning or when you only need to "
        "open and inspect models, manage jobs, run careful single-point checks, or search manuals."
    ),
    "basic_fem": (
        "Recommended for most users. It covers ordinary FEM model building and result export, "
        "and includes tools for making a Windows standalone package. Choose it for general "
        "simulation work that does not need a specialist profile."
    ),
    "wave_optics": (
        "For optical and metasurface work. Adds materials, field review, Wave Optics checks, "
        "point audits, and staged parameter workflows to Core."
    ),
    "semantic_docs": (
        "For searching prepared local COMSOL manuals with text and meaning-based search. "
        "Choose it only after the optional manual indexes and search model have been prepared."
    ),
    "desktop_shared": (
        "For users who want to watch the same model in COMSOL Desktop while MCP takes a turn "
        "through a local Server. Choose it only after Desktop and Server are connected and "
        "shared mode is enabled."
    ),
    "experimental": (
        "For testing extra helpers that are broader or less mature. Use it only when a required "
        "tool is missing from the safer profiles, and check every output carefully."
    ),
    "full": (
        "For old workflows that need nearly every tool. It keeps older broad path behavior and "
        "has weaker file containment. New users should not choose it."
    ),
}
GUI_SCALE_HELP_ID = (
    "Size of text and controls. Following Windows is recommended. "
    "Other choices are previewed immediately and saved for the next opening."
)


@dataclass(frozen=True)
class FieldDescriptor:
    key: str
    tab: str
    kind: FieldKind
    nullable: bool = False
    choices: tuple[str, ...] = ()
    help_id: str = ""


FIELDS = (
    FieldDescriptor(
        "schema_name",
        "general",
        "readonly",
        help_id="Internal settings format name. You do not need to change it.",
    ),
    FieldDescriptor(
        "schema_version",
        "general",
        "readonly",
        help_id="Settings format version used when this file is saved.",
    ),
    FieldDescriptor(
        "gui.language",
        "general",
        "choice",
        choices=GUI_LANGUAGES,
        help_id="Language used by this Settings window.",
    ),
    FieldDescriptor(
        "gui.scale",
        "general",
        "choice",
        choices=GUI_SCALES,
        help_id=GUI_SCALE_HELP_ID,
    ),
    FieldDescriptor(
        "profile.name",
        "profile",
        "choice",
        choices=tuple(PROFILE_NAMES),
        help_id=PROFILE_HELP_IDS["core"],
    ),
    FieldDescriptor(
        "runtime.directory",
        "runtime",
        "directory",
        True,
        help_id=(
            "Folder for working files and locks. Use an ASCII-only path. "
            "\nExample: %PROGRAMDATA%\\comsol_mcp\\runtime"
        ),
    ),
    FieldDescriptor(
        "runtime.jobs_directory",
        "runtime",
        "directory",
        True,
        help_id=(
            "Optional separate folder for resumable jobs. "
            "Leave it empty to use the runtime folder. "
            "\nExample: %PROGRAMDATA%\\comsol_mcp\\runtime\\jobs"
        ),
    ),
    FieldDescriptor(
        "paths.model_read_roots",
        "runtime",
        "roots",
        help_id=(
            "Folders where COMSOL MCP may read your .mph files. Chinese paths are supported. "
            "\nExample: %LOCALAPPDATA%\\comsol_mcp\\models"
        ),
    ),
    FieldDescriptor(
        "paths.artifact_write_root",
        "runtime",
        "directory",
        True,
        help_id=(
            "Folder where COMSOL MCP writes results and evidence. Use an ASCII-only path. "
            "\nExample: %PROGRAMDATA%\\comsol_mcp\\artifacts"
        ),
    ),
    FieldDescriptor(
        "comsol.installation_root",
        "comsol_java",
        "directory",
        True,
        help_id=(
            "Folder where COMSOL Multiphysics 6.4 is installed. "
            "\nExample: C:\\COMSOL64\\Multiphysics"
        ),
    ),
    FieldDescriptor(
        "java.java_home",
        "comsol_java",
        "directory",
        True,
        help_id=(
            "Java folder used by COMSOL. Auto-detect can fill this value. "
            "\nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre"
        ),
    ),
    FieldDescriptor(
        "java.jdk_home",
        "comsol_java",
        "directory",
        True,
        help_id=(
            "JDK folder used by COMSOL. Auto-detect can fill this value. "
            "\nExample: C:\\COMSOL64\\Multiphysics\\java\\win64\\jre"
        ),
    ),
    FieldDescriptor(
        "shared_server.enabled",
        "shared_server",
        "boolean",
        help_id="Allow MCP to work with a COMSOL Desktop connected to a local Server.",
    ),
    FieldDescriptor(
        "evidence_integrity.checks.outcome_contract_validation",
        "evidence",
        "boolean",
        help_id="Check that execution results and scientific conclusions are reported separately.",
    ),
    FieldDescriptor(
        "evidence_integrity.checks.artifact_chain_verification",
        "evidence",
        "boolean",
        help_id="Check saved result files and their hashes.",
    ),
    FieldDescriptor(
        "evidence_integrity.checks.summary_claim_verification",
        "evidence",
        "boolean",
        help_id="Check that summary statements match the saved result values.",
    ),
    FieldDescriptor(
        "evidence_integrity.checks.producer_driver_compatibility",
        "evidence",
        "boolean",
        help_id="Check that a resumed job uses the same producer and driver.",
    ),
    FieldDescriptor(
        "semantic_docs.root",
        "semantic",
        "directory",
        True,
        help_id=(
            "Optional folder containing prepared semantic-search files. "
            "This is not COMSOL's built-in manual folder. "
            "\nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals"
        ),
    ),
    FieldDescriptor(
        "semantic_docs.lexical_index",
        "semantic",
        "file",
        True,
        help_id=(
            "Optional SQLite file used for manual text search. "
            "\nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\manuals.sqlite3"
        ),
    ),
    FieldDescriptor(
        "semantic_docs.model_path",
        "semantic",
        "directory",
        True,
        help_id=(
            "Optional folder containing the local semantic-search model. "
            "\nExample: %LOCALAPPDATA%\\comsol_mcp\\semantic\\models"
        ),
    ),
    FieldDescriptor(
        "ownership.owner",
        "ownership",
        "text",
        True,
        help_id="Optional name that identifies who owns the COMSOL session.",
    ),
)
FIELD_BY_KEY = {field.key: field for field in FIELDS}
TAB_IDS = (
    "general",
    "profile",
    "runtime",
    "comsol_java",
    "shared_server",
    "evidence",
    "semantic",
    "ownership",
    "about",
)


def _field_error_message(key: str) -> str:
    field = FIELD_BY_KEY.get(key)
    if field is None:
        return "Enter a valid value for this setting."
    if key in ASCII_PATH_FIELDS:
        return "Enter an ASCII-only full path, or leave this setting empty."
    if field.kind in {"directory", "file", "roots"}:
        return (
            "Enter a valid absolute path or clear this setting."
            if field.nullable
            else "Enter a valid absolute path."
        )
    return "Enter a valid value for this setting."


def get_value(document: dict[str, Any], dotted: str) -> Any:
    value: Any = document
    for part in dotted.split("."):
        value = value[part]
    return value


def set_value(document: dict[str, Any], dotted: str, value: Any) -> None:
    target = document
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def leaf_keys(document: dict[str, Any], prefix: str = "") -> set[str]:
    result: set[str] = set()
    for key, value in document.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            result.update(leaf_keys(value, dotted))
        else:
            result.add(dotted)
    return result


def field_key_for_error(path: str) -> str | None:
    key = re.sub(r"\[\d+\]", "", path.removeprefix("settings."))
    return key if key in FIELD_BY_KEY else None


def profile_help_id(profile: Any) -> str:
    return PROFILE_HELP_IDS.get(str(profile), PROFILE_HELP_IDS["core"])


class SettingsFormModel:
    """Preserve raw values while exposing canonical validity and dirty state."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = deepcopy(document)
        self.baseline = deepcopy(document)
        self.errors: dict[str, str] = {}
        self.canonical: dict[str, Any] | None = None
        self.validate()

    @classmethod
    def from_raw(cls, document: dict[str, Any]) -> "SettingsFormModel":
        report = normalize_settings_document(document)
        merged = deepcopy(report["settings"])
        for field in FIELDS:
            try:
                raw_value = get_value(document, field.key)
            except KeyError, TypeError:
                continue
            if field.key == "gui.language" and isinstance(raw_value, str):
                normalized = raw_value.casefold().replace("_", "-")
                if normalized in GUI_LANGUAGES:
                    raw_value = normalized
            if field.kind in {"directory", "file"} and isinstance(raw_value, str):
                if raw_value.casefold().startswith(("%localappdata%", "%programdata%")):
                    raw_value = get_value(merged, field.key)
            if field.kind == "roots" and isinstance(raw_value, list):
                if any(
                    isinstance(item, str)
                    and item.casefold().startswith(("%localappdata%", "%programdata%"))
                    for item in raw_value
                ):
                    raw_value = get_value(merged, field.key)
            set_value(merged, field.key, deepcopy(raw_value))
        for key, value in document.items():
            if key not in merged and not key.startswith("_comment"):
                merged[key] = deepcopy(value)
        return cls(merged)

    @property
    def dirty(self) -> bool:
        return self.document != self.baseline

    @property
    def valid(self) -> bool:
        return not self.errors and self.canonical is not None

    @property
    def language(self) -> str:
        value = get_value(self.document, "gui.language")
        normalized = value.casefold().replace("_", "-") if isinstance(value, str) else ""
        return normalized if normalized in GUI_LANGUAGES else "en"

    @property
    def scale(self) -> str:
        value = get_value(self.document, "gui.scale")
        return value if isinstance(value, str) and value in GUI_SCALES else "system"

    def update(self, key: str, value: Any) -> None:
        if key not in FIELD_BY_KEY:
            raise KeyError(key)
        if FIELD_BY_KEY[key].kind == "readonly":
            raise ValueError("read-only settings cannot be changed")
        set_value(self.document, key, value)
        self.validate()

    def validate(self) -> None:
        report = normalize_settings_document(self.document)
        self.errors = {}
        for item in report["errors"]:
            key = field_key_for_error(item["path"]) or item["path"].removeprefix("settings.")
            self.errors[key] = _field_error_message(key)
        expected = set(FIELD_BY_KEY)
        observed = leaf_keys(self.document)
        missing = expected - observed
        unknown = observed - expected
        for key in sorted(missing):
            self.errors[key] = "Required setting is missing."
        for key in sorted(unknown):
            self.errors[key] = "Unknown setting is not supported."
        self.canonical = report["settings"] if not self.errors else None

    def mark_saved(self) -> None:
        if self.canonical is None:
            raise ValueError("invalid settings cannot become the baseline")
        self.document = deepcopy(self.canonical)
        self.baseline = deepcopy(self.canonical)
        self.validate()


__all__ = [
    "FIELDS",
    "FIELD_BY_KEY",
    "GUI_SCALE_HELP_ID",
    "PROFILE_HELP_IDS",
    "TAB_IDS",
    "FieldDescriptor",
    "SettingsFormModel",
    "field_key_for_error",
    "get_value",
    "leaf_keys",
    "profile_help_id",
    "set_value",
]
