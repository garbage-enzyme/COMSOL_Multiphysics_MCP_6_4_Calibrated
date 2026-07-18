"""Generate and check the durable release-facts view."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from src import __version__
from src.compatibility import load_runtime_compatibility
from src.schema_registry import get_schema_registry
from src.tools.catalog import PROFILE_NAMES, TOOL_METADATA
from src.tools.profiles import tool_names_for_profile


ROOT = Path(__file__).parents[2]
FACTS_PATH = ROOT / "development_kit" / "release" / "release_facts.json"


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_release_facts() -> dict[str, Any]:
    """Build the machine-readable release view from live implementation data."""
    profiles = {
        profile: sorted(tool_names_for_profile(profile))
        for profile in PROFILE_NAMES
    }
    schema_registry = get_schema_registry()
    compatibility = load_runtime_compatibility()
    catalog = {
        name: metadata.to_dict()
        for name, metadata in sorted(TOOL_METADATA.items())
    }
    body = {
        "schema_name": "comsol_mcp.release_facts",
        "schema_version": "1.0.0",
        "package_version": __version__,
        "tool_count": len(catalog),
        "profiles": {
            name: {"tool_count": len(tools)}
            for name, tools in profiles.items()
        },
        "schema_registry": {
            "entry_count": schema_registry["entry_count"],
            "registry_sha256": schema_registry["registry_sha256"],
        },
        "identities": {
            "catalog_sha256": _canonical_sha256(catalog),
            "profile_tools_sha256": _canonical_sha256(profiles),
            "runtime_compatibility_sha256": _canonical_sha256(compatibility),
        },
    }
    return body


def check_release_facts(path: Path = FACTS_PATH) -> None:
    """Fail if a committed release-facts view differs from live source data."""
    expected = build_release_facts()
    actual = json.loads(path.read_text(encoding="utf-8"))
    if actual != expected:
        raise SystemExit(
            f"release facts differ from live implementation: {path}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the committed release-facts view",
    )
    args = parser.parse_args()
    if args.check:
        check_release_facts()
    else:
        print(json.dumps(build_release_facts(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
