import json
import re
from pathlib import Path

from src.jobs import native_cancel_probe as probe


def test_discover_environment_records_build_and_hashes(monkeypatch, tmp_path):
    root = tmp_path / "comsol"
    api = root / "apiplugins" / "com.comsol.api_1.0.0.jar"
    model = root / "plugins" / "com.comsol.model_1.0.0.jar"
    client = root / "plugins" / "com.comsol.clientapi_1.0.0.jar"
    for path in (api, model, client):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode("ascii"))

    monkeypatch.setattr(
        probe.mph.discovery,
        "backend",
        lambda version=None: {
            "name": "6.4",
            "major": 6,
            "minor": 4,
            "patch": 0,
            "build": 293,
            "root": root,
            "jvm": root / "java" / "jvm.dll",
        },
    )

    manifest = probe.discover_environment()

    assert manifest["backend"]["build"] == 293
    assert manifest["jars"]["api"]["exists"] is True
    assert len(manifest["jars"]["clientapi"]["sha256"]) == 64
    assert manifest["candidates"]["connection_internal"]["required_methods"] == [
        "cancelRunnable()",
        "stopRunnable(int)",
    ]


def test_reflection_refuses_to_start_a_jvm(monkeypatch):
    monkeypatch.setattr(probe.jpype, "isJVMStarted", lambda: False, raising=False)
    try:
        probe.reflect_candidate_signatures()
    except RuntimeError as exc:
        assert "probe-only" in str(exc)
    else:
        raise AssertionError("reflection unexpectedly proceeded without a JVM")


def _profile() -> dict:
    path = Path(probe.__file__).with_name("native_cancel_profiles.json")
    profiles = json.loads(path.read_text(encoding="utf-8"))["profiles"]
    return next(
        item
        for item in profiles
        if item["profile_id"] == "comsol-6.4.0.293-progress-context-20260712"
    )


def _matching_environment(profile: dict) -> dict:
    root = Path(r"C:\COMSOL64")
    return {
        "backend": {
            **profile["backend"],
            "jvm": r"C:\COMSOL64\java\win64\jre\bin\server\jvm.dll",
        },
        "jars": {
            role: {
                **identity,
                "exists": True,
                "path": str(
                    root / ("apiplugins" if role == "api" else "plugins") / identity["basename"]
                ),
            }
            for role, identity in profile["jars"].items()
        },
    }


def test_native_cancel_profile_is_data_only_and_pins_all_required_jars():
    profile = _profile()

    assert profile["backend"] == {"major": 6, "minor": 4, "patch": 0, "build": 293}
    assert set(profile["jars"]) == {"api", "model", "clientapi"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in profile["jars"].values())
    assert profile["candidate"]["jar_role"] == "model"
    assert profile["candidate"]["methods"] == ["cancel()", "stop(int)"]
    assert profile["native_cancel_gate"]["fresh_subprocess_runs"] == 3


def test_empty_jar_mapping_never_selects_a_native_profile(monkeypatch):
    profile = _profile()
    malformed = {**profile, "jars": {}}
    monkeypatch.setattr(probe, "_load_native_cancel_profiles", lambda: [malformed])

    assert probe.select_progress_context_profile(_matching_environment(profile)) is None


def test_matching_environment_selects_the_named_profile():
    profile = _profile()

    selected = probe.select_progress_context_profile(_matching_environment(profile))

    assert selected is not None
    assert selected["profile_id"] == profile["profile_id"]


def test_backend_mismatch_never_selects_an_otherwise_matching_profile():
    profile = _profile()
    environment = _matching_environment(profile)
    environment["backend"]["build"] += 1

    assert probe.select_progress_context_profile(environment) is None


def test_jar_mismatch_never_selects_an_otherwise_matching_profile():
    profile = _profile()
    environment = _matching_environment(profile)
    environment["jars"]["clientapi"]["sha256"] = "0" * 64

    assert probe.select_progress_context_profile(environment) is None


