"""native cancellation-only COMSOL native-cancellation inspection helpers.

Nothing in this module is a production cancellation path.  native cancellation uses it from a
fresh, opt-in integration subprocess to record the installed COMSOL build,
JAR identities, and Java method signatures before any future worker is allowed
to call an internal COMSOL cancellation API.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

import jpype
import mph

# These are candidate APIs only.  They are deliberately not an allowlist for a
# production worker: a real native cancellation probe must prove a prompt stop and cleanup.
NATIVE_CANCEL_CANDIDATES = {
    "progress_context": {
        "class_name": "com.comsol.model.util.ProgressContext",
        "jar_role": "model",
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

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


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

            def signature(method: Any) -> str:
                parameters = ",".join(str(item.getName()) for item in method.getParameterTypes())
                return f"{method.getName()}({parameters})"

            methods = sorted(
                signature(method)
                for method in cls.class_.getMethods()
                if str(method.getName()) in {"cancel", "stop", "cancelRunnable", "stopRunnable"}
            )
            required = set(candidate["methods"])
            results[name] = {
                "class_name": candidate["class_name"],
                "available": True,
                "methods": methods,
                "required_methods_present": all(expected in methods for expected in required),
            }
        except Exception as exc:
            results[name] = {
                "class_name": candidate["class_name"],
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    return results


def _load_native_cancel_profiles() -> list[dict[str, Any]]:
    profiles_path = Path(__file__).with_name("native_cancel_profiles.json")
    try:
        document = json.loads(profiles_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(document, Mapping):
        return []
    profiles = document.get("profiles", [])
    if not isinstance(profiles, list):
        return []
    return [profile for profile in profiles if isinstance(profile, dict)]


def _profile_matches_environment(
    profile: Mapping[str, Any], environment: Mapping[str, Any]
) -> bool:
    backend = profile.get("backend")
    observed_backend = environment.get("backend")
    jars = profile.get("jars")
    observed_jars = environment.get("jars")
    candidate = profile.get("candidate")
    profile_id = profile.get("profile_id")
    expected_candidate = NATIVE_CANCEL_CANDIDATES["progress_context"]
    if not all(
        isinstance(value, Mapping)
        for value in (backend, observed_backend, jars, observed_jars, candidate)
    ):
        return False
    if not isinstance(profile_id, str) or not profile_id:
        return False
    if set(jars) != set(_REQUIRED_JARS):
        return False
    if candidate.get("name") != "progress_context":
        return False
    if candidate.get("class_name") != expected_candidate["class_name"]:
        return False
    if candidate.get("jar_role") != expected_candidate["jar_role"]:
        return False
    if candidate.get("methods") != list(expected_candidate["methods"]):
        return False
    try:
        if any(
            int(observed_backend[key]) != int(backend[key])
            for key in ("major", "minor", "patch", "build")
        ):
            return False
    except KeyError, TypeError, ValueError:
        return False
    for role, (_, required_basename) in _REQUIRED_JARS.items():
        expected = jars.get(role)
        observed = observed_jars.get(role)
        if not isinstance(expected, Mapping) or not isinstance(observed, Mapping):
            return False
        expected_digest = expected.get("sha256")
        if expected.get("basename") != required_basename:
            return False
        if (
            not isinstance(expected_digest, str)
            or _SHA256_PATTERN.fullmatch(expected_digest) is None
        ):
            return False
        if observed.get("exists") is not True:
            return False
        if observed.get("basename") != expected.get("basename"):
            return False
        if observed.get("sha256") != expected_digest:
            return False
    return True


def select_progress_context_profile(
    environment: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the exact allowlisted profile for this installation, if any."""
    observed = discover_environment() if environment is None else environment
    for profile in _load_native_cancel_profiles():
        if _profile_matches_environment(profile, observed):
            return profile
    return None


def _normalized_windows_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(Path(value).resolve(strict=False)).replace("/", "\\").casefold()
    except OSError, ValueError:
        return None


