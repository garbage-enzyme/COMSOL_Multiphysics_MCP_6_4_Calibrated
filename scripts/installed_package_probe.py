"""Verify discovery from a non-editable installed wheel without starting COMSOL."""

from __future__ import annotations

import argparse
import asyncio
from importlib.metadata import requires, version
import json
from pathlib import Path
import sys


HEAVY_SEMANTIC_MODULES = {"chromadb", "sentence_transformers", "torch"}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import mph

    mph.Client = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("installed-package discovery must not start COMSOL")
    )

    import src
    from src.server import create_server
    from src.tools.catalog import PROFILE_NAMES, snapshot_tool_schemas
    from src.tools.capabilities import get_capabilities
    from src.tools.profiles import resolve_profile

    expected_names = _load_json(args.snapshot_dir / "profile_tool_names.json")
    expected_schemas = _load_json(args.snapshot_dir / "full_tool_schemas.json")
    actual_counts: dict[str, int] = {}
    deployment_identities: list[dict] = []

    if tuple(expected_names) != PROFILE_NAMES:
        raise AssertionError("installed profile order differs from the frozen snapshot")

    for profile in PROFILE_NAMES:
        selection = resolve_profile(profile)
        server = create_server(f"installed-{profile}", profile=selection.name)
        schemas = asyncio.run(snapshot_tool_schemas(server))
        names = expected_names[profile]
        if sorted(schemas) != names:
            raise AssertionError(f"installed {profile} membership differs from snapshot")
        expected_profile_schemas = {name: expected_schemas[name] for name in names}
        if schemas != expected_profile_schemas:
            raise AssertionError(f"installed {profile} schemas differ from snapshot")
        actual_counts[profile] = len(schemas)
        deployment_identities.append(
            get_capabilities(selection)["deployment_identity"]
        )

    if not all(identity == deployment_identities[0] for identity in deployment_identities[1:]):
        raise AssertionError("installed profiles disagree on deployment identity")
    deployment_identity = deployment_identities[0]
    if deployment_identity["source_classification"] != "installed_site_package":
        raise AssertionError("installed deployment identity reports source-tree shadowing")
    if deployment_identity.get("contains_local_path") is not False:
        raise AssertionError("installed deployment identity leaks a local path")

    imported_heavy = sorted(HEAVY_SEMANTIC_MODULES.intersection(sys.modules))
    if imported_heavy:
        raise AssertionError(f"discovery imported heavy semantic modules: {imported_heavy}")

    package_requirements = sorted(requires("comsol-mcp") or [])
    result = {
        "schema_version": "1.0.0",
        "installed_package": {
            "name": "comsol-mcp",
            "version": version("comsol-mcp"),
            "module_path_is_site_package": "site-packages" in str(Path(src.__file__).resolve()).lower(),
            "requirements": package_requirements,
        },
        "profile_counts": actual_counts,
        "deployment_identity": deployment_identity,
        "schema_snapshot_match": True,
        "comsol_client_started": False,
        "heavy_semantic_modules_imported": imported_heavy,
    }
    if not result["installed_package"]["module_path_is_site_package"]:
        raise AssertionError(f"probe imported source tree instead of installed wheel: {src.__file__}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
