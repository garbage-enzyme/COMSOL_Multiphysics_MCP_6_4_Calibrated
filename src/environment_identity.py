"""Bounded, path-redacted, solver-free runtime environment identity."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import platform
import struct
import sys
import sysconfig
from typing import Any

from src import __version__
from src.compatibility import load_runtime_compatibility
from src.durable import canonical_sha256_v1


_DIRECT_DISTRIBUTIONS = (
    "matplotlib",
    "mcp",
    "mph",
    "numpy",
    "pydantic",
    "psutil",
    "scipy",
)
_RELEVANT_TRANSITIVE_DISTRIBUTIONS = (
    "anyio",
    "httpx",
    "httpx-sse",
    "jpype1",
    "pydantic-core",
    "starlette",
    "uvicorn",
)
_OPTIONAL_FEATURES = {
    "manuals": ("pymupdf",),
    "semantic_docs": ("sentence-transformers", "torch", "transformers"),
}


def _distribution_record(name: str) -> dict[str, Any]:
    try:
        installed_version = version(name)
    except PackageNotFoundError:
        return {"name": name, "availability": "not_installed", "version": None}
    return {"name": name, "availability": "installed", "version": installed_version}


def get_environment_identity() -> dict[str, Any]:
    """Return installed dependency and platform identity without external probes."""
    direct = [_distribution_record(name) for name in _DIRECT_DISTRIBUTIONS]
    transitive = [
        _distribution_record(name) for name in _RELEVANT_TRANSITIVE_DISTRIBUTIONS
    ]
    optional_features = {}
    for feature, names in _OPTIONAL_FEATURES.items():
        distributions = [_distribution_record(name) for name in names]
        optional_features[feature] = {
            "availability": (
                "dependencies_installed"
                if all(item["availability"] == "installed" for item in distributions)
                else "dependencies_incomplete"
            ),
            "distributions": distributions,
        }

    compatibility = load_runtime_compatibility()
    accepted_lane = compatibility["licensed_acceptance"][0]
    gil_probe = getattr(sys, "_is_gil_enabled", None)
    identity = {
        "schema_name": "comsol_mcp.environment_identity",
        "schema_version": "1.0.0",
        "collection_mode": "solver_free_metadata_only",
        "package": {"name": "comsol-mcp", "version": __version__},
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "abi_tag": sys.implementation.cache_tag,
            "soabi": sysconfig.get_config_var("SOABI"),
            "pointer_bits": struct.calcsize("P") * 8,
            "gil_enabled": bool(gil_probe()) if callable(gil_probe) else None,
        },
        "platform": {
            "os": platform.system(),
            "os_release": platform.release(),
            "architecture": platform.machine(),
        },
        "distributions": {
            "direct": direct,
            "relevant_transitive": transitive,
        },
        "optional_features": optional_features,
        "licensed_runtime_declaration": {
            "status": accepted_lane["status"],
            "comsol_build": accepted_lane["comsol_build"],
            "java_version": accepted_lane["java_version"],
            "mph_version": accepted_lane["mph_version"],
            "python_version": accepted_lane["python_version"],
        },
        "observed_external_runtime": {
            "status": "not_observed",
            "comsol_build": None,
            "java_version": None,
            "reason": "solver_free_collection_does_not_probe_external_runtimes",
        },
        "redaction": {
            "paths_included": False,
            "host_identity_included": False,
            "user_identity_included": False,
        },
    }
    return {**identity, "identity_sha256": canonical_sha256_v1(identity)}


__all__ = ["get_environment_identity"]
