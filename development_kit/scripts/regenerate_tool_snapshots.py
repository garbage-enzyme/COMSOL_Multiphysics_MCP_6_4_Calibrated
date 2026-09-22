"""Regenerate frozen tool-schema/profile snapshots after a public surface change."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from comsol_mcp.server import create_server
from comsol_mcp.settings import (
    LEXICAL_DOCS_ENABLED_ENV,
    SEMANTIC_ENABLED_ENV,
    SHARED_SERVER_ENV,
)
from comsol_mcp.tools.catalog import (
    FEATURE_NAMES,
    PROFILE_NAMES,
    TOOL_METADATA,
    snapshot_tool_schemas,
)
from comsol_mcp.tools.profiles import (
    resolve_profile,
    tool_names_for_profile,
)

SNAPSHOTS = ROOT / "development_kit" / "tests" / "snapshots"


def _dump(path: Path, payload: object, *, sort_keys: bool = True) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=sort_keys, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")


async def _collect() -> dict[str, dict]:
    schemas: dict[str, dict] = {}
    for profile in PROFILE_NAMES:
        server = create_server(f"snapshot-{profile}", profile=profile)
        schemas.update(await snapshot_tool_schemas(server))
    selection = resolve_profile(
        "core",
        environ={
            LEXICAL_DOCS_ENABLED_ENV: "true",
            SEMANTIC_ENABLED_ENV: "true",
            SHARED_SERVER_ENV: "true",
        },
    )
    server = create_server("snapshot-features", profile=selection)
    schemas.update(await snapshot_tool_schemas(server))
    return schemas


def main() -> int:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    schemas = asyncio.run(_collect())
    if set(schemas) != set(TOOL_METADATA):
        missing = sorted(set(TOOL_METADATA) - set(schemas))
        extra = sorted(set(schemas) - set(TOOL_METADATA))
        raise SystemExit(f"schema/tool mismatch missing={missing} extra={extra}")

    profile_names = {
        profile: sorted(tool_names_for_profile(profile)) for profile in PROFILE_NAMES
    }
    feature_names = {
        feature: sorted(
            name for name, meta in TOOL_METADATA.items() if meta.feature_gate == feature
        )
        for feature in FEATURE_NAMES
    }
    _dump(SNAPSHOTS / "full_tool_schemas.json", schemas)
    _dump(SNAPSHOTS / "profile_tool_names.json", profile_names, sort_keys=False)
    _dump(SNAPSHOTS / "feature_tool_names.json", feature_names, sort_keys=False)

    # Deployment identity hashes of the three frozen snapshots (LF-normalized).
    import hashlib

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    manifest_path = ROOT / "comsol_mcp" / "deployment_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("snapshot_identities", None)
    manifest["full_tool_schemas_sha256"] = sha(SNAPSHOTS / "full_tool_schemas.json")
    manifest["profile_tool_names_sha256"] = sha(SNAPSHOTS / "profile_tool_names.json")
    manifest["feature_tool_names_sha256"] = sha(SNAPSHOTS / "feature_tool_names.json")
    _dump(manifest_path, manifest)

    # Release facts view is rebuilt by the official builder when available.
    try:
        from development_kit.scripts.release_facts import (
            FACTS_PATH,
            build_release_facts,
        )

        _dump(FACTS_PATH, build_release_facts(), sort_keys=False)
    except Exception:
        facts_path = ROOT / "development_kit" / "release" / "release_facts.json"
        facts = json.loads(facts_path.read_text(encoding="utf-8"))
        facts["tool_count"] = len(TOOL_METADATA)
        facts["profiles"] = {
            profile: {"tool_count": len(tool_names_for_profile(profile))}
            for profile in PROFILE_NAMES
        }
        _dump(facts_path, facts, sort_keys=False)

    # Support matrix profile counts.
    matrix_path = ROOT / "development_kit" / "release" / "support_matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    counts = {profile: len(tool_names_for_profile(profile)) for profile in PROFILE_NAMES}
    for item in matrix.get("profiles", []):
        name = item.get("name")
        if name in counts:
            item["tool_count"] = counts[name]
    existing = {item.get("name") for item in matrix.get("profiles", [])}
    if "electro_chemistry" not in existing:
        matrix.setdefault("profiles", []).append(
            {
                "name": "electro_chemistry",
                "support": "experimental",
                "tool_count": counts["electro_chemistry"],
            }
        )
    _dump(matrix_path, matrix, sort_keys=False)
    print("snapshot regeneration complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
