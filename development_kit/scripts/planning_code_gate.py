"""Freeze and verify the exact historical planning-code compatibility surface."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping

TEXT_SUFFIXES = frozenset({".json", ".md", ".py", ".toml", ".yaml", ".yml"})
_LEGACY_TOKENS = (
    "h" + "1",
    "h" + "2a",
    "h" + "2d",
    "h" + "2f",
    "h" + "3c",
    "h" + "3d",
    "h" + "3e",
    "h" + "3f",
    "h" + "4a",
    "h" + "4b",
    "h" + "4c",
    "h" + "4d",
    "h" + "4e",
    "h" + "4f",
    "e" + "4r",
)
_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[EMH][0-9]+[A-Za-z]?|P(?:3|4|9|10|11|12))(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:" + "|".join(_LEGACY_TOKENS) + r")(?![A-Za-z0-9])"
)


def _matches(text: str) -> list[dict[str, object]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return [{"token": match.group(), "start": match.start()} for match in _PATTERN.finditer(text)]


def _fingerprint(matches: list[dict[str, object]]) -> str:
    payload = json.dumps(matches, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_planning_code_allowlist(path: str | Path) -> dict[str, dict[str, object]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_name", "schema_version", "entries"}
        or value.get("schema_name") != "comsol_mcp.planning_code_allowlist"
        or value.get("schema_version") != "1.0.0"
        or not isinstance(value.get("entries"), list)
    ):
        raise ValueError("planning-code allowlist schema is invalid")
    entries: dict[str, dict[str, object]] = {}
    for item in value["entries"]:
        if not isinstance(item, dict) or set(item) != {"path", "match_count", "match_sha256"}:
            raise ValueError("planning-code allowlist entry fields are invalid")
        path_text = item["path"]
        count = item["match_count"]
        digest = item["match_sha256"]
        if (
            not isinstance(path_text, str)
            or not path_text
            or path_text in entries
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("planning-code allowlist entry is invalid")
        entries[path_text] = {"match_count": count, "match_sha256": digest}
    return entries


def verify_planning_code_texts(
    texts: Mapping[str, str],
    *,
    allowlist: Mapping[str, Mapping[str, object]],
    require_all_allowlisted: bool,
) -> dict[str, object]:
    actual: dict[str, dict[str, object]] = {}
    for path in sorted(texts):
        matches = _matches(texts[path])
        if matches:
            actual[path] = {
                "match_count": len(matches),
                "match_sha256": _fingerprint(matches),
            }
    unexpected = sorted(set(actual) - set(allowlist))
    missing = sorted(set(allowlist) - set(actual)) if require_all_allowlisted else []
    mismatched = sorted(
        path for path in set(actual) & set(allowlist) if dict(actual[path]) != dict(allowlist[path])
    )
    if unexpected or missing or mismatched:
        raise RuntimeError(
            "planning-code compatibility surface changed: "
            f"unexpected={unexpected}, missing={missing}, mismatched={mismatched}"
        )
    receipt = {
        "verified": True,
        "scanned_text_file_count": len(texts),
        "matched_file_count": len(actual),
        "matched_occurrence_count": sum(int(item["match_count"]) for item in actual.values()),
        "exact_allowlist_required": require_all_allowlisted,
    }
    return receipt


__all__ = [
    "TEXT_SUFFIXES",
    "load_planning_code_allowlist",
    "verify_planning_code_texts",
]
