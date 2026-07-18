"""One validated project settings file for every MCP startup configuration."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from src.durable import canonical_sha256_v1


SETTINGS_PATH_ENV = "COMSOL_MCP_SETTINGS_PATH"
SETTINGS_SCHEMA = "comsol_mcp.settings"
SETTINGS_VERSION = "1.0.0"
MAX_SETTINGS_BYTES = 64 * 1024

PROFILE_ENV = "COMSOL_MCP_PROFILE"
RUNTIME_ENV = "COMSOL_MCP_RUNTIME_DIR"
JOBS_ENV = "COMSOL_MCP_JOBS_DIR"
MODEL_READ_ROOTS_ENV = "COMSOL_MCP_MODEL_READ_ROOTS"
ARTIFACT_WRITE_ROOT_ENV = "COMSOL_MCP_ARTIFACT_WRITE_ROOT"
SHARED_SERVER_ENV = "COMSOL_MCP_ENABLE_SHARED_SERVER"
OWNER_ENV = "COMSOL_MCP_OWNER"
SEMANTIC_ROOT_ENV = "COMSOL_SEMANTIC_ROOT"
SEMANTIC_LEXICAL_ENV = "COMSOL_SEMANTIC_LEXICAL_INDEX"
SEMANTIC_MODEL_ENV = "COMSOL_SEMANTIC_MODEL_PATH"
JAVA_HOME_ENV = "JAVA_HOME"
JDK_HOME_ENV = "JDK_HOME"

_PROFILE_NAMES = frozenset(
    {
        "core",
        "basic_fem",
        "wave_optics",
        "semantic_docs",
        "desktop_shared",
        "experimental",
        "full",
    }
)
_EVIDENCE_CHECKS = (
    "outcome_contract_validation",
    "artifact_chain_verification",
    "summary_claim_verification",
    "producer_driver_compatibility",
)
_LEGACY_ENVIRONMENT_NAMES = frozenset(
    {
        PROFILE_ENV,
        RUNTIME_ENV,
        JOBS_ENV,
        MODEL_READ_ROOTS_ENV,
        ARTIFACT_WRITE_ROOT_ENV,
        SHARED_SERVER_ENV,
        OWNER_ENV,
        SEMANTIC_ROOT_ENV,
        SEMANTIC_LEXICAL_ENV,
        SEMANTIC_MODEL_ENV,
    }
)
_COMMENT_PREFIX = "_comment"

_DEFAULT_SETTINGS = {
    "schema_name": SETTINGS_SCHEMA,
    "schema_version": SETTINGS_VERSION,
    "profile": {"name": "core"},
    "runtime": {"directory": None, "jobs_directory": None},
    "paths": {"model_read_roots": [], "artifact_write_root": None},
    "shared_server": {"enabled": False},
    "evidence_integrity": {
        "checks": {name: True for name in _EVIDENCE_CHECKS},
    },
    "semantic_docs": {
        "root": "D:/comsol_semantic",
        "lexical_index": "D:/comsol_docs_fts/manuals.sqlite3",
        "model_path": None,
    },
    "ownership": {"owner": None},
    "java": {"java_home": None, "jdk_home": None},
}


class SettingsError(ValueError):
    """Raised when a settings file path or JSON document cannot be trusted."""

    def __init__(self, message: str, *, reason_code: str = "settings_invalid") -> None:
        self.reason_code = reason_code
        super().__init__(message)


class _DuplicateJsonKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _is_comment_key(key: str) -> bool:
    return key.startswith(_COMMENT_PREFIX)


def _record_error(
    errors: list[dict[str, str]], location: str, error: Exception
) -> None:
    errors.append(
        {
            "path": location,
            "reason_code": getattr(error, "reason_code", "settings_invalid"),
            "error_type": type(error).__name__,
            "message": str(error)[:512],
        }
    )


def _validate_comment(value: Any, *, location: str) -> None:
    if not isinstance(value, str) or len(value) > 2048:
        raise SettingsError(f"{location} must be a bounded English comment string")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SettingsError(f"{location} must contain English/ASCII text") from exc
    if any(ord(character) < 32 and character not in "\t\r\n" for character in value):
        raise SettingsError(f"{location} contains an illegal control character")


def _validate_comments(value: Any, *, location: str = "settings") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_comment_key(key):
                _validate_comment(item, location=f"{location}.{key}")
            else:
                _validate_comments(item, location=f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_comments(item, location=f"{location}[{index}]")


def _object(
    value: Any,
    *,
    location: str,
    defaults: Mapping[str, Any],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        _record_error(errors, location, SettingsError(f"{location} must be a JSON object"))
        return deepcopy(dict(defaults))
    actual = {key for key in value if not _is_comment_key(key)}
    unknown = sorted(actual - set(defaults))
    for key in unknown:
        _record_error(
            errors,
            f"{location}.{key}",
            SettingsError(
                f"{location}.{key} is unknown",
                reason_code="settings_unknown_field",
            ),
        )
    return {
        key: value[key] if key in value else deepcopy(defaults[key])
        for key in defaults
    }


def _read_value(
    value: Any,
    *,
    location: str,
    default: Any,
    parser: Callable[[Any], Any],
    errors: list[dict[str, str]],
) -> Any:
    try:
        return parser(value)
    except (SettingsError, TypeError, ValueError) as exc:
        _record_error(errors, location, exc)
        return deepcopy(default)


def _absolute_string(value: Any, *, location: str, allow_none: bool) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"{location} must be an absolute path or null")
    if any(ord(character) < 32 for character in value):
        raise SettingsError(
            f"{location} contains an illegal control character",
            reason_code="settings_value_invalid",
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SettingsError(f"{location} must be an absolute path")
    return str(path)


def _parse_profile(value: Any) -> str:
    if not isinstance(value, str) or value.strip().casefold() not in _PROFILE_NAMES:
        raise SettingsError(
            f"settings.profile.name must be one of {sorted(_PROFILE_NAMES)}",
            reason_code="settings_value_invalid",
        )
    return value.strip().casefold()


def _parse_bool(value: Any, *, location: str) -> bool:
    if not isinstance(value, bool):
        raise SettingsError(
            f"{location} must be a JSON boolean",
            reason_code="settings_value_invalid",
        )
    return value


def _parse_roots(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SettingsError(
            "settings.paths.model_read_roots must be a JSON string list",
            reason_code="settings_value_invalid",
        )
    result: list[str] = []
    for index, item in enumerate(value):
        normalized = _absolute_string(
            item,
            location=f"settings.paths.model_read_roots[{index}]",
            allow_none=False,
        )
        if normalized is None:
            raise SettingsError("model read roots cannot contain null values")
        result.append(normalized)
    if len({os.path.normcase(item) for item in result}) != len(result):
        raise SettingsError(
            "settings.paths.model_read_roots must not contain duplicates",
            reason_code="settings_value_invalid",
        )
    return result


def _parse_owner(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise SettingsError(
            "settings.ownership.owner must be a bounded string or null",
            reason_code="settings_value_invalid",
        )
    return value.strip()


def _normalize(
    document: Any,
    *,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    if not isinstance(document, dict):
        _record_error(
            errors,
            "settings",
            SettingsError("settings.json must contain a JSON object"),
        )
        document = {}
    try:
        _validate_comments(document)
    except SettingsError as exc:
        _record_error(errors, "settings.comments", exc)

    top = _object(document, location="settings", defaults=_DEFAULT_SETTINGS, errors=errors)
    schema_name = _read_value(
        top["schema_name"],
        location="settings.schema_name",
        default=SETTINGS_SCHEMA,
        parser=lambda value: value
        if value == SETTINGS_SCHEMA
        else (_ for _ in ()).throw(
            SettingsError("settings.schema_name is unsupported", reason_code="settings_value_invalid")
        ),
        errors=errors,
    )
    schema_version = _read_value(
        top["schema_version"],
        location="settings.schema_version",
        default=SETTINGS_VERSION,
        parser=lambda value: value
        if value == SETTINGS_VERSION
        else (_ for _ in ()).throw(
            SettingsError("settings.schema_version is unsupported", reason_code="settings_value_invalid")
        ),
        errors=errors,
    )

    profile = _object(
        top["profile"],
        location="settings.profile",
        defaults=_DEFAULT_SETTINGS["profile"],
        errors=errors,
    )
    name = _read_value(
        profile["name"],
        location="settings.profile.name",
        default=_DEFAULT_SETTINGS["profile"]["name"],
        parser=_parse_profile,
        errors=errors,
    )

    runtime = _object(
        top["runtime"],
        location="settings.runtime",
        defaults=_DEFAULT_SETTINGS["runtime"],
        errors=errors,
    )
    runtime_dir = _read_value(
        runtime["directory"],
        location="settings.runtime.directory",
        default=_DEFAULT_SETTINGS["runtime"]["directory"],
        parser=lambda value: _absolute_string(
            value, location="settings.runtime.directory", allow_none=True
        ),
        errors=errors,
    )
    jobs_dir = _read_value(
        runtime["jobs_directory"],
        location="settings.runtime.jobs_directory",
        default=_DEFAULT_SETTINGS["runtime"]["jobs_directory"],
        parser=lambda value: _absolute_string(
            value, location="settings.runtime.jobs_directory", allow_none=True
        ),
        errors=errors,
    )

    paths = _object(
        top["paths"],
        location="settings.paths",
        defaults=_DEFAULT_SETTINGS["paths"],
        errors=errors,
    )
    normalized_roots = _read_value(
        paths["model_read_roots"],
        location="settings.paths.model_read_roots",
        default=_DEFAULT_SETTINGS["paths"]["model_read_roots"],
        parser=_parse_roots,
        errors=errors,
    )
    artifact_root = _read_value(
        paths["artifact_write_root"],
        location="settings.paths.artifact_write_root",
        default=_DEFAULT_SETTINGS["paths"]["artifact_write_root"],
        parser=lambda value: _absolute_string(
            value, location="settings.paths.artifact_write_root", allow_none=True
        ),
        errors=errors,
    )

    shared = _object(
        top["shared_server"],
        location="settings.shared_server",
        defaults=_DEFAULT_SETTINGS["shared_server"],
        errors=errors,
    )
    shared_enabled = _read_value(
        shared["enabled"],
        location="settings.shared_server.enabled",
        default=_DEFAULT_SETTINGS["shared_server"]["enabled"],
        parser=lambda value: _parse_bool(value, location="settings.shared_server.enabled"),
        errors=errors,
    )

    evidence = _object(
        top["evidence_integrity"],
        location="settings.evidence_integrity",
        defaults=_DEFAULT_SETTINGS["evidence_integrity"],
        errors=errors,
    )
    checks = _object(
        evidence["checks"],
        location="settings.evidence_integrity.checks",
        defaults=_DEFAULT_SETTINGS["evidence_integrity"]["checks"],
        errors=errors,
    )
    normalized_checks = {
        name: _read_value(
            checks[name],
            location=f"settings.evidence_integrity.checks.{name}",
            default=True,
            parser=lambda value, name=name: _parse_bool(
                value,
                location=f"settings.evidence_integrity.checks.{name}",
            ),
            errors=errors,
        )
        for name in _EVIDENCE_CHECKS
    }

    semantic = _object(
        top["semantic_docs"],
        location="settings.semantic_docs",
        defaults=_DEFAULT_SETTINGS["semantic_docs"],
        errors=errors,
    )
    semantic_root = _read_value(
        semantic["root"],
        location="settings.semantic_docs.root",
        default=_DEFAULT_SETTINGS["semantic_docs"]["root"],
        parser=lambda value: _absolute_string(
            value, location="settings.semantic_docs.root", allow_none=False
        ),
        errors=errors,
    )
    lexical_index = _read_value(
        semantic["lexical_index"],
        location="settings.semantic_docs.lexical_index",
        default=_DEFAULT_SETTINGS["semantic_docs"]["lexical_index"],
        parser=lambda value: _absolute_string(
            value, location="settings.semantic_docs.lexical_index", allow_none=False
        ),
        errors=errors,
    )
    model_path = _read_value(
        semantic["model_path"],
        location="settings.semantic_docs.model_path",
        default=_DEFAULT_SETTINGS["semantic_docs"]["model_path"],
        parser=lambda value: _absolute_string(
            value, location="settings.semantic_docs.model_path", allow_none=True
        ),
        errors=errors,
    )

    ownership = _object(
        top["ownership"],
        location="settings.ownership",
        defaults=_DEFAULT_SETTINGS["ownership"],
        errors=errors,
    )
    owner = _read_value(
        ownership["owner"],
        location="settings.ownership.owner",
        default=_DEFAULT_SETTINGS["ownership"]["owner"],
        parser=_parse_owner,
        errors=errors,
    )

    java = _object(
        top["java"],
        location="settings.java",
        defaults=_DEFAULT_SETTINGS["java"],
        errors=errors,
    )
    java_home = _read_value(
        java["java_home"],
        location="settings.java.java_home",
        default=_DEFAULT_SETTINGS["java"]["java_home"],
        parser=lambda value: _absolute_string(
            value, location="settings.java.java_home", allow_none=True
        ),
        errors=errors,
    )
    jdk_home = _read_value(
        java["jdk_home"],
        location="settings.java.jdk_home",
        default=_DEFAULT_SETTINGS["java"]["jdk_home"],
        parser=lambda value: _absolute_string(
            value, location="settings.java.jdk_home", allow_none=True
        ),
        errors=errors,
    )

    return {
        "schema_name": schema_name,
        "schema_version": schema_version,
        "profile": {"name": name},
        "runtime": {"directory": runtime_dir, "jobs_directory": jobs_dir},
        "paths": {
            "model_read_roots": normalized_roots,
            "artifact_write_root": artifact_root,
        },
        "shared_server": {"enabled": shared_enabled},
        "evidence_integrity": {"checks": normalized_checks},
        "semantic_docs": {
            "root": semantic_root,
            "lexical_index": lexical_index,
            "model_path": model_path,
        },
        "ownership": {"owner": owner},
        "java": {"java_home": java_home, "jdk_home": jdk_home},
    }


def _validate_file(path: Path) -> Path:
    if not path.is_absolute():
        raise SettingsError(f"{SETTINGS_PATH_ENV} must be an absolute path")
    resolved = path.resolve(strict=True)
    is_junction = getattr(path, "is_junction", lambda: False)
    if path.is_symlink() or is_junction() or not resolved.is_file():
        raise SettingsError("settings.json must be a regular non-link file")
    return resolved


def default_settings_path(environ: Mapping[str, str] | None = None) -> Path:
    """Locate the shared root settings file or the bundled wheel copy."""
    environment = os.environ if environ is None else environ
    configured = environment.get(SETTINGS_PATH_ENV)
    if configured:
        return _validate_file(Path(configured))
    source_root = Path(__file__).resolve().parents[1] / "settings.json"
    if source_root.is_file():
        return _validate_file(source_root)
    bundled = Path(__file__).resolve().parent / "settings.json"
    return _validate_file(bundled)


def _report_error(error: Exception, *, location: str = "settings") -> dict[str, str]:
    return {
        "path": location,
        "reason_code": getattr(error, "reason_code", "settings_invalid"),
        "error_type": type(error).__name__,
        "message": str(error)[:512],
    }


def load_settings_report(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load settings and return safe defaults plus bounded validation errors."""
    try:
        path = default_settings_path(environ)
        raw = path.read_bytes()
    except (OSError, RuntimeError, SettingsError) as exc:
        return {
            "settings": deepcopy(_DEFAULT_SETTINGS),
            "errors": [_report_error(exc)],
        }
    if not raw or len(raw) > MAX_SETTINGS_BYTES:
        error = SettingsError(
            f"settings.json must contain 1..{MAX_SETTINGS_BYTES} bytes",
            reason_code="settings_size_invalid",
        )
        return {"settings": deepcopy(_DEFAULT_SETTINGS), "errors": [_report_error(error)]}
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except UnicodeDecodeError as exc:
        error = SettingsError("settings.json must be UTF-8", reason_code="settings_encoding_invalid")
        return {"settings": deepcopy(_DEFAULT_SETTINGS), "errors": [_report_error(error)]}
    except (json.JSONDecodeError, _DuplicateJsonKey) as exc:
        error = SettingsError(
            "settings.json contains invalid or duplicate JSON",
            reason_code="settings_json_invalid",
        )
        return {"settings": deepcopy(_DEFAULT_SETTINGS), "errors": [_report_error(error)]}
    errors: list[dict[str, str]] = []
    settings = _normalize(document, errors=errors)
    return {"settings": settings, "errors": errors}