def _active_candidate_origin(class_name: str) -> str | None:
    try:
        candidate_class = jpype.JClass(class_name)
        code_source = candidate_class.class_.getProtectionDomain().getCodeSource()
        if code_source is None:
            return None
        paths = jpype.JClass("java.nio.file.Paths")
        return str(paths.get(code_source.getLocation().toURI()).toAbsolutePath().normalize())
    except Exception:
        return None


def _running_jvm_profile_status(
    profile: Mapping[str, Any], environment: Mapping[str, Any]
) -> tuple[bool, str]:
    backend = environment.get("backend")
    if not isinstance(backend, Mapping):
        return False, "jvm_environment_unverified"
    jvm_path = backend.get("jvm")
    if not isinstance(jvm_path, str):
        return False, "jvm_environment_unverified"
    path = Path(jvm_path)
    if (
        path.name.casefold() != "jvm.dll"
        or path.parent.name.casefold() != "server"
        or path.parent.parent.name.casefold() != "bin"
    ):
        return False, "jvm_environment_unverified"
    expected_java_home = _normalized_windows_path(str(path.parent.parent.parent))
    try:
        system = jpype.JClass("java.lang.System")
        active_java_home = _normalized_windows_path(str(system.getProperty("java.home")))
    except Exception:
        return False, "jvm_environment_unverified"
    if active_java_home is None or active_java_home != expected_java_home:
        return False, "jvm_installation_mismatch"
    candidate = profile.get("candidate")
    if not isinstance(candidate, Mapping):
        return False, "jvm_signature_mismatch"
    candidate_class_name = candidate.get("class_name")
    jar_role = candidate.get("jar_role")
    observed_jars = environment.get("jars")
    if (
        not isinstance(candidate_class_name, str)
        or not isinstance(jar_role, str)
        or not isinstance(observed_jars, Mapping)
    ):
        return False, "jvm_candidate_origin_unverified"
    expected_jar = observed_jars.get(jar_role)
    if not isinstance(expected_jar, Mapping):
        return False, "jvm_candidate_origin_unverified"
    active_origin = _normalized_windows_path(_active_candidate_origin(candidate_class_name))
    expected_origin = _normalized_windows_path(expected_jar.get("path"))
    if active_origin is None or expected_origin is None:
        return False, "jvm_candidate_origin_unverified"
    if active_origin != expected_origin:
        return False, "jvm_candidate_origin_mismatch"
    reflected = reflect_candidate_signatures().get(str(candidate.get("name")))
    if not isinstance(reflected, Mapping) or reflected.get("required_methods_present") is not True:
        return False, "jvm_signature_mismatch"
    if reflected.get("class_name") != candidate.get("class_name"):
        return False, "jvm_signature_mismatch"
    return True, "verified"


def request_native_cancel_once() -> dict[str, Any]:
    """Invoke the native cancellation-approved public candidate only in an exact profile.

    Caller owns attempt binding and process-level verification. This function
    neither starts a JVM nor claims that the solve has stopped.
    """
    environment = discover_environment()
    profile = select_progress_context_profile(environment)
    if profile is None:
        return {"attempted": False, "supported": False, "outcome": "unsupported_for_environment"}
    if not jpype.isJVMStarted():
        return {"attempted": False, "supported": True, "outcome": "jvm_not_started"}
    jvm_verified, jvm_outcome = _running_jvm_profile_status(profile, environment)
    if not jvm_verified:
        return {
            "attempted": False,
            "supported": False,
            "outcome": jvm_outcome,
            "profile_id": profile["profile_id"],
        }
    try:
        jpype.JClass(profile["candidate"]["class_name"])().cancel()
        return {
            "attempted": True,
            "supported": True,
            "outcome": "returned",
            "profile_id": profile["profile_id"],
        }
    except Exception as exc:
        return {
            "attempted": True,
            "supported": True,
            "outcome": f"{type(exc).__name__}: {exc}",
            "profile_id": profile["profile_id"],
        }