def test_reflection_distinguishes_boxed_integer_from_primitive_int(monkeypatch):
    class FakeType:
        def __init__(self, name):
            self.name = name

        def getName(self):
            return self.name

    class FakeMethod:
        def __init__(self, name, *parameters):
            self.name = name
            self.parameters = [FakeType(item) for item in parameters]

        def getName(self):
            return self.name

        def getParameterTypes(self):
            return self.parameters

    class FakeReflection:
        @staticmethod
        def getMethods():
            return [FakeMethod("cancel"), FakeMethod("stop", "java.lang.Integer")]

    class FakeClass:
        class_ = FakeReflection()

    monkeypatch.setattr(probe.jpype, "isJVMStarted", lambda: True)
    monkeypatch.setattr(probe.jpype, "JClass", lambda _name: FakeClass)

    result = probe.reflect_candidate_signatures()["progress_context"]

    assert result["methods"] == ["cancel()", "stop(java.lang.Integer)"]
    assert result["required_methods_present"] is False


def test_running_jvm_must_match_selected_installation(monkeypatch):
    profile = _profile()
    environment = _matching_environment(profile)

    class FakeSystem:
        @staticmethod
        def getProperty(name):
            assert name == "java.home"
            return r"C:\OTHER\java\jre"

    monkeypatch.setattr(probe.jpype, "JClass", lambda name: FakeSystem)

    assert probe._running_jvm_profile_status(profile, environment) == (
        False,
        "jvm_installation_mismatch",
    )


def test_running_jvm_requires_exact_selected_candidate_signatures(monkeypatch):
    profile = _profile()
    environment = _matching_environment(profile)

    class FakeSystem:
        @staticmethod
        def getProperty(name):
            assert name == "java.home"
            return r"C:\COMSOL64\java\win64\jre"

    monkeypatch.setattr(probe.jpype, "JClass", lambda name: FakeSystem)
    monkeypatch.setattr(
        probe,
        "_active_candidate_origin",
        lambda _name: environment["jars"]["model"]["path"],
    )
    monkeypatch.setattr(
        probe,
        "reflect_candidate_signatures",
        lambda: {
            "progress_context": {
                "class_name": profile["candidate"]["class_name"],
                "required_methods_present": False,
            }
        },
    )

    assert probe._running_jvm_profile_status(profile, environment) == (
        False,
        "jvm_signature_mismatch",
    )


def test_running_jvm_accepts_matching_installation_and_signatures(monkeypatch):
    profile = _profile()
    environment = _matching_environment(profile)

    class FakeSystem:
        @staticmethod
        def getProperty(name):
            assert name == "java.home"
            return r"C:\COMSOL64\java\win64\jre"

    monkeypatch.setattr(probe.jpype, "JClass", lambda name: FakeSystem)
    monkeypatch.setattr(
        probe,
        "_active_candidate_origin",
        lambda _name: environment["jars"]["model"]["path"],
    )
    monkeypatch.setattr(
        probe,
        "reflect_candidate_signatures",
        lambda: {
            "progress_context": {
                "class_name": profile["candidate"]["class_name"],
                "required_methods_present": True,
            }
        },
    )

    assert probe._running_jvm_profile_status(profile, environment) == (
        True,
        "verified",
    )


def test_running_jvm_rejects_candidate_loaded_from_another_jar(monkeypatch):
    profile = _profile()
    environment = _matching_environment(profile)

    class FakeSystem:
        @staticmethod
        def getProperty(name):
            assert name == "java.home"
            return r"C:\COMSOL64\java\win64\jre"

    monkeypatch.setattr(probe.jpype, "JClass", lambda name: FakeSystem)
    monkeypatch.setattr(
        probe,
        "_active_candidate_origin",
        lambda _name: r"C:\OTHER\plugins\com.comsol.model_1.0.0.jar",
    )

    assert probe._running_jvm_profile_status(profile, environment) == (
        False,
        "jvm_candidate_origin_mismatch",
    )
