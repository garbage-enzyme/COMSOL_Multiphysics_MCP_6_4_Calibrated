"""H2a-only COMSOL native-cancellation inspection helpers.

Nothing in this module is a production cancellation path.  H2a uses it from a
fresh, opt-in integration subprocess to record the installed COMSOL build,
JAR identities, and Java method signatures before any future worker is allowed
to call an internal COMSOL cancellation API.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import mph
import jpype


# These are candidate APIs only.  They are deliberately not an allowlist for a
# production worker: a real H2a probe must prove a prompt stop and cleanup.
NATIVE_CANCEL_CANDIDATES = {
    "progress_context": {
        "class_name": "com.comsol.model.util.ProgressContext",
        "methods": ("cancel()", "stop(int)"),
    },
    "connection_internal": {
        "class_name": "com.comsol.clientapi.engine.MphServerConnectionInternal",
        "methods": ("cancelRunnable()", "stopRunnable(int)"),
    },
}

_REQUIRED_JARS = {
    "api": ("apiplugins", "com.comsol.api_1.0.0.jar"),
    "model": ("plugins", "com.comsol.model_1.0.0.jar"),
    "clientapi": ("plugins", "com.comsol.clientapi_1.0.0.jar"),
}


def _hash_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def discover_environment(version: str | None = None) -> dict[str, Any]:
    """Return a data-only compatibility record without starting COMSOL."""
    backend = mph.discovery.backend(version)
    root = Path(backend["root"])
    jars: dict[str, dict[str, Any]] = {}
    for role, (folder, name) in _REQUIRED_JARS.items():
        path = root / folder / name
        jars[role] = {
            "basename": name,
            "path": str(path),
            "exists": path.is_file(),
            "sha256": _hash_file(path) if path.is_file() else None,
        }
    return {
        "manifest_schema_version": "1",
        "mph_version": getattr(mph, "__version__", None),
        "backend": {
            "name": str(backend["name"]),
            "major": int(backend["major"]),
            "minor": int(backend["minor"]),
            "patch": int(backend["patch"]),
            "build": int(backend["build"]),
            "root": str(root),
            "jvm": str(backend["jvm"]),
        },
        "jars": jars,
        "candidates": {
            name: {"class_name": value["class_name"], "required_methods": list(value["methods"])}
            for name, value in NATIVE_CANCEL_CANDIDATES.items()
        },
    }


def reflect_candidate_signatures() -> dict[str, dict[str, Any]]:
    """Inspect candidate classes in an already-started COMSOL JVM.

    This is intentionally separate from :func:`discover_environment`: loading
    classes must never make status/preflight calls start a JVM.
    """
    if not jpype.isJVMStarted():
        raise RuntimeError("COMSOL JVM is not started; reflection is probe-only")
    results: dict[str, dict[str, Any]] = {}
    for name, candidate in NATIVE_CANCEL_CANDIDATES.items():
        try:
            cls = jpype.JClass(candidate["class_name"])
            methods = sorted(
                f"{method.getName()}({','.join(str(item.getName()) for item in method.getParameterTypes())})"
                for method in cls.class_.getMethods()
                if str(method.getName()) in {"cancel", "stop", "cancelRunnable", "stopRunnable"}
            )
            required = set(candidate["methods"])
            normalized = {
                item.replace("java.lang.Integer", "int").replace("java.lang.", "")
                for item in methods
            }
            results[name] = {
                "class_name": candidate["class_name"],
                "available": True,
                "methods": methods,
                "required_methods_present": all(
                    expected in normalized for expected in required
                ),
            }
        except Exception as exc:
            results[name] = {
                "class_name": candidate["class_name"],
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return results


def select_progress_context_profile() -> dict[str, Any] | None:
    """Return the exact allowlisted profile for this installation, if any."""
    environment = discover_environment()
    profiles_path = Path(__file__).with_name("native_cancel_profiles.json")
    profiles = json.loads(profiles_path.read_text(encoding="utf-8")).get("profiles", [])
    for profile in profiles:
        backend = profile.get("backend", {})
        observed = environment["backend"]
        if any(int(observed[key]) != int(backend.get(key, -1)) for key in ("major", "minor", "patch", "build")):
            continue
        if all(
            environment["jars"].get(role, {}).get("sha256") == expected.get("sha256")
            and environment["jars"].get(role, {}).get("basename") == expected.get("basename")
            for role, expected in profile.get("jars", {}).items()
        ):
            return profile
    return None


def request_native_cancel_once() -> dict[str, Any]:
    """Invoke the H2a-approved public candidate only in an exact profile.

    Caller owns attempt binding and process-level verification. This function
    neither starts a JVM nor claims that the solve has stopped.
    """
    profile = select_progress_context_profile()
    if profile is None:
        return {"attempted": False, "supported": False, "outcome": "unsupported_for_environment"}
    if not jpype.isJVMStarted():
        return {"attempted": False, "supported": True, "outcome": "jvm_not_started"}
    try:
        jpype.JClass("com.comsol.model.util.ProgressContext")().cancel()
        return {"attempted": True, "supported": True, "outcome": "returned", "profile_id": profile["profile_id"]}
    except Exception as exc:
        return {"attempted": True, "supported": True, "outcome": f"{type(exc).__name__}: {exc}", "profile_id": profile["profile_id"]}