def load_settings(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load normalized settings; invalid entries use defaults and remain reportable."""
    return load_settings_report(environ)["settings"]


def settings_fingerprint(settings: Mapping[str, Any] | None = None) -> str:
    """Return the canonical settings hash without exposing local paths."""
    value = dict(settings) if settings is not None else load_settings()
    return canonical_sha256_v1(value)


def settings_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return bounded settings state, including any default-fallback errors."""
    report = load_settings_report(environ)
    settings = report["settings"]
    errors = report["errors"]
    result = {
        "success": True,
        "schema_name": SETTINGS_SCHEMA,
        "schema_version": SETTINGS_VERSION,
        "configuration_state": "degraded" if errors else "valid",
        "settings_fingerprint_sha256": settings_fingerprint(settings),
        "settings_path_included": False,
        "settings_path_environment_variable": SETTINGS_PATH_ENV,
        "defaults_used_for_invalid_or_missing_entries": bool(errors),
        "settings_errors": errors,
        "legacy_environment_overrides_supported": True,
        "legacy_environment_overrides_documented": True,
    }
    if errors:
        result["reason_code"] = errors[0]["reason_code"]
    return result


def settings_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return effective legacy-shaped values with project settings as defaults."""
    base = dict(os.environ if environ is None else environ)
    if environ is not None and _LEGACY_ENVIRONMENT_NAMES.intersection(base):
        return base
    settings = load_settings(environ)

    def set_default(name: str, value: str | None) -> None:
        if value is not None and not base.get(name):
            base[name] = value

    set_default(PROFILE_ENV, settings["profile"]["name"])
    set_default(SHARED_SERVER_ENV, str(settings["shared_server"]["enabled"]).lower())
    set_default(RUNTIME_ENV, settings["runtime"]["directory"])
    set_default(JOBS_ENV, settings["runtime"]["jobs_directory"])
    roots = settings["paths"]["model_read_roots"]
    set_default(MODEL_READ_ROOTS_ENV, os.pathsep.join(roots) if roots else None)
    set_default(ARTIFACT_WRITE_ROOT_ENV, settings["paths"]["artifact_write_root"])
    set_default(OWNER_ENV, settings["ownership"]["owner"])
    set_default(SEMANTIC_ROOT_ENV, settings["semantic_docs"]["root"])
    set_default(SEMANTIC_LEXICAL_ENV, settings["semantic_docs"]["lexical_index"])
    set_default(SEMANTIC_MODEL_ENV, settings["semantic_docs"]["model_path"])
    set_default(JAVA_HOME_ENV, settings["java"]["java_home"])
    set_default(JDK_HOME_ENV, settings["java"]["jdk_home"])
    return base


def apply_java_settings(environ: Mapping[str, str] | None = None) -> None:
    """Apply only configured Java paths before a clientapi import/connection."""
    effective = settings_environment(environ)
    for name in (JAVA_HOME_ENV, JDK_HOME_ENV):
        value = effective.get(name)
        if value:
            os.environ[name] = value


__all__ = [
    "ARTIFACT_WRITE_ROOT_ENV",
    "JOBS_ENV",
    "JAVA_HOME_ENV",
    "JDK_HOME_ENV",
    "MODEL_READ_ROOTS_ENV",
    "OWNER_ENV",
    "PROFILE_ENV",
    "RUNTIME_ENV",
    "SEMANTIC_LEXICAL_ENV",
    "SEMANTIC_MODEL_ENV",
    "SEMANTIC_ROOT_ENV",
    "SETTINGS_PATH_ENV",
    "SETTINGS_SCHEMA",
    "SETTINGS_VERSION",
    "SHARED_SERVER_ENV",
    "SettingsError",
    "apply_java_settings",
    "default_settings_path",
    "load_settings",
    "load_settings_report",
    "settings_environment",
    "settings_fingerprint",
    "settings_status",
]
